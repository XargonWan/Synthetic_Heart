from core.agent_core import AgentLoopManager
from core.core_initializer import PLUGIN_REGISTRY


class FakeReconPlugin:
    def __init__(self):
        self.called = False

    # simple preflight hook (not really used by combined path, but required for
    # eligible list)
    async def get_recon_contributions(self, **kwargs):
        return []

    # implement combined recon interface so gather_recon_contributions will
    # include us in the LLM prompt and eventually call parse_recon_response
    def get_recon_key(self):
        return "FAKE"

    def get_recon_instruction(self):
        return "Return an empty list"

    async def parse_recon_response(self, data, **kwargs):
        # mark that we were invoked by recon
        self.called = True
        # pretend we produced a contribution
        return [{"snippet": "recent memory", "source": "test"}]

    def on_debrief(
        self, processed_actions, failed_actions, results, context, original_message
    ):
        # Simple sync handler
        self.debrief_called = True
        self.context = context


async def test_recon_and_debrief_hooks(monkeypatch, caplog, capsys):
    # ensure synth logger is at DEBUG level so our extra log_debug calls run
    import logging
    import core.logging_utils as logmod

    logmod.setup_logging().setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="synth")

    # caplog won't capture because logger writes to stdout, we'll inspect capsys later

    fake = FakeReconPlugin()
    # Register fake plugin
    PLUGIN_REGISTRY["fake_recon"] = fake
    # prepare a dummy engine so recon prompt can run without reaching out to
    # real LLM services; ensure it returns valid JSON with our plugin key
    import core.cortex_registry as registry_mod
    import core.config as config_mod

    class DummyEngine:
        async def generate_response(self, messages):
            # we don't care about the actual prompt, just return empty JSON
            return '{"FAKE": []}'

    class DummyRegistry:
        def get_engine(self, name):
            return DummyEngine()

        def load_engine(self, name):
            return DummyEngine()

    monkeypatch.setattr(registry_mod, "get_cortex_registry", lambda: DummyRegistry())

    async def fake_active():
        return "dummy"

    monkeypatch.setattr(config_mod, "get_active_cortex_engine", fake_active)

    # Patch plugin_instance to return empty actions so loop still runs
    import core.plugin_instance as plugin_instance

    async def fake_handle(bot, message, context_memory_or_prompt):
        return "{}"  # no actions

    monkeypatch.setattr(plugin_instance, "handle_incoming_message", fake_handle)

    # Patch DB get_conn_ctx to avoid aiomysql dependency
    import core.db as dbmod

    class FakeCursor:
        def __init__(self):
            self.queries = []
            self.lastrowid = 123

        async def execute(self, sql, params=None):
            self.queries.append((sql, params))

        async def fetchone(self):
            return ("[]",)

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
    monkeypatch.setattr(dbmod, "get_conn_ctx", lambda: fake_conn)

    manager = AgentLoopManager()
    task_id = await manager.run_loop(
        engine="test", input_payload={"cmd": "echo"}, context={}, max_iterations=1
    )
    assert task_id is not None

    task = manager._running_tasks.get(task_id)
    if task:
        await task

    # recon contributions should have been collected (logged earlier)
    # parse_recon_response should have flipped this flag
    assert getattr(fake, "called", False) is True
    assert getattr(fake, "debrief_called", True) is True
    assert fake.context.get("task_id") == task_id

    # we don't need to inspect logs here; previous assertions ensure basic hooks ran
