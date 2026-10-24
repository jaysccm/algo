"""Alpaca news fetching."""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

try:
    from datetime import UTC
except ImportError:  # pragma: no cover - Python < 3.11
    UTC = timezone.utc  # noqa: UP017

logger = logging.getLogger(__name__)

# Safety valve for "fetch the whole window" calls (limit=None). Alpaca sorts
# newest-first, so whatever a cap cuts off is the *oldest* news in the window
# — the previous afternoon's after-close earnings, overnight and weekend
# items. The old session default of 300 was hit every single day (Alpaca
# publishes ~2,400 items/day) and shrank the effective window to 1.5–9 hours.
# 10,000 covers ~4 days of volume; a 7-day window after an outage can exceed
# it, which the warning below makes visible instead of silent.
NEWS_FETCH_CEILING = 10_000


@dataclass
class NewsItem:
    """Structured news item from Alpaca."""
    id: str
    headline: str
    summary: str
    author: str
    source: str
    symbols: list[str]
    published_at: datetime
    url: str


def get_news_client() -> NewsClient:
    """Create Alpaca news client from environment variables."""
    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")

    if not api_key or not secret_key:
        raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set")

    return NewsClient(api_key, secret_key)


def fetch_news(
    hours: int = 24,
    symbols: list[str] | None = None,
    limit: int | None = 50
) -> list[NewsItem]:
    """Fetch news from Alpaca News API.

    `limit=None` fetches the entire window (the SDK pages 50 items at a
    time until Alpaca runs out), capped at NEWS_FETCH_CEILING as a safety
    valve. Because results are newest-first, any cap drops the oldest
    news in the window, so callers that care about coverage should pass
    None rather than a round number.
    """
    client = get_news_client()

    # T2.3: Alpaca returns aware UTC timestamps; the start filter must
    # be aware too. A naive `datetime.now()` is interpreted as local
    # time by some pieces of the SDK, which can shift the cutoff and
    # cause "lost" news on hosts whose clock is not UTC.
    start_time = datetime.now(UTC) - timedelta(hours=hours)

    effective_limit = NEWS_FETCH_CEILING if limit is None else limit
    request_params = {
        "start": start_time,
        "limit": effective_limit,
        "sort": "desc"
    }

    if symbols:
        request_params["symbols"] = symbols

    request = NewsRequest(**request_params)
    response = client.get_news(request)

    items = []
    for news in response.data.get("news", []):
        items.append(NewsItem(
            id=str(news.id),
            headline=news.headline,
            summary=news.summary or "",
            author=news.author or "",
            source=news.source or "",
            symbols=news.symbols or [],
            published_at=news.created_at,
            url=news.url or ""
        ))

    if limit is None and len(items) >= NEWS_FETCH_CEILING:
        logger.warning(
            "news: fetch hit NEWS_FETCH_CEILING=%d for a %dh window — the oldest "
            "news in the window was dropped. Raise the ceiling or shorten the window.",
            NEWS_FETCH_CEILING, hours,
        )

    return items


def fetch_broad_news(hours: int = 24, limit: int | None = None) -> list[NewsItem]:
    """Fetch broad market news without symbol filter.

    Defaults to the whole window (limit=None); see fetch_news for why a
    fixed cap is the wrong default for the ingestion path.
    """
    return fetch_news(hours=hours, symbols=None, limit=limit)


def fetch_ticker_news(ticker: str, hours: int = 24, limit: int = 20) -> list[NewsItem]:
    """Fetch news for a specific ticker."""
    return fetch_news(hours=hours, symbols=[ticker], limit=limit)
