"""Lean agent prompt: goal plus tool history as plain OpenAI messages."""

from __future__ import annotations

import json

import pytest

from core import agent_core
from core.agent_core import AgentLoopManager


def _prompt(observations=None, context=None) -> dict:
    prompt = {
        "input": {
            "payload": {
                "text": "GOAL: x\nAVAILABLE TOOLS: ...",
                "system": "AGENTIC SYS",
            }
        },
        "system_message": {"type": "agent_turn", "goal": "Restart the worst server."},
        "agent_mode": True,
        "observation_history": observations or [],
    }
    if context:
        prompt["context"] = context
    return prompt


def test_goal_only_is_a_single_user_message():
    assert AgentLoopManager._lean_messages(_prompt()) == [
        {"role": "user", "content": "Restart the worst server."}
    ]


def test_tool_history_becomes_assistant_and_tool_messages():
    observations = [
        {
            "role": "tool_results",
            "content": [
                {
                    "tool": "get_error_count",
                    "arguments": {"server": "srv-01"},
                    "ok": True,
                    "result": "srv-01: 12 errors",
                },
                {
                    "tool": "get_error_count",
                    "arguments": {"server": "srv-02"},
                    "ok": False,
                    "error": "timeout",
                },
            ],
        },
        {"role": "system", "content": "Keep going."},
    ]
    messages = AgentLoopManager._lean_messages(_prompt(observations))

    assert [m["role"] for m in messages] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "user",
    ]
    calls = messages[1]["tool_calls"]
    assert [c["id"] for c in calls] == ["call_1", "call_2"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"server": "srv-01"}
    assert messages[2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "srv-01: 12 errors",
    }
    assert messages[3]["content"] == "error: timeout"
    assert messages[4]["content"] == "Keep going."


def test_missing_arguments_and_structured_results_are_serialised():
    observations = [
        {
            "role": "tool_results",
            "content": [{"tool": "list", "ok": True, "result": {"a": 1}}],
        },
    ]
    messages = AgentLoopManager._lean_messages(_prompt(observations))
    assert messages[1]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert messages[2]["content"] == '{"a": 1}'


def test_reply_channel_is_told_in_the_first_message():
    messages = AgentLoopManager._lean_messages(
        _prompt(context={"interface_path": "telegram/chat-7"})
    )
    assert "telegram/chat-7" in messages[0]["content"]
    assert messages[0]["content"].endswith("Restart the worst server.")


class _Bridge:
    lean_agent_prompt = True
    _adapter = object()

    def __init__(self) -> None:
        self.calls: list[tuple[list, dict]] = []

    async def generate_response(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return "ok"


def _install(monkeypatch, engine) -> None:
    class Registry:
        def get_engine(self, name):
            return engine

        def load_engine(self, name):
            return engine

    monkeypatch.setattr("core.cortex_registry.get_cortex_registry", lambda: Registry())
    monkeypatch.setattr(
        agent_core,
        "_build_openai_tool_manifests",
        lambda: [{"type": "function", "function": {"name": "t"}}],
    )


@pytest.mark.asyncio
async def test_lean_endpoint_gets_no_system_text_and_native_tools(monkeypatch):
    engine = _Bridge()
    _install(monkeypatch, engine)

    out = await AgentLoopManager()._call_engine_direct(_prompt(), "zen")

    messages, kwargs = engine.calls[0]
    assert out == "ok"
    assert [m["role"] for m in messages] == ["user"]
    assert "AGENTIC" not in json.dumps(
        messages
    ) and "AVAILABLE TOOLS" not in json.dumps(messages)
    assert kwargs["tools"] == [{"type": "function", "function": {"name": "t"}}]
    assert kwargs["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_a_regular_endpoint_still_gets_the_system_prompt(monkeypatch):
    engine = _Bridge()
    engine.lean_agent_prompt = False
    _install(monkeypatch, engine)

    await AgentLoopManager()._call_engine_direct(_prompt(), "zen")

    messages, _ = engine.calls[0]
    assert messages[0] == {"role": "system", "content": "AGENTIC SYS"}


@pytest.mark.asyncio
async def test_lean_is_skipped_for_the_prose_only_calls(monkeypatch):
    engine = _Bridge()
    _install(monkeypatch, engine)

    await AgentLoopManager()._call_engine_direct(_prompt(), "zen", native_tools=False)

    messages, kwargs = engine.calls[0]
    assert messages[0]["role"] == "system" and "tools" not in kwargs
