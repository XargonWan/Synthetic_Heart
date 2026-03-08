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

    # ensure live engine is registered
    try:
        pass  # registers itself
    except Exception:
        pass

    resp = client.post(
        "/api/components/cortex/model",
        json={"engine": "gemini_live", "model": "whatever"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") == "ok"
    assert data.get("engine") == "gemini_live"
    assert data.get("model") == "whatever"
