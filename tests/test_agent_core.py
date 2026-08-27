import asyncio
import pytest
from core.agent_core import AgentLoopManager, _call_with_hard_timeout


class FakeEngine:
    def __init__(self):
        self.attached = False
        self.attach_calls = []

    def attach_agent(self, plugin):
        self.attached = True
        self.attach_calls.append(plugin)

    def detach_agent(self, plugin):
        self.attached = False


@pytest.mark.asyncio
@pytest.mark.skip(
    reason="legacy AgentCore attach/proposal API removed in 5310ce4a; "
    "AgentLoopManager has no attach_agent"
)
async def test_attach_to_engine_calls_attach(monkeypatch):
    fake_engine = FakeEngine()

    class FakeRegistry:
        def get_engine(self, name):
            return fake_engine

        def load_engine(self, name):
            return fake_engine

    monkeypatch.setattr(
        "core.config.get_active_cortex_engine",
        lambda: asyncio.sleep(0, result="gemini_api"),
    )
    monkeypatch.setattr(
        "core.cortex_registry.get_cortex_registry", lambda: FakeRegistry()
    )

    agent = AgentLoopManager()
    agent._enabled = True

    await agent.attach_to_active_engine()

    assert fake_engine.attached is True
    assert fake_engine.attach_calls and fake_engine.attach_calls[0] is agent


@pytest.mark.asyncio
@pytest.mark.skip(reason="legacy AgentCore proposal/approval API removed in 5310ce4a")
async def test_propose_and_approve_flow(monkeypatch):
    called = {}

    async def fake_create(command, proposer=None, metadata=None):
        called["created"] = command
        return 555

    async def fake_update(aid, **kwargs):
        called.setdefault("updates", []).append((aid, kwargs))

    async def fake_insert(aid, cmd, **kwargs):
        called.setdefault("execs", []).append((aid, cmd, kwargs))
        return 999

    def fake_notify(msg):
        called["notify"] = msg

    async def fake_run(cmd, timeout=30.0):
        called["ran"] = cmd
        return "OUTPUT: " + cmd

    agent = AgentLoopManager()
    agent._notify_fn = fake_notify
    agent._create_activity_log = fake_create
    agent._update_activity_log = fake_update
    agent._insert_action_exec = fake_insert
    agent._run_command = fake_run
    agent._enabled = True

    # Propose
    res = await agent.execute_action(
        {"type": "propose_action", "payload": {"command": "touch /tmp/test"}},
        {},
        None,
        None,
    )
    assert res.get("status") == "proposed"
    assert res.get("proposal_id") == 555
    assert "notify" in called

    # Approve (provide command directly to avoid real DB lookup in unit test)
    res2 = await agent.execute_action(
        {
            "type": "approve_action",
            "payload": {"proposal_id": 555, "command": "echo approved"},
        },
        {},
        None,
        {"sender_id": 42},
    )
    assert res2.get("status") == "executed"
    assert res2.get("proposal_id") == 555
    assert called.get("ran") is not None
    assert "execs" in called


@pytest.mark.asyncio
async def test_diary_only_executed_at_start_and_end(monkeypatch):
    """The agent loop must execute diary tools only on the first (start) and
    last (end) iteration, suppressing them on the intermediate ones so a single
    task produces at most one opening and one closing diary entry."""
    import json

    from core.agent_core import _agent_loop_manager

    # Engine emits, on every iteration, a working tool call (keeps the loop
    # alive) plus a diary tool call (which should be suppressed mid-task).
    async def fake_call_engine_direct(prompt, engine_name, cortex_scope="agent"):
        return json.dumps(
            {
                "actions": [
                    {"type": "agent_read_file", "payload": {"path": "/tmp/x"}},
                    {
                        "type": "create_personal_diary_entry",
                        "payload": {"content": "note"},
                    },
                ]
            }
        )

    monkeypatch.setattr(
        _agent_loop_manager, "_call_engine_direct", fake_call_engine_direct
    )

    async def fake_persist(**kwargs):
        return 1

    monkeypatch.setattr(_agent_loop_manager, "_persist_agentic_turn", fake_persist)

    executed: list[str] = []

    async def fake_execute(name, args, context=None, original_message=None):
        executed.append(name)
        return {"ok": True, "result": "done"}

    monkeypatch.setattr(
        "core.agent_tool_executor.agent_tool_executor.execute", fake_execute
    )

    max_iterations = 3
    result = await _agent_loop_manager.run_agentic_turn(
        goal="multi-step task",
        engine="fake-engine",
        max_iterations=max_iterations,
        timeout_seconds=30.0,
    )

    diary_execs = [n for n in executed if n == "create_personal_diary_entry"]
    # Diary executed exactly twice: iteration 1 (start) and iteration 3 (end).
    assert len(diary_execs) == 2, (
        f"expected diary to run only at start/end, got {len(diary_execs)}: {executed}"
    )
    # The identical working call is executed once, then deduped on later
    # iterations (cross-iteration identical-call dedup — re-running the same
    # call is a loop artifact, not real work).
    work_execs = [n for n in executed if n == "agent_read_file"]
    assert len(work_execs) == 1, executed
    assert result is not None


