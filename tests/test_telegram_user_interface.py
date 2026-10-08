from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

try:
    import interface.telegram as tguser
    from interface.telegram import _compat
except Exception:
    pytest.skip(
        "telethon / python-telegram-bot not installed; skipping telegram user tests",
        allow_module_level=True,
    )

from telethon import errors as tl_errors


def _iface(bot: Any = None) -> Any:
    iface = tguser.TelegramUserInterface()
    iface.bot = bot or SimpleNamespace()
    return iface


# --- error translation (feeds the shared stale-identifier ladder) -------------


def test_flood_wait_becomes_retry_after() -> None:
    exc = _compat.translate_error(tl_errors.FloodWaitError(request=None, capture=7))
    assert isinstance(exc, _compat.RetryAfter)
    assert getattr(exc, "retry_after", None) == 7


@pytest.mark.parametrize(
    ("rpc_name", "wording"),
    [
        ("MsgIdInvalidError", "replied not found"),
        ("PeerIdInvalidError", "chat not found"),
        ("MessageTooLongError", "too long"),
    ],
)
def test_rpc_errors_use_bot_api_wording(rpc_name: str, wording: str) -> None:
    exc = _compat.translate_error(getattr(tl_errors, rpc_name)(request=None))
    assert isinstance(exc, _compat.BadRequest)
    assert wording in str(exc).lower()


def test_unknown_peer_value_error_is_chat_not_found() -> None:
    exc = _compat.translate_error(ValueError("Could not find the input entity for 5"))
    assert isinstance(exc, _compat.BadRequest)
    assert "chat not found" in str(exc).lower()


def test_legacy_markdown_conversion() -> None:
    assert _compat.legacy_markdown_to_telethon("*waves* hi _quietly_") == (
        "**waves** hi __quietly__"
    )
    assert _compat.legacy_markdown_to_telethon("snake_case_name") == "snake_case_name"


# --- registration / routing tables --------------------------------------------


def test_interface_identity_and_schema() -> None:
    iface = _iface()
    assert iface.get_interface_id() == "telegram"
    assert "send_message" in iface.get_supported_actions()
    assert iface.display_name != "Telegram Bot"


def test_telegram_is_not_a_legacy_alias_of_telegram_bot() -> None:
    from core import message_chain
    from interface import message_send_utils

    assert "telegram" not in message_send_utils._INTERFACE_DISPLAY_ALIASES
    assert message_chain._INTERFACE_TO_MESSAGE_ACTION["telegram"] == "send_message"
    assert message_chain._INTERFACE_TO_MESSAGE_ACTION["telegram_bot"] == "send_message"


def test_validate_payload_coerces_voice_flag_and_requires_content() -> None:
    iface = _iface()
    assert iface.validate_payload("send_message", {})
    payload = {"text": "hi", "send_as_voice": "true"}
    assert iface.validate_payload("send_message", payload) == []
    assert payload["send_as_voice"] is True


# --- message building -----------------------------------------------------------


def test_thread_of_forum_topic_message() -> None:
    reply = SimpleNamespace(forum_topic=True, reply_to_top_id=None, reply_to_msg_id=55)
    assert _compat.thread_of(SimpleNamespace(reply_to=reply)) == (55, True)
    inside = SimpleNamespace(forum_topic=True, reply_to_top_id=55, reply_to_msg_id=90)
    assert _compat.thread_of(SimpleNamespace(reply_to=inside)) == (55, True)
    plain_reply = SimpleNamespace(
        forum_topic=False, reply_to_top_id=12, reply_to_msg_id=13
    )
    assert _compat.thread_of(SimpleNamespace(reply_to=plain_reply)) == (None, False)


# --- send_message ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_routes_through_thread_fallback(monkeypatch) -> None:
    iface = _iface()
    sender = AsyncMock(return_value=SimpleNamespace(message_id=1))
    monkeypatch.setattr(tguser, "resolve_and_touch", AsyncMock())
    monkeypatch.setattr(tguser, "send_with_thread_fallback", sender)

    original = SimpleNamespace(chat_id=-100123, message_id=77, thread_id=9)
    ok = await iface.send_message(
        {"text": "hello", "interface_path": "telegram/-100123", "skip_history": True},
        original,
    )

    assert ok is True
    kwargs = sender.await_args.kwargs
    assert sender.await_args.args[1] == "-100123"
    assert kwargs["reply_to_message_id"] == 77
    assert kwargs["thread_id"] == 9
    # Groups get no parse_mode (peer-instance asterisk rule) and a telegram/ path.
    assert kwargs["parse_mode"] is None
    assert kwargs["interface_path"] == "telegram/-100123"


