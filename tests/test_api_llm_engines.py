from fastapi.testclient import TestClient

from core.webui import SynthWebUIInterface
from core.cortex_registry import get_cortex_registry


def test_components_includes_cortex_login_fields():
    webui = SynthWebUIInterface(autostart=False)
    client = TestClient(webui.app)

    resp = client.get("/api/components")
    assert resp.status_code == 200
    payload = resp.json()
    assert "cortex" in payload
    engines = payload["cortex"].get("engines", [])
    # Ensure at least the fields are present for each engine entry
    for e in engines:
        assert "name" in e
        assert "login_state" in e
        assert "logged_in" in e


def test_cortex_login_endpoint_starts_flow():
    webui = SynthWebUIInterface(autostart=False)
    client = TestClient(webui.app)

    resp = client.post(
        "/api/components/cortex/login", json={"name": "selenium_chatgpt"}
    )
    assert resp.status_code == 422
    assert "no longer supported" in resp.json()["detail"].lower()


def test_cortex_login_endpoint_errors_for_missing_or_non_selenium():
    webui = SynthWebUIInterface(autostart=False)
    client = TestClient(webui.app)

    # Deprecated endpoint returns 422 regardless of engine selection.
    resp = client.post("/api/components/cortex/login", json={"name": "no_such_engine"})
    assert resp.status_code == 422

    # Load a non-selenium engine (manual) and try
    registry = get_cortex_registry()
    try:
        manual = registry.load_engine("manual")
    except Exception:
        manual = None

    if manual:
        resp = client.post("/api/components/cortex/login", json={"name": "manual"})
        assert resp.status_code == 422
