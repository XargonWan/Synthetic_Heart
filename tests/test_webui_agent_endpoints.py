import pytest
import json

from core.webui import SynthWebUIInterface
from plugins.agent_core import AgentCorePlugin
from core.core_initializer import PLUGIN_REGISTRY


class FakeRequest:
    def __init__(self, payload=None):
        self._payload = payload or {}

    async def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_approve_agent_proposal_calls_plugin(monkeypatch):
    plugin = AgentCorePlugin()

    # Register plugin in PLUGIN_REGISTRY
    prev = PLUGIN_REGISTRY.get('agent')
    PLUGIN_REGISTRY['agent'] = plugin

    try:
        webui = SynthWebUIInterface(autostart=False)

        called = {}

        async def fake_execute(action, context, bot, original_message):
            called['action'] = action
            called['original_message'] = original_message
            return {'status': 'ok', 'executed': True}

        monkeypatch.setattr(plugin, 'execute_action', fake_execute)

        req = FakeRequest({'trainer': 'webui'})
        resp = await webui.approve_agent_proposal(42, req)
        assert resp is not None
        # JSONResponse.body is bytes
        body = resp.body
        data = json.loads(body)
        assert 'result' in data
        assert data['result']['status'] == 'ok'
        assert called['action']['type'] == 'approve_action'
        assert called['action']['payload']['proposal_id'] == 42
    finally:
        # restore
        if prev is None:
            PLUGIN_REGISTRY.pop('agent', None)
        else:
            PLUGIN_REGISTRY['agent'] = prev
