import pytest
import json

from core.webui import SynthWebUIInterface


class FakeRegistry:
    def __init__(self):
        self._engine_meta = {"manual": {"cortex": "llm_provider"}}

    def get_available_engines(self, cortex=None):
        if cortex is None or cortex == "llm_provider":
            return ["manual"]
        return []


@pytest.mark.asyncio
async def test_components_summary_resilient(monkeypatch):
    """Ensure components_summary returns a JSONResponse even when config helpers are unavailable."""
    # Patch cortex registry
    monkeypatch.setattr(
        "core.cortex_registry.get_cortex_registry", lambda: FakeRegistry()
    )

    # Simulate get_active_cortex_engine raising an exception to ensure webui handles it
    def fake_get_active_cortex_engine():
        raise RuntimeError("DB not ready")

    monkeypatch.setattr(
        "core.config.get_active_cortex_engine",
        fake_get_active_cortex_engine,
        raising=False,
    )

    webui = SynthWebUIInterface(autostart=False)

    resp = await webui.components_summary()
    assert resp.status_code == 200

    payload = json.loads(resp.body)
    # Basic structural checks
    assert "cortex" in payload
    assert "interfaces" in payload
    assert "plugins" in payload


@pytest.mark.asyncio
async def test_components_summary_includes_disabled_options(monkeypatch):
    """Engine lists should always include a disabled entry at the front."""
    # this test can use the default registries; just instantiate webui
    webui = SynthWebUIInterface(autostart=False)
    resp = await webui.components_summary()
    assert resp.status_code == 200
    payload = json.loads(resp.body)
    for key in ("auris", "vox", "live", "iris"):
        assert key in payload, f"{key} missing from summary"
        names = [e.get("name") for e in payload[key]]
        assert names, f"{key} list empty"
        assert names[0] == "disabled", f"{key} disabled entry not first"
