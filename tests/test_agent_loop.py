import asyncio
import pytest
import datetime

from core.agent_core import AgentLoopManager


class FakeCursor:
    def __init__(self, ctx):
        self.ctx = ctx
        self.queries = []
        self.lastrowid = 123

    async def execute(self, sql, params=None):
        self.queries.append((sql, params))

    async def fetchone(self):
        # Prefer to inspect the most recent query to avoid ambiguity
        last_sql = (self.queries[-1][0] if self.queries else '')
        if 'agent_activity_log' in last_sql and 'WHERE status=%s' in last_sql:
            return (321, 'echo approved', datetime.datetime.utcnow())
        if 'SELECT command, status, metadata FROM agent_activity_log' in last_sql or ('WHERE id=%s' in last_sql and 'agent_activity_log' in last_sql):
            return ('echo approved', 'proposed', '{"task_id": 123}')
        # Fallback: if a recent agent_activity_log status scan appears anywhere in the last queries
        if any('agent_activity_log' in (q[0] or '') and 'WHERE status=%s' in (q[0] or '') for q in self.queries[-8:]):
            return (321, 'echo approved', datetime.datetime.utcnow())
        # Return JSON array placeholder for iterations_meta by default
        return ('[]',)


class FakeConn:
    def __init__(self, ctx):
        self.ctx = ctx
        self.cur = FakeCursor(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        # Async context manager for cursor
        parent = self

        class Ctx:
            async def __aenter__(self_inner):
                return parent.cur

            async def __aexit__(self_inner, exc_type, exc, tb):
                return False

        return Ctx()


class FakePool:
    def __init__(self):
        self.conn = FakeConn(self)

    def __call__(self):
        return self.conn


@pytest.mark.asyncio
async def test_agent_loop_persistence_and_completion(monkeypatch):
    fake_pool = FakePool()

    # Monkeypatch the core.db.get_conn_ctx used inside agent_core
    import core.db as dbmod
    monkeypatch.setattr(dbmod, 'get_conn_ctx', lambda: fake_pool.conn)

    # Monkeypatch plugin_instance to return a JSON proposing an action (to trigger waiting_for_approval)
    import core.plugin_instance as plugin_instance

    async def fake_handle(bot, message, context_memory_or_prompt):
        # Return JSON actions that propose an action requiring approval
        return '{"actions": [{"type": "propose_action", "payload": {"command": "touch /tmp/agent_test"}}], "meta": {"agent_continue": false}}'

    monkeypatch.setattr(plugin_instance, 'handle_incoming_message', fake_handle)

    manager = AgentLoopManager()

    task_id = await manager.run_loop(engine='test', input_payload={'cmd': 'echo hi'}, context={'who': 'tester'}, max_iterations=3)
    assert task_id is not None

    # Wait for background task to process at least one iteration
    task = manager._running_tasks.get(task_id)
    if task:
        await task

    # Validate that the fake cursor recorded INSERT and UPDATE commands
    cursor = fake_pool.conn.cur
    sqls = [q[0] for q in cursor.queries]
    assert any('INSERT INTO agent_tasks' in (s or '') for s in sqls)
    assert any('UPDATE agent_tasks SET status=%s' in (s or '') for s in sqls)
    # Check that one of the updates included waiting_for_approval as a param
    params_list = [p for (_sql, p) in cursor.queries if p]
    assert any(p and 'waiting_for_approval' in (p if isinstance(p, tuple) else ()) for p in params_list)


@pytest.mark.asyncio
async def test_proposal_links_to_task_and_resume(monkeypatch):
    fake_pool = FakePool()

    # Monkeypatch the core.db.get_conn_ctx used inside agent_core
    import core.db as dbmod
    monkeypatch.setattr(dbmod, 'get_conn_ctx', lambda: fake_pool.conn)

    # Prepare manager and start a task that will be paused by a proposal
    manager = AgentLoopManager()

    # Patch plugin_instance to return a propose_action that will include a proposal
    import core.plugin_instance as plugin_instance

    async def fake_handle(bot, message, context_memory_or_prompt):
        # Should record a propose_action; ensure it includes task context
        return '{"actions": [{"type": "propose_action", "payload": {"command": "touch /tmp/agent_test"}}], "meta": {"agent_continue": false}}'

    monkeypatch.setattr(plugin_instance, 'handle_incoming_message', fake_handle)

    task_id = await manager.run_loop(engine='test', input_payload={'cmd': 'echo hi'}, context={'who': 'tester'}, max_iterations=1)
    assert task_id is not None

    # Wait a short time to ensure loop processed and paused
    task = manager._running_tasks.get(task_id)
    if task:
        await task

    # Now simulate trainer approving the proposal - call AgentPlugin.approve_action
    from plugins.agent_plugin import AgentPlugin
    plugin = AgentPlugin(notify_fn=lambda m: None)

    # Patch _run_command to avoid executing shell
    monkeypatch.setattr(plugin, '_run_command', lambda cmd, timeout=30.0: asyncio.sleep(0, result='ok'))

    # We need to know the proposal id - fake cursor's fetchone returned 321 in earlier tests
    # DEBUG: print queries to understand why approve_action may fail
    cursor = fake_pool.conn.cur
    print('DEBUG: recorded queries:', cursor.queries)
    res = await plugin.execute_action({"type": "approve_action", "payload": {"proposal_id": 321}}, {}, None, {"sender_id": "trainer"})
    print('DEBUG: approve_action result:', res)
    assert res.get('status') == 'executed'

    # Ensure the manager resumed the task (no paused event should remain)
    assert task_id not in manager._paused_tasks or manager._paused_tasks.get(task_id).is_set()


@pytest.mark.asyncio
async def test_start_task_action(monkeypatch):
    # Test that the agent plugin start_task action delegates to AgentLoopManager
    from plugins.agent_plugin import AgentPlugin

    plugin = AgentPlugin(notify_fn=lambda m: None)

    async def fake_run_loop(engine, input_payload, context=None, max_iterations=None):
        return 9999

    from core.agent_core import get_agent_loop_manager
    manager = get_agent_loop_manager()
    monkeypatch.setattr(manager, 'run_loop', fake_run_loop)

    res = await plugin.execute_action({"type": "start_task", "payload": {"engine": "manual", "input": {"text": "do thing"}}}, {}, None, None)
    assert isinstance(res, dict)
    assert res.get('status') == 'started'
    assert res.get('task_id') == 9999
