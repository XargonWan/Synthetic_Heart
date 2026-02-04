import pytest

from plugins.agent_core import AgentCorePlugin


@pytest.mark.asyncio
async def test_propose_action_and_approve(monkeypatch):
    called = {}

    # Fake DB get_conn_ctx
    class FakeCursor:
        def __init__(self):
            self.lastrowid = 777
            self.execs = []

        async def execute(self, sql, params=None):
            self.execs.append((sql, params))

        async def fetchone(self):
            return (None,)

        async def fetchall(self):
            return []

    class FakeConn:
        def __init__(self):
            self.cur = FakeCursor()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            parent = self

            class Ctx:
                async def __aenter__(self_inner):
                    return parent.cur

                async def __aexit__(self_inner, exc_type, exc, tb):
                    return False

            return Ctx()

        def commit(self):
            called['committed'] = True

    import core.db as dbmod
    monkeypatch.setattr(dbmod, 'get_conn_ctx', lambda: FakeConn())

    notifications = []

    def fake_notify(msg):
        notifications.append(msg)

    plugin = AgentCorePlugin(notify_fn=fake_notify)

    res = await plugin.propose_action(proposer='tester', command={'cmd': 'ls -la'}, metadata={'k': 'v'})
    assert res.get('status') == 'proposed'
    assert res.get('proposal_id') == 777
    assert notifications, 'expected notification on propose'

    # Approve & execute flow - monkeypatch run_command
    async def fake_run(cmd):
        called['ran'] = cmd
        return {'ok': True, 'output': 'ok'}

    monkeypatch.setattr(plugin, '_run_command', fake_run)

    res2 = await plugin.approve_action(proposal_id=777, approver='approver', command={'cmd': 'echo hi'})
    assert res2.get('status') == 'executed'
    assert 'ran' in called
