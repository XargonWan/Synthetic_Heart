from starlette.testclient import TestClient
from core.webui import SynthWebUIInterface


def create_client():
    ui = SynthWebUIInterface(autostart=False)
    return TestClient(ui.app)


def test_index_includes_main_js_and_config():
    client = create_client()
    r = client.get("/")
    assert r.status_code == 200
    text = r.text
    assert "/js/main.js" in text
    assert "window.__SYNTH_CONFIG" in text
    assert "RESPONSE_TIMEOUT" in text
    # Accent color runtime config exposed by server
    assert "WEBUI_ACCENT_COLOR" in text
    assert "#6bfefe" in text
    assert "WEBUI_ACCENT_PRESETS" in text


def test_templates_skins_served():
    client = create_client()
    r = client.get("/templates/skins.html")
    assert r.status_code == 200
    assert "skins-grid" in r.text


def test_static_js_files_served():
    client = create_client()
    r = client.get("/js/main.js")
    assert r.status_code == 200
    assert "loadSection" in r.text or "SynthWebUI" in r.text
    r2 = client.get("/js/skins.js")
    assert r2.status_code == 200
    assert "initSkinsTab" in r2.text
