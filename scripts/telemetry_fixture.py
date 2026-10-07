#!/usr/bin/env python3
"""Run synthetic daily sessions through the real orchestrator and v2.otel (JAY-92).

Every stage, the database and the Claude client are stubbed: no provider call,
no broker call, no database, no order. It only exercises the telemetry path so
the exported series can be inspected. Usage (env must carry OTEL_EXPORTER_*):

    INSTANCE=paper python3 scripts/telemetry_fixture.py [success|stage-failure|skip]
"""
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("ALPACA_API_KEY", "fixture")
os.environ.setdefault("ALPACA_SECRET_KEY", "fixture")
os.environ.setdefault("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
os.environ.setdefault("ALPACA_PAPER", "true")
os.environ.setdefault("ANTHROPIC_API_KEY", "fixture")
os.environ.setdefault("DATABASE_URL", "postgresql://fixture:fixture@127.0.0.1:1/fixture")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from v2 import claude_client, session  # noqa: E402


def _llm(model, n_in, n_out, delay):
    def stage(*args, **kwargs):
        time.sleep(delay)
        claude_client._record_usage(model, SimpleNamespace(
            input_tokens=n_in, output_tokens=n_out,
            cache_creation_input_tokens=10, cache_read_input_tokens=20))
        return None
    return stage


def main(mode: str) -> int:
    trading = SimpleNamespace(trades_executed=3, trades_failed=1, errors=[], market_closed=False)
    boom = {"strategist": RuntimeError("boom account 123456789")} if mode == "stage-failure" else {}

    def failing(name, fn):
        def wrapper(*a, **k):
            if name in boom:
                raise boom[name]
            return fn(*a, **k)
        return wrapper

    with patch.object(session, "get_session_for_date",
                      return_value={"status": "completed"} if mode == "skip" else None), \
         patch.object(session, "insert_session_record", return_value=None), \
         patch.object(session, "expire_stale_playbook_actions", return_value=0), \
         patch.object(session, "run_backfill", side_effect=_llm("claude-haiku-4-5", 100, 50, .2)), \
         patch.object(session, "compute_signal_attribution", return_value=[]), \
         patch.object(session, "build_attribution_constraints", return_value=""), \
         patch.object(session, "run_supervisor", return_value=1), \
         patch.object(session, "run_pipeline", side_effect=_llm("claude-haiku-4-5", 400, 80, .3)), \
         patch.object(session, "run_strategist_loop",
                      side_effect=failing("strategist", _llm("claude-opus-4-8", 2000, 600, .4))), \
         patch.object(session, "get_playbook", return_value={"id": 1}), \
         patch.object(session, "run_trading_session", return_value=trading), \
         patch.object(session, "run_strategy_reflection",
                      return_value=SimpleNamespace(memo_written=True)), \
         patch.object(session, "run_dashboard_stage", return_value=SimpleNamespace()), \
         patch.object(session, "_log_session_costs"), \
         patch("v2.pricing.stage_cost_usd", return_value=0.0123):
        os.environ["ALGO_DASHBOARD_PUBLISH"] = "true"
        result = session.run_session()
    print(f"mode={mode} has_errors={result.has_errors} skip={bool(result.idempotent_skip)} "
          f"duration={result.duration_seconds:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "success"))
