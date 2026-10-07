"""Operational OpenTelemetry export for the daily session (JAY-92).

Off unless an OTLP endpoint is configured (OTEL_EXPORTER_OTLP_ENDPOINT or a
per-signal variant). Every public function is a safe no-op when disabled and
swallows its own errors: telemetry must never fail or delay a session. Export
is bounded by short timeouts and a hard join timeout on the final flush.

Only operational data leaves the process: counts, durations, token usage,
outcomes and the instance name (a bounded label). Never P&L, positions, order
details, account numbers, credentials or error text (exception class names
only). Warning/error log records are length-limited and scrubbed.

Distinct from v2.telemetry, which records agent_events rows in Postgres.
"""
import logging
import os
import re
import threading
import time

logger = logging.getLogger(__name__)

_FLUSH_TIMEOUT_S = 3.0
_EXPORT_TIMEOUT_S = 3.0
_STAGES = frozenset({"learning", "supervisor", "pipeline", "strategist",
                     "executor", "strategy", "dashboard"})
_OUTCOMES = frozenset({"success", "failed", "skipped"})

_state = None  # _State once enabled; stays None when disabled/failed
_configured = False
_lock = threading.Lock()


def _endpoint_configured() -> bool:
    return any(os.environ.get(k, "").strip() for k in (
        "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"))


def instance_name() -> str:
    """The instance label (paper/live/...), constrained to a bounded charset."""
    raw = os.environ.get("INSTANCE", "").strip().lower()
    return raw if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", raw) else "unknown"


_SCRUBS = [
    (re.compile(r"(?i)\b(bearer|token|key|secret|password|authorization)\b\s*[:=]?\s*\S+"), r"\1=[redacted]"),
    (re.compile(r"\b[A-Za-z0-9_\-]{24,}\b"), "[redacted]"),
    (re.compile(r"\b\d{6,}\b"), "[redacted]"),
    (re.compile(r"postgres(?:ql)?://\S+"), "[redacted-dsn]"),
]


def scrub(text: str, limit: int = 300) -> str:
    text = str(text)
    for pattern, repl in _SCRUBS:
        text = pattern.sub(repl, text)
    return text[:limit]