@pytest.mark.asyncio
async def test_send_message_ignores_reply_to_holding_the_chat_id(monkeypatch) -> None:
    iface = _iface()
    sender = AsyncMock(return_value=SimpleNamespace(message_id=1))
    monkeypatch.setattr(tguser, "resolve_and_touch", AsyncMock())
    monkeypatch.setattr(tguser, "send_with_thread_fallback", sender)

    original = SimpleNamespace(chat_id=5208932647, message_id=31)
    await iface.send_message(
        {
            "text": "hi",
            "interface_path": "telegram/5208932647",
            "reply_to": "5208932647",
            "skip_history": True,
        },
        original,
    )

    assert sender.await_args.kwargs["reply_to_message_id"] == 31


@pytest.mark.asyncio
async def test_send_message_bad_request_records_failure_and_notifies(
    monkeypatch,
) -> None:
    iface = _iface()
    notify = AsyncMock()
    record = AsyncMock()
    monkeypatch.setattr(tguser, "resolve_and_touch", AsyncMock())
    monkeypatch.setattr(
        tguser,
        "send_with_thread_fallback",
        AsyncMock(side_effect=_compat.BadRequest("Chat not found")),
    )
    monkeypatch.setattr(tguser, "_record_telegram_delivery_failure", record)
    monkeypatch.setattr(
        "core.transport_layer.notify_corrector_of_system_message", notify
    )

    ok = await iface.send_message({"text": "x", "interface_path": "telegram/1"})

    assert ok is False
    record.assert_awaited_once()
    assert notify.await_args.kwargs["interface"] == "telegram"


@pytest.mark.asyncio
async def test_send_message_without_destination_is_ignored() -> None:
    assert await _iface().send_message({"text": "synthetic"}) is True


# --- login flow -----------------------------------------------------------------------


class _FakeClient:
    def __init__(self, *, needs_password: bool = False) -> None:
        self.needs_password = needs_password
        self.session = object()
        self.disconnected = False

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        self.disconnected = True

    async def send_code_request(self, phone: str) -> Any:
        return SimpleNamespace(phone_code_hash="hash")

    async def sign_in(self, **kwargs: Any) -> None:
        if "code" in kwargs and self.needs_password:
            raise tl_errors.SessionPasswordNeededError(request=None)


@pytest.fixture
def login(monkeypatch):
    from interface.telegram import _login

    flow = _login.LoginFlow()
    made: list[_FakeClient] = []

    def factory(needs_password: bool) -> None:
        def _make(*_a: Any, **_k: Any) -> _FakeClient:
            client = _FakeClient(needs_password=needs_password)
            made.append(client)
            return client

        monkeypatch.setattr("telethon.TelegramClient", _make)

    monkeypatch.setattr(
        "telethon.sessions.StringSession.save", staticmethod(lambda _s: "SESSION")
    )
    saved: dict[str, Any] = {}

    async def fake_set_value(key: str, value: Any, **_k: Any) -> None:
        saved[key] = value

    monkeypatch.setattr("core.config_manager.config_registry.set_value", fake_set_value)
    return flow, factory, saved, made


@pytest.mark.asyncio
async def test_login_without_2fa_stores_session(login) -> None:
    flow, factory, saved, _made = login
    factory(False)
    snap = await flow.send_code(1, "hash", "+391234567890")
    assert snap["state"] == "code_sent"
    assert "1234567" not in str(snap["phone"])  # masked
    snap = await flow.verify_code("12345")
    assert snap["state"] == "authorized"
    assert saved["TELEGRAM_SESSION"] == "SESSION"


@pytest.mark.asyncio
async def test_login_with_2fa_requires_password(login) -> None:
    flow, factory, saved, _made = login
    factory(True)
    await flow.send_code(1, "hash", "+391234567890")
    assert (await flow.verify_code("12345"))["state"] == "password_needed"
    assert "TELEGRAM_SESSION" not in saved
    assert (await flow.verify_password("pw"))["state"] == "authorized"
    assert saved["TELEGRAM_SESSION"] == "SESSION"


@pytest.mark.asyncio
async def test_verify_code_without_request_is_rejected(login) -> None:
    flow, _factory, saved, _made = login
    snap = await flow.verify_code("1")
    assert snap["state"] == "idle" and snap["error"]
    assert saved == {}


# --- WebUI route auth ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_routes_enforce_api_token(monkeypatch) -> None:
    from interface.telegram import webui_login

    monkeypatch.setenv("SYNTH_WEBUI_API_TOKEN", "secret")
    anonymous = SimpleNamespace(headers={}, query_params={})
    resp = await webui_login._route_status(anonymous)
    assert getattr(resp, "status_code", None) == 401

    ok = SimpleNamespace(headers={"authorization": "Bearer secret"}, query_params={})
    monkeypatch.setattr(webui_login, "_status", lambda: {"connected": False})
    assert await webui_login._route_status(ok) == {"connected": False}
