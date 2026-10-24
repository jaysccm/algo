"""Tests for v2/news.py — Alpaca fetch window and the limit=None ceiling."""
import logging
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from v2 import news
from v2.news import NEWS_FETCH_CEILING, fetch_broad_news, fetch_news


def _alpaca_item(i: int):
    item = MagicMock()
    item.id = i
    item.headline = f"Headline {i}"
    item.summary = ""
    item.author = ""
    item.source = "benzinga"
    item.symbols = ["AAPL"]
    item.created_at = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    item.url = ""
    return item


def _client_returning(n: int):
    client = MagicMock()
    response = MagicMock()
    response.data = {"news": [_alpaca_item(i) for i in range(n)]}
    client.get_news.return_value = response
    return client


class TestFetchNewsLimit:
    @patch("v2.news.NewsRequest")
    @patch("v2.news.get_news_client")
    def test_limit_none_requests_the_ceiling(self, mock_client, mock_request):
        """limit=None means 'whole window': the SDK pages until Alpaca runs
        out, bounded only by the safety ceiling."""
        mock_client.return_value = _client_returning(3)
        items = fetch_news(hours=24, limit=None)
        assert len(items) == 3
        assert mock_request.call_args.kwargs["limit"] == NEWS_FETCH_CEILING
        assert mock_request.call_args.kwargs["sort"] == "desc"

    @patch("v2.news.NewsRequest")
    @patch("v2.news.get_news_client")
    def test_explicit_limit_is_passed_through(self, mock_client, mock_request):
        mock_client.return_value = _client_returning(1)
        fetch_news(hours=24, limit=25)
        assert mock_request.call_args.kwargs["limit"] == 25

    @patch("v2.news.NewsRequest")
    @patch("v2.news.get_news_client")
    def test_warns_when_ceiling_is_hit(self, mock_client, mock_request, monkeypatch, caplog):
        """Alpaca is newest-first, so hitting the ceiling means the oldest
        news in the window was dropped — that must be visible in the log."""
        monkeypatch.setattr(news, "NEWS_FETCH_CEILING", 2)
        mock_client.return_value = _client_returning(2)
        with caplog.at_level(logging.WARNING, logger="v2.news"):
            fetch_news(hours=72, limit=None)
        assert any("NEWS_FETCH_CEILING" in r.message and "72h" in r.message for r in caplog.records)

    @patch("v2.news.NewsRequest")
    @patch("v2.news.get_news_client")
    def test_no_warning_below_ceiling(self, mock_client, mock_request, monkeypatch, caplog):
        monkeypatch.setattr(news, "NEWS_FETCH_CEILING", 5)
        mock_client.return_value = _client_returning(2)
        with caplog.at_level(logging.WARNING, logger="v2.news"):
            fetch_news(hours=24, limit=None)
        assert not [r for r in caplog.records if "NEWS_FETCH_CEILING" in r.message]

    @patch("v2.news.NewsRequest")
    @patch("v2.news.get_news_client")
    def test_explicit_limit_never_warns(self, mock_client, mock_request, monkeypatch, caplog):
        """An operator-chosen cap is deliberate; only the whole-window path warns."""
        monkeypatch.setattr(news, "NEWS_FETCH_CEILING", 2)
        mock_client.return_value = _client_returning(2)
        with caplog.at_level(logging.WARNING, logger="v2.news"):
            fetch_news(hours=24, limit=2)
        assert not [r for r in caplog.records if "NEWS_FETCH_CEILING" in r.message]


class TestFetchBroadNews:
    @patch("v2.news.fetch_news")
    def test_defaults_to_whole_window(self, mock_fetch):
        mock_fetch.return_value = []
        fetch_broad_news()
        mock_fetch.assert_called_once_with(hours=24, symbols=None, limit=None)

    @patch("v2.news.fetch_news")
    def test_passes_explicit_values(self, mock_fetch):
        mock_fetch.return_value = []
        fetch_broad_news(hours=72, limit=100)
        mock_fetch.assert_called_once_with(hours=72, symbols=None, limit=100)
