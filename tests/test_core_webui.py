import pytest
import json

from core.webui import SynthWebUIInterface


class FakeRegistry:
    def __init__(self):
        self._engine_meta = {'manual': {'cortex': 'llm'}}

    def get_available_engines(self, cortex=None):
        if cortex is None or cortex == 'llm':
            return ['manual']
        return []


@pytest.mark.asyncio
async def test_components_summary_resilient(monkeypatch):
    """Ensure components_summary returns a JSONResponse even when config helpers are unavailable."""
    # Patch cortex registry
    monkeypatch.setattr('core.cortex_registry.get_cortex_registry', lambda: FakeRegistry())

    # Simulate get_active_llm raising an exception to ensure webui handles it
    def fake_get_active_llm():
        raise RuntimeError("DB not ready")

    monkeypatch.setattr('core.config.get_active_llm', fake_get_active_llm, raising=False)

    webui = SynthWebUIInterface(autostart=False)

    resp = await webui.components_summary()
    assert resp.status_code == 200

    payload = json.loads(resp.body)
    # Basic structural checks
    assert 'llm' in payload
    assert 'interfaces' in payload
    assert 'plugins' in payload
    assert 'cortex' in payload
