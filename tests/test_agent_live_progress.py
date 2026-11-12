"""The Agent panel shows steps while a task runs, and sub-agents link to their parent."""

from __future__ import annotations

import json

import pytest

from core.agent_core import AgentLoopManager

OBSERVATIONS = [
    {
        "iteration": 1,
        "role": "tool_results",
        "content": [
            {
                "tool": "agent_list_files",
                "arguments": {"path": "."},
                "ok": True,
                "result": "a b",
            }
        ],
    },
    {"iteration": 2, "role": "assistant", "content": "thinking"},
]


def test_observations_become_the_stored_shape():
    meta = AgentLoopManager._observations_to_meta(OBSERVATIONS)
    assert meta[0] == {
        "iteration": 1,
        "role": "tool_results",
        "result": OBSERVATIONS[0]["content"],
    }
    assert meta[1]["role"] == "assistant" and meta[1]["result"] == "thinking"
    assert AgentLoopManager._observations_to_meta(None) == []
    assert AgentLoopManager._observations_to_meta(["junk", 3]) == []


class _Cursor:
    def __init__(self, log):
        self.log = log

    async def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return _Cursor(self.log)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _manager(log):
    manager = AgentLoopManager()

    async def conn_ctx():
        return _Conn(log)

    async def commit(conn):
        log.append(("commit", None))

    manager._get_conn_ctx = conn_ctx  # type: ignore[method-assign]
    manager._maybe_commit = commit  # type: ignore[method-assign]
    return manager


@pytest.mark.asyncio
async def test_progress_updates_only_the_running_row():
    log: list = []
    await _manager(log)._persist_progress(41, OBSERVATIONS)

    sql, params = log[0]
    assert "UPDATE agent_tasks SET iterations_meta" in sql and "status='running'" in sql
    assert params[1] == 41
    assert json.loads(params[0])[0]["result"][0]["tool"] == "agent_list_files"
    assert log[-1][0] == "commit"


@pytest.mark.asyncio
async def test_progress_does_nothing_without_a_row_or_steps():
    log: list = []
    manager = _manager(log)
    await manager._persist_progress(None, OBSERVATIONS)
    await manager._persist_progress(41, [])
    assert log == []


@pytest.mark.asyncio
async def test_progress_failures_never_break_the_turn():
    manager = AgentLoopManager()

    async def broken():
        raise RuntimeError("db down")

    manager._get_conn_ctx = broken  # type: ignore[method-assign]
    await manager._persist_progress(41, OBSERVATIONS)


@pytest.mark.asyncio
async def test_loop_stores_steps_as_it_goes_and_tells_tools_which_task_it_is(
    monkeypatch,
):
    """Tools (spawn_drone) see the running task's id; the panel sees each step."""
    from core.agent_tool_executor import agent_tool_executor

    replies = iter(
        [
            json.dumps(
                {"actions": [{"type": "mcp_fs_read", "payload": {"path": "/x"}}]}
            ),
            json.dumps(
                {
                    "actions": [
                        {"type": "attempt_completion", "payload": {"summary": "done"}}
                    ]
                }
            ),
        ]
    )

    async def fake_handle(bot, message, context_memory_or_prompt):
        return next(replies)

    monkeypatch.setattr("core.plugin_instance.handle_incoming_message", fake_handle)

    seen_contexts: list = []

    async def fake_execute(name, arguments, context=None, original_message=None):
        seen_contexts.append(context)
        return {"ok": True, "tool": name, "result": "file contents", "error": None}

    monkeypatch.setattr(agent_tool_executor, "execute", fake_execute)

    manager = AgentLoopManager()
    progress: list = []

    async def fake_begin(**kwargs):
        return 77

    async def fake_progress(task_id, observations):
        progress.append((task_id, [o.get("role") for o in observations]))

    async def fake_persist(**kwargs):
        return kwargs.get("task_id")

    manager._begin_agentic_turn = fake_begin  # type: ignore[method-assign]
    manager._persist_progress = fake_progress  # type: ignore[method-assign]
    manager._persist_agentic_turn = fake_persist  # type: ignore[method-assign]

    out = await manager.run_agentic_turn(
        goal="read the file", max_iterations=5, timeout_seconds=30, context={}
    )

    assert out["stop_reason"] == "completed"
    assert seen_contexts[0]["agent_task_id"] == 77
    # The tool result is stored before the loop asks the model for its next step.
    assert progress == [(77, ["tool_results"])]
