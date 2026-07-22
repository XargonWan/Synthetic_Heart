"""Tests for the Agentic Runtime 2.0 (Fasi A-F).

Covers:
* Unified ToolRegistry (Fase B) — internal + MCP tool namespacing.
* AgentToolExecutor (Fase D) — unknown-tool branch + internal dispatch.
* AgentLoopManager.run_agentic_turn (Fase D) — bounded loop, observation re-injection.
* agent_router.classify (Fase E) — Fast vs Agent lane decisions.
* MCP server exposure (Fase F) — build_server registers whitelisted actions.
"""

import json

import pytest

from core.agent_core import AgentLoopManager
from core.agent_router import classify
from core.agent_tool_executor import agent_tool_executor


@pytest.mark.asyncio
async def test_executor_unknown_tool(monkeypatch):
    """An unknown tool name must fail gracefully, not raise."""
    res = await agent_tool_executor.execute("no_such_tool", {"x": 1})
    assert res["ok"] is False
    assert "Unknown tool" in res["error"]


@pytest.mark.asyncio
async def test_executor_internal_dispatch(monkeypatch):
    """Internal tools route through run_action and return a string observation."""

    captured = {}

    async def fake_run_action(action, context, bot, original_message):
        captured["action"] = action
        return {"result": "did the thing"}

    monkeypatch.setattr("core.action_parser.run_action", fake_run_action)

    # Register a fake internal tool in the registry.
    from core.tool_registry import tool_registry

    tool_registry._tools.clear()
    tool_registry.load_internal_actions(
        {
            "fake_action": {
                "schema": {"type": "object", "properties": {}},
                "brief": "test",
                "security_level": "low",
                "external_effects": [],
            }
        }
    )

    try:
        res = await agent_tool_executor.execute("fake_action", {"payload": {"a": 1}})
        assert res["ok"] is True
        assert res["result"] == "did the thing"
        assert captured["action"]["type"] == "fake_action"
    finally:
        tool_registry._tools.clear()


@pytest.mark.asyncio
async def test_run_agentic_turn_model_done(monkeypatch):
    """When the model emits no tool calls, the loop stops with model_done."""

    calls = []

    async def fake_handle(bot, message, context_memory_or_prompt):
        calls.append(context_memory_or_prompt)
        # First iteration: ask for a tool; second: final answer, no tool calls.
        if len(calls) == 1:
            return json.dumps(
                {"actions": [{"type": "mcp_fs_read", "payload": {"path": "/x"}}]}
            )
        return "All done, file read."

    monkeypatch.setattr("core.plugin_instance.handle_incoming_message", fake_handle)

    # Make the tool executor treat mcp_fs_read as a no-op success.
    async def fake_execute(name, arguments, context=None, original_message=None):
        return {
            "ok": True,
            "tool": name,
            "source": "mcp:fs",
            "result": "file contents",
            "error": None,
        }

    monkeypatch.setattr(agent_tool_executor, "execute", fake_execute)

    manager = AgentLoopManager()
    out = await manager.run_agentic_turn(
        goal="read the file", max_iterations=5, timeout_seconds=30
    )
    assert out["stop_reason"] == "model_done"
    assert out["final_text"] == "All done, file read."
    assert out["iterations"] >= 2
    # Observation history must include the tool result.
    tool_obs = [o for o in out["observations"] if o.get("role") == "tool_results"]
    assert tool_obs, "expected a tool_results observation"


def test_router_fast_lane_pure_message(monkeypatch):
    """A single plain message stays on the Fast Lane."""
    monkeypatch.setattr(
        "core.agent_router.config_registry",
        type("C", (), {"get_var": lambda *a, **k: True})(),
    )
    lane = classify([{"type": "message", "payload": {"text": "hi"}}])
    assert lane == "fast"


def test_router_agent_lane_tool_call(monkeypatch):
    """An mcp_ tool call forces the Agent Lane."""
    monkeypatch.setattr(
        "core.agent_router.config_registry",
        type("C", (), {"get_var": lambda *a, **k: True})(),
    )
    lane = classify([{"type": "mcp_fs_read", "payload": {"path": "/x"}}])
    assert lane == "agent"


def test_router_message_only_batch_stays_fast(monkeypatch):
    """A batch of pure message actions must NOT engage the Agent Lane.

    Synth actions such as ``message_*`` / ``tts_speak`` are plain replies; they
    must be delivered on the classic Fast Lane and never drive the agent loop.
    """
    monkeypatch.setattr(
        "core.agent_router.config_registry",
        type("C", (), {"get_var": lambda *a, **k: True})(),
    )
    lane = classify(
        [
            {"type": "message", "payload": {"text": "a"}},
            {"type": "tts_speak", "payload": {"text": "b"}},
        ]
    )
    assert lane == "fast"


def test_router_mixed_actions_force_agent(monkeypatch):
    """A mixed batch containing a real tool call forces the Agent Lane."""
    monkeypatch.setattr(
        "core.agent_router.config_registry",
        type("C", (), {"get_var": lambda *a, **k: True})(),
    )
    lane = classify(
        [
            {"type": "message", "payload": {"text": "a"}},
            {"type": "mcp_fs_read", "payload": {"path": "/x"}},
        ]
    )
    assert lane == "agent"


