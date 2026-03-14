from core.config_manager import config_registry
from core.model_manager import MODEL_MANAGER
from plugins.auris_engines import vosk_engine


def test_default_model_path_changes_with_language(tmp_path, monkeypatch):
    # monkeypatch config_registry to simulate language settings instead of relying
    # on the database (which may be unavailable in the test container).
    monkeypatch.setattr(
        config_registry,
        "get_value",
        lambda key, default=None, **kw: "it-it" if key == "VOSK_LANGUAGE" else default,
    )
    # New path is MODEL_MANAGER.model_dir("vosk-it-it")
    expected = MODEL_MANAGER.model_dir("vosk-it-it")
    assert vosk_engine._default_model_path() == expected

    monkeypatch.setattr(
        config_registry,
        "get_value",
        lambda key, default=None, **kw: "en-us" if key == "VOSK_LANGUAGE" else default,
    )
    expected2 = MODEL_MANAGER.model_dir("vosk-en-us")
    assert vosk_engine._default_model_path() == expected2


def test_vosk_download_endpoint():
    # create a fresh TestClient
    from fastapi.testclient import TestClient
    from core.webui import SynthWebUIInterface

    webui = SynthWebUIInterface(autostart=False)
    client = TestClient(webui.app)

    # request download for a fake language (use en-us); the endpoint now delegates
    # to MODEL_MANAGER which fires the download in the background and always
    # returns 200 with success=True.
    r = client.post("/api/auris/vosk/download", data={"language": "en-us"})
    assert r.status_code == 200
    payload = r.json()
    assert payload.get("success") is True
    assert payload.get("language") == "en-us"
    assert "model_id" in payload
