import pytest
from pathlib import Path

from core.webui import SynthWebUIInterface
from core.config_manager import config_registry
from fastapi.websockets import WebSocketDisconnect


def test_multisession_var_registered():
    from core.config_manager import config_registry

    assert "MULTI_SESSION" in config_registry._definitions
    defn = config_registry._definitions["MULTI_SESSION"]
    assert defn.component == "webui"
    assert defn.value_type is bool
    assert defn.advanced is True


class FakeWS:
    def __init__(self):
        self.sent = []
        # mimic minimal attributes
        self.client = None

    async def accept(self):
        pass

    async def send_json(self, payload):
        self.sent.append(payload)

    async def receive_text(self):
        # immediately close the connection
        raise WebSocketDisconnect()


@pytest.mark.asyncio
async def test_single_session_persistence(tmp_path, monkeypatch):
    # ensure flag is off
    await config_registry.set_value("MULTI_SESSION", False)
    calls = []

    def fake_ensure(self, force_write=False):
        calls.append(force_write)

    # patch class method before instantiation so __init__ uses stub
    monkeypatch.setattr(
        SynthWebUIInterface, "_ensure_persistent_session_id", fake_ensure
    )
    webui = SynthWebUIInterface(autostart=False)
    # point session file to temp directory to avoid touching repo
    webui.session_id_file = tmp_path / "webui_session_id.txt"

    ws1 = FakeWS()
    await webui.websocket_endpoint(ws1)  # type: ignore[arg-type]
    assert ws1.sent, "session handshake should be sent"
    assert ws1.sent[0]["type"] == "session"
    sid1 = ws1.sent[0]["session_id"]
    assert webui.session_id == sid1
    assert calls, "persistence should have been attempted"

    # second connection should reuse same id
    ws2 = FakeWS()
    await webui.websocket_endpoint(ws2)  # type: ignore[arg-type]
    sid2 = ws2.sent[0]["session_id"]
    assert sid1 == sid2


@pytest.mark.asyncio
async def test_multi_session_flag(tmp_path, monkeypatch):
    await config_registry.set_value("MULTI_SESSION", True)
    webui = SynthWebUIInterface(autostart=False)
    # ensure persistence method not called at all
    called = []
    monkeypatch.setattr(
        webui,
        "_ensure_persistent_session_id",
        lambda force_write=False: called.append(True),
    )

    # path should still exist but we won't read/write it; just ensure it's not created
    webui.session_id_file = tmp_path / "webui_session_id.txt"

    ws1 = FakeWS()
    await webui.websocket_endpoint(ws1)  # type: ignore[arg-type]
    assert ws1.sent and ws1.sent[0]["type"] == "session"
    sid1 = ws1.sent[0]["session_id"]
    assert sid1 != "", "session id must be non-empty"
    assert webui.session_id is None or webui.session_id == sid1

    ws2 = FakeWS()
    await webui.websocket_endpoint(ws2)  # type: ignore[arg-type]
    sid2 = ws2.sent[0]["session_id"]
    assert sid1 != sid2, "two connections should receive different ids"
    assert not called, "persistence should not be invoked when MULTI_SESSION is true"
    assert not (webui.session_id_file.exists()), "session file should not be written"


def test_template_includes_multisession_placeholder():
    path = (
        Path(__file__).parent.parent
        / "core"
        / "webui_templates"
        / "synth_webui_shell.html"
    )
    content = path.read_text(encoding="utf-8")
    assert "%%MULTI_SESSION%%" in content


def test_ws_closure_notification_present():
    # sanity-check that the base template includes the user notification text we added
    path = Path(__file__).parent.parent / "core" / "webui_templates" / "base.html"
    content = path.read_text(encoding="utf-8")
    assert "Connection lost" in content
    assert "Chat WebSocket error" in content


@pytest.mark.asyncio
async def test_disconnect_cleans_up(tmp_path, monkeypatch):
    # simulate a websocket that sends one message then drops connection
    await config_registry.set_value("MULTI_SESSION", False)
    webui = SynthWebUIInterface(autostart=False)
    webui.session_id_file = tmp_path / "webui_session_id.txt"

    class FlakyWS:
        def __init__(self):
            self.sent = []
            self._msgs = ["hello"]

        async def accept(self):
            pass

        async def send_json(self, payload):
            self.sent.append(payload)

        async def receive_text(self):
            if self._msgs:
                return self._msgs.pop(0)
            raise WebSocketDisconnect()

    ws = FlakyWS()
    await webui.websocket_endpoint(ws)  # type: ignore[arg-type]
    # after the endpoint returns, connection/session should be removed
    assert webui.connections == {}
    assert webui.message_history == {}


@pytest.mark.asyncio
async def test_render_index_reflects_flag(tmp_path):
    # verify that rendered HTML shows true/false based on config
    webui = SynthWebUIInterface(autostart=False)
    # monkeypatch storage path to avoid side effects
    webui.session_id_file = tmp_path / "webui_session_id.txt"

    # default (false)
    await config_registry.set_value("MULTI_SESSION", False)
    html_false = webui._render_index()
    assert "MULTI_SESSION: 'false' === 'true'" in html_false

    # true
    await config_registry.set_value("MULTI_SESSION", True)
    html_true = webui._render_index()
    assert "MULTI_SESSION: 'true' === 'true'" in html_true
