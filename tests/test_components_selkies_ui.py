from fastapi.testclient import TestClient

from core.webui import SynthWebUIInterface


def test_main_js_contains_selkies_open_logic():
    path = "res/synth_webui/js/main.js"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "/api/selkies" in content
    assert "selkies_desktop" in content
    assert "window.open(" in content
    # UI button label for Selkies should be a "Login" action (opens login page)
    assert (
        "Login'" in content or 'Login"' in content or "textContent = 'Login'" in content
    )


def test_api_components_includes_selkies_desktop():
    webui = SynthWebUIInterface(autostart=False)
    client = TestClient(webui.app)

    resp = client.get("/api/components")
    assert resp.status_code == 200
    data = resp.json()

    # Find selkies_desktop in the interfaces list
    interfaces = data.get("interfaces") or []
    sel = next((i for i in interfaces if i.get("name") == "selkies_desktop"), None)
    assert sel is not None, "selkies_desktop component must be present"
    assert sel.get("is_external") is True
    # Ensure protocol/port hints are provided server-side
    assert "selkies_protocol" in sel and "selkies_port" in sel
