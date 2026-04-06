from fastapi.testclient import TestClient

from core.webui import SynthWebUIInterface


def test_set_live_model_no_error(monkeypatch):
    """Posting a model update for a live engine should not return 404.

    Historically the `/api/components/cortex/model` endpoint queried the
    Cortex registry; since live engines are not registered there any attempt to
    set a model would receive a 404 and the UI would show "disabled".  The
    behaviour now quietly succeeds (no-op) and logs an informational message.
    """
    webui = SynthWebUIInterface(autostart=False)
    client = TestClient(webui.app)

    # Patch LIVE_REGISTRY to report "gemini_live" as available without actually
    # loading the engine module (which may not be available in the test env).
    from core.live_registry import LIVE_REGISTRY

    original_modules = LIVE_REGISTRY._engine_modules.copy()
    LIVE_REGISTRY._engine_modules["gemini_live"] = "fake.module"

    try:
        resp = client.post(
            "/api/components/cortex/model",
            json={"engine": "gemini_live", "model": "whatever"},
        )
    finally:
        LIVE_REGISTRY._engine_modules.clear()
        LIVE_REGISTRY._engine_modules.update(original_modules)

    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") == "ok"
    assert data.get("engine") == "gemini_live"
    assert data.get("model") == "whatever"
