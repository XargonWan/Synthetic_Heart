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
async def test_run_agentic_turn_completed(monkeypatch):
    """The loop ends when the model calls the attempt_completion sentinel."""

    calls = []

    async def fake_handle(bot, message, context_memory_or_prompt):
        calls.append(context_memory_or_prompt)
        # First iteration: ask for a tool; second: explicit completion.
        if len(calls) == 1:
            return json.dumps(
                {"actions": [{"type": "mcp_fs_read", "payload": {"path": "/x"}}]}
            )
        return json.dumps(
            {
                "actions": [
                    {
                        "type": "attempt_completion",
                        "payload": {"summary": "All done, file read."},
                    }
                ]
            }
        )

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
    assert out["stop_reason"] == "completed"
    assert out["final_text"] == "All done, file read."
    assert out["iterations"] >= 2
    # Observation history must include the tool result.
    tool_obs = [o for o in out["observations"] if o.get("role") == "tool_results"]
    assert tool_obs, "expected a tool_results observation"


@pytest.mark.asyncio
async def test_run_agentic_turn_completed_tool_key(monkeypatch):
    """Completion via the ``{"tool": ..., "payload": ...}`` shape must be honoured.

    Regression: some engines (e.g. logfare-claude) emit a single top-level object
    keyed by ``tool`` instead of ``type``/``name`` — e.g.
    ``{"tool": "attempt_completion", "payload": {"summary": "..."}}``. Previously
    ``_extract_tool_calls`` did not recognise the ``tool`` key, so the completion
    sentinel was silently dropped and the raw JSON leaked into ``final_text`` and
    got delivered verbatim to the interface (then rejected as unrecognised JSON).
    """

    calls = []

    async def fake_handle(bot, message, context_memory_or_prompt):
        calls.append(context_memory_or_prompt)
        return json.dumps(
            {
                "tool": "attempt_completion",
                "payload": {"summary": "Message delivered to Jay."},
            }
        )

    monkeypatch.setattr("core.plugin_instance.handle_incoming_message", fake_handle)

    manager = AgentLoopManager()
    out = await manager.run_agentic_turn(
        goal="tell Jay", max_iterations=5, timeout_seconds=30
    )
    assert out["stop_reason"] == "completed"
    assert out["final_text"] == "Message delivered to Jay."


@pytest.mark.asyncio
async def test_run_agentic_turn_intent_text_does_not_stop(monkeypatch):
    """Plain intent text (no tool call) must NOT end the turn prematurely.

    Regression for agent task 33: the model announced future work ("I'll check
    the codebase now...") without any tool call and the loop wrongly stopped
    with model_done. With the explicit-completion contract the loop re-injects a
    nudge and keeps iterating instead of exiting early.
    """

    calls = []

    async def fake_handle(bot, message, context_memory_or_prompt):
        calls.append(context_memory_or_prompt)
        # Iter 1: a real tool call.
        if len(calls) == 1:
            return json.dumps(
                {"actions": [{"type": "mcp_fs_read", "payload": {"path": "/x"}}]}
            )
        # Iter 2: intent-only text, no tool call, no user message.
        if len(calls) == 2:
            return "I'll check the codebase now and draft a plan."
        # Iter 3: explicit completion.
        return json.dumps(
            {
                "actions": [
                    {"type": "attempt_completion", "payload": {"summary": "Done."}}
                ]
            }
        )

    monkeypatch.setattr("core.plugin_instance.handle_incoming_message", fake_handle)

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
        goal="plan the work", max_iterations=5, timeout_seconds=30
    )
    # It must NOT have stopped on the intent-only iteration 2.
    assert out["stop_reason"] == "completed"
    assert out["iterations"] >= 3
    assert out["final_text"] == "Done."
    # The nudge must have been re-injected after the intent-only iteration.
    nudge_obs = [
        o
        for o in out["observations"]
        if o.get("role") == "system"
        and "not finished" in str(o.get("content", "")).lower()
    ]
    assert nudge_obs, "expected a completion nudge observation"


