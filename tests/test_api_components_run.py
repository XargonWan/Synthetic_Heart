import asyncio
from fastapi.testclient import TestClient

from core.webui import SynthWebUIInterface


import pytest

@pytest.mark.asyncio
async def test_run_component_with_run_action(monkeypatch):
    # Create a fake plugin that implements run_action
    class FakeRunner:
        async def run_action(self, action_type, payload, context=None):
            # Simple echo of payload
            return {"action": action_type, "payload": payload}

    from core.core_initializer import PLUGIN_REGISTRY
    prev = PLUGIN_REGISTRY.get("fake_runner")
    plugin = FakeRunner()
    PLUGIN_REGISTRY["fake_runner"] = plugin

    try:
        result = await plugin.run_action("compact_now", {"cycles": 2})
        assert result["action"] == "compact_now"
        assert result["payload"]["cycles"] == 2
    finally:
        # Restore
        if prev is None:
            del PLUGIN_REGISTRY["fake_runner"]
        else:
            PLUGIN_REGISTRY["fake_runner"] = prev


import pytest

@pytest.mark.asyncio
async def test_run_grillo_compactor_dry_run(monkeypatch):
    # Ensure a GrilloCompactorPlugin instance is registered
    from plugins.grillo.grillo_compactor import GrilloCompactorPlugin
    from core.core_initializer import PLUGIN_REGISTRY

    prev = PLUGIN_REGISTRY.get("grillo_compactor")
    plugin_instance = GrilloCompactorPlugin()
    PLUGIN_REGISTRY["grillo_compactor"] = plugin_instance

    # Mock DB and LLM so no writes occur
    class DummyCursor:
        async def execute(self, sql, params=None):
            pass
        async def fetchall(self):
            return [ {"id": 401, "content": "I love hiking", "tags": json.dumps(["hiking"]), "timestamp": "2020-03-01"} ]
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc, tb):
            return False

    class DummyConn:
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc, tb):
            return False
        def cursor(self):
            return DummyCursor()

    def mock_get_conn_ctx():
        return DummyConn()

    import core.db as cdb
    monkeypatch.setattr(cdb, "get_conn_ctx", mock_get_conn_ctx)

    class FakeEngine:
        async def generate_response(self, prompt):
            return json.dumps({"clusters": [{"cluster_id":1, "should_compact": True, "summary": "Hiking memory", "summary_chars": 13, "tags":["hiking"], "feeling":"happy", "source_ids":[401], "confidence":"high", "justification":"shared hiking theme"}]})

    class FakeRegistry:
        def get_engine(self, name):
            return FakeEngine()

    # Patch llm registry
    monkeypatch.setattr('core.llm_registry.get_llm_registry', lambda: FakeRegistry())
    monkeypatch.setattr('core.config.get_active_llm', lambda: asyncio.sleep(0, result='dummy'))

    try:
        result = await plugin_instance.run_action("compact_now", {"cycles":1, "dry_run": True})
        assert result.get('status') == 'ok'
        assert result.get('dry_run') is True
    finally:
        # Restore previous plugin
        if prev is None:
            del PLUGIN_REGISTRY["grillo_compactor"]
        else:
            PLUGIN_REGISTRY["grillo_compactor"] = prev


import pytest
from fastapi import HTTPException

@pytest.mark.asyncio
async def test_run_component_rejects_missing_run(monkeypatch):
    webui = SynthWebUIInterface(autostart=False)

    class NoRunPlugin:
        pass

    from core.core_initializer import PLUGIN_REGISTRY
    prev = PLUGIN_REGISTRY.get("norun")
    PLUGIN_REGISTRY["norun"] = NoRunPlugin()

    # Build a dummy request object
    class DummyRequest:
        def __init__(self, payload):
            self._payload = payload
        async def json(self):
            return self._payload

    req = DummyRequest({"name": "norun", "action": "compact_now"})

    try:
        with pytest.raises(HTTPException) as excinfo:
            await webui.run_component(req)
        assert excinfo.value.status_code == 400
    finally:
        if prev is None:
            del PLUGIN_REGISTRY["norun"]
        else:
            PLUGIN_REGISTRY["norun"] = prev
