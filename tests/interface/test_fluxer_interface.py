import asyncio

import pytest

from interface import fluxer_interface as fi


def _no_loop(monkeypatch):
    """Prevent the constructor from scheduling a background start()."""
    monkeypatch.setattr(
        asyncio, "get_running_loop", lambda: (_ for _ in ()).throw(RuntimeError())
    )


def _make_interface(monkeypatch, token="Bot secret-token"):
    _no_loop(monkeypatch)
    return fi.FluxerInterface(
        token,
        api_base="https://api.example.com/v{v}",
        gateway_url="wss://gw.example.com/?v=1&encoding=json",
        api_version=1,
    )


def test_resolve_api_base_substitutes_version():
    assert (
        fi._resolve_api_base("https://api.example.com/v{v}", 2)
        == "https://api.example.com/v2"
    )
    assert (
        fi._resolve_api_base("https://api.example.com/v{v}/", "3")
        == "https://api.example.com/v3"
    )


def test_rest_auth_header_prefixes_bot_token():
    assert fi._rest_auth_header("abc") == "Bot abc"
    assert fi._rest_auth_header("Bot abc") == "Bot abc"
    assert fi._rest_auth_header("flx_userthing") == "flx_userthing"


def test_gateway_token_strips_bot_prefix():
    assert fi._gateway_token("Bot abc") == "abc"
    assert fi._gateway_token("abc") == "abc"
    assert fi._gateway_token("flx_userthing") == "flx_userthing"


def test_channel_from_interface_path():
    fn = fi.FluxerInterface._channel_from_interface_path
    assert fn("fluxer_bot/123/456") == "456"
    assert fn("fluxer_bot/123/456/789") == "456"
    assert fn("fluxer_bot/999") == "999"
    assert fn("telegram_bot/1/2") is None
    assert fn(None) is None


def test_interface_initializes_and_registers(monkeypatch):
    inst = _make_interface(monkeypatch)
    assert inst.is_enabled is True
    assert inst.api_base == "https://api.example.com/v1"
    assert fi.FluxerInterface.get_interface_id() == "fluxer_bot"


def test_supported_actions_present_when_enabled(monkeypatch):
    _make_interface(monkeypatch)
    actions = fi.FluxerInterface.get_supported_actions()
    assert fi.ACTION_TYPE in actions
    assert fi.FILE_ACTION_TYPE in actions
    assert "text" in actions[fi.ACTION_TYPE]["required_fields"]
    assert "path" in actions[fi.FILE_ACTION_TYPE]["required_fields"]
    assert actions[fi.FILE_ACTION_TYPE]["security_level"] == "medium"
    assert actions[fi.FILE_ACTION_TYPE]["external_effects"] == ["filesystem"]


def test_validate_payload_message(monkeypatch):
    _make_interface(monkeypatch)
    # missing target
    errs = fi.FluxerInterface.validate_payload(fi.ACTION_TYPE, {"text": "hi"})
    assert any("channel_id" in e for e in errs)
    # missing text
    errs = fi.FluxerInterface.validate_payload(fi.ACTION_TYPE, {"channel_id": "1"})
    assert any("text" in e for e in errs)
    # valid
    errs = fi.FluxerInterface.validate_payload(
        fi.ACTION_TYPE, {"channel_id": "1", "text": "hi"}
    )
    assert errs == []


def test_validate_payload_file(monkeypatch):
    _make_interface(monkeypatch)
    errs = fi.FluxerInterface.validate_payload(
        fi.FILE_ACTION_TYPE, {"interface_path": "fluxer_bot/1/2"}
    )
    assert any("path" in e for e in errs)
    errs = fi.FluxerInterface.validate_payload(
        fi.FILE_ACTION_TYPE, {"channel_id": "2", "path": "/app/x.txt"}
    )
    assert errs == []


