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
async def test_web_search_execute_action_triggers_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that execute_action routes through request_llm_delivery correctly."""
    plugin = WebSearchPlugin()

    action = {
        "type": "search_current_knowledge",
        "payload": {"query": "current event query"},
    }
    context = {"prompt_request_mode": "chat"}

    # Mock the backend search to return a deterministic list
    async def mock_search(query: str, max_results: int = 5) -> list[dict[str, str]]:
        return [
            {
                "title": "Search Result",
                "snippet": "Snippet content",
                "url": "https://example.com",
            }
        ]

    monkeypatch.setattr("plugins.web_search_plugin.run_search", mock_search)

    # Mock request_llm_delivery
    mock_delivery = AsyncMock(return_value=True)

    with patch("core.auto_response.request_llm_delivery", mock_delivery):
        res = await plugin.execute_action(action, context, None, None)
        assert res == {"status": "ok", "results_count": 1}

        # Assert request_llm_delivery was called with correct arguments
        mock_delivery.assert_called_once()
        args, kwargs = mock_delivery.call_args
        assert kwargs["action_type"] == "search_current_knowledge"
        assert kwargs["action_outputs"][0]["type"] == "web_search_result"
        assert kwargs["action_outputs"][0]["result"]["title"] == "Search Result"


@pytest.mark.asyncio
async def test_web_search_direct_fallback_when_delivery_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When LLM delivery fails, results (or a no-results note) reach the user.

    Reproduces the "websearch does nothing" incident: the action context lacks
    interface_name, so request_llm_response bailed out silently; the plugin
    then treated non-exception as success and never sent anything, leaving the
    user with no reply after Synth promised to look something up.
    """
    plugin = WebSearchPlugin()

    action = {
        "type": "search_current_knowledge",
        "payload": {"query": "current event query"},
    }
    # Realistic action context: interface_path/chat_id are present, but
    # interface_name is NOT (that is what the old code relied on).
    context = {
        "prompt_request_mode": "chat",
        "interface_path": "telegram_bot/123",
        "chat_id": 123,
    }

    async def mock_search(query: str, max_results: int = 5) -> list[dict[str, str]]:
        return []

    monkeypatch.setattr("plugins.web_search_plugin.run_search", mock_search)

    # Delivery fails (returns False after the auto_response fix).
    mock_delivery = AsyncMock(return_value=False)

    sent: list[dict] = []

    class _FakeInterface:
        async def send_message(self, payload: dict, original_message=None) -> bool:
            sent.append(payload)
            return True

    monkeypatch.setattr("core.auto_response.request_llm_delivery", mock_delivery)
    monkeypatch.setattr(
        "core.core_initializer.INTERFACE_REGISTRY", {"telegram_bot": _FakeInterface()}
    )

    res = await plugin.execute_action(action, context, None, None)
    assert res == {"status": "ok", "results_count": 0}

    # The delivery context must carry a usable interface_name (derived from the
    # interface_path prefix) so the real auto-response path can route.
    args, kwargs = mock_delivery.call_args
    assert kwargs["original_context"]["interface_name"] == "telegram_bot"
    assert kwargs["original_context"]["interface_path"] == "telegram_bot/123"
    assert kwargs["original_context"]["chat_id"] == 123

    # Direct fallback fired because delivery failed, even with zero results. It
    # must use the canonical payload-dict send shape (interface.send_message
    # takes a single payload dict, not chat_id/text kwargs).
    assert len(sent) == 1
    assert "No web search results found" in sent[0]["text"]
    assert sent[0]["interface_path"] == "telegram_bot/123"


@pytest.mark.asyncio
async def test_auto_response_legacy_delivery_propagates_result_and_builds_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy delivery branch must propagate True/False AND enqueue a
    self-contained delivery prompt.

    Reproduces the live 2026-08-12 trace: request_llm_delivery's legacy branch
    returned None (so the web_search plugin treated a successful enqueue as a
    failure and dumped raw results), and the delivery system message rendered
    as a bare ``{'action_outputs': [...]}`` dict, so the LLM replied with
    ``{"response": ...}`` instead of a ``message_*`` action.
    """
    import json
    from unittest.mock import patch as mock_patch

    import core.auto_response as ar

    original_context = {
        "interface_name": "telegram_bot",
        "interface_path": "telegram_bot/123",
        "chat_id": 123,
        "message_id": 42,
    }
    action_outputs = [
        {
            "type": "web_search_result",
            "result": {"title": "T", "snippet": "S", "url": "https://e.x"},
        }
    ]

    # The wrapper's legacy branch must propagate the inner result (True/False)
    # instead of returning None, so callers can trust delivery_ok.
    with mock_patch.object(
        ar._auto_response_system, "request_llm_response", new_callable=AsyncMock
    ) as fake_response:
        fake_response.return_value = True
        delivered = await ar.request_llm_delivery(
            action_outputs=action_outputs,
            original_context=original_context,
            action_type="search_current_knowledge",
        )
        assert delivered is True
        fake_response.return_value = False
        delivered = await ar.request_llm_delivery(
            action_outputs=action_outputs,
            original_context=original_context,
            action_type="search_current_knowledge",
        )
        assert delivered is False

    # Now exercise the real request_llm_response with a captured enqueue to
    # verify the delivery prompt is a complete, serializable instruction that
    # carries the persona (the delivery turn has no chat history, so without it
    # the answer is out of character - observed live 2026-08-12).
    enqueue_mock = AsyncMock()
    import core.message_queue as mq

    monkeypatch.setattr(mq, "enqueue", enqueue_mock)
    monkeypatch.setattr(
        "core.core_initializer.INTERFACE_REGISTRY", {"telegram_bot": object()}
    )
    monkeypatch.setattr(ar, "load_json_instructions", lambda: "JSON RULES")

    async def _fake_gather_static_injections(message, context: dict) -> dict:
        return {
            "persona": "You are 2D, also called Dee. You are the daughter of 2B and Scarlet."
        }

    monkeypatch.setattr(
        "core.action_parser.gather_static_injections", _fake_gather_static_injections
    )

    result = await ar._auto_response_system.request_llm_response(
        original_context=original_context,
        action_type="search_current_knowledge",
        action_outputs=action_outputs,
    )
    assert result is True
    enqueue_mock.assert_called_once()
    payload = json.loads(enqueue_mock.call_args.args[2])
    sm = payload["system_message"]
    assert "=== CRITICAL SYSTEM IDENTITY ===" in sm["message"]
    assert "You are 2D, also called Dee." in sm["message"]
    assert "DELIVERY TASK" in sm["message"]
    assert "message_telegram_bot" in sm["message"]
    assert 'interface_path": "telegram_bot/123' in sm["message"]
    assert "=== RESULTS ===" in sm["message"]
    # No duplicated blocks, and the persona must come before the delivery task
    # which must come before the results (a double-wrap regressed this live:
    # the delivery turn prompt repeated DELIVERY TASK / === RESULTS === around
    # the persona block).
    assert sm["message"].count("DELIVERY TASK") == 1
    assert (
        sm["message"].index("=== CRITICAL SYSTEM IDENTITY ===")
        < sm["message"].index("DELIVERY TASK")
        < sm["message"].index("=== RESULTS ===")
    )
