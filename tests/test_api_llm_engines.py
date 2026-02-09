import asyncio
import time

from fastapi.testclient import TestClient

from core.webui import SynthWebUIInterface
from core.cortex_registry import get_cortex_registry


def test_components_includes_llm_login_fields():
    webui = SynthWebUIInterface(autostart=False)
    client = TestClient(webui.app)

    resp = client.get("/api/components")
    assert resp.status_code == 200
    payload = resp.json()
    assert "llm" in payload
    engines = payload["llm"].get("engines", [])
    # Ensure at least the fields are present for each engine entry
    for e in engines:
        assert "name" in e
        assert "login_state" in e
        assert "logged_in" in e


def test_llm_login_endpoint_starts_flow(monkeypatch):
    webui = SynthWebUIInterface(autostart=False)
    client = TestClient(webui.app)

    registry = get_cortex_registry()
    # Ensure selenium_chatgpt is loadable
    try:
        engine = registry.load_engine("selenium_chatgpt")
    except Exception:
        # If the real selenium engine can't be loaded in the test env, skip
        return

    # Patch the engine.start_login_flow to record it was called
    called = {"flag": False}

    async def fake_start():
        called["flag"] = True
        return {"logged_in": False, "login_state": "unlogged"}

    monkeypatch.setattr(engine, "start_login_flow", fake_start)

    resp = client.post("/api/components/llm/login", json={"name": "selenium_chatgpt"})
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") == "ok"

    # Give the background task a short moment to run
    time.sleep(0.1)
    assert called["flag"] is True


def test_llm_login_endpoint_errors_for_missing_or_non_selenium():
    webui = SynthWebUIInterface(autostart=False)
    client = TestClient(webui.app)

    # Unknown engine -> 404
    resp = client.post('/api/components/llm/login', json={'name': 'no_such_engine'})
    assert resp.status_code == 404

    # Load a non-selenium engine (manual) and try
    from core.cortex_registry import get_cortex_registry
    registry = get_cortex_registry()
    try:
        manual = registry.load_engine('manual')
    except Exception:
        manual = None

    if manual:
        resp = client.post('/api/components/llm/login', json={'name': 'manual'})
        assert resp.status_code == 400
