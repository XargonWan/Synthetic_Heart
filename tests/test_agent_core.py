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
    # The working tool ran on every iteration (never suppressed).
    work_execs = [n for n in executed if n == "agent_read_file"]
    assert len(work_execs) == max_iterations
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