@pytest.mark.asyncio
async def test_call_with_hard_timeout_returns_on_success() -> None:
    """A prompt completion within the deadline returns its value unchanged."""

    async def _fast() -> str:
        return "done"

    assert await _call_with_hard_timeout(_fast(), timeout=5.0) == "done"


@pytest.mark.asyncio
async def test_call_with_hard_timeout_raises_without_awaiting_cancel() -> None:
    """A hung engine call must NOT freeze the caller past the deadline.

    Regression test for the live-observed Drone hang: goal-expansion Drones went
    silent mid-loop and their ``agent_tasks`` rows stayed ``pending`` forever
    because the engine call was stuck in non-cancellable code and
    ``asyncio.wait_for`` awaited the stuck task's cancellation. This helper must
    raise ``TimeoutError`` promptly even when the inner coroutine never
    terminates.
    """
    started = asyncio.Event()
    release = asyncio.Event()

    async def _stuck() -> str:
        started.set()
        await release.wait()  # never released — the call is "hung"
        return "late"

    task = asyncio.create_task(_call_with_hard_timeout(_stuck(), timeout=0.05))
    await started.wait()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(task, timeout=5.0)
    # The helper returned on time; release the orphan so the test loop drains.
    release.set()
    await asyncio.sleep(0)


def test_extract_tool_calls_from_text_parses_protocol_blocks():
    """Recovers name + args pairs from Anthropic-style text protocol output,
    including nested JSON objects."""
    text = (
        "I'll do it now.\n\n"
        "Tool Call: agent_read_file\n"
        '{"path": "core/main.py", "meta": {"nested": true}}\n\n'
        "Tool Call: message_telegram_bot\n"
        '{"text": "hi", "interface_path": "telegram_bot/123"}'
    )
    calls = AgentLoopManager._extract_tool_calls_from_text(text)
    assert len(calls) == 2
    assert calls[0]["name"] == "agent_read_file"
    assert calls[0]["arguments"] == {"path": "core/main.py", "meta": {"nested": True}}
    assert calls[1]["name"] == "message_telegram_bot"
    assert calls[1]["arguments"]["interface_path"] == "telegram_bot/123"


def test_extract_tool_calls_from_text_ignores_prose():
    assert (
        AgentLoopManager._extract_tool_calls_from_text("I'll check the codebase now.")
        == []
    )
    assert AgentLoopManager._extract_tool_calls_from_text("") == []


def test_extract_tool_calls_from_text_skips_broken_json():
    """A tool call whose JSON is truncated/unbalanced is skipped, not misparsed."""
    text = 'Tool Call: agent_read_file\n{"path": "core/main.py"'
    assert AgentLoopManager._extract_tool_calls_from_text(text) == []


def test_extract_tool_calls_from_text_parses_bold_fenced_blocks():
    """Handles the markdown-bold header + ```json fence format engines
    actually emit (Langfuse c1e66673)."""
    text = (
        "Let me read the file first.\n\n"
        "**Tool Call: agent_read_file**\n"
        "```json\n"
        '{\n  "path": "D:\\\\app\\\\a.pdf",\n  "max_chars": "5000"\n}\n'
        "```\n\n"
        "**Tool Call: message_telegram_bot**\n"
        "```\n"
        '{"text": "done", "interface_path": "telegram_bot/123"}\n'
        "```"
    )
    calls = AgentLoopManager._extract_tool_calls_from_text(text)
    assert len(calls) == 2
    assert calls[0]["name"] == "agent_read_file"
    assert calls[0]["arguments"] == {"path": "D:\\app\\a.pdf", "max_chars": "5000"}
    assert calls[1]["name"] == "message_telegram_bot"
    assert calls[1]["arguments"]["interface_path"] == "telegram_bot/123"


