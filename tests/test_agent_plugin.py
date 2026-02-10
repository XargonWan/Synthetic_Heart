import pytest

from plugins.agent_plugin import AgentPlugin


@pytest.mark.asyncio
async def test_whitelist_mode_executes_whitelisted_command(monkeypatch):
    p = AgentPlugin()
    p._enabled = True
    p._approval_mode = "whitelist"
    p._whitelist = ["echo", "ls"]

    async def fake_run(cmd, timeout=30.0):
        return "OK: " + cmd

    monkeypatch.setattr(p, "_run_command", fake_run)

    res = await p.execute_action(
        {"type": "agent_execute", "payload": {"command": "echo hello"}}, {}, None, None
    )
    assert res == "OK: echo hello"


@pytest.mark.asyncio
async def test_whitelist_mode_rejects_non_whitelisted(monkeypatch):
    p = AgentPlugin()
    p._enabled = True
    p._approval_mode = "whitelist"
    p._whitelist = ["ls"]

    res = await p.execute_action(
        {"type": "agent_execute", "payload": {"command": "rm -rf /"}}, {}, None, None
    )
    assert "not allowed" in str(res).lower()


@pytest.mark.asyncio
async def test_always_ask_mode_proposes(monkeypatch):
    called = {}

    def notify(msg):
        called["msg"] = msg

    p = AgentPlugin(notify_fn=notify)
    p._enabled = True
    p._approval_mode = "always_ask"

    res = await p.execute_action(
        {"type": "agent_execute", "payload": {"command": "do something"}},
        {},
        None,
        None,
    )
    assert "proposal" in str(res).lower() or "awaiting" in str(res).lower()
    assert "msg" in called


@pytest.mark.asyncio
async def test_execute_notifies_trainer_when_enabled(monkeypatch):
    called = {}

    def notify(msg):
        called['msg'] = msg

    p = AgentPlugin(notify_fn=notify)
    p._enabled = True
    p._approval_mode = "always_approve"

    async def fake_run(cmd, timeout=30.0):
        return "OUT: " + cmd

    monkeypatch.setattr(p, "_run_command", fake_run)

    res = await p.execute_action({"type": "agent_execute", "payload": {"command": "echo hi"}}, {}, None, None)
    assert "OUT: echo hi" in res
    assert 'msg' in called and 'Agent executed command' in called['msg']


@pytest.mark.asyncio
async def test_execute_delivers_output_to_interface(monkeypatch):
    called = {}

    async def fake_run(cmd, timeout=30.0):
        return "OUT: " + cmd

    async def fake_request_llm_delivery(action_outputs=None, original_context=None, action_type=None, **kwargs):
        called['args'] = {'action_outputs': action_outputs, 'original_context': original_context, 'action_type': action_type}
        return True

    p = AgentPlugin()
    p._enabled = True
    p._approval_mode = "whitelist"
    p._whitelist = ["echo"]

    monkeypatch.setattr(p, "_run_command", fake_run)
    monkeypatch.setattr('core.auto_response.request_llm_delivery', fake_request_llm_delivery)

    context = {'interface': 'telegram_bot'}
    original_message = {'chat_id': 42, 'message_id': 100, 'interface_path': 'telegram_bot:42'}

    res = await p.execute_action({"type": "agent_execute", "payload": {"command": "echo hi"}}, context, None, original_message)
    assert 'OUT: echo hi' in res
    assert 'args' in called and called['args']['action_type'] == 'agent_execute'
    assert called['args']['original_context']['interface_name'] == 'telegram_bot'
    assert 'OUT: echo hi' in called['args']['action_outputs'][0]['output']


@pytest.mark.asyncio
async def test_disabled_mode_rejects(monkeypatch):
    p = AgentPlugin()
    p._enabled = True
    p._approval_mode = "disabled"

    res = await p.execute_action(
        {"type": "agent_execute", "payload": {"command": "ls"}}, {}, None, None
    )
    assert "disabled" in str(res).lower()


@pytest.mark.asyncio
async def test_propose_action_creates_proposal_and_notifies(monkeypatch):
    called = {}

    def notify(msg):
        called["msg"] = msg

    p = AgentPlugin(notify_fn=notify)
    p._enabled = True
    p._approval_mode = "always_ask"

    async def fake_create(cmd, proposer=None, metadata=None):
        return 123

    monkeypatch.setattr(p, "_create_activity_log", fake_create)

    res = await p.execute_action(
        {"type": "propose_action", "payload": {"command": "touch /tmp/test"}},
        {},
        None,
        None,
    )
    assert res.get("status") == "proposed"
    assert res.get("proposal_id") == 123
    assert "msg" in called


@pytest.mark.asyncio
async def test_approve_action_executes_and_persists(monkeypatch):
    p = AgentPlugin()
    p._enabled = True

    recorded = {}

    async def fake_create(cmd, proposer=None, metadata=None):
        recorded["created"] = cmd
        return 200

    async def fake_update(aid, **kwargs):
        recorded["updated"] = (aid, kwargs)

    async def fake_insert_exec(aid, cmd, **kwargs):
        recorded["exec"] = (aid, cmd, kwargs)
        return 555

    async def fake_run(cmd, timeout=30.0):
        return "OUTPUT: " + cmd

    monkeypatch.setattr(p, "_create_activity_log", fake_create)
    monkeypatch.setattr(p, "_update_activity_log", fake_update)
    monkeypatch.setattr(p, "_insert_action_exec", fake_insert_exec)
    monkeypatch.setattr(p, "_run_command", fake_run)

    res = await p.execute_action(
        {"type": "approve_action", "payload": {"command": "echo approved"}},
        {},
        None,
        None,
    )
    assert res.get("status") == "executed"
    assert res.get("proposal_id") == 200
    assert "echo approved" in recorded.get("created")
    assert recorded.get("exec")[0] == 200
    assert recorded.get("exec")[1] == "echo approved"


@pytest.mark.asyncio
async def test_agent_command_approve_calls_plugin(monkeypatch):
    from types import SimpleNamespace

    called = {}

    class FakeAgent:
        async def execute_action(self, action, context, bot, original_message):
            called["action"] = action
            called["original"] = original_message
            return {
                "status": "executed",
                "proposal_id": action.get("payload", {}).get("proposal_id"),
            }

    fake_registry = {"agent": FakeAgent()}
    monkeypatch.setattr("core.core_initializer.PLUGIN_REGISTRY", fake_registry)

    interface_context = {
        "update": SimpleNamespace(effective_user=SimpleNamespace(id=42))
    }

    from core.command_registry import agent_command

    res = await agent_command("approve", "123", interface_context=interface_context)
    assert "Approval result" in res
    assert called["action"]["type"] == "approve_action"
    assert called["original"]["sender_id"] == 42
