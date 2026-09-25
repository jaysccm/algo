"""Executor LLM integration -- gets structured trading decisions from Claude Haiku."""

import json
import logging
import os
from dataclasses import asdict, dataclass
from decimal import Decimal

from .claude_client import AgentPurpose, _call_with_retry, get_claude_client
from .prompts import load_prompt
from .telemetry import record_event

EXECUTOR_KNOWN_TOP_KEYS = {
    "decisions", "thesis_invalidations", "market_summary", "risk_assessment",
}
EXECUTOR_KNOWN_DECISION_KEYS = {
    "playbook_action_id", "ticker", "action", "intent_type",
    "intent_magnitude", "reasoning", "confidence",
    "is_off_playbook", "signal_refs", "thesis_id",
    # Legacy "quantity" field still appears in some test fixtures and
    # historical responses; tolerated by the parser, listed here so the
    # schema-drift canary doesn't flag it as new.
    "quantity",
}

EXECUTOR_RAW_TEXT_CAP = 4096
VALID_ACTIONS = {"buy", "sell", "hold"}
VALID_CONFIDENCES = {"high", "medium", "low"}
VALID_SELL_INTENTS = {
    "exit_full", "exit_partial_pct", "exit_dollar", "trim_to_portfolio_pct",
}
VALID_BUY_INTENTS = {
    "invest_dollar", "invest_portfolio_pct", "invest_buying_power_pct",
    "add_to_target_pct",
}

logger = logging.getLogger(__name__)

DEFAULT_EXECUTOR_MODEL = os.environ.get("ALGO_EXECUTOR_MODEL", "claude-haiku-4-5-20251001")
EXECUTOR_MAX_TOKENS = int(os.environ.get("ALGO_EXECUTOR_MAX_TOKENS", "8192"))


