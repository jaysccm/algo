"""v2.otel: off by default, never fails or delays a session, bounded labels."""
import logging
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from v2 import otel


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    for k in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
              "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"):
        monkeypatch.delenv(k, raising=False)
    otel._state, otel._configured = None, False
    yield
    state = otel._state
    if state is not None and getattr(state, "log_handler", None) is not None:
        logging.getLogger().removeHandler(state.log_handler)
    otel._state, otel._configured = None, False


def test_disabled_without_endpoint_is_a_noop():
    otel.begin_session()
    otel.stage_start("executor")
    otel.record_executor(3, 1)
    otel.stage_end("executor", ok=True, usage=SimpleNamespace(model="claude-x"))
    otel.end_session("success", 1.0)
    otel.record_skipped_session()
    assert otel._state is None


def test_sdk_not_imported_when_disabled():
    with patch.dict("sys.modules", {"opentelemetry": None}):
        otel.begin_session()
        otel.end_session("success")
    assert otel._state is None


def test_missing_sdk_disables_without_raising(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    with patch.dict("sys.modules", {"opentelemetry": None}):
        otel.begin_session()
        otel.stage_start("learning")
        otel.stage_end("learning", ok=True)
        otel.end_session("success")
    assert otel._state is None


def test_unreachable_endpoint_does_not_fail_or_delay(monkeypatch):
    pytest.importorskip("opentelemetry.sdk")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")  # nothing listens
    monkeypatch.setenv("INSTANCE", "paper")
    started = time.monotonic()
    with patch.object(otel, "_load_last_success_from_db", return_value=None):
        otel.begin_session()
        for stage in ("learning", "executor"):
            otel.stage_start(stage)
            otel.stage_end(stage, ok=stage == "learning")
        otel.record_executor(2, 1)
        logging.getLogger("session").error("something failed")
        otel.end_session("failed", 2.0)
    assert time.monotonic() - started < 15
    assert otel._state is not None


def test_instance_label_is_bounded(monkeypatch):
    monkeypatch.setenv("INSTANCE", "paper")
    assert otel.instance_name() == "paper"
    for bad in ("", "Has Space", "x" * 40, "acct=123456789", "a/b"):
        monkeypatch.setenv("INSTANCE", bad)
        assert otel.instance_name() == ("unknown" if bad != "" else "unknown")


def test_scrub_redacts_secrets_numbers_and_truncates():
    text = ("failed token=abc123 postgresql://algo:pw@db:5432/trading acct 123456789 "
            "PKABCDEFGHIJKLMNOPQRSTUVWXYZ0123 " + "x " * 400)
    out = otel.scrub(text)
    assert "abc123" not in out and "algo:pw" not in out and "123456789" not in out
    assert "PKABCDEFGH" not in out
    assert len(out) <= 300


def test_unknown_stage_names_are_ignored(monkeypatch):
    pytest.importorskip("opentelemetry.sdk")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    with patch.object(otel, "_load_last_success_from_db", return_value=None):
        otel.begin_session()
        otel.stage_start("order-12345")
        assert otel._state.stages == {}
        otel.end_session("success")
