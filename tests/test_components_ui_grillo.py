import asyncio
import json
import pytest

from core.webui import SynthWebUIInterface

@pytest.mark.asyncio
async def test_components_summary_includes_grillo_action(monkeypatch):
    # Ensure plugin present
    from plugins.grillo.grillo_compactor import GrilloCompactorPlugin
    from core.core_initializer import PLUGIN_REGISTRY

    prev = PLUGIN_REGISTRY.get('grillo_compactor')
    plugin_instance = GrilloCompactorPlugin()
    PLUGIN_REGISTRY['grillo_compactor'] = plugin_instance

    try:
        webui = SynthWebUIInterface(autostart=False)
        response = await webui.components_summary()
        # components_summary returns a JSONResponse; extract the payload
        import json as _json
        payload = _json.loads(response.body) if hasattr(response, 'body') else response
        plugins = payload.get('plugins', [])
        grillo = next((p for p in plugins if p.get('name') == 'grillo_compactor'), None)
        assert grillo is not None
        actions = grillo.get('actions') or []
        names = [a.get('name') for a in actions]
        assert 'compact_now' in names
    finally:
        if prev is None:
            del PLUGIN_REGISTRY['grillo_compactor']
        else:
            PLUGIN_REGISTRY['grillo_compactor'] = prev

@pytest.mark.asyncio
async def test_run_grillo_via_webui_api(monkeypatch):
    # Create plugin instance registered
    from plugins.grillo.grillo_compactor import GrilloCompactorPlugin
    from core.core_initializer import PLUGIN_REGISTRY

    prev = PLUGIN_REGISTRY.get('grillo_compactor')
    plugin_instance = GrilloCompactorPlugin()
    PLUGIN_REGISTRY['grillo_compactor'] = plugin_instance

    # Mock DB and LLM
    class DummyCursor:
        async def execute(self, sql, params=None):
            pass
        async def fetchall(self):
            return [ {"id": 501, "content": "I like mountains", "tags": json.dumps(["hike"]), "timestamp": "2020-06-01"} ]
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
    monkeypatch.setattr(cdb, 'get_conn_ctx', mock_get_conn_ctx)

    class FakeEngine:
        async def generate_response(self, prompt):
            return json.dumps({"clusters": [{"cluster_id":1, "should_compact": True, "summary": "Mountains memory", "summary_chars": 16, "tags":["hike"], "feeling":"calm", "source_ids":[501], "confidence":"high", "justification":"same theme"}]})

    class FakeRegistry:
        def get_engine(self, name):
            return FakeEngine()

    monkeypatch.setattr('core.cortex_registry.get_cortex_registry', lambda: FakeRegistry())
    monkeypatch.setattr('core.config.get_active_cortex_engine', lambda: asyncio.sleep(0, result='dummy'))

    # Call run_action directly (same as webui API would)
    try:
        res = await plugin_instance.run_action('compact_now', {'cycles':1, 'dry_run': True, 'marker': 'test-marker-1'})
        assert res.get('status') == 'ok'
        assert res.get('dry_run') is True
    finally:
        if prev is None:
            del PLUGIN_REGISTRY['grillo_compactor']
        else:
            PLUGIN_REGISTRY['grillo_compactor'] = prev


@pytest.mark.asyncio
async def test_run_action_emits_logs(monkeypatch):
    from plugins.grillo.grillo_compactor import GrilloCompactorPlugin
    from core.core_initializer import PLUGIN_REGISTRY

    prev = PLUGIN_REGISTRY.get('grillo_compactor')
    plugin_instance = GrilloCompactorPlugin()
    PLUGIN_REGISTRY['grillo_compactor'] = plugin_instance

    # Capture log_info calls
    logged = []
    # Patch the log symbol used inside the plugin module (plugin imports log_info locally)
    monkeypatch.setattr('plugins.grillo.grillo_compactor.log_info', lambda msg, *a, **kw: logged.append(str(msg)))

    # Minimal DB/LLM mocks
    class DummyCursor:
        async def execute(self, sql, params=None):
            pass
        async def fetchall(self):
            return [ {"id": 601, "content": "Walk in park", "tags": json.dumps(["park"]), "timestamp": "2020-07-01"} ]
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
    monkeypatch.setattr(cdb, 'get_conn_ctx', mock_get_conn_ctx)

    class FakeEngine:
        async def generate_response(self, prompt):
            return json.dumps({"clusters": [{"cluster_id":1, "should_compact": True, "summary": "Park memory", "summary_chars": 11, "tags":["park"], "feeling":"calm", "source_ids":[601], "confidence":"high", "justification":"same theme"}]})

    class FakeRegistry:
        def get_engine(self, name):
            return FakeEngine()

    monkeypatch.setattr('core.cortex_registry.get_cortex_registry', lambda: FakeRegistry())
    monkeypatch.setattr('core.config.get_active_cortex_engine', lambda: asyncio.sleep(0, result='dummy'))

    try:
        await plugin_instance.run_action('compact_now', {'cycles':1, 'dry_run': True, 'marker': 'log-marker-99'})
        assert any('run_action called' in m for m in logged), f"Expected 'run_action called' in logs: {logged}"
        assert any('log-marker-99' in m for m in logged), f"Expected marker in logs: {logged}"
    finally:
        if prev is None:
            del PLUGIN_REGISTRY['grillo_compactor']
        else:
            PLUGIN_REGISTRY['grillo_compactor'] = prev