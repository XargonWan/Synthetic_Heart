import asyncio
import pytest

from core.agent_core import AgentCore


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
async def test_attach_to_engine_calls_attach(monkeypatch):
    fake_engine = FakeEngine()

    class FakeRegistry:
        def get_engine(self, name):
            return fake_engine

        def load_engine(self, name):
            return fake_engine

    monkeypatch.setattr('core.config.get_active_cortex_engine', lambda: asyncio.sleep(0, result='gemini_api'))
    monkeypatch.setattr('core.cortex_registry.get_cortex_registry', lambda: FakeRegistry())

    agent = AgentCore()
    agent._enabled = True

    await agent.attach_to_active_engine()

    assert fake_engine.attached is True
    assert fake_engine.attach_calls and fake_engine.attach_calls[0] is agent


@pytest.mark.asyncio
async def test_propose_and_approve_flow(monkeypatch):
    called = {}

    async def fake_create(command, proposer=None, metadata=None):
        called['created'] = command
        return 555

    async def fake_update(aid, **kwargs):
        called.setdefault('updates', []).append((aid, kwargs))

    async def fake_insert(aid, cmd, **kwargs):
        called.setdefault('execs', []).append((aid, cmd, kwargs))
        return 999

    def fake_notify(msg):
        called['notify'] = msg

    async def fake_run(cmd, timeout=30.0):
        called['ran'] = cmd
        return "OUTPUT: " + cmd

    agent = AgentCore()
    agent._notify_fn = fake_notify
    agent._create_activity_log = fake_create
    agent._update_activity_log = fake_update
    agent._insert_action_exec = fake_insert
    agent._run_command = fake_run
    agent._enabled = True

    # Propose
    res = await agent.execute_action({"type": "propose_action", "payload": {"command": "touch /tmp/test"}}, {}, None, None)
    assert res.get('status') == 'proposed'
    assert res.get('proposal_id') == 555
    assert 'notify' in called

    # Approve (provide command directly to avoid real DB lookup in unit test)
    res2 = await agent.execute_action({"type": "approve_action", "payload": {"proposal_id": 555, "command": "echo approved"}}, {}, None, {'sender_id': 42})
    assert res2.get('status') == 'executed'
    assert res2.get('proposal_id') == 555
    assert called.get('ran') is not None
    assert 'execs' in called
