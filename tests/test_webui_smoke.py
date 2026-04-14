import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import tempfile

import core
import core.message_queue
import core.session_meta as session_meta
from starlette.testclient import TestClient
from core.webui import SynthWebUIInterface
from core import plugin_instance


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


def test_iris_disabled_config_exposed(monkeypatch):
    import core.webui as core_webui

    original_get_value = core_webui.config_registry.get_value

    def fake_get_value(
        key: str,
        default: Any = None,
        value_type: Any = None,
        group: str | None = None,
        component: str | None = None,
    ) -> Any:
        if key == "ACTIVE_IRIS_ENGINE":
            return "disabled"
        return original_get_value(
            key,
            default=default,
            value_type=value_type,
            group=group or "core",
            component=component or "core",
        )

    monkeypatch.setattr(core_webui.config_registry, "get_value", fake_get_value)
    ui = SynthWebUIInterface(autostart=False)
    client = TestClient(ui.app)
    r = client.get("/")
    assert r.status_code == 200
    assert "IRIS_ENABLED" in r.text
    assert "IRIS_ENABLED: false" in r.text


def test_static_js_files_served():
    client = create_client()
    r = client.get("/js/main.js")
    assert r.status_code == 200
    assert "loadSection" in r.text or "SynthWebUI" in r.text
    r2 = client.get("/js/skins.js")
    assert r2.status_code == 200
    assert "initSkinsTab" in r2.text


def test_uploads_route_mounted(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTH_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    ui = SynthWebUIInterface(autostart=False)
    assert ui.attachments_dir == Path(str(tmp_path / "attachments"))
    assert ui.attachments_dir.exists()

    test_file = ui.attachments_dir / "hello.txt"
    test_file.write_text("hello")

    client = TestClient(ui.app)
    r = client.get("/uploads/hello.txt")
    assert r.status_code == 200
    assert r.text == "hello"


def test_attachments_directory_uses_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.delenv("SYNTH_ATTACHMENTS_ROOT", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    ui = SynthWebUIInterface(autostart=False)

    assert ui.attachments_dir == Path(str(tmp_path / "xdg")) / "attachments"
    assert ui.attachments_dir.exists()


def test_chat_attachment_upload_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTH_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    ui = SynthWebUIInterface(autostart=False)
    client = TestClient(ui.app)

    files = {"file": ("hello.txt", b"hello", "text/plain")}
    r = client.post("/api/chat/attachments", files=files)
    assert r.status_code == 200
    data = r.json()
    assert data.get("status") == "ok"
    assert data.get("filename") == "hello.txt"
    assert data.get("url", "").startswith("/uploads/")
    uploaded_name = data["url"].split("/uploads/")[1]
    assert (ui.attachments_dir / uploaded_name).exists()


def test_send_message_forwards_attachments_to_websocket(monkeypatch):
    ui = SynthWebUIInterface(autostart=False)

    sent_payloads: list[dict[str, Any]] = []

    class DummyWebSocket:
        async def send_json(self, payload: dict[str, Any]) -> None:
            sent_payloads.append(payload)

    ui.connections["session1"] = DummyWebSocket()
    metadata = {
        "attachments": [
            {
                "url": "/uploads/test.jpg",
                "filename": "test.jpg",
                "mime_type": "image/jpeg",
                "size": 123,
            }
        ]
    }

    import asyncio

    asyncio.run(ui.send_message("session1", text="Hello", metadata=metadata))

    assert len(sent_payloads) == 1
    payload = sent_payloads[0]
    assert payload["attachments"] == metadata["attachments"]
    assert payload["data"]["attachments"] == metadata["attachments"]


def test_normalize_webui_attachment_local_path(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTH_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    ui = SynthWebUIInterface(autostart=False)
    local_file = ui.attachments_dir / "test.jpg"
    local_file.write_bytes(b"dummy")

    attachment = {
        "url": "/uploads/test.jpg",
        "filename": "test.jpg",
        "mime_type": "image/jpeg",
        "size": 5,
    }
    normalized = ui._normalize_webui_attachment(attachment)

    assert normalized["path"] == str(local_file)
    assert normalized["file_path"] == str(local_file)
    assert normalized["filename"] == "test.jpg"
    assert normalized["mime_type"] == "image/jpeg"
    assert normalized["size"] == 5
    assert normalized["data"] == base64.b64encode(b"dummy").decode("utf-8")


def test_extract_image_data_from_webui_attachment():
    message = SimpleNamespace(
        attachments=[
            {
                "url": "/uploads/test.jpg",
                "filename": "test.jpg",
                "mime_type": "image/jpeg",
                "size": 5,
            }
        ],
        text="",
        caption="",
    )

    image_data, has_trigger = asyncio.run(
        plugin_instance._extract_image_data_from_message(message, "synth_webui")
    )

    assert has_trigger is True
    assert image_data["type"] == "attachment"
    assert image_data["content_type"] == "image/jpeg"
    assert image_data["filename"] == "test.jpg"
    assert image_data["url"] == "/uploads/test.jpg"


def test_extract_multimodal_attachments_from_webui_attachment(tmp_path):
    payload = {
        "url": "/uploads/test.jpg",
        "filename": "test.jpg",
        "mime_type": "image/jpeg",
        "size": 5,
        "data": base64.b64encode(b"dummy").decode("utf-8"),
    }
    message = SimpleNamespace(attachments=[payload])

    attachments = asyncio.run(
        plugin_instance._extract_multimodal_attachments(None, message, "synth_webui")
    )

    assert len(attachments) == 1
    assert attachments[0]["mime_type"] == "image/jpeg"
    assert attachments[0]["data"] == payload["data"]


async def test_handle_user_message_normalizes_webui_attachment(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTH_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    ui = SynthWebUIInterface(autostart=False)
    local_file = ui.attachments_dir / "test.jpg"
    local_file.write_bytes(b"dummy")

    captured: dict[str, Any] = {}

    async def fake_enqueue(
        bot=None,
        message=None,
        context_memory=None,
        priority=None,
        interface_id=None,
        skip_mention_check=None,
        original_message=None,
    ):
        captured["message"] = message or original_message

    monkeypatch.setattr(core.message_queue, "enqueue", fake_enqueue)

    monkeypatch.setattr(
        session_meta,
        "get_session_meta",
        lambda interface_path: None,
    )
    async def dummy_set_session_meta(interface_path, value):
        return None
    monkeypatch.setattr(session_meta, "set_session_meta", dummy_set_session_meta)

    await ui._handle_user_message(
        session_id="session1",
        text="Hello",
        attachments=[
            {
                "url": "/uploads/test.jpg",
                "filename": "test.jpg",
                "mime_type": "image/jpeg",
                "size": 5,
            }
        ],
        is_voice_input=False,
    )

    assert "message" in captured
    assert captured["message"].attachments[0]["path"] == str(local_file)
    assert captured["message"].attachments[0]["file_path"] == str(local_file)


def test_attachments_directory_falls_back_to_temp(monkeypatch):
    monkeypatch.delenv("SYNTH_ATTACHMENTS_ROOT", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    original_mkdir = Path.mkdir

    def fake_mkdir(self, parents=False, exist_ok=False):
        if str(self).startswith("/config"):
            raise PermissionError("Permission denied: '/config/uploads'")
        return original_mkdir(self, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)

    ui = SynthWebUIInterface(autostart=False)

    assert "/config/uploads" not in str(ui.attachments_dir)
    assert ui.attachments_dir.exists()
    assert str(ui.attachments_dir).startswith(tempfile.gettempdir())
