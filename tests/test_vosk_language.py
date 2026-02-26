import os
from pathlib import Path

import pytest

from core.config_manager import config_registry
from plugins.auris_engines import vosk_engine


def test_default_model_path_changes_with_language(tmp_path, monkeypatch):
    # ensure config value is used for default path
    config_registry.set_value("VOSK_LANGUAGE", "it-it")
    expected = Path.home() / ".cache" / "vosk" / "vosk-model-small-it-it"
    assert vosk_engine._default_model_path() == expected

    config_registry.set_value("VOSK_LANGUAGE", "en-us")
    expected2 = Path.home() / ".cache" / "vosk" / "vosk-model-small-en-us"
    assert vosk_engine._default_model_path() == expected2


def test_vosk_download_endpoint():
    # create a fresh TestClient
    from fastapi.testclient import TestClient
    from core.webui import SynthWebUIInterface

    webui = SynthWebUIInterface(autostart=False)
    client = TestClient(webui.app)

    # request download for a fake language (use en-us to avoid large download)
    r = client.post("/api/auris/vosk/download", data={"language": "en-us"})
    assert r.status_code == 200
    payload = r.json()
    assert payload.get("success") is True
    assert payload.get("language") == "en-us"
    # config should be updated
    assert config_registry.get_value("VOSK_LANGUAGE") == "en-us"