def test_extract_tool_calls_from_text_still_parses_plain_blocks():
    """The original plain ``Tool Call: name`` + JSON form keeps working."""
    text = (
        'Tool Call: agent_read_file\n{"path": "core/main.py", "meta": {"nested": true}}'
    )
    calls = AgentLoopManager._extract_tool_calls_from_text(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "agent_read_file"
    assert calls[0]["arguments"]["meta"] == {"nested": True}


def test_extract_tool_calls_from_text_parses_xml_function_blocks():
    """Parses the DeepSeek-native <function> XML format (Langfuse ff1bbae0),
    both the compact arg-tag and the canonical function_name/parameters forms."""
    from core.agent_core import AgentLoopManager

    compact = (
        "<function>\n"
        "agent_read_file\n"
        "<path>D:\\app\\attachments\\1786655465343_Untitled_document.pdf</path>\n"
        "</function>"
    )
    calls = AgentLoopManager._extract_tool_calls_from_text(compact)
    assert len(calls) == 1
    assert calls[0]["name"] == "agent_read_file"
    assert calls[0]["arguments"]["path"] == (
        "D:\\app\\attachments\\1786655465343_Untitled_document.pdf"
    )

    canonical = (
        "<function>\n"
        "<function_name>\nmessage_telegram_bot\n</function_name>\n"
        "<parameters>\n"
        '{"text": "done", "interface_path": "telegram_bot/123"}\n'
        "</parameters>\n"
        "</function>"
    )
    calls = AgentLoopManager._extract_tool_calls_from_text(canonical)
    assert len(calls) == 1
    assert calls[0]["name"] == "message_telegram_bot"
    assert calls[0]["arguments"] == {
        "text": "done",
        "interface_path": "telegram_bot/123",
    }


def test_extract_tool_calls_from_text_parses_bare_python_calls():
    """Parses bare name(key="value") calls (Langfuse fdf08aef)."""
    text = (
        "Let me read it.\n\n"
        'agent_read_file(path="D:\\app\\attachments\\a.pdf", max_chars="5000")'
    )
    calls = AgentLoopManager._extract_tool_calls_from_text(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "agent_read_file"
    assert calls[0]["arguments"]["path"] == "D:\\app\\attachments\\a.pdf"
    assert calls[0]["arguments"]["max_chars"] == 5000


def test_extract_tool_calls_from_text_parses_backtick_json_call():
    """Backtick-wrapped name({...json...}) completion calls are recovered."""
    calls = AgentLoopManager._extract_tool_calls_from_text(
        '`attempt_completion({"summary": "done"})`'
    )
    assert len(calls) == 1
    assert calls[0]["name"] == "attempt_completion"
    assert calls[0]["arguments"] == {"summary": "done"}


def test_extract_tool_calls_from_text_parses_claude_invoke_blocks():
    """Claude-style <tool_calls><invoke> XML is recovered (Langfuse 5ea2ff8c)."""
    text = (
        "<tool_calls>\n"
        '<invoke name="agent_run_shell">\n'
        '<parameter name="command">echo hi</parameter>\n'
        '<parameter name="timeout">15</parameter>\n'
        "</invoke>\n"
        "</tool_calls>"
    )
    calls = AgentLoopManager._extract_tool_calls_from_text(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "agent_run_shell"
    assert calls[0]["arguments"] == {"command": "echo hi", "timeout": 15}


def test_extract_tool_calls_from_text_dedupes_duplicate_calls():
    """The same call emitted in multiple formats executes only once."""
    text = (
        'Tool Call: agent_read_file\n{"path": "a.pdf"}\n\n'
        "<function>\nagent_read_file\n<path>a.pdf</path>\n</function>\n\n"
        'agent_read_file(path="a.pdf")'
    )
    calls = AgentLoopManager._extract_tool_calls_from_text(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "agent_read_file"
    assert calls[0]["arguments"]["path"] == "a.pdf"


# ---------------------------------------------------------------------------
# Engine-failure classification (offline endpoint / bad key / empty / timeout)
# ---------------------------------------------------------------------------

from core.agent_core import (
    _describe_engine_failure,
    _engine_failure_from_diagnostics,
    classify_engine_failure,
)


@pytest.mark.parametrize(
    "error_text,kwargs,expected_kind",
    [
        ("APIConnectionError: Connection error.", {}, "connection"),
        ("Connection refused when connecting to the endpoint", {}, "connection"),
        ("getaddrinfo failed: Name or service not known", {}, "connection"),
        ("Error code: 401 - Unauthorized", {}, "auth"),
        ("Error code: 403 - Forbidden", {}, "auth"),
        ("invalid API key provided", {}, "auth"),
        (
            "Error code: 400 - This model supports at most 20 tool definitions",
            {},
            "bad_request",
        ),
        ("Error code: 429 - rate limit exceeded", {}, "rate_limited"),
        ("Error code: 503 - Service Unavailable", {}, "server_error"),
        ("ReadTimeout: timed out", {}, "timeout"),
        (
            "TimeoutError: LLM request timed out after 30s and 3 retry attempts",
            {},
            "timeout",
        ),
        (None, {"timed_out": True}, "timeout"),
        (None, {"empty_body": True}, "empty"),
        # Our own code crashing on a non-standard response body (live
        # harmonyai agent-cortex failure, 2026-08-26).
        (
            "TypeError: 'NoneType' object is not subscriptable",
            {},
            "internal",
        ),
        (
            "AttributeError: 'NoneType' object has no attribute 'content'",
            {},
            "internal",
        ),
        ("something utterly strange happened", {}, "unknown"),
    ],
)
def test_classify_engine_failure_kinds(error_text, kwargs, expected_kind):
    kind, hint = classify_engine_failure(error_text, **kwargs)
    assert kind == expected_kind
    assert hint  # every kind carries an operator-facing hint


def test_classify_engine_failure_error_beats_timeout_flag():
    """A recorded connection error classifies as connection even when the loop
    only saw a hard timeout — the offline-endpoint production case."""
    kind, _ = classify_engine_failure(
        "APIConnectionError: Connection error.", timed_out=True
    )
    assert kind == "connection"


def test_classify_engine_failure_provider_error_wins_over_internal_marker():
    """A provider error whose body merely mentions a type name stays a
    provider kind — internal matching is prefix-based and runs last."""
    kind, _ = classify_engine_failure(
        "Error code: 503 - upstream raised TypeError while proxying"
    )
    assert kind == "server_error"


def test_describe_engine_failure_without_diagnostics_is_honest():
    """No diagnostics → describe the symptom, never guess 'bad API key'."""
    desc = _describe_engine_failure(None, "some-engine")
    assert "empty response" in desc
    assert "API key" not in desc


class _FakeBridgeEngine:
    """Mimics a cortex bridge's diagnostic attributes."""

    def __init__(self, last_error=None, empty_body=False):
        self._last_attempt_error = last_error
        self._last_response_metadata = {"empty_response": empty_body}


class _FakeCortexRegistry:
    def __init__(self, engine_obj):
        self._engine_obj = engine_obj

    def get_engine(self, name):
        return self._engine_obj


def _install_agent_harness(monkeypatch, engine_reply, bridge_engine):
    """Patch the agent loop for turn-level tests.

    ``engine_reply`` may be a string (returned for every direct engine call) or
    an async callable ``(prompt, engine_name) -> str``.
    """
    from core.agent_core import _agent_loop_manager

    if isinstance(engine_reply, str):
        text = engine_reply

        async def fake_call_engine_direct(prompt, engine_name, cortex_scope="agent"):
            return text

    else:
        fake_call_engine_direct = engine_reply

    monkeypatch.setattr(
        _agent_loop_manager, "_call_engine_direct", fake_call_engine_direct
    )

    async def fake_persist(**kwargs):
        return 1

    monkeypatch.setattr(_agent_loop_manager, "_persist_agentic_turn", fake_persist)

    monkeypatch.setattr(
        "core.cortex_registry.get_cortex_registry",
        lambda: _FakeCortexRegistry(bridge_engine),
    )

    async def fake_base_engine(*args, **kwargs):
        return "base-engine"

    monkeypatch.setattr("core.config.get_active_cortex_engine", fake_base_engine)

    notified: list[str] = []
    monkeypatch.setattr("core.notifier.notifier", lambda msg: notified.append(msg))

    return _agent_loop_manager, notified


@pytest.mark.asyncio
async def test_offline_endpoint_fails_fast_with_connection_cause(monkeypatch):
    """An offline agent endpoint (bridge records a connection error, the loop
    sees an empty response) must end the turn promptly as engine_error with the
    TRUE cause — not grind iterations and not blame a bad API key."""
    bridge = _FakeBridgeEngine(last_error="APIConnectionError: Connection error.")
    manager, notified = _install_agent_harness(monkeypatch, "", bridge)

    call_log: list[str] = []

    async def fake_call(prompt, engine_name, cortex_scope="agent"):
        call_log.append(engine_name)
        return ""

    monkeypatch.setattr(manager, "_call_engine_direct", fake_call)

    result = await manager.run_agentic_turn(
        goal="do a thing",
        engine="fake-engine",
        max_iterations=5,
        timeout_seconds=30.0,
    )

    assert result["stop_reason"] == "engine_error"
    error_contents = [
        str(o.get("content"))
        for o in result["observations"]
        if o.get("role") == "error"
    ]
    assert any("connection" in c for c in error_contents), error_contents
    # Structural failure: no pointless same-engine retry — only the primary
    # call plus the Base Cortex safety-net call.
    assert call_log == ["fake-engine", "base-engine"]
    # The operator message carries the truthful cause, not a guessed bad key.
    engine_msgs = [m for m in notified if "fake-engine" in m]
    assert engine_msgs and "unreachable" in engine_msgs[0]
    assert "Please fix AGENTCORTEX" not in engine_msgs[0].replace("_", "")


@pytest.mark.asyncio
async def test_offline_endpoint_timeout_surfaces_connection_error(monkeypatch):
    """The exact production shape: each connection attempt hangs until the
    per-call budget dies, so the loop sees asyncio.TimeoutError while the bridge
    already recorded 'Connection error.' — the report must say connection."""
    import asyncio as _asyncio

    bridge = _FakeBridgeEngine(last_error="APIConnectionError: Connection error.")

    async def hanging_primary(prompt, engine_name, cortex_scope="agent"):
        if engine_name == "fake-engine":
            await _asyncio.sleep(30)  # wedged offline endpoint
        return ""

    manager, notified = _install_agent_harness(monkeypatch, hanging_primary, bridge)

    result = await manager.run_agentic_turn(
        goal="do a thing",
        engine="fake-engine",
        max_iterations=5,
        timeout_seconds=1.0,
    )

    assert result["stop_reason"] == "engine_error"
    error_contents = [
        str(o.get("content"))
        for o in result["observations"]
        if o.get("role") == "error"
    ]
    assert any("timed out" in c and "Connection error" in c for c in error_contents), (
        error_contents
    )


@pytest.mark.asyncio
async def test_empty_response_is_not_structural_and_reports_honestly(monkeypatch):
    """A 200-with-empty-body failure is NOT structural: the turn keeps the
    existing retry/empty_response behaviour, and the warning describes an empty
    body instead of asserting a bad API key."""
    bridge = _FakeBridgeEngine(last_error=None, empty_body=True)
    manager, notified = _install_agent_harness(monkeypatch, "", bridge)

    result = await manager.run_agentic_turn(
        goal="do a thing",
        engine="fake-engine",
        max_iterations=2,
        timeout_seconds=30.0,
    )

    assert result["stop_reason"] == "empty_response"
    assert result["stop_reason"] != "engine_error"
    error_contents = [
        str(o.get("content"))
        for o in result["observations"]
        if o.get("role") == "error"
    ]
    assert any(c.startswith("empty_model_response") for c in error_contents)
    assert any("empty body" in c for c in error_contents), error_contents
    engine_msgs = [m for m in notified if "fake-engine" in m]
    assert engine_msgs and "empty" in engine_msgs[0].lower()
    # One notification per turn, not one per iteration.
    assert len(engine_msgs) == 1


@pytest.mark.asyncio
async def test_malformed_response_recorded_and_never_shipped(monkeypatch):
    """A response full of unparseable JSON is recorded as malformed_response,
    nudged once, and its raw text never becomes the final answer."""
    bridge = _FakeBridgeEngine()
    # Truncated JSON with no "actions" key: json_repair recovers an object but
    # it carries no actions, so nothing executable can be extracted from it.
    broken = '{"summary": "I will check the codebase now and th'
    manager, _notified = _install_agent_harness(monkeypatch, broken, bridge)

    result = await manager.run_agentic_turn(
        goal="do a thing",
        engine="fake-engine",
        max_iterations=2,
        timeout_seconds=30.0,
    )

    assert result["stop_reason"] == "malformed_response"
    assert result["final_text"] == ""  # garbage is never shipped as the answer
    assert result.get("malformed_responses") == 2
    error_contents = [
        str(o.get("content"))
        for o in result["observations"]
        if o.get("role") == "error"
    ]
    assert any(c.startswith("malformed_response") for c in error_contents)
    # A repair nudge was injected for the non-final iteration.
    system_contents = [
        str(o.get("content"))
        for o in result["observations"]
        if o.get("role") == "system"
    ]
    assert any("malformed" in c for c in system_contents)


def test_engine_failure_from_diagnostics_paths():
    import core.cortex_registry as reg_mod

    class _ErroredObj:
        _last_attempt_error = "Error code: 401 - Unauthorized"
        _last_response_metadata = {"empty_response": True}

    class _EmptyObj:
        _last_attempt_error = None
        _last_response_metadata = {"empty_response": True}

    class _CleanObj:
        _last_attempt_error = None
        _last_response_metadata = {}

    original = reg_mod.get_cortex_registry
    try:
        # Error text wins over the empty-body flag.
        reg_mod.get_cortex_registry = lambda: _FakeCortexRegistry(_ErroredObj())
        failure = _engine_failure_from_diagnostics("any-engine")
        assert failure is not None
        assert failure["kind"] == "auth"
        assert failure["detail"].startswith("Error code: 401")

        # Empty body with no error classifies as "empty".
        reg_mod.get_cortex_registry = lambda: _FakeCortexRegistry(_EmptyObj())
        failure = _engine_failure_from_diagnostics("any-engine")
        assert failure is not None
        assert failure["kind"] == "empty"

        # A clean engine carries no failure.
        reg_mod.get_cortex_registry = lambda: _FakeCortexRegistry(_CleanObj())
        assert _engine_failure_from_diagnostics("any-engine") is None
    finally:
        reg_mod.get_cortex_registry = original


# ---------------------------------------------------------------------------
# Repeatable (poll) actions & timeout delivery
# ---------------------------------------------------------------------------

import json  # noqa: E402  (section-local; earlier tests import inside functions)


@pytest.mark.asyncio
async def test_repeatable_action_bypasses_identical_call_dedup(monkeypatch):
    """Poll actions (schema ``repeatable: true``) must re-execute on every
    identical invocation — the dedup serves cached stale state otherwise, so
    a model polling a slow external operation can never observe progress
    (live: a stuck-queued transfer polled ~15x against the first snapshot)."""
    import core.agent_core as agent_core_mod
    from core.agent_core import _agent_loop_manager

    call_json = json.dumps(
        {"actions": [{"type": "poll_status", "payload": {"id": "t-1"}}]}
    )

    async def fake_call_engine_direct(prompt, engine_name, cortex_scope="agent"):
        return call_json

    monkeypatch.setattr(
        _agent_loop_manager, "_call_engine_direct", fake_call_engine_direct
    )

    async def fake_persist(**kwargs):
        return 1

    monkeypatch.setattr(_agent_loop_manager, "_persist_agentic_turn", fake_persist)

    executed: list[str] = []

    async def fake_execute(name, args, context=None, original_message=None):
        executed.append(name)
        return {"ok": True, "result": f"fresh-state-{len(executed)}"}

    monkeypatch.setattr(
        "core.agent_tool_executor.agent_tool_executor.execute", fake_execute
    )

    # Opt the fake tool into re-execution via the structural schema flag.
    monkeypatch.setattr(
        agent_core_mod,
        "_repeatable_action_names",
        lambda: frozenset({"poll_status"}),
    )

    result = await _agent_loop_manager.run_agentic_turn(
        goal="watch the thing",
        engine="fake-engine",
        max_iterations=3,
        timeout_seconds=30.0,
    )
    # All three iterations executed a FRESH poll, none was served from cache.
    assert executed == ["poll_status"] * 3, executed


@pytest.mark.asyncio
async def test_non_repeatable_action_still_deduped(monkeypatch):
    """Without the flag the historical dedup behaviour is unchanged."""
    import core.agent_core as agent_core_mod
    from core.agent_core import _agent_loop_manager

    call_json = json.dumps(
        {"actions": [{"type": "agent_read_file", "payload": {"path": "/tmp/x"}}]}
    )

    async def fake_call_engine_direct(prompt, engine_name, cortex_scope="agent"):
        return call_json

    monkeypatch.setattr(
        _agent_loop_manager, "_call_engine_direct", fake_call_engine_direct
    )

    async def fake_persist(**kwargs):
        return 1

    monkeypatch.setattr(_agent_loop_manager, "_persist_agentic_turn", fake_persist)

    executed: list[str] = []

    async def fake_execute(name, args, context=None, original_message=None):
        executed.append(name)
        return {"ok": True, "result": "done"}

    monkeypatch.setattr(
        "core.agent_tool_executor.agent_tool_executor.execute", fake_execute
    )
    monkeypatch.setattr(agent_core_mod, "_repeatable_action_names", lambda: frozenset())

    await _agent_loop_manager.run_agentic_turn(
        goal="read it",
        engine="fake-engine",
        max_iterations=3,
        timeout_seconds=30.0,
    )
    assert executed == ["agent_read_file"], executed


@pytest.mark.asyncio
async def test_timeout_with_actions_composes_pause_message(monkeypatch):
    """A turn that runs out of TIME mid-task (with real work done) must not
    end in silence: the loop composes the model-authored pause message and
    parks the task as resumable 'paused_timeout' so it is delivered (live:
    the agent diagnosed a stuck download in every iteration's reasoning yet
    Telegram never received a word)."""
    import asyncio as _asyncio

    from core.agent_core import _agent_loop_manager

    call_json = json.dumps(
        {"actions": [{"type": "agent_read_file", "payload": {"path": "/tmp/x"}}]}
    )

    async def slow_engine(prompt, engine_name, cortex_scope="agent"):
        await _asyncio.sleep(0.6)  # burn wall-clock budget like a real engine
        return call_json

    monkeypatch.setattr(_agent_loop_manager, "_call_engine_direct", slow_engine)

    async def fake_persist(**kwargs):
        return 1

    monkeypatch.setattr(_agent_loop_manager, "_persist_agentic_turn", fake_persist)

    async def fake_execute(name, args, context=None, original_message=None):
        return {"ok": True, "result": "done"}

    monkeypatch.setattr(
        "core.agent_tool_executor.agent_tool_executor.execute", fake_execute
    )

    async def fake_compose_pause_message(**kwargs):
        return "I'm still on it — the download is queued, want me to keep waiting?"

    monkeypatch.setattr(
        _agent_loop_manager,
        "_compose_pause_message",
        fake_compose_pause_message,
    )

    result = await _agent_loop_manager.run_agentic_turn(
        goal="pull the song",
        engine="fake-engine",
        max_iterations=10,
        timeout_seconds=1.0,
    )
    assert result["stop_reason"] == "paused_timeout"
    assert result["final_text"] == (
        "I'm still on it — the download is queued, want me to keep waiting?"
    )


@pytest.mark.asyncio
async def test_timeout_with_zero_actions_stays_plain_timeout(monkeypatch):
    """The garbage-output guard stays intact: a timeout with NO executed
    actions must not fabricate a pause message (nothing to report)."""
    import asyncio as _asyncio

    from core.agent_core import _agent_loop_manager

    async def empty_slow_engine(prompt, engine_name, cortex_scope="agent"):
        await _asyncio.sleep(0.05)
        return ""

    monkeypatch.setattr(_agent_loop_manager, "_call_engine_direct", empty_slow_engine)

    async def fake_persist(**kwargs):
        return 1

    monkeypatch.setattr(_agent_loop_manager, "_persist_agentic_turn", fake_persist)

    async def fake_compose_pause_message(**kwargs):
        raise AssertionError("composer must not run for an actionless timeout")

    monkeypatch.setattr(
        _agent_loop_manager,
        "_compose_pause_message",
        fake_compose_pause_message,
    )

    result = await _agent_loop_manager.run_agentic_turn(
        goal="do nothing",
        engine="fake-engine",
        max_iterations=1000,
        timeout_seconds=1.0,
    )
    assert result["stop_reason"] == "timeout"
    assert result["final_text"] == ""


@pytest.mark.asyncio
async def test_interim_message_cap_suppresses_reworded_duplicates(monkeypatch):
    """One interim status message per turn: re-WORDed updates (which defeat
    the exact-text dedup) are suppressed past AGENT_MAX_INTERIM_MESSAGES
    (live: a single turn sent three near-identical Telegram updates because
    every iteration re-narrated the same status slightly differently)."""
    from core.agent_core import _agent_loop_manager

    iteration = {"n": 0}

    async def fake_call_engine_direct(prompt, engine_name, cortex_scope="agent"):
        iteration["n"] += 1
        # Message + poll each iteration, message text re-worded every time —
        # exactly the live incident shape.
        return json.dumps(
            {
                "actions": [
                    {
                        "type": "send_message",
                        "payload": {
                            "text": f"update number {iteration['n']}",
                            "interface_path": "telegram_bot/1",
                        },
                    },
                    {
                        "type": "agent_read_file",
                        "payload": {"path": "/tmp/x"},
                    },
                ]
            }
        )

    monkeypatch.setattr(
        _agent_loop_manager, "_call_engine_direct", fake_call_engine_direct
    )

    async def fake_persist(**kwargs):
        return 1

    monkeypatch.setattr(_agent_loop_manager, "_persist_agentic_turn", fake_persist)

    sent: list[tuple[str, str]] = []

    async def fake_execute(name, args, context=None, original_message=None):
        if name == "send_message":
            sent.append((name, args.get("text")))
            return {"ok": True, "result": "sent"}
        return {"ok": True, "result": "done"}

    monkeypatch.setattr(
        "core.agent_tool_executor.agent_tool_executor.execute", fake_execute
    )

    result = await _agent_loop_manager.run_agentic_turn(
        goal="pull the song",
        engine="fake-engine",
        max_iterations=3,
        timeout_seconds=30.0,
    )
    # Exactly ONE interim message reached the interface; the re-worded
    # duplicates on iterations 2 and 3 were suppressed.
    assert len(sent) == 1, sent
    assert sent[0][1] == "update number 1"
    # The work tool kept running on every iteration (suppression must not
    # stall the loop).
    obs_dump = json.dumps(result["observations"], default=str)
    assert obs_dump.count("agent_read_file") >= 3
    assert "suppressed" in obs_dump
    # The suppressed texts must NOT be hoisted into the final answer.
    assert result["final_text"] == ""


@pytest.mark.asyncio
async def test_interim_message_cap_zero_disables_interim_messages(monkeypatch):
    """AGENT_MAX_INTERIM_MESSAGES=0 silences all mid-loop updates; the final
    answer channel (attempt_completion) is unaffected."""
    from core.agent_core import _agent_loop_manager
    from core.config_manager import config_registry

    original = config_registry.get_value

    def fake_get_value(key, default=None, *a, **kw):
        if key == "AGENT_MAX_INTERIM_MESSAGES":
            return 0
        return original(key, default, *a, **kw)

    monkeypatch.setattr(config_registry, "get_value", fake_get_value)

    async def fake_call_engine_direct(prompt, engine_name, cortex_scope="agent"):
        return json.dumps(
            {
                "actions": [
                    {
                        "type": "message_telegram_bot",
                        "payload": {
                            "text": "status!",
                            "interface_path": "telegram_bot/1",
                        },
                    },
                    {"type": "agent_read_file", "payload": {"path": "/tmp/x"}},
                ]
            }
        )

    monkeypatch.setattr(
        _agent_loop_manager, "_call_engine_direct", fake_call_engine_direct
    )

    async def fake_persist(**kwargs):
        return 1

    monkeypatch.setattr(_agent_loop_manager, "_persist_agentic_turn", fake_persist)

    sent: list[str] = []

    async def fake_execute(name, args, context=None, original_message=None):
        if name == "send_message":
            sent.append(args.get("text"))
            return {"ok": True, "result": "sent"}
        return {"ok": True, "result": "done"}

    monkeypatch.setattr(
        "core.agent_tool_executor.agent_tool_executor.execute", fake_execute
    )

    await _agent_loop_manager.run_agentic_turn(
        goal="quiet task",
        engine="fake-engine",
        max_iterations=2,
        timeout_seconds=30.0,
    )
    assert sent == []


# ---------------------------------------------------------------------------
# Persona voiceover (final agent result re-voiced through the persona engine)
# ---------------------------------------------------------------------------


def _voiceover_harness(monkeypatch, engine_reply):
    """Patch persona_voiceover's collaborators; return the engine-call log."""
    from core.agent_core import _agent_loop_manager

    calls: list[dict] = []

    async def fake_engine(
        prompt, engine_name, cortex_scope="agent", *, native_tools=True
    ):
        calls.append(
            {
                "engine": engine_name,
                "native_tools": native_tools,
                "system": (prompt.get("input") or {})
                .get("payload", {})
                .get("system", ""),
                "text": (prompt.get("input") or {}).get("payload", {}).get("text", ""),
            }
        )
        return engine_reply

    monkeypatch.setattr(_agent_loop_manager, "_call_engine_direct", fake_engine)

    async def fake_base_engine(*args, **kwargs):
        return "persona-engine"

    monkeypatch.setattr("core.config.get_active_cortex_engine", fake_base_engine)

    async def fake_build_prompt_request(
        message, context_memory, interface_name=None, **kw
    ):
        return {
            "input": {
                "payload": {
                    "system": "FULL PERSONA SYSTEM PROMPT",
                    "text": getattr(message, "text", ""),
                }
            }
        }

    monkeypatch.setattr(
        "core.prompt_engine.build_prompt_request", fake_build_prompt_request
    )
    return _agent_loop_manager, calls


@pytest.mark.asyncio
async def test_persona_voiceover_prose_success(monkeypatch):
    manager, calls = _voiceover_harness(monkeypatch, "Voiced result in her tone!")
    voiced = await manager.persona_voiceover(
        "task complete: downloaded to E:/Media/Music/unsorted",
        goal="pull the song",
        context={"interface_path": "telegram_bot/123"},
        message=None,
    )
    assert voiced == "Voiced result in her tone!"
    assert len(calls) == 1
    call = calls[0]
    # Routed to the persona (Base Cortex) engine, prose-only.
    assert call["engine"] == "persona-engine"
    assert call["native_tools"] is False
    # The full persona system prompt was used, and the instruction carries
    # both the agent result and the goal.
    assert call["system"] == "FULL PERSONA SYSTEM PROMPT"
    assert "E:/Media/Music/unsorted" in call["text"]
    assert "pull the song" in call["text"]


@pytest.mark.asyncio
async def test_persona_voiceover_unwraps_action_json(monkeypatch):
    """The persona chat prompt invites an action reply — a message action's
    text IS the styled result."""
    reply = (
        '{"actions": [{"type": "message_telegram_bot", "payload": '
        '{"text": "Unwrapped styled reply", "interface_path": "telegram_bot/1"}}]}'
    )
    manager, _calls = _voiceover_harness(monkeypatch, reply)
    voiced = await manager.persona_voiceover(
        "raw result",
        goal="g",
        context={"interface_path": "telegram_bot/1"},
        message=None,
    )
    assert voiced == "Unwrapped styled reply"


@pytest.mark.asyncio
async def test_persona_voiceover_failure_returns_empty(monkeypatch):
    manager, _calls = _voiceover_harness(monkeypatch, "")
    voiced = await manager.persona_voiceover(
        "raw result",
        goal="g",
        context={"interface_path": "telegram_bot/1"},
        message=None,
    )
    assert voiced == ""


@pytest.mark.asyncio
async def test_persona_voiceover_empty_input_short_circuits(monkeypatch):
    manager, calls = _voiceover_harness(monkeypatch, "should not be called")
    voiced = await manager.persona_voiceover("   ", goal="g", context={}, message=None)
    assert voiced == ""
    assert calls == []