def test_router_disabled_returns_fast(monkeypatch):
    """When the feature flag is off, everything is Fast Lane."""
    monkeypatch.setattr(
        "core.agent_router.config_registry",
        type("C", (), {"get_var": lambda *a, **k: False})(),
    )
    lane = classify([{"type": "mcp_fs_read", "payload": {}}])
    assert lane == "fast"


def test_router_context_agent_needed_forces_agent(monkeypatch):
    """The pre-LLM recon flag ``agent_needed`` deterministically forces AGENT.

    This is the authoritative routing signal: even a batch that would otherwise
    look like a plain message must go to the Agent Lane when the recon judged
    the user's request as agentic work.
    """
    monkeypatch.setattr(
        "core.agent_router.config_registry",
        type("C", (), {"get_var": lambda *a, **k: True})(),
    )
    lane = classify(
        [{"type": "message", "payload": {"text": "hi"}}],
        context={"agent_needed": True},
    )
    assert lane == "agent"


def test_router_mixed_non_tool_stays_fast(monkeypatch):
    """A multi-action batch with no tool call and no ``agent_needed`` flag stays
    on the Fast Lane.

    This is the regression guard for the misrouted greeting: the main model
    emitting several non-tool actions (e.g. message + diary + emotion) must no
    longer escalate on its own — the decision belongs to the recon flag.
    """
    monkeypatch.setattr(
        "core.agent_router.config_registry",
        type("C", (), {"get_var": lambda *a, **k: True})(),
    )
    lane = classify(
        [
            {"type": "message", "payload": {"text": "hi"}},
            {"type": "diary_entry", "payload": {"text": "note"}},
            {"type": "emotion_update", "payload": {"joy": 0.2}},
        ]
    )
    assert lane == "fast"


def test_mcp_server_build_registers_actions(monkeypatch):
    """build_server exposes the whitelisted actions as MCP tools."""
    import sys
    import types

    monkeypatch.setattr(
        "core.mcp_bridge.server._get_exposed_action_names",
        lambda: ["tts_speak"],
    )

    registered = {}

    class FakeMCP:
        def add_tool(self, fn, name, description):
            registered[name] = fn

        def tool(self, name=None):
            def deco(fn):
                registered[name or fn.__name__] = fn
                return fn

            return deco

    # Inject a fake mcp.server.fastmcp module so the real import works in this
    # environment (the real FastMCP pulls in pydantic_settings/dotenv which is
    # broken here). Only the symbol used by server.py matters.
    fake_mod = types.ModuleType("mcp.server.fastmcp")
    fake_mod.FastMCP = lambda name: FakeMCP()
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fake_mod)

    # Avoid DB-backed safety; just ensure build does not crash.
    monkeypatch.setattr(
        "core.tool_registry.tool_registry",
        type("R", (), {"get_tool": lambda self, n: None})(),
    )

    from core.mcp_bridge.server import build_server

    build_server("test")
    assert "synth_tts_speak" in registered


@pytest.mark.asyncio
async def test_call_engine_direct_agent_mode_uses_role_separated_messages(monkeypatch):
    """An agent-mode prompt must reach the engine via role-separated messages.

    Regression guard for the ``system_message`` collision: the agent prompt
    carries its ``agent_turn`` block under the ``system_message`` key, which the
    cortex bridge's _build_messages mistakes for a corrector payload — discarding
    the real GOAL/TOOLS/system text and emitting a near-empty prompt (which
    external web-driven engines pad with canvas/JSON boilerplate). _call_engine_direct
    must instead build explicit role-separated messages from
    ``input.payload.system``/``input.payload.text`` and call ``generate_response``,
    never ``handle_incoming_message``.
    """
    captured = {}

    class FakeEngine:
        async def generate_response(self, messages):
            captured["messages"] = messages
            return "engine reply"

        async def handle_incoming_message(self, bot, message, context_memory_or_prompt):
            captured["hidden_path"] = True
            return "SHOULD NOT BE USED"

    fake_engine = FakeEngine()

    class FakeRegistry:
        def get_engine(self, name):
            return fake_engine

        def load_engine(self, name):
            return fake_engine

    monkeypatch.setattr(
        "core.cortex_registry.get_cortex_registry", lambda: FakeRegistry()
    )
    monkeypatch.setattr(
        "core.config.get_active_cortex_engine",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should use pinned")),
    )

    prompt = {
        "input": {"payload": {"text": "GOAL: do the thing", "system": "AGENTIC SYS"}},
        "system_message": {"type": "agent_turn", "goal": "do the thing"},
        "agent_mode": True,
    }

    manager = AgentLoopManager()
    out = await manager._call_engine_direct(prompt, "pinned-engine")

    assert out == "engine reply"
    assert "hidden_path" not in captured, "handle_incoming_message must not be used"
    assert captured["messages"] == [
        {"role": "system", "content": "AGENTIC SYS"},
        {"role": "user", "content": "GOAL: do the thing"},
    ]
