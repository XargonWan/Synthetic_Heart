import time

from fastapi.testclient import TestClient

from core.webui import SynthWebUIInterface
from core.cortex_registry import get_cortex_registry


def test_set_cortex_engine_switches_to_manual():
    webui = SynthWebUIInterface(autostart=False)
    client = TestClient(webui.app)

    resp = client.post("/api/components/cortex", json={"name": "manual"})
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") == "ok"
    assert data.get("active") == "manual"


def test_cortex_login_endpoint_starts_flow(monkeypatch):
    webui = SynthWebUIInterface(autostart=False)
    client = TestClient(webui.app)

    registry = get_cortex_registry()
    # Ensure selenium_gemini is loadable; skip if cannot
    try:
        engine = registry.load_engine("selenium_gemini")
    except Exception:
        return

    called = {"flag": False}

    async def fake_start():
        called["flag"] = True
        return {"logged_in": False, "login_state": "unlogged"}

    monkeypatch.setattr(engine, "start_login_flow", fake_start)

    resp = client.post("/api/components/cortex/login", json={"name": "selenium_gemini"})
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") == "ok"

    time.sleep(0.1)
    assert called["flag"] is True


def test_cortex_login_endpoint_errors_for_missing_or_non_selenium():
    webui = SynthWebUIInterface(autostart=False)
    client = TestClient(webui.app)

    resp = client.post("/api/components/cortex/login", json={"name": "no_such_engine"})
    assert resp.status_code == 404

    registry = get_cortex_registry()
    try:
        manual = registry.load_engine("manual")
    except Exception:
        manual = None

    if manual:
        resp = client.post("/api/components/cortex/login", json={"name": "manual"})
        assert resp.status_code == 400