@pytest.mark.asyncio
async def test_run_agentic_turn_message_intent_does_not_stop(monkeypatch):
    """A message action carrying intent text must NOT end the turn.

    Regression for agent task 34: on a synchronous interface (Ollama) the model
    emitted a ``message_ollama_serve`` action whose text was only an intent
    statement ("Inizio subito l'esplorazione...") and the loop wrongly stopped
    with model_done. A message is delivered but is NOT a completion signal — the
    loop must nudge and keep iterating until attempt_completion.
    """

    calls = []

    async def fake_handle(bot, message, context_memory_or_prompt):
        calls.append(context_memory_or_prompt)
        # Iter 1: a real tool call.
        if len(calls) == 1:
            return json.dumps(
                {"actions": [{"type": "mcp_fs_read", "payload": {"path": "/x"}}]}
            )
        # Iter 2: a message action with intent-only text (no real tool).
        if len(calls) == 2:
            return json.dumps(
                {
                    "actions": [
                        {
                            "type": "message_ollama_serve",
                            "payload": {
                                "target": "ollama_serve/x",
                                "text": "I'll start exploring the files now.",
                            },
                        }
                    ]
                }
            )
        # Iter 3: explicit completion.
        return json.dumps(
            {
                "actions": [
                    {
                        "type": "attempt_completion",
                        "payload": {"summary": "Exploration complete."},
                    }
                ]
            }
        )

    monkeypatch.setattr("core.plugin_instance.handle_incoming_message", fake_handle)

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
        goal="explore the codebase", max_iterations=5, timeout_seconds=30
    )
    # It must NOT have stopped on the message-intent iteration 2.
    assert out["stop_reason"] == "completed"
    assert out["iterations"] >= 3
    assert out["final_text"] == "Exploration complete."
    # A nudge must have been re-injected after the intent-only message.
    nudge_obs = [
        o
        for o in out["observations"]
        if o.get("role") == "system"
        and "not finished" in str(o.get("content", "")).lower()
    ]
    assert nudge_obs, "expected a completion nudge observation after the message"


@pytest.mark.asyncio
async def test_run_agentic_turn_budget_exhausted_pauses(monkeypatch):
    """Exhausting the iteration budget without attempt_completion pauses the turn.

    Regression for agent task 36: the model kept executing tool calls until the
    iteration budget ran out but never called ``attempt_completion``. Under the
    explicit-completion contract this is NOT done — the turn must end with
    ``paused_max_iterations`` (mapped to a ``pending`` task) so the user can
    grant more iterations via "Continue", instead of being falsely reported as
    completed.
    """

    async def fake_handle(bot, message, context_memory_or_prompt):
        # Every iteration returns a real tool call, never attempt_completion.
        return json.dumps(
            {"actions": [{"type": "mcp_fs_read", "payload": {"path": "/x"}}]}
        )

    monkeypatch.setattr("core.plugin_instance.handle_incoming_message", fake_handle)

    async def fake_execute(name, arguments, context=None, original_message=None):
        return {
            "ok": True,
            "tool": name,
            "source": "mcp:fs",
            "result": "file contents",
            "error": None,
        }

    monkeypatch.setattr(agent_tool_executor, "execute", fake_execute)

    # Ensure the explicit-completion contract is active.
    from core.config_manager import config_registry

    monkeypatch.setattr(
        config_registry,
        "get_var",
        lambda key, default=None: (
            True if key == "AGENT_REQUIRE_EXPLICIT_COMPLETION" else default
        ),
    )

    manager = AgentLoopManager()
    out = await manager.run_agentic_turn(
        goal="keep working forever", max_iterations=3, timeout_seconds=30
    )
    assert out["stop_reason"] == "paused_max_iterations"
    # Every iteration executed a tool action.
    tool_obs = [o for o in out["observations"] if o.get("role") == "tool_results"]
    assert len(tool_obs) == 3


@pytest.mark.asyncio
async def test_run_agentic_turn_resume_reinjects_prior_observations(monkeypatch):
    """Resuming with prior_observations continues the same turn's history.

    When the user presses "Continue", the WebUI rebuilds the prior observations
    from ``iterations_meta`` and re-runs ``run_agentic_turn`` on the SAME task
    row (``task_id`` supplied). The prior observations must be seeded into the
    new run so the model has the earlier context.
    """

    async def fake_handle(bot, message, context_memory_or_prompt):
        # Complete immediately on resume.
        return json.dumps(
            {
                "actions": [
                    {"type": "attempt_completion", "payload": {"summary": "Done now."}}
                ]
            }
        )

    monkeypatch.setattr("core.plugin_instance.handle_incoming_message", fake_handle)

    async def fake_execute(name, arguments, context=None, original_message=None):
        return {"ok": True, "tool": name, "result": "ok", "error": None}

    monkeypatch.setattr(agent_tool_executor, "execute", fake_execute)

    prior = [
        {"iteration": 1, "role": "tool_results", "content": [{"tool": "mcp_fs_read"}]},
        {"iteration": 2, "role": "assistant", "content": "made some progress"},
    ]

    manager = AgentLoopManager()
    out = await manager.run_agentic_turn(
        goal="resume the work",
        max_iterations=5,
        timeout_seconds=30,
        task_id=99,
        prior_observations=prior,
    )
    assert out["stop_reason"] == "completed"
    # The prior observations must be present at the front of the history.
    assert out["observations"][0]["content"] == [{"tool": "mcp_fs_read"}]
    assert out["observations"][1]["content"] == "made some progress"