@pytest.mark.asyncio
async def test_inbound_message_enqueues(monkeypatch):
    inst = _make_interface(monkeypatch)
    inst._self_user_id = "self-id"

    async def _fake_add(*a, **k):
        return None

    import core.chat_context_manager as ccm

    monkeypatch.setattr(ccm, "add_message_to_context", _fake_add)

    async def _fake_touch(*a, **k):
        return None

    monkeypatch.setattr(fi, "resolve_and_touch", _fake_touch)

    captured = []

    async def _fake_enqueue(bot, wrapped, interface_id=None, **k):
        captured.append((bot, wrapped, interface_id))

    monkeypatch.setattr(fi.message_queue, "enqueue", _fake_enqueue)

    data = {
        "message": {
            "id": "m1",
            "channel_id": "456",
            "guild_id": "123",
            "author": {"id": "user-2", "username": "alice"},
            "content": "hello synth",
        }
    }
    await inst._on_message(data)

    assert len(captured) == 1
    bot, wrapped, interface_id = captured[0]
    assert interface_id == "fluxer_bot"
    assert wrapped.interface_path == "fluxer_bot/123/456"
    assert wrapped.text == "hello synth"
    assert wrapped.chat_id == "456"


@pytest.mark.asyncio
async def test_inbound_ignores_self_message(monkeypatch):
    inst = _make_interface(monkeypatch)
    inst._self_user_id = "self-id"

    captured = []

    async def _fake_enqueue(bot, wrapped, interface_id=None, **k):
        captured.append(wrapped)

    monkeypatch.setattr(fi.message_queue, "enqueue", _fake_enqueue)

    data = {
        "message": {
            "id": "m1",
            "channel_id": "456",
            "author": {"id": "self-id", "username": "synth"},
            "content": "hi",
        }
    }
    await inst._on_message(data)
    assert captured == []


@pytest.mark.asyncio
async def test_send_message_via_payload_dict(monkeypatch):
    inst = _make_interface(monkeypatch)

    sent = []

    class _FakeRest:
        async def send_message(self, channel_id, content, reply_to_message_id=None):
            sent.append((channel_id, content, reply_to_message_id))
            return {"id": "m2"}

    inst._rest = _FakeRest()

    async def _fake_save(*a, **k):
        return None

    import core.chat_context_manager as ccm

    monkeypatch.setattr(ccm, "save_response_message", _fake_save)

    ok = await inst.send_message(
        {"interface_path": "fluxer_bot/123/456", "text": "reply text"}
    )
    assert ok is True
    assert sent == [("456", "reply text", None)]


@pytest.mark.asyncio
async def test_send_message_requires_channel(monkeypatch):
    inst = _make_interface(monkeypatch)

    class _FakeRest:
        async def send_message(self, *a, **k):
            return {"id": "x"}

    inst._rest = _FakeRest()
    ok = await inst.send_message({"text": "no channel here"})
    assert ok is False


@pytest.mark.asyncio
async def test_execute_action_send_file(monkeypatch, tmp_path):
    inst = _make_interface(monkeypatch)

    f = tmp_path / "doc.txt"
    f.write_text("hello")

    def _fake_resolve(raw):
        return (f, None)

    import core.outbound_file_utils as ofu

    monkeypatch.setattr(ofu, "resolve_safe_outbound_path", _fake_resolve)

    uploaded = []

    class _FakeRest:
        async def send_file(self, channel_id, path, caption=None, mime_type=None):
            uploaded.append((channel_id, path, caption))
            return {"id": "f1"}

    inst._rest = _FakeRest()

    result = await inst.execute_action(
        {
            "type": fi.FILE_ACTION_TYPE,
            "payload": {
                "interface_path": "fluxer_bot/123/456",
                "path": str(f),
                "caption": "here",
            },
        }
    )
    assert result["status"] == "success"
    assert uploaded[0][0] == "456"
    assert uploaded[0][2] == "here"


@pytest.mark.asyncio
async def test_execute_action_rejects_bad_path(monkeypatch):
    inst = _make_interface(monkeypatch)

    def _fake_resolve(raw):
        return (None, "path escapes sandbox")

    import core.outbound_file_utils as ofu

    monkeypatch.setattr(ofu, "resolve_safe_outbound_path", _fake_resolve)

    result = await inst.execute_action(
        {
            "type": fi.FILE_ACTION_TYPE,
            "payload": {"channel_id": "456", "path": "../etc/passwd"},
        }
    )
    assert result["status"] == "failed"
