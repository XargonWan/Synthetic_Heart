# tests/test_chronos_grounding.py
"""Unit and integration tests for the Chronos temporal and factual grounding features."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugins.grillo.grillo_temporal_reflection import (
    GrilloTemporalReflectionPlugin,
    format_time_delta,
)
from plugins.web_search_plugin import WebSearchPlugin


def test_format_time_delta() -> None:
    """Test natural language formatting of time durations."""
    assert format_time_delta(30) == "just a moment ago"
    assert format_time_delta(120) == "2 minutes ago"
    assert format_time_delta(3600) == "1 hour ago"
    assert format_time_delta(3600 * 5) == "5 hours ago"
    assert format_time_delta(3600 * 25) == "1 day ago"
    assert format_time_delta(3600 * 48) == "2 days ago"


@pytest.mark.asyncio
async def test_grillo_temporal_reflection_time_delta() -> None:
    """Test that temporal reflection correctly queries the database and calculates elapsed time."""
    plugin = GrilloTemporalReflectionPlugin()

    # Mock DB connection
    mock_cursor = MagicMock()
    # Return a deterministic timestamp from 4 hours ago
    last_message_time = datetime.now(timezone.utc) - timedelta(hours=4)

    # We mock fetchone to return the timestamp wrapped in a tuple
    mock_cursor.fetchone = AsyncMock(return_value=(last_message_time,))

    # Mock cursor execute
    mock_cursor.execute = AsyncMock()

    # Context manager mock
    class MockConnCtx:
        async def __aenter__(self):
            conn = MagicMock()

            # Need an async cursor context manager
            class MockCursorCtx:
                async def __aenter__(self):
                    return mock_cursor

                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass

            conn.cursor = MockCursorCtx
            return conn

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    with patch(
        "plugins.grillo.grillo_temporal_reflection.get_conn_ctx",
        return_value=MockConnCtx(),
    ):
        delta = await plugin.get_time_delta()
        assert delta is not None
        # Should be roughly 4 hours (14400 seconds)
        assert abs(delta - 14400) < 5

        prompt = await plugin.build_prompt()
        assert "[SYSTEM: AUTONOMOUS TEMPORAL REFLECTION]" in prompt
        assert "Your last interaction with the user was 4 hours ago" in prompt
        assert "create_personal_diary_entry" in prompt
        # 4 hours is a routine gap — must not be framed as an absence.
        assert "ordinary gap" in prompt
        assert "not a factual observation about where the user currently is" in prompt


@pytest.mark.asyncio
async def test_grillo_temporal_reflection_unusual_gap_keeps_absence_framing() -> None:
    """A genuinely long gap (beyond the routine threshold) may still prompt the
    loneliness/reach-out framing — but even then must not assert the user is away."""
    plugin = GrilloTemporalReflectionPlugin()

    mock_cursor = MagicMock()
    last_message_time = datetime.now(timezone.utc) - timedelta(hours=36)
    mock_cursor.fetchone = AsyncMock(return_value=(last_message_time,))
    mock_cursor.execute = AsyncMock()

    class MockConnCtx:
        async def __aenter__(self):
            conn = MagicMock()

            class MockCursorCtx:
                async def __aenter__(self):
                    return mock_cursor

                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass

            conn.cursor = MockCursorCtx
            return conn

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    with patch(
        "plugins.grillo.grillo_temporal_reflection.get_conn_ctx",
        return_value=MockConnCtx(),
    ):
        prompt = await plugin.build_prompt()
        assert "Your last interaction with the user was 1 day ago" in prompt
        assert "inclination to reach out" in prompt
        assert "ordinary gap" not in prompt
        # Even for an unusual gap, this must stay a private feeling, not a claim.
        assert "not a factual observation about where the user currently is" in prompt


@pytest.mark.asyncio
async def test_grillo_temporal_reflection_handles_naive_db_timestamps() -> None:
    """DB drivers return naive datetimes (stored as UTC); the delta must not raise."""
    plugin = GrilloTemporalReflectionPlugin()

    mock_cursor = MagicMock()
    # Naive UTC timestamp from 2 hours ago, as returned by aiomysql/MariaDB
    last_message_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        hours=2
    )
    mock_cursor.fetchone = AsyncMock(return_value=(last_message_time,))
    mock_cursor.execute = AsyncMock()

    class MockConnCtx:
        async def __aenter__(self):
            conn = MagicMock()

            class MockCursorCtx:
                async def __aenter__(self):
                    return mock_cursor

                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass

            conn.cursor = MockCursorCtx
            return conn

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    with patch(
        "plugins.grillo.grillo_temporal_reflection.get_conn_ctx",
        return_value=MockConnCtx(),
    ):
        delta = await plugin.get_time_delta()
        assert delta is not None
        assert abs(delta - 7200) < 5


@pytest.mark.asyncio
async def test_web_search_tavily() -> None:
    """Test Tavily search query handling and serialization."""
    plugin = WebSearchPlugin()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = MagicMock(
        return_value={
            "results": [
                {
                    "title": "Tavily Search Result",
                    "content": "This is content from Tavily.",
                    "url": "https://tavily.com/result",
                }
            ]
        }
    )

    with patch("requests.post", return_value=mock_response):
        results = await plugin._search_tavily("fake_key", "test query")
        assert len(results) == 1
        assert results[0]["title"] == "Tavily Search Result"
        assert results[0]["snippet"] == "This is content from Tavily."
        assert results[0]["url"] == "https://tavily.com/result"


@pytest.mark.asyncio
async def test_web_search_duckduckgo_fallback() -> None:
    """Test DuckDuckGo HTML scraping fallback."""
    plugin = WebSearchPlugin()

    dummy_html = """
    <html>
        <body>
            <div class="result">
                <h2 class="result__title">
                    <a class="result__a" href="/l/?kh=-1&uddg=https%3A%2F%2Fexample.com%2Fddg_result">DuckDuckGo Scraped Result</a>
                </h2>
                <div class="result__snippet">This is scraped snippet content from DDG.</div>
                <span class="result__url">example.com/ddg_result</span>
            </div>
        </body>
    </html>
    """

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = dummy_html

    with patch("requests.get", return_value=mock_response):
        results = await plugin._search_duckduckgo("test query")
        assert len(results) == 1
        assert results[0]["title"] == "DuckDuckGo Scraped Result"
        assert results[0]["snippet"] == "This is scraped snippet content from DDG."
        assert results[0]["url"] == "https://example.com/ddg_result"


@pytest.mark.asyncio
async def test_web_search_execute_action_triggers_delivery() -> None:
    """Test that execute_action routes through request_llm_delivery correctly."""
    plugin = WebSearchPlugin()

    action = {
        "type": "search_current_knowledge",
        "payload": {"query": "current event query"},
    }
    context = {"prompt_request_mode": "chat"}

    # Mock the search function to return a deterministic list
    async def mock_search(query):
        return [
            {
                "title": "Search Result",
                "snippet": "Snippet content",
                "url": "https://example.com",
            }
        ]

    plugin._search_duckduckgo = mock_search

    # Mock request_llm_delivery
    mock_delivery = AsyncMock(return_value=True)

    with patch("core.auto_response.request_llm_delivery", mock_delivery):
        # We patch TAVILY_API_KEY to empty so it uses duckduckgo
        with patch("plugins.web_search_plugin.TAVILY_API_KEY", ""):
            res = await plugin.execute_action(action, context, None, None)
            assert res == {"status": "ok", "results_count": 1}

            # Assert request_llm_delivery was called with correct arguments
            mock_delivery.assert_called_once()
            args, kwargs = mock_delivery.call_args
            assert kwargs["action_type"] == "search_current_knowledge"
            assert kwargs["action_outputs"][0]["type"] == "web_search_result"
            assert kwargs["action_outputs"][0]["result"]["title"] == "Search Result"
