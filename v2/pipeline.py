"""News pipeline orchestrator — fetch, classify, store."""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime

from .classifier import build_batch_user_message, classify_news_batch, parse_batch_entries
from .database.trading_db import insert_macro_signals_batch, insert_news_signals_batch
from .news import fetch_broad_news

logger = logging.getLogger("pipeline")


@dataclass
class PipelineStats:
    """Statistics from a pipeline run."""
    news_fetched: int
    ticker_signals_stored: int
    macro_signals_stored: int
    noise_dropped: int
    errors: int


def _store_results(results: list, stats: "PipelineStats", dry_run: bool) -> "PipelineStats":
    """Store classified signals and fold counts into `stats`.

    Shared by the API path (`run_pipeline`) and the file-based ingest path
    (`ingest_batches`) so the two cannot drift on insert semantics.
    """
    ticker_signals_batch = []
    macro_signals_batch = []

    for result in results:
        if result.news_type == "noise":
            stats.noise_dropped += 1
            continue

        for signal in result.ticker_signals:
            ticker_signals_batch.append((
                signal.ticker, signal.headline, signal.category,
                signal.sentiment, signal.confidence, signal.published_at,
                signal.alpaca_id, signal.summary,
            ))

        if result.macro_signal:
            s = result.macro_signal
            macro_signals_batch.append((
                s.headline, s.category, s.affected_sectors,
                s.sentiment, s.published_at, s.alpaca_id,
            ))

    if not dry_run:
        try:
            stats.ticker_signals_stored = insert_news_signals_batch(ticker_signals_batch)
        except Exception as e:
            logger.error("Error batch-inserting ticker signals: %s", e)
            stats.errors += 1
        try:
            stats.macro_signals_stored = insert_macro_signals_batch(macro_signals_batch)
        except Exception as e:
            logger.error("Error batch-inserting macro signals: %s", e)
            stats.errors += 1

    return stats


def run_pipeline(hours: int = 24, limit: int = 300, dry_run: bool = False, session_id: int | None = None) -> PipelineStats:
    """Run the news pipeline: fetch -> classify -> store."""
    stats = PipelineStats(news_fetched=0, ticker_signals_stored=0,
                          macro_signals_stored=0, noise_dropped=0, errors=0)

    # Step 1: Fetch news from Alpaca
    news_items = fetch_broad_news(hours=hours, limit=limit)
    stats.news_fetched = len(news_items)

    if not news_items:
        return stats

    # Step 2: Classify with Haiku (batched)
    headlines = [item.headline for item in news_items]
    published_ats = [item.published_at for item in news_items]
    alpaca_ids = [item.id for item in news_items]
    summaries = [item.summary for item in news_items]
    results = classify_news_batch(headlines, published_ats, alpaca_ids=alpaca_ids, summaries=summaries, session_id=session_id)

    # Step 3: Store signals
    return _store_results(results, stats, dry_run)


def emit_batches(out_dir: str, *, hours: int, limit: int, batch_size: int = 50) -> int:
    """Driver mode step 1: fetch news and write one JSON file per classifier batch."""
    os.makedirs(out_dir, exist_ok=True)
    news_items = fetch_broad_news(hours=hours, limit=limit)
    count = 0
    for start in range(0, len(news_items), batch_size):
        chunk = news_items[start:start + batch_size]
        payload = {
            "items": [
                {
                    "index": i + 1,
                    "headline": item.headline,
                    "published_at": item.published_at.isoformat(),
                    "alpaca_id": item.id,
                    "summary": item.summary or "",
                }
                for i, item in enumerate(chunk)
            ],
            "user_message": build_batch_user_message([item.headline for item in chunk]),
        }
        with open(os.path.join(out_dir, f"batch-{count:02d}.json"), "w") as fh:
            json.dump(payload, fh)
        count += 1
    logger.info("Emitted %d classifier batch(es) for %d headlines", count, len(news_items))
    return count


def ingest_batches(in_dir: str, *, session_id: int | None, dry_run: bool) -> PipelineStats:
    """Driver mode step 3: read each batch + its claude result and store signals.

    A batch with no result file, an `is_error` result, or no
    `structured_output.classifications` list is marked noise — the same
    outcome as a rate-limited batch on the API path.
    """
    stats = PipelineStats(news_fetched=0, ticker_signals_stored=0, macro_signals_stored=0, noise_dropped=0, errors=0)
    results = []
    names = sorted(n for n in os.listdir(in_dir) if n.startswith("batch-") and n.endswith(".json") and ".result." not in n)
    for name in names:
        with open(os.path.join(in_dir, name)) as fh:
            batch = json.load(fh)
        items = batch["items"]
        stats.news_fetched += len(items)
        headlines = [it["headline"] for it in items]
        published_ats = [datetime.fromisoformat(it["published_at"]) for it in items]
        alpaca_ids = [it.get("alpaca_id") for it in items]
        summaries = [it.get("summary") or "" for it in items]
        entries = None
        result_path = os.path.join(in_dir, name[:-5] + ".result.json")
        if os.path.exists(result_path):
            with open(result_path) as fh:
                result = json.load(fh)
            if not result.get("is_error"):
                entries = (result.get("structured_output") or {}).get("classifications")
        if not isinstance(entries, list):
            logger.error("Batch %s has no usable classifier result — marking noise", name)
            entries = []
        results.extend(parse_batch_entries(entries, headlines, published_ats, alpaca_ids, summaries))
    return _store_results(results, stats, dry_run)


def main() -> int:
    """CLI entry point for news pipeline."""
    import argparse

    from .log_config import setup_logging

    setup_logging()
    parser = argparse.ArgumentParser(description="Run news processing pipeline")
    parser.add_argument("--hours", type=int, default=24, help="Hours of news to fetch")
    parser.add_argument("--limit", type=int, default=50, help="Max news items")
    parser.add_argument("--dry-run", action="store_true", help="Don't store to database")
    parser.add_argument("--session-id", type=int, default=None)
    parser.add_argument("--emit-batches", metavar="DIR", help="Driver mode: fetch news and write classifier batches to DIR")
    parser.add_argument("--ingest", metavar="DIR", help="Driver mode: store signals from DIR's batch + result files")
    args = parser.parse_args()

    if args.emit_batches:
        emit_batches(args.emit_batches, hours=args.hours, limit=args.limit)
        return 0
    if args.ingest:
        stats = ingest_batches(args.ingest, session_id=args.session_id, dry_run=args.dry_run)
    else:
        stats = run_pipeline(hours=args.hours, limit=args.limit, dry_run=args.dry_run, session_id=args.session_id)
    logger.info("Pipeline stats: %s", stats)
    return 1 if stats.errors > 0 else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
