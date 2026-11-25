from fastapi.testclient import TestClient

from core.webui import SynthWebUIInterface


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