class _FakeCursor:
    def __init__(self, row):
        self._row = row
        self.executed: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))

    async def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _FakeCursor(self._row)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_find_resumable_task_for_interface_hit(monkeypatch):
    """A pending task on the interface is returned with rebuilt observations.

    Replying in chat (any interface) that already owns a paused agentic task
    must resume THAT task, not spawn a new one. The lookup is purely by
    interface_path — no keyword/language detection.
    """
    iterations = json.dumps(
        [
            {"iteration": 1, "role": "tool_results", "result": [{"tool": "x"}]},
            {"iteration": 2, "role": "assistant", "result": "progress"},
        ]
    )
    row = (42, "logfare-claude", json.dumps({"goal": "finish the report"}), iterations)

    manager = AgentLoopManager()

    async def fake_conn_ctx():
        return _FakeConn(row)

    monkeypatch.setattr(manager, "_get_conn_ctx", fake_conn_ctx)

    out = await manager.find_resumable_task_for_interface("telegram_bot/123")
    assert out is not None
    assert out["task_id"] == 42
    assert out["goal"] == "finish the report"
    assert out["engine"] == "logfare-claude"
    assert out["prior_observations"][0]["content"] == [{"tool": "x"}]
    assert out["prior_observations"][1]["content"] == "progress"


@pytest.mark.asyncio
async def test_find_resumable_task_for_interface_miss(monkeypatch):
    """No pending task -> returns None (a fresh task will be opened)."""
    manager = AgentLoopManager()

    async def fake_conn_ctx():
        return _FakeConn(None)

    monkeypatch.setattr(manager, "_get_conn_ctx", fake_conn_ctx)

    out = await manager.find_resumable_task_for_interface("telegram_bot/123")
    assert out is None


@pytest.mark.asyncio
async def test_find_resumable_task_for_interface_no_path(monkeypatch):
    """A missing/empty interface_path never resumes anything."""
    manager = AgentLoopManager()
    assert await manager.find_resumable_task_for_interface(None) is None
    assert await manager.find_resumable_task_for_interface("") is None


@pytest.mark.asyncio
async def test_find_task_by_id_pending_hit(monkeypatch):
    """A pending task loaded by id (cross-interface) rebuilds observations.

    The model may reference a task created on a different interface (e.g. a
    Grillo task referenced from Telegram); find_task_by_id ignores the
    interface and only validates status == 'pending'.
    """
    iterations = json.dumps(
        [
            {"iteration": 1, "role": "tool_results", "result": [{"tool": "x"}]},
            {"iteration": 2, "role": "assistant", "result": "progress"},
        ]
    )
    row = (
        37,
        "logfare-claude",
        json.dumps({"goal": "finish the report"}),
        iterations,
        "pending",
    )

    manager = AgentLoopManager()

    async def fake_conn_ctx():
        return _FakeConn(row)

    monkeypatch.setattr(manager, "_get_conn_ctx", fake_conn_ctx)

    out = await manager.find_task_by_id(37)
    assert out is not None
    assert out["task_id"] == 37
    assert out["goal"] == "finish the report"
    assert out["engine"] == "logfare-claude"
    assert out["prior_observations"][0]["content"] == [{"tool": "x"}]
    assert out["prior_observations"][1]["content"] == "progress"


@pytest.mark.asyncio
async def test_find_task_by_id_not_pending(monkeypatch):
    """A task that is not 'pending' is not resumable -> None."""
    row = (
        37,
        "logfare-claude",
        json.dumps({"goal": "x"}),
        json.dumps([]),
        "completed",
    )
    manager = AgentLoopManager()

    async def fake_conn_ctx():
        return _FakeConn(row)

    monkeypatch.setattr(manager, "_get_conn_ctx", fake_conn_ctx)
    assert await manager.find_task_by_id(37) is None


@pytest.mark.asyncio
async def test_find_task_by_id_unknown(monkeypatch):
    """An unknown id (no row) returns None."""
    manager = AgentLoopManager()

    async def fake_conn_ctx():
        return _FakeConn(None)

    monkeypatch.setattr(manager, "_get_conn_ctx", fake_conn_ctx)
    assert await manager.find_task_by_id(999) is None


@pytest.mark.asyncio
async def test_find_task_by_id_invalid(monkeypatch):
    """A non-numeric id is rejected without touching the DB."""
    manager = AgentLoopManager()
    assert await manager.find_task_by_id("not-a-number") is None  # type: ignore[arg-type]


def test_extract_resume_task_id_from_action():
    """The router extracts the numeric id from a resume_agent_task action."""
    from core.agent_router import _extract_resume_task_id

    actions = [
        {"type": "message", "payload": {"text": "sure"}},
        {"type": "resume_agent_task", "payload": {"task_id": 37}},
    ]
    assert _extract_resume_task_id(actions) == 37


def test_extract_resume_task_id_string_id():
    """A stringified numeric id is coerced; the model decides the id, not text parsing."""
    from core.agent_router import _extract_resume_task_id

    actions = [{"type": "resume_agent_task", "payload": {"task_id": "40"}}]
    assert _extract_resume_task_id(actions) == 40


def test_extract_resume_task_id_absent():
    """No resume_agent_task action -> None."""
    from core.agent_router import _extract_resume_task_id

    assert _extract_resume_task_id([{"type": "message", "payload": {}}]) is None
    assert _extract_resume_task_id([]) is None


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
    """build_server exposes registered actions as MCP tools (named synth_*)."""
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
