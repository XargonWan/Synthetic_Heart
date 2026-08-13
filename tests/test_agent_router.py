"""Unit tests for the Agent Lane reply-delivery guard (core/agent_router.py)."""

from __future__ import annotations

from typing import Any

import pytest

from core import agent_router


def test_agent_actions_executed_counts_tool_results() -> None:
    result: dict[str, Any] = {
        "observations": [
            {"iteration": 1, "role": "error", "content": "empty_model_response"},
            {"iteration": 2, "role": "assistant", "content": "thought\nthought"},
            {
                "iteration": 3,
                "role": "tool_results",
                "content": [
                    {"ok": True, "tool": "mcp_fs_read"},
                    {"ok": True, "tool": "mcp_fs_read"},
                ],
            },
        ]
    }
    assert agent_router._agent_actions_executed(result) == 2


def test_agent_actions_executed_zero_for_no_tool_results() -> None:
    result: dict[str, Any] = {
        "observations": [
            {"iteration": 1, "role": "error", "content": "empty_model_response"},
            {"iteration": 2, "role": "assistant", "content": "thought\nthought"},
        ]
    }
    assert agent_router._agent_actions_executed(result) == 0


def _delivery_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict[str, Any]], Any]:
    delivered: list[dict[str, Any]] = []

    async def fake_run_action(
        action: dict[str, Any], context: Any, bot: Any, message: Any
    ) -> dict[str, Any]:
        delivered.append(action)
        return {"ok": True}

    monkeypatch.setattr("core.action_parser.run_action", fake_run_action)
    return delivered, fake_run_action


@pytest.mark.asyncio
async def test_deliver_suppresses_timeout_with_zero_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivered, _ = _delivery_fixture(monkeypatch)
    result: dict[str, Any] = {
        "stop_reason": "timeout",
        "final_text": "thought\nthought",
        "observations": [
            {"iteration": 1, "role": "error", "content": "empty_model_response"},
            {"iteration": 2, "role": "assistant", "content": "thought\nthought"},
        ],
    }
    context = {"interface_path": "telegram_bot/123"}

    await agent_router._deliver_agent_reply(result, context, None, None)

    assert delivered == []


@pytest.mark.asyncio
async def test_deliver_ships_timeout_with_executed_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivered, _ = _delivery_fixture(monkeypatch)
    result: dict[str, Any] = {
        "stop_reason": "timeout",
        "final_text": "I read the file — it is small.",
        "observations": [
            {
                "iteration": 1,
                "role": "tool_results",
                "content": [{"ok": True, "tool": "mcp_fs_read"}],
            },
            {"iteration": 2, "role": "assistant", "content": "I read the file."},
        ],
    }
    context = {"interface_path": "telegram_bot/123"}

    await agent_router._deliver_agent_reply(result, context, None, None)

    assert len(delivered) == 1
    action = delivered[0]
    assert action["type"] == "message_telegram_bot"
    assert action["payload"]["text"] == "I read the file — it is small."
    assert action["payload"]["interface_path"] == "telegram_bot/123"


@pytest.mark.asyncio
async def test_deliver_ships_normal_stop_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivered, _ = _delivery_fixture(monkeypatch)
    result: dict[str, Any] = {
        "stop_reason": "model_done",
        "final_text": "Exploration complete.",
        "observations": [],
    }
    context = {"interface_path": "telegram_bot/123"}

    await agent_router._deliver_agent_reply(result, context, None, None)

    assert len(delivered) == 1
    assert delivered[0]["payload"]["text"] == "Exploration complete."


@pytest.mark.asyncio
async def test_deliver_skips_empty_final_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivered, _ = _delivery_fixture(monkeypatch)
    result: dict[str, Any] = {
        "stop_reason": "timeout",
        "final_text": "   ",
        "observations": [],
    }
    context = {"interface_path": "telegram_bot/123"}

    await agent_router._deliver_agent_reply(result, context, None, None)

    assert delivered == []


@pytest.mark.asyncio
async def test_deliver_suppresses_paused_timeout_with_zero_actions_but_keeps_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivered, _ = _delivery_fixture(monkeypatch)
    result: dict[str, Any] = {
        "stop_reason": "paused_max_iterations",
        "final_text": "I'm not finished, shall I continue?",
        "observations": [],
    }
    context = {"interface_path": "telegram_bot/123"}

    await agent_router._deliver_agent_reply(result, context, None, None)

    # The pause path composes its own message and must still be delivered.
    assert len(delivered) == 1
    assert delivered[0]["payload"]["text"] == "I'm not finished, shall I continue?"