class _ScrubFilter(logging.Filter):
    """Applied to the OTLP log handler only; local log files are untouched."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith(("opentelemetry", "urllib3")):
            return False  # exporter noise must not feed back into export
        try:
            record.msg = scrub(record.getMessage())
            record.args = None
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        except Exception:
            return False
        return True


class _State:
    def __init__(self):
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        self.instance = instance_name()
        self.resource = Resource.create({
            "service.name": "pinchy",
            "service.namespace": "pinchy",
            "service.instance.id": self.instance,
            "pinchy.instance": self.instance,
            "deployment.environment.name":
                os.environ.get("ALGO_DEPLOYMENT_ENVIRONMENT", "").strip() or "production",
        })
        self.tracer_provider = TracerProvider(resource=self.resource)
        self.tracer_provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(timeout=_EXPORT_TIMEOUT_S), schedule_delay_millis=2000))
        self.reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(timeout=_EXPORT_TIMEOUT_S),
            export_interval_millis=60_000, export_timeout_millis=int(_EXPORT_TIMEOUT_S * 1000))
        self.meter_provider = MeterProvider(resource=self.resource, metric_readers=[self.reader])
        # Providers are held privately rather than set globally, so nothing
        # else in the process is instrumented by accident.
        self.tracer = self.tracer_provider.get_tracer("v2.otel")
        meter = self.meter_provider.get_meter("v2.otel")
        self.sessions = meter.create_counter(
            "pinchy.sessions", unit="{session}",
            description="Terminal outcome per session invocation (success, failed, skipped).")
        self.stage_duration = meter.create_histogram(
            "pinchy.stage.duration", unit="s",
            description="Elapsed time per session stage.",
            explicit_bucket_boundaries_advisory=[1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600])
        self.session_duration = meter.create_histogram(
            "pinchy.session.duration", unit="s",
            description="Elapsed time per session.",
            explicit_bucket_boundaries_advisory=[30, 60, 120, 300, 600, 1200, 1800, 3600, 7200])
        self.executor_results = meter.create_counter(
            "pinchy.executor.results", unit="{order}",
            description="Executor order attempts by outcome (counts only).")
        self.tokens = meter.create_counter(
            "pinchy.llm.tokens", unit="{token}",
            description="Claude API tokens per stage, model and token type.")
        self.cost = meter.create_counter(
            "pinchy.llm.cost", unit="{usd}",
            description="Estimated Claude API cost per stage from the model_pricing table.")
        self.last_success = None  # unix seconds
        meter.create_observable_gauge(
            "pinchy.last_success_timestamp", unit="s",
            callbacks=[self._observe_last_success],
            description="Unix time of the last fully successful session (from the sessions table).")
        self.log_provider = None
        self._attach_logs()
        self.root = None
        self.root_token = None
        self.stages = {}  # stage -> (span, start_monotonic, token)
        self.started = None
        self.trace = trace
        self.metrics = metrics

    def _observe_last_success(self, options):
        from opentelemetry.metrics import Observation
        if self.last_success is not None:
            yield Observation(self.last_success, {"pinchy.instance": self.instance})

    def _attach_logs(self):
        try:
            from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
            from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
            from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
            self.log_provider = LoggerProvider(resource=self.resource)
            self.log_provider.add_log_record_processor(BatchLogRecordProcessor(
                OTLPLogExporter(timeout=_EXPORT_TIMEOUT_S), schedule_delay_millis=2000))
            handler = LoggingHandler(level=logging.WARNING, logger_provider=self.log_provider)
            handler.addFilter(_ScrubFilter())
            handler._pinchy_otel = True
            logging.getLogger().addHandler(handler)
            self.log_handler = handler
        except Exception:
            logger.debug("OTLP log export unavailable", exc_info=True)
            self.log_provider = None


def _get():
    """Lazily build the exporter state; None when disabled or unavailable."""
    global _state, _configured
    if _configured:
        return _state
    with _lock:
        if _configured:
            return _state
        _configured = True
        if not _endpoint_configured():
            return None
        try:
            _state = _State()
            logger.info("OpenTelemetry export enabled (instance=%s)", _state.instance)
        except Exception as e:  # missing packages, bad endpoint config, ...
            logger.warning("OpenTelemetry export disabled: %s", type(e).__name__)
            _state = None
        return _state


def _safe(fn):
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            try:
                logger.debug("telemetry call failed", exc_info=True)
            except Exception:
                pass
            return None
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _attrs(s, **extra):
    return {"pinchy.instance": s.instance, **extra}


def _load_last_success_from_db():
    from .database.trading_db import get_cursor
    with get_cursor() as cur:
        cur.execute("""
            SELECT EXTRACT(EPOCH FROM MAX(completed_at)) AS ts
            FROM sessions WHERE status = 'completed' AND session_type = 'daily'
        """)
        row = cur.fetchone()
    value = row["ts"] if row else None
    return float(value) if value is not None else None


@_safe
def begin_session(dry_run: bool = False) -> None:
    """Open the root span for a started session and load persisted last-success."""
    s = _get()
    if s is None:
        return
    s.started = time.monotonic()
    s.root = s.tracer.start_span("pinchy.session", attributes=_attrs(s, **{"pinchy.dry_run": bool(dry_run)}))
    from opentelemetry import context
    s.root_token = context.attach(s.trace.set_span_in_context(s.root))
    try:
        s.last_success = _load_last_success_from_db()
    except Exception:
        s.last_success = None


@_safe
def stage_start(stage: str) -> None:
    s = _get()
    if s is None or s.root is None or stage not in _STAGES:
        return
    from opentelemetry import context
    span = s.tracer.start_span(f"pinchy.stage.{stage}", attributes=_attrs(s, **{"pinchy.stage": stage}))
    token = context.attach(s.trace.set_span_in_context(span))
    s.stages[stage] = (span, time.monotonic(), token)


def _cost_for(usage):
    try:
        from .pricing import stage_cost_usd
        return stage_cost_usd(usage.model, usage.input_tokens, usage.output_tokens,
                              usage.cache_creation_tokens, usage.cache_read_tokens)
    except Exception:
        return None  # unknown model or DB unavailable: absent, not zero


@_safe
def stage_end(stage: str, ok: bool, usage=None) -> None:
    """Close a stage span and record its duration and token/cost usage."""
    s = _get()
    if s is None or stage not in s.stages:
        return
    span, began, token = s.stages.pop(stage)
    elapsed = time.monotonic() - began
    outcome = "success" if ok else "failed"
    from opentelemetry import context
    from opentelemetry.trace import Status, StatusCode
    span.set_attribute("pinchy.outcome", outcome)
    if usage is not None and getattr(usage, "model", None):
        # Bounded model values only: free-form names are collapsed to "other".
        model = usage.model if re.fullmatch(r"claude-[a-z0-9.\-]{1,40}", usage.model) else "other"
        mixed = bool(getattr(usage, "mixed_models", False))
        model_label = "mixed" if mixed else model
        for kind, n in (("input", usage.input_tokens), ("output", usage.output_tokens),
                        ("cache_creation", usage.cache_creation_tokens),
                        ("cache_read", usage.cache_read_tokens)):
            span.set_attribute(f"pinchy.llm.tokens.{kind}", int(n))
            s.tokens.add(int(n), _attrs(s, **{"pinchy.stage": stage, "pinchy.model": model_label,
                                              "pinchy.token_type": kind}))
        if not mixed:
            cost = _cost_for(usage)
            if cost is not None:
                span.set_attribute("pinchy.llm.cost_usd", cost)
                s.cost.add(cost, _attrs(s, **{"pinchy.stage": stage, "pinchy.model": model_label}))
    if not ok:
        # Failure is marked without the error text, which may carry content.
        span.set_status(Status(StatusCode.ERROR))
    s.stage_duration.record(elapsed, _attrs(s, **{"pinchy.stage": stage, "pinchy.outcome": outcome}))
    try:
        context.detach(token)
    except Exception:
        pass
    span.end()


@_safe
def record_executor(trades_executed: int, trades_failed: int) -> None:
    s = _get()
    if s is None:
        return
    for outcome, n in (("executed", trades_executed), ("failed", trades_failed)):
        if n and n > 0:
            s.executor_results.add(int(n), _attrs(s, **{"pinchy.outcome": outcome}))
    if s.root is not None and "executor" in s.stages:
        span = s.stages["executor"][0]
        span.set_attribute("pinchy.executor.executed", int(trades_executed or 0))
        span.set_attribute("pinchy.executor.failed", int(trades_failed or 0))


def _flush_bounded(s) -> None:
    def work():
        for provider in (s.tracer_provider, s.meter_provider, s.log_provider):
            if provider is None:
                continue
            try:
                provider.force_flush(int(_FLUSH_TIMEOUT_S * 1000))
            except Exception:
                pass
    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(_FLUSH_TIMEOUT_S + 1)  # hard bound even if an exporter hangs


@_safe
def end_session(outcome: str, duration_seconds: float | None = None) -> None:
    """Record the terminal outcome, close the trace and flush (bounded)."""
    s = _get()
    if s is None:
        return
    if outcome not in _OUTCOMES:
        outcome = "failed"
    for stage in list(s.stages):  # orphaned stages (should not happen)
        stage_end(stage, ok=False)
    if outcome == "success":
        s.last_success = time.time()
    s.sessions.add(1, _attrs(s, **{"pinchy.outcome": outcome}))
    if s.root is not None:
        from opentelemetry import context
        from opentelemetry.trace import Status, StatusCode
        s.root.set_attribute("pinchy.outcome", outcome)
        if outcome == "failed":
            s.root.set_status(Status(StatusCode.ERROR))
        if s.started is not None or duration_seconds is not None:
            d = duration_seconds if duration_seconds is not None else time.monotonic() - s.started
            s.session_duration.record(d, _attrs(s, **{"pinchy.outcome": outcome}))
        try:
            context.detach(s.root_token)
        except Exception:
            pass
        s.root.end()
        s.root = None
    _flush_bounded(s)


@_safe
def record_skipped_session() -> None:
    """A halted or idempotent no-op run: count it, export promptly, no trace."""
    s = _get()
    if s is None:
        return
    s.sessions.add(1, _attrs(s, **{"pinchy.outcome": "skipped"}))
    _flush_bounded(s)
