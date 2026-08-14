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