def _safe_int(value) -> int | None:
    """Coerce a value to int, returning None if not possible."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _validate_decision_contract(raw: dict, index: int) -> tuple[str, str, str | None]:
    action = str(raw.get("action", "hold")).strip().lower()
    if action not in VALID_ACTIONS:
        raise ValueError(f"decision[{index}].action invalid: {raw.get('action')!r}")

    confidence = str(raw.get("confidence", "low")).strip().lower()
    if confidence not in VALID_CONFIDENCES:
        raise ValueError(f"decision[{index}].confidence invalid: {raw.get('confidence')!r}")

    intent_type = raw.get("intent_type")
    if intent_type is not None:
        intent_type = str(intent_type).strip()
        if not intent_type:
            intent_type = None

    if action == "hold":
        if intent_type is not None or raw.get("intent_magnitude") is not None:
            raise ValueError(f"decision[{index}] hold must not include an intent")
        return action, confidence, None

    valid_intents = VALID_BUY_INTENTS if action == "buy" else VALID_SELL_INTENTS
    if intent_type not in valid_intents:
        raise ValueError(
            f"decision[{index}].intent_type invalid for {action}: {intent_type!r}"
        )
    if action == "buy" and raw.get("intent_magnitude") is None:
        raise ValueError(f"decision[{index}] buy intent requires intent_magnitude")
    return action, confidence, intent_type


@dataclass
class PlaybookAction:
    """A structured action from the playbook.

    intent_type + intent_magnitude are the LLM-authored sizing signal.
    The trader resolves them to exact shares at execution time via v2.intents.

    signal_refs is the list of news/macro/thesis IDs the strategist cited as
    evidence on the underlying thesis. The executor copies it verbatim into
    its decision so attribution can trace the trade back to evidence.
    """
    id: int
    ticker: str
    action: str
    thesis_id: int | None
    reasoning: str
    confidence: str
    intent_type: str | None
    intent_magnitude: Decimal | None
    priority: int
    signal_refs: list[dict] | None = None

    def __post_init__(self):
        if self.signal_refs is None:
            self.signal_refs = []


@dataclass
class ExecutorInput:
    """Structured input for the executor."""
    playbook_actions: list[PlaybookAction]
    positions: list[dict]
    account: dict
    attribution_summary: dict
    recent_outcomes: list[dict]
    market_outlook: str
    risk_notes: str
    current_prices: dict[str, Decimal] = None
    strategy_identity: str = ""
    strategy_rules: str = ""
    equity_summary: str = ""
    todays_decisions: list[dict] = None
    recent_ticker_decisions: list[dict] = None
    playbook_action_history: list[dict] = None

    def __post_init__(self):
        if self.current_prices is None:
            self.current_prices = {}
        if self.todays_decisions is None:
            self.todays_decisions = []
        if self.recent_ticker_decisions is None:
            self.recent_ticker_decisions = []
        if self.playbook_action_history is None:
            self.playbook_action_history = []


def executor_input_to_dict(executor_input: ExecutorInput) -> dict:
    """The exact payload the executor prompt receives (json.dumps with default=str)."""
    return {
        "playbook_actions": [asdict(a) for a in executor_input.playbook_actions],
        "positions": executor_input.positions,
        "account": executor_input.account,
        "attribution_summary": executor_input.attribution_summary,
        "recent_outcomes": executor_input.recent_outcomes,
        "market_outlook": executor_input.market_outlook,
        "risk_notes": executor_input.risk_notes,
        "current_prices": {k: str(v) for k, v in executor_input.current_prices.items()},
        "strategy_identity": executor_input.strategy_identity,
        "strategy_rules": executor_input.strategy_rules,
        "equity_summary": executor_input.equity_summary,
        "todays_decisions": executor_input.todays_decisions,
        "recent_ticker_decisions": executor_input.recent_ticker_decisions,
        "playbook_action_history": executor_input.playbook_action_history,
    }


def executor_input_from_dict(data: dict) -> ExecutorInput:
    """Inverse of executor_input_to_dict, for the --decisions-file path.

    playbook_actions are rebuilt as PlaybookAction (intent_magnitude back to
    Decimal) and current_prices values as Decimal; everything else is carried
    through as the JSON-decoded values.
    """
    fields = set(ExecutorInput.__dataclass_fields__)
    kwargs = {k: v for k, v in data.items() if k in fields}
    action_fields = set(PlaybookAction.__dataclass_fields__)
    actions = []
    for a in data.get("playbook_actions") or []:
        a_kwargs = {k: v for k, v in a.items() if k in action_fields}
        if a_kwargs.get("intent_magnitude") is not None:
            a_kwargs["intent_magnitude"] = Decimal(str(a_kwargs["intent_magnitude"]))
        actions.append(PlaybookAction(**a_kwargs))
    kwargs["playbook_actions"] = actions
    kwargs["current_prices"] = {
        k: Decimal(str(v)) for k, v in (data.get("current_prices") or {}).items()
    }
    return ExecutorInput(**kwargs)


@dataclass
class ExecutorDecision:
    """A trading decision from the executor.

    intent_type + intent_magnitude are LLM-authored. quantity is set by the
    trader after resolving the intent against live portfolio state — it is
    NOT populated by the LLM.
    """
    playbook_action_id: int | None
    ticker: str
    action: str
    intent_type: str | None
    intent_magnitude: Decimal | None
    reasoning: str
    confidence: str
    is_off_playbook: bool
    signal_refs: list = None
    thesis_id: int | None = None
    quantity: Decimal | None = None  # filled in by trader.resolve

    def __post_init__(self):
        if self.signal_refs is None:
            self.signal_refs = []


@dataclass
class ThesisInvalidation:
    """A thesis flagged for invalidation."""
    thesis_id: int
    reason: str


@dataclass
class AgentResponse:
    """Full response from trading executor."""
    decisions: list[ExecutorDecision]
    thesis_invalidations: list[ThesisInvalidation]
    market_summary: str
    risk_assessment: str


def executor_output_schema() -> dict:
    """JSON Schema for the executor's structured output (claude -p --json-schema).

    Built from the same constants the parser validates against, so a new
    intent or confidence value changes both in one place.
    """
    signal_ref = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["news_signal", "macro_signal", "thesis"]},
            "id": {"type": "integer"},
        },
        "required": ["type", "id"],
    }
    decision = {
        "type": "object",
        "properties": {
            "playbook_action_id": {"type": ["integer", "null"]},
            "ticker": {"type": "string"},
            "action": {"type": "string", "enum": sorted(VALID_ACTIONS)},
            "intent_type": {
                "type": ["string", "null"],
                "enum": sorted(VALID_BUY_INTENTS | VALID_SELL_INTENTS) + [None],
            },
            "intent_magnitude": {"type": ["number", "null"]},
            "reasoning": {"type": "string"},
            "confidence": {"type": "string", "enum": sorted(VALID_CONFIDENCES)},
            "is_off_playbook": {"type": "boolean"},
            "signal_refs": {"type": "array", "items": signal_ref},
            "thesis_id": {"type": ["integer", "null"]},
        },
        "required": ["ticker", "action", "reasoning", "confidence", "is_off_playbook", "signal_refs"],
    }
    return {
        "type": "object",
        "properties": {
            "decisions": {"type": "array", "items": decision},
            "thesis_invalidations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"thesis_id": {"type": "integer"}, "reason": {"type": "string"}},
                    "required": ["thesis_id", "reason"],
                },
            },
            "market_summary": {"type": "string"},
            "risk_assessment": {"type": "string"},
        },
        "required": ["decisions", "thesis_invalidations", "market_summary", "risk_assessment"],
    }


TRADING_SYSTEM_PROMPT = load_prompt("executor")


def get_trading_decisions(
    executor_input: ExecutorInput,
    model: str = DEFAULT_EXECUTOR_MODEL,
    session_id: int | None = None,
) -> AgentResponse:
    """
    Get trading decisions from Claude Haiku.

    Args:
        executor_input: Structured executor input with playbook, positions, account, etc.
        model: Claude model to use (default: claude-haiku-4-5-20251001)

    Returns:
        AgentResponse with decisions and analysis
    """
    client = get_claude_client()

    # Serialize ExecutorInput as JSON
    input_data = executor_input_to_dict(executor_input)
    input_json = json.dumps(input_data, default=str)

    response = _call_with_retry(
        client,
        model=model,
        max_tokens=EXECUTOR_MAX_TOKENS,
        system=TRADING_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": input_json,
            }
        ],
        session_id=session_id,
        stage_name="trading",
        purpose=AgentPurpose.EXECUTOR,
    )

    # Extract text response
    response_text = ""
    for block in response.content:
        if hasattr(block, "text"):
            response_text += block.text

    logger.info(
        "Haiku tokens -- input: %d, output: %d, stop_reason: %s",
        response.usage.input_tokens,
        response.usage.output_tokens,
        response.stop_reason,
    )

    if response.stop_reason == "max_tokens":
        record_event(
            session_id=session_id,
            stage_name="trading",
            event_type="executor_response",
            payload={
                "parse_succeeded": False,
                "stop_reason": response.stop_reason,
                "decision_count": 0,
                "thesis_invalidation_count": 0,
                "unknown_top_level_keys": [],
                "unknown_decision_keys": [],
                "raw_response_text_truncated": response_text[:EXECUTOR_RAW_TEXT_CAP],
                "error": "max_tokens_truncation",
            },
        )
        raise ValueError(
            f"Executor response truncated at {response.usage.output_tokens} tokens. "
            "Context may be too large -- consider reducing input size."
        )

    # Parse JSON response
    try:
        # Handle potential markdown code blocks
        text = response_text.strip()
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        data = json.loads(text)
    except json.JSONDecodeError as e:
        record_event(
            session_id=session_id,
            stage_name="trading",
            event_type="executor_response",
            payload={
                "parse_succeeded": False,
                "stop_reason": response.stop_reason,
                "decision_count": 0,
                "thesis_invalidation_count": 0,
                "unknown_top_level_keys": [],
                "unknown_decision_keys": [],
                "raw_response_text_truncated": response_text[:EXECUTOR_RAW_TEXT_CAP],
                "error": f"JSONDecodeError: {e}",
            },
        )
        raise ValueError(f"Failed to parse LLM response as JSON: {response_text}") from e

    return parse_executor_response(
        data, raw_text=response_text, stop_reason=response.stop_reason, session_id=session_id,
    )


def parse_executor_response(
    data: dict, *, raw_text: str, stop_reason: str | None, session_id: int | None,
) -> AgentResponse:
    """Validate a decoded executor payload and build the AgentResponse.

    Shared by the API path (get_trading_decisions) and the Claude Code driver
    path (trader.py --decisions-file). Emits the executor_response event on
    both success and schema failure; raises ValueError on failure.
    """
    # Build response object
    decisions = []
    try:
        for i, d in enumerate(data.get("decisions", [])):
            if not isinstance(d, dict):
                raise ValueError(f"decision[{i}] must be an object")
            action, confidence, intent_type = _validate_decision_contract(d, i)
            raw_mag = d.get("intent_magnitude")
            try:
                magnitude = Decimal(str(raw_mag)) if raw_mag is not None else None
            except Exception as e:
                raise ValueError(f"decision[{i}].intent_magnitude invalid: {raw_mag!r}") from e
            # T1.3: normalize at the parse boundary so every downstream consumer
            # (sector lookup, position dict keys, sell precheck) sees the same
            # canonical TICKER. LLM has been observed emitting "aapl" or " AAPL ".
            ticker = (d.get("ticker") or "").strip().upper()
            decisions.append(ExecutorDecision(
                playbook_action_id=d.get("playbook_action_id"),
                ticker=ticker,
                action=action,
                intent_type=intent_type,
                intent_magnitude=magnitude,
                reasoning=d.get("reasoning", ""),
                confidence=confidence,
                is_off_playbook=d.get("is_off_playbook", False),
                signal_refs=d.get("signal_refs", []),
                thesis_id=_safe_int(d.get("thesis_id")),
            ))
    except ValueError as e:
        record_event(
            session_id=session_id,
            stage_name="trading",
            event_type="executor_response",
            payload={
                "parse_succeeded": False,
                "stop_reason": stop_reason,
                "decision_count": 0,
                "thesis_invalidation_count": 0,
                "unknown_top_level_keys": [],
                "unknown_decision_keys": [],
                "raw_response_text_truncated": raw_text[:EXECUTOR_RAW_TEXT_CAP],
                "error": f"SchemaValidationError: {e}",
            },
        )
        raise ValueError(f"Executor response failed schema validation: {e}") from e

    thesis_invalidations = []
    for inv in data.get("thesis_invalidations", []):
        raw_id = inv.get("thesis_id")
        try:
            tid = int(raw_id)
        except (TypeError, ValueError):
            logger.warning("Skipping thesis invalidation with non-integer thesis_id: %s", raw_id)
            continue
        thesis_invalidations.append(ThesisInvalidation(
            thesis_id=tid,
            reason=inv.get("reason", ""),
        ))

    unknown_top = sorted(set(data.keys()) - EXECUTOR_KNOWN_TOP_KEYS)
    unknown_dec = sorted({
        k for d in data.get("decisions", []) if isinstance(d, dict)
        for k in d
    } - EXECUTOR_KNOWN_DECISION_KEYS)
    record_event(
        session_id=session_id,
        stage_name="trading",
        event_type="executor_response",
        payload={
            "parse_succeeded": True,
            "stop_reason": stop_reason,
            "decision_count": len(decisions),
            "thesis_invalidation_count": len(thesis_invalidations),
            "unknown_top_level_keys": unknown_top,
            "unknown_decision_keys": unknown_dec,
            "raw_response_text_truncated": raw_text[:EXECUTOR_RAW_TEXT_CAP],
            "error": None,
        },
    )

    return AgentResponse(
        decisions=decisions,
        thesis_invalidations=thesis_invalidations,
        market_summary=data.get("market_summary", ""),
        risk_assessment=data.get("risk_assessment", ""),
    )


def format_decisions_for_logging(response: AgentResponse) -> dict:
    """
    Format agent response for database logging.

    Returns dict suitable for signals_used JSONB column.
    """
    return {
        "market_summary": response.market_summary,
        "risk_assessment": response.risk_assessment,
        "decision_count": len(response.decisions),
        "thesis_invalidation_count": len(response.thesis_invalidations),
    }


# Valid signal types and their corresponding DB tables
_SIGNAL_TYPE_TABLES = {
    "news_signal": "news_signals",
    "macro_signal": "macro_signals",
    "thesis": "theses",
}


def validate_signal_refs(signal_refs: list[dict]) -> list[dict]:
    """Validate signal refs against the database, stripping invalid ones.

    Batches queries by signal type (one query per type) to avoid N+1 behaviour.

    Args:
        signal_refs: List of {"type": str, "id": int} dicts from LLM output

    Returns:
        Filtered list containing only refs that exist in the database.
    """
    if not signal_refs:
        return []

    from .database.connection import get_cursor

    # Group refs by signal type, dropping any with unknown types up-front
    by_type: dict[str, list[dict]] = {}
    for ref in signal_refs:
        sig_type = ref.get("type", "")
        if not _SIGNAL_TYPE_TABLES.get(sig_type):
            logger.warning("Stripping signal ref with unknown type: %s", sig_type)
            continue
        by_type.setdefault(sig_type, []).append(ref)

    valid = []
    for sig_type, refs in by_type.items():
        table = _SIGNAL_TYPE_TABLES[sig_type]
        # Defense-in-depth: f-string interpolation is safe because `table` is
        # sourced exclusively from the hardcoded `_SIGNAL_TYPE_TABLES` allowlist
        # (the loop above drops any sig_type not present). Assert the invariant
        # here so a future refactor that changes how `table` is populated trips
        # immediately rather than silently introducing an injection vector.
        assert table in _SIGNAL_TYPE_TABLES.values(), f"unexpected table: {table!r}"
        ids = [r["id"] for r in refs if r.get("id") is not None]
        if not ids:
            continue
        with get_cursor() as cur:
            cur.execute(f"SELECT id FROM {table} WHERE id = ANY(%s)", (ids,))  # noqa: S608
            found_ids = {row["id"] for row in cur.fetchall()}
        for ref in refs:
            if ref.get("id") in found_ids:
                valid.append(ref)
            else:
                logger.warning(
                    "Stripping signal ref %s:%s — not found in DB", sig_type, ref.get("id")
                )

    return valid
