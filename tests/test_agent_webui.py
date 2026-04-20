import json

from core.webui import WebUI


class FakeAgentPlugin:
    def __init__(self):
        self.approved = False

    async def execute_action(self, action, context, bot, original_message):
        if action.get("type") == "approve_action":
            self.approved = True
            return {
                "status": "executed",
                "proposal_id": action.get("payload", {}).get("proposal_id"),
            }
        return None


async def test_approve_endpoint(monkeypatch):
    # Prepare webui instance
    ui = WebUI(autostart=False)

    # Register fake plugin
    from core.core_initializer import PLUGIN_REGISTRY

    PLUGIN_REGISTRY["agent"] = FakeAgentPlugin()

    # Create a fake request with trainer info
    class Req:
        async def json(self):
            return {"trainer": "trainer1"}

    req = Req()

    res = await ui.approve_agent_proposal(111, req)
    assert res.status_code == 200
    body = json.loads(res.body.decode()) if hasattr(res, "body") else res.body
    assert body.get("result", {}).get("status") == "executed"
    assert body.get("result", {}).get("proposal_id") == 111


async def test_create_agent_task_endpoint(monkeypatch):
    ui = WebUI(autostart=False)

    # Patch config_registry to enable AGENT_ENABLED
    from core.config_manager import config_registry

    monkeypatch.setattr(
        config_registry,
        "get_var",
        lambda k, d=None: True if k == "AGENT_ENABLED" else d,
    )

    # Patch AgentLoopManager.run_loop to return a task id
    from core.agent_core import get_agent_loop_manager

    manager = get_agent_loop_manager()

    async def fake_run_loop(engine, input_payload, context=None, max_iterations=None):
        return 4242

    monkeypatch.setattr(manager, "run_loop", fake_run_loop)

    # Fake request body
    class Req:
        async def json(self):
            return {"engine": "manual", "input": {"text": "do it"}}

    req = Req()
    res = await ui.create_agent_task(req)
    assert res.status_code == 200
    body = json.loads(res.body.decode()) if hasattr(res, "body") else res.body
    assert body.get("task_id") == 4242
