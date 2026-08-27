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
    voiceover: str = "",
) -> tuple[list[dict[str, Any]], Any]:
    delivered: list[dict[str, Any]] = []

    async def fake_run_action(
        action: dict[str, Any], context: Any, bot: Any, message: Any
    ) -> dict[str, Any]:
        delivered.append(action)
        return {"ok": True}

    monkeypatch.setattr("core.action_parser.run_action", fake_run_action)

    # Keep delivery-guard tests focused: the persona voiceover defaults to a
    # no-op returning ``voiceover`` (empty → original text ships unchanged).
    from core.agent_core import AgentLoopManager

    async def fake_voiceover(self, final_text, **kwargs):
        return voiceover

    monkeypatch.setattr(AgentLoopManager, "persona_voiceover", fake_voiceover)
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


# --------------------------------------------------------------------------- #
# Single-gate routing (AGENT_ENABLED is the only authoritative toggle; the old
# AGENTIC_ROUTING_ENABLED feature flag was removed).
# --------------------------------------------------------------------------- #
def _set_agent_enabled(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    def fake_get_var(key, default=None, value_type=None):
        if key == "AGENT_ENABLED":
            return enabled
        return default

    monkeypatch.setattr("core.config_manager.config_registry.get_var", fake_get_var)


def test_classify_agent_enabled_with_agent_needed_returns_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_agent_enabled(monkeypatch, True)
    assert (
        agent_router.classify([], context={"agent_needed": True}) == agent_router.AGENT
    )


def test_classify_agent_disabled_returns_fast_even_with_agent_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With the agent OFF, even a clear agentic signal must stay on the Fast Lane.
    _set_agent_enabled(monkeypatch, False)
    assert (
        agent_router.classify([], context={"agent_needed": True}) == agent_router.FAST
    )


def test_classify_agent_enabled_no_signal_returns_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_agent_enabled(monkeypatch, True)
    assert agent_router.classify([], context={}) == agent_router.FAST


def test_classify_agent_enabled_tool_call_returns_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_agent_enabled(monkeypatch, True)
    # An mcp_* action is always a tool call, so the safety net escalates to the
    # Agent Lane regardless of the tool registry state.
    actions = [{"type": "mcp_fs_read", "payload": {"path": "/app/x.txt"}}]
    assert agent_router.classify(actions, context={}) == agent_router.AGENT


@pytest.mark.asyncio
async def test_deliver_revoices_final_text_through_persona(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the persona voiceover returns styled text, THAT is what ships to
    the interface — the agent's raw operational summary never reaches the
    user directly."""
    delivered, _ = _delivery_fixture(monkeypatch, voiceover="Voiced by Dee!")
    result: dict[str, Any] = {
        "stop_reason": "completed",
        "final_text": "task complete: file written to /tmp/x",
        "observations": [],
    }
    context = {"interface_path": "telegram_bot/123"}

    await agent_router._deliver_agent_reply(
        result, context, None, None, goal="write the file"
    )

    assert len(delivered) == 1
    assert delivered[0]["payload"]["text"] == "Voiced by Dee!"


@pytest.mark.asyncio
async def test_deliver_persona_disabled_ships_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AGENT_PERSONA_DELIVERY=False is a hard bypass: the voiceover is never
    invoked and the original agent text ships as-is."""
    from core.config_manager import config_registry
    from core.agent_core import AgentLoopManager

    original = config_registry.get_value

    def fake_get_value(key, default=None, *a, **kw):
        if key == "AGENT_PERSONA_DELIVERY":
            return False
        return original(key, default, *a, **kw)

    monkeypatch.setattr(config_registry, "get_value", fake_get_value)

    async def must_not_run(self, final_text, **kwargs):
        raise AssertionError("voiceover must not run when disabled")

    monkeypatch.setattr(AgentLoopManager, "persona_voiceover", must_not_run)

    delivered, _ = _delivery_fixture(monkeypatch)
    # Re-apply the voiceover stub AFTER the fixture (fixture overrides it).
    monkeypatch.setattr(AgentLoopManager, "persona_voiceover", must_not_run)

    result: dict[str, Any] = {
        "stop_reason": "completed",
        "final_text": "raw agent text",
        "observations": [],
    }
    context = {"interface_path": "telegram_bot/123"}

    await agent_router._deliver_agent_reply(result, context, None, None)

    assert len(delivered) == 1
    assert delivered[0]["payload"]["text"] == "raw agent text"


@pytest.mark.asyncio
async def test_deliver_voiceover_failure_falls_back_to_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A voiceover that fails (returns empty) must never lose the result."""
    from core.agent_core import AgentLoopManager

    async def failing_voiceover(self, final_text, **kwargs):
        return ""

    delivered, _ = _delivery_fixture(monkeypatch)
    monkeypatch.setattr(AgentLoopManager, "persona_voiceover", failing_voiceover)

    result: dict[str, Any] = {
        "stop_reason": "completed",
        "final_text": "the important result",
        "observations": [],
    }
    context = {"interface_path": "telegram_bot/123"}

    await agent_router._deliver_agent_reply(result, context, None, None)

    assert len(delivered) == 1
    assert delivered[0]["payload"]["text"] == "the important result"
