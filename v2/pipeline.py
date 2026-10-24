"""News pipeline orchestrator — fetch, classify, store."""

import logging
from dataclasses import dataclass

from .classifier import classify_news_batch
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


def run_pipeline(
    hours: int = 24, limit: int | None = None, dry_run: bool = False, session_id: int | None = None,
) -> PipelineStats:
    """Run the news pipeline: fetch -> classify -> store.

    `limit=None` (the default) ingests the whole `hours` window. A numeric
    limit keeps only the newest N items — Alpaca sorts newest-first — so
    it silently discards the oldest part of the window; the old 300
    default was hit every day and cut the window to a few hours.
    """
    stats = PipelineStats(news_fetched=0, ticker_signals_stored=0,
                          macro_signals_stored=0, noise_dropped=0, errors=0)

    # Step 1: Fetch news from Alpaca
    news_items = fetch_broad_news(hours=hours, limit=limit)
    stats.news_fetched = len(news_items)
    logger.info("pipeline: fetched %d news items (window=%dh, limit=%s)",
                stats.news_fetched, hours, "none" if limit is None else limit)

    if not news_items:
        return stats

    # Step 2: Classify with Haiku (batched)
    headlines = [item.headline for item in news_items]
    published_ats = [item.published_at for item in news_items]
    alpaca_ids = [item.id for item in news_items]
    summaries = [item.summary for item in news_items]
    results = classify_news_batch(headlines, published_ats, alpaca_ids=alpaca_ids, summaries=summaries, session_id=session_id)

    # Step 3: Store signals
    ticker_signals_batch = []
    macro_signals_batch = []

    for result in results:
        if result.news_type == "noise":
            stats.noise_dropped += 1
            continue

        for signal in result.ticker_signals:
            # A ticker_specific headline the classifier files under category
            # "noise" is "not directly relevant to stock price" by definition.
            # Storing it showed it to the strategist and executor and counted
            # it in sentiment tallies (10.6% of paper rows in 30 days; JAY-171).
            if signal.category == "noise":
                stats.noise_dropped += 1
                continue
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

    logger.info(
        "pipeline: %s ticker_signals=%d macro_signals=%d noise_dropped=%d errors=%d",
        "dry-run, would store" if dry_run else "stored",
        len(ticker_signals_batch) if dry_run else stats.ticker_signals_stored,
        len(macro_signals_batch) if dry_run else stats.macro_signals_stored,
        stats.noise_dropped, stats.errors,
    )
    return stats


def main():
    """CLI entry point for news pipeline."""
    import argparse
    import sys

    from .log_config import setup_logging

    setup_logging()

    parser = argparse.ArgumentParser(description="Run news processing pipeline")
    parser.add_argument("--hours", type=int, default=24, help="Hours of news to fetch")
    parser.add_argument("--limit", type=int, default=None,
                        help="Keep only the newest N items (default: whole window)")
    parser.add_argument("--dry-run", action="store_true", help="Don't store to database")

    args = parser.parse_args()

    stats = run_pipeline(
        hours=args.hours,
        limit=args.limit,
        dry_run=args.dry_run
    )

    if stats.errors > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
