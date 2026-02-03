import asyncio

from core.agent_core import AgentLoopManager
from core.core_initializer import PLUGIN_REGISTRY


class FakeReconPlugin:
    def __init__(self):
        self.called = False

    async def get_recon_contributions(self, **kwargs):
        self.called = True
        return [{'snippet': 'recent memory', 'source': 'test'}]

    def on_debrief(self, processed_actions, failed_actions, results, context, original_message):
        # Simple sync handler
        self.debrief_called = True
        self.context = context


async def test_recon_and_debrief_hooks(monkeypatch):
    fake = FakeReconPlugin()
    # Register fake plugin
    PLUGIN_REGISTRY['fake_recon'] = fake

    # Patch plugin_instance to return empty actions so loop still runs
    import core.plugin_instance as plugin_instance

    async def fake_handle(bot, message, context_memory_or_prompt):
        return '{}'  # no actions

    monkeypatch.setattr(plugin_instance, 'handle_incoming_message', fake_handle)

    # Patch DB get_conn_ctx to avoid aiomysql dependency
    import core.db as dbmod

    class FakeCursor:
        def __init__(self):
            self.queries = []
            self.lastrowid = 123

        async def execute(self, sql, params=None):
            self.queries.append((sql, params))

        async def fetchone(self):
            return ('[]',)

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

    fake_conn = FakeConn()
    monkeypatch.setattr(dbmod, 'get_conn_ctx', lambda: fake_conn)

    manager = AgentLoopManager()
    task_id = await manager.run_loop(engine='test', input_payload={'cmd': 'echo'}, context={}, max_iterations=1)
    assert task_id is not None

    task = manager._running_tasks.get(task_id)
    if task:
        await task

    assert getattr(fake, 'called', False) is True
    assert getattr(fake, 'debrief_called', True) is True
    assert fake.context.get('task_id') == task_id
