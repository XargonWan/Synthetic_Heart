import pytest
import json
from collections import deque

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


@pytest.mark.asyncio
async def test_session_history_load_uses_persisted_webui_limit(monkeypatch):
    loaded_paths: list[str] = []
    context_deque = deque(
        ({"text": f"ctx-{index}"} for index in range(10)),
        maxlen=10,
    )
    persisted_history = deque(
        ({"text": f"persisted-{index}"} for index in range(25)),
        maxlen=100,
    )

    async def fake_load_context(interface_path: str) -> None:
        loaded_paths.append(interface_path)

    async def fake_load_persisted(interface_path: str, limit: int | None = None):
        assert interface_path == "synth_webui/webui_default"
        assert limit == 100
        return persisted_history

    monkeypatch.setattr(
        "core.chat_context_manager.load_chat_history",
        fake_load_context,
    )
    monkeypatch.setattr(
        "core.chat_context_manager.get_or_create_chat_context",
        lambda interface_path: context_deque,
    )
    monkeypatch.setattr(
        "core.chat_history_cache.load_chat_history",
        fake_load_persisted,
    )

    webui = SynthWebUIInterface(autostart=False)
    webui.max_history = 100

    await webui._ensure_session_history_loaded("webui_default")

    assert loaded_paths == ["synth_webui/webui_default"]
    assert len(webui.message_history["webui_default"]) == 25
    assert webui.message_history["webui_default"].maxlen == 100
    assert webui.message_history["webui_default"] is not context_deque
