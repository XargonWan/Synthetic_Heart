"""The Telegram user-account extension, end to end against a fake Telethon.

The extension runs as a real sandboxed process; the Core side is the real gateway,
interface API, panel API, call API and media bridge. ``tests/support/fake_telethon.py``
puts a fake ``telethon`` into the extension's ``lib/`` and is driven through files.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
from datetime import datetime, timedelta, timezone
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from core.extensions.api import register_core_api
from core.extensions.avatar import register_avatar_api
from core.extensions.calls import CallPorts, register_call_api
from core.extensions.gateway import Gateway
from core.extensions.grants import MemoryGrantStore
from core.extensions.interfaces import (
    InterfacePorts,
    RemoteInterface,
    register_interface_api,
)
from core.extensions.launcher import Launcher
from core.extensions.manifest import load_manifest
from core.extensions.media import MediaBridge
from core.extensions.memory_ports import (
    MemoryConfig,
    MemoryInterfaceHost,
    MemoryLog,
    MemoryMessaging,
    MemoryStorage,
    memory_ports,
)
from core.extensions.panels import PanelStore, register_panel_api, run_action
from core.extensions.sandbox import RuntimeLayout, SandboxPolicy
from core.extensions.supervisor import RestartPolicy, Supervisor
from tests.support import fake_telethon
from tests.test_extensions_calls import FakePipeline, FakeVideo
from tests.test_extensions_runtime import Harness

SOURCE = Path(__file__).resolve().parent.parent / "extensions_src" / "telegram"
EXT_ID = "official/telegram"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
ALICE = {"id": 42, "first_name": "Alice", "last_name": "Smith", "username": "alice_x"}
PRIVATE = {"type": "user", **ALICE}
GROUP = {
    "type": "group",
    "id": -100123,
    "title": "Dev Room",
    "username": None,
    "forum": True,
}


class Rig(SimpleNamespace):
    harness: Harness
    host: MemoryInterfaceHost
    messaging: MemoryMessaging
    panels: PanelStore
    control: fake_telethon.Control
    outgoing: Path


@pytest_asyncio.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    ports, grants = memory_ports(), MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    host, messaging = MemoryInterfaceHost(), MemoryMessaging()
    layout = RuntimeLayout(tmp_path / "extensions")
    outgoing = tmp_path / "outgoing"
    outgoing.mkdir()

    def resolve(raw: str) -> tuple[Path | None, str | None]:
        path = Path(raw).resolve()
        if outgoing not in path.parents:
            return None, "Path is outside allowed roots"
        return (path, None) if path.is_file() else (None, "File does not exist")

    media = MediaBridge(
        data_dir=layout.data_dir,
        uid=lambda ext: None,
        inbound_root=tmp_path / "media",
        resolve_outbound=resolve,
    )
    avatar_state: dict[str, Any] = {"value": (PNG, "v1")}
    register_interface_api(gateway, InterfacePorts(host, messaging, media))
    register_avatar_api(gateway, lambda: avatar_state["value"])
    panels = PanelStore()
    register_panel_api(gateway, panels)
    manager_module = __import__("core.call_manager", fromlist=["CallManager"])
    register_call_api(
        gateway,
        CallPorts(
            manager=lambda: manager_module.CallManager(),
            pipeline=lambda: FakePipeline(),
            video=lambda: FakeVideo(),
            jpeg_to_i420=lambda j, w, h: b"",
            video_settings=lambda: SimpleNamespace(fps=10, width=4, height=2),
        ),
        messaging,
        media,
    )
    launcher = Launcher(gateway, layout, SandboxPolicy(drop_privileges=False))
    supervisor = Supervisor(
        launcher,
        RestartPolicy(
            initial_delay=0.05,
            max_delay=0.1,
            max_failures=3,
            hello_timeout=10.0,
            stop_grace=1.0,
            term_wait=0.5,
        ),  # fmt: skip
    )
    harness = Harness(layout, gateway, grants, ports, launcher, supervisor)
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    made = Rig(
        harness=harness,
        host=host,
        messaging=messaging,
        panels=panels,
        control=fake_telethon.Control(control_dir),
        outgoing=outgoing,
    )
    try:
        yield made
    finally:
        await supervisor.stop_all()
        await gateway.close_all()


def install(rig: Rig, lib_root: Path, *, session: bool = True, **config: Any) -> None:
    h = rig.harness
    manifest = load_manifest(SOURCE / "synth-extension.toml")
    assert manifest.id == EXT_ID
    directory = h.layout.install_dir(EXT_ID)
    directory.mkdir(parents=True)
    for name in ("ext_main.py", "convert.py", "tl.py", "login.py", "calls.py"):
        shutil.copy(SOURCE / name, directory / name)
    lib = directory / "lib"
    fake_telethon.install(lib, rig.control.dir)
    h.grants.grant(EXT_ID, *manifest.permissions.capabilities)
    config_port = h.ports.config
    assert isinstance(config_port, MemoryConfig)
    config_port.values[EXT_ID] = {
        "TELEGRAM_API_ID": "123456",
        "TELEGRAM_API_HASH": "abcdef",
        "TELEGRAM_CALLS_ENABLED": False,
        **config,
    }
    if session:
        storage = h.ports.storage
        assert isinstance(storage, MemoryStorage)
        storage._data.setdefault(EXT_ID, {})["session"] = json.dumps("SESSION:alice")  # noqa: SLF001
    h.supervisor.register(manifest)


async def start(rig: Rig, tmp_path: Path, **kw: Any) -> RemoteInterface:
    install(rig, tmp_path, **kw)
    await rig.harness.supervisor.start(EXT_ID)
    await wait_for(
        lambda: "telegram" in rig.host.interfaces, what="registered", rig=rig
    )
    remote = rig.host.interfaces["telegram"]
    assert isinstance(remote, RemoteInterface)
    return remote


async def connected(rig: Rig, tmp_path: Path, **kw: Any) -> RemoteInterface:
    remote = await start(rig, tmp_path, **kw)
    await wait_for(lambda: current(rig).is_enabled, what="enabled", rig=rig)
    return remote


async def wait_for(
    predicate: Any, timeout: float = 15.0, what: str = "", rig: Rig | None = None
) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.02)

    try:
        await asyncio.wait_for(poll(), timeout)
    except TimeoutError:
        extra = ""
        if rig is not None:
            log = rig.harness.ports.log
            extra = f" status={rig.harness.supervisor.status()} log={getattr(log, 'records', [])[-4:]}"
        pytest.fail(f"timed out waiting for {what}.{extra}")


def message(
    text: str,
    *,
    chat: dict[str, Any] = PRIVATE,
    sender: dict[str, Any] = PRIVATE,
    **extra: Any,
) -> dict[str, Any]:
    return {"chat": chat, "sender": sender, "text": text, **extra}


def sent(rig: Rig) -> list[dict[str, Any]]:
    return rig.control.calls("send_message")


def current(rig: Rig) -> RemoteInterface:
    """The interface as registered *now* (it is registered again once connected)."""
    remote = rig.host.interfaces["telegram"]
    assert isinstance(remote, RemoteInterface)
    return remote


class TestLoginAndPanel:
    @pytest.mark.asyncio
    async def test_without_credentials_the_interface_waits_disabled_with_a_hint(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        remote = await start(
            rig, tmp_path, session=False, TELEGRAM_API_ID="", TELEGRAM_API_HASH=""
        )
        await wait_for(
            lambda: rig.panels.for_interface("telegram"), what="panel", rig=rig
        )
        assert (
            remote.is_enabled is False
            and "login" in str(remote.disabled_reason).lower()
        )
        [entry] = rig.panels.for_interface("telegram")
        assert "API ID" in entry["state"]["blocks"][0]["text"]
        assert rig.control.calls("connect") == []
        assert (
            await remote.send_message({"text": "x", "interface_path": "telegram/1"})
            is False
        )

    @pytest.mark.asyncio
    async def test_the_whole_login_through_the_panel_then_connected(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await start(rig, tmp_path, session=False)
        await wait_for(
            lambda: rig.panels.for_interface("telegram"), what="panel", rig=rig
        )
        gw = rig.harness.gateway

        def blocks() -> list[dict[str, Any]]:
            return rig.panels.for_interface("telegram")[0]["state"]["blocks"]

        assert blocks()[0]["type"] == "form" and blocks()[0]["action"] == "send_code"
        result = await run_action(
            gw, EXT_ID, "telegram", "login", "send_code", {"phone": "+391234567890"}
        )
        assert result["ok"] is True
        assert rig.control.calls("send_code_request")[0]["phone"] == "+391234567890"
        assert any(
            b["type"] == "form" and b["action"] == "verify_code" for b in blocks()
        )
        assert (
            "+39" in blocks()[0]["text"] and "1234567890" not in blocks()[0]["text"]
        )  # masked

        wrong = await run_action(
            gw, EXT_ID, "telegram", "login", "verify_code", {"code": "00000"}
        )
        assert wrong == {"ok": False, "error": "The code is wrong."}
        assert blocks()[0]["type"] == "error"

        right = await run_action(
            gw, EXT_ID, "telegram", "login", "verify_code", {"code": "12345"}
        )
        assert right["ok"] is True
        await wait_for(
            lambda: current(rig).is_enabled, what="connected after login", rig=rig
        )
        storage = rig.harness.ports.storage
        assert isinstance(storage, MemoryStorage)
        assert json.loads(storage._data[EXT_ID]["session"]) == "SESSION:alice"  # noqa: SLF001
        await wait_for(
            lambda: any(b.get("text", "").startswith("Connected as") for b in blocks()),
            what="connected panel",
            rig=rig,
        )

    @pytest.mark.asyncio
    async def test_two_step_verification_asks_for_the_password(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        rig.control.state["two_step"] = True
        rig.control.write_state()
        await start(rig, tmp_path, session=False)
        await wait_for(
            lambda: rig.panels.for_interface("telegram"), what="panel", rig=rig
        )
        gw = rig.harness.gateway
        await run_action(
            gw, EXT_ID, "telegram", "login", "send_code", {"phone": "+391"}
        )
        await run_action(
            gw, EXT_ID, "telegram", "login", "verify_code", {"code": "12345"}
        )
        form = next(
            b
            for b in rig.panels.for_interface("telegram")[0]["state"]["blocks"]
            if b["type"] == "form"
        )
        assert (
            form["action"] == "verify_password"
            and form["fields"][0]["kind"] == "password"
        )
        bad = await run_action(
            gw, EXT_ID, "telegram", "login", "verify_password", {"password": "nope"}
        )
        assert bad["ok"] is False
        good = await run_action(
            gw, EXT_ID, "telegram", "login", "verify_password", {"password": "pw"}
        )
        assert good["ok"] is True
        await wait_for(lambda: current(rig).is_enabled, what="connected", rig=rig)

    @pytest.mark.asyncio
    async def test_secrets_never_reach_the_log_or_the_panel(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await start(rig, tmp_path, session=False)
        await wait_for(
            lambda: rig.panels.for_interface("telegram"), what="panel", rig=rig
        )
        gw = rig.harness.gateway
        await run_action(
            gw, EXT_ID, "telegram", "login", "send_code", {"phone": "+391234567890"}
        )
        await run_action(
            gw, EXT_ID, "telegram", "login", "verify_code", {"code": "99999"}
        )
        log = rig.harness.ports.log
        assert isinstance(log, MemoryLog)
        dump = str(rig.panels.states) + str(log.records)
        assert "99999" not in dump and "1234567890" not in dump

    @pytest.mark.asyncio
    async def test_logout_forgets_the_session_and_disables_the_interface(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path)
        await run_action(rig.harness.gateway, EXT_ID, "telegram", "login", "logout", {})
        await wait_for(lambda: not current(rig).is_enabled, what="disabled", rig=rig)
        storage = rig.harness.ports.storage
        assert isinstance(storage, MemoryStorage)
        assert "session" not in storage._data.get(EXT_ID, {})  # noqa: SLF001
        assert any(c["method"] == "disconnect" for c in rig.control.calls())

    @pytest.mark.asyncio
    async def test_an_expired_session_is_reported_not_retried(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        rig.control.state["authorized"] = False
        rig.control.write_state()
        await start(rig, tmp_path)
        await wait_for(
            lambda: "expired" in str(current(rig).disabled_reason),
            what="expired",
            rig=rig,
        )
        assert current(rig).is_enabled is False
        assert rig.harness.supervisor.status()[EXT_ID]["restarts"] == 0


class TestInbound:
    @pytest.mark.asyncio
    async def test_a_private_message_reaches_the_core_with_who_said_it(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path)
        rig.control.deliver(
            **message("hello there", id=9, date=datetime.now(timezone.utc).isoformat())
        )
        await wait_for(lambda: rig.messaging.enqueued, what="a queued message", rig=rig)
        item = rig.messaging.enqueued[0]
        wrapped = item["message"]
        assert item["interface_id"] == "telegram"
        assert (wrapped.interface_path, wrapped.chat_id, wrapped.message_id) == (
            "telegram/42",
            "42",
            "9",
        )
        assert wrapped.chat.type == "private" and wrapped.thread_id is None
        assert (
            wrapped.from_user.username == "alice_x"
            and wrapped.from_user.full_name == "Alice Smith"
        )
        assert [i for i, _ in rig.messaging.aliases] == ["telegram"]
        assert rig.messaging.recorded[0]["sender_name"] == "Alice Smith"
        assert not hasattr(wrapped, "is_voice_input")

    @pytest.mark.asyncio
    async def test_a_forum_topic_is_the_thread_and_a_plain_topic_message_is_no_reply(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path)
        rig.control.deliver(
            **message(
                "status?", chat=GROUP, date=datetime.now(timezone.utc).isoformat(),
                reply_to={"msg_id": 77, "forum_topic": True},
                replied={"id": 77, "chat": GROUP, "sender": PRIVATE, "text": "topic opener", "date": "2026-01-01T00:00:00+00:00"},
            )
        )  # fmt: skip
        await wait_for(lambda: rig.messaging.enqueued, what="queued", rig=rig)
        wrapped = rig.messaging.enqueued[0]["message"]
        assert (
            wrapped.interface_path == "telegram/-100123/77"
            and wrapped.thread_id == "77"
        )
        assert wrapped.chat.type == "group" and wrapped.chat.title == "Dev Room"
        assert wrapped.reply_to_message is None  # the topic header is not a quote

    @pytest.mark.asyncio
    async def test_a_real_reply_carries_who_and_what_was_quoted(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path)
        bob = {"type": "user", "id": 7, "first_name": "Bob", "username": "bob_x"}
        rig.control.deliver(
            **message(
                "agreed", chat=GROUP, date=datetime.now(timezone.utc).isoformat(),
                reply_to={"msg_id": 3},
                replied={"id": 3, "chat": GROUP, "sender": bob, "text": "pizza tonight?", "date": "2026-01-01T00:00:00+00:00"},
            )
        )  # fmt: skip
        await wait_for(lambda: rig.messaging.enqueued, what="queued", rig=rig)
        quoted = rig.messaging.enqueued[0]["message"].reply_to_message
        assert quoted.text == "pizza tonight?" and quoted.message_id == "3"
        assert quoted.from_user.full_name == "Bob"

    @pytest.mark.asyncio
    async def test_commands_are_answered_by_the_core(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path)
        rig.control.deliver(
            **message("/help", date=datetime.now(timezone.utc).isoformat())
        )
        await wait_for(lambda: rig.messaging.commands, what="a command", rig=rig)
        assert rig.messaging.commands[0][0] == "/help" and rig.messaging.enqueued == []

    @pytest.mark.asyncio
    async def test_old_messages_are_history_not_something_to_answer(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path)
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        rig.control.deliver(**message("/help me", date=old))
        rig.control.deliver(
            **message("live one", date=datetime.now(timezone.utc).isoformat())
        )
        await wait_for(lambda: rig.messaging.enqueued, what="the live message", rig=rig)
        assert [m["message"].text for m in rig.messaging.enqueued] == ["live one"]
        assert [r["text"] for r in rig.messaging.recorded] == ["/help me", "live one"]
        assert rig.messaging.recorded[0]["timestamp"] == datetime.fromisoformat(old)
        assert rig.messaging.commands == []  # an old command is not run

    @pytest.mark.asyncio
    async def test_a_photo_and_a_voice_note_are_downloaded_and_flagged(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path)
        rig.control.make_file("p.jpg", PNG)
        rig.control.make_file("v.ogg", b"OggS")
        now = datetime.now(timezone.utc).isoformat()
        rig.control.deliver(
            **message(
                "look",
                date=now,
                media={"kind": "photo", "file": "p.jpg", "size": len(PNG)},
            )
        )
        rig.control.deliver(
            **message(
                "",
                date=now,
                media={
                    "kind": "voice",
                    "file": "v.ogg",
                    "mime": "audio/ogg",
                    "size": 4,
                },
            )
        )
        await wait_for(
            lambda: len(rig.messaging.enqueued) == 2, what="two messages", rig=rig
        )
        photo, voice = (m["message"] for m in rig.messaging.enqueued)
        assert (
            photo.text == "look"
            and Path(photo.attachments[0]["path"]).read_bytes() == PNG
        )
        assert (
            voice.is_voice_input is True
            and voice.attachments[0]["mime_type"] == "audio/ogg"
        )
        inbox = rig.harness.layout.data_dir(EXT_ID) / "inbox"
        await wait_for(
            lambda: not inbox.exists() or not any(inbox.iterdir()),
            what="inbox cleaned",
            rig=rig,
        )

    @pytest.mark.asyncio
    async def test_a_file_that_cannot_be_downloaded_does_not_lose_the_text(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path)
        rig.control.deliver(
            **message(
                "see attached",
                date=datetime.now(timezone.utc).isoformat(),
                media={
                    "kind": "document",
                    "file": "missing.pdf",
                    "name": "a.pdf",
                    "size": 9,
                },
            )
        )
        await wait_for(lambda: rig.messaging.enqueued, what="queued", rig=rig)
        wrapped = rig.messaging.enqueued[0]["message"]
        assert wrapped.text == "see attached" and not getattr(
            wrapped, "attachments", None
        )

    @pytest.mark.asyncio
    async def test_a_message_delivered_twice_is_handled_once(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path)
        now = datetime.now(timezone.utc).isoformat()
        rig.control.deliver(**message("once", id=500, date=now))
        rig.control.deliver(**message("once", id=500, date=now))
        rig.control.deliver(**message("then", id=501, date=now))
        await wait_for(
            lambda: len(rig.messaging.enqueued) == 2, what="two messages", rig=rig
        )
        await asyncio.sleep(0.2)
        assert [m["message"].text for m in rig.messaging.enqueued] == ["once", "then"]

    @pytest.mark.asyncio
    async def test_service_messages_and_unknown_senders_are_ignored(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path)
        now = datetime.now(timezone.utc).isoformat()
        rig.control.deliver(chat=PRIVATE, sender=None, text="anonymous", date=now)
        rig.control.deliver(**message("", date=now))
        rig.control.deliver(**message("real", date=now))
        await wait_for(lambda: rig.messaging.enqueued, what="queued", rig=rig)
        assert [m["message"].text for m in rig.messaging.enqueued] == ["real"]


class TestRecovery:
    @pytest.mark.asyncio
    async def test_what_arrived_while_offline_is_picked_up_once(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(minutes=10)).isoformat()
        handled = (now - timedelta(minutes=20)).isoformat()
        rig.control.state["dialogs"] = [{"entity": PRIVATE, "date": recent}]
        rig.control.state["history"] = {
            "42": [
                {
                    "id": 3,
                    "chat": PRIVATE,
                    "sender": PRIVATE,
                    "text": "missed",
                    "date": recent,
                },
                {
                    "id": 2,
                    "chat": PRIVATE,
                    "sender": PRIVATE,
                    "text": "already handled",
                    "date": handled,
                },
                {
                    "id": 1,
                    "chat": PRIVATE,
                    "sender": PRIVATE,
                    "text": "mine",
                    "date": handled,
                    "out": True,
                },
            ]
        }
        rig.control.write_state()
        rig.messaging.seen_keys.add(
            ("telegram/42", "42", datetime.fromisoformat(handled))
        )
        await connected(rig, tmp_path)
        await wait_for(
            lambda: rig.messaging.recorded, what="recovered message", rig=rig
        )
        await asyncio.sleep(0.3)
        assert [r["text"] for r in rig.messaging.recorded] == ["missed"]
        assert rig.messaging.enqueued == []  # ten minutes old: history only

    @pytest.mark.asyncio
    async def test_recovery_can_be_switched_off(self, rig: Rig, tmp_path: Path) -> None:
        recent = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        rig.control.state["dialogs"] = [{"entity": PRIVATE, "date": recent}]
        rig.control.state["history"] = {
            "42": [
                {
                    "id": 3,
                    "chat": PRIVATE,
                    "sender": PRIVATE,
                    "text": "missed",
                    "date": recent,
                }
            ]
        }
        rig.control.write_state()
        await connected(rig, tmp_path, TELEGRAM_RECOVERY_WINDOW_SECONDS=0)
        await asyncio.sleep(0.4)
        assert rig.messaging.recorded == []


class TestOutbound:
    @pytest.mark.asyncio
    async def test_a_reply_quotes_the_message_and_stays_in_the_topic(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path)
        origin = SimpleNamespace(message_id="31", interface_path="telegram/-100123/77")
        ok = await current(rig).send_message(
            {"text": "all green", "interface_path": "telegram/-100123"},
            original_message=origin,
        )
        assert ok is True
        [call] = sent(rig)
        assert call["peer"] == "-100123" and call["text"] == "all green"
        assert (
            call["reply_to"] == 31 and call["parse_mode"] is None
        )  # groups: plain text

    @pytest.mark.asyncio
    async def test_a_topic_without_a_quote_replies_to_the_topic(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path)
        assert await current(rig).send_message(
            {"text": "hi", "interface_path": "telegram/-100123/77"}
        )
        assert (
            sent(rig)[0]["reply_to"] == 77
        )  # Telethon addresses a topic by replying to it

    @pytest.mark.asyncio
    async def test_private_chats_use_markdown_in_telethons_dialect(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path)
        assert await current(rig).send_message(
            {"text": "*waves* _gently_", "interface_path": "telegram/42"}
        )
        [call] = sent(rig)
        assert call["text"] == "**waves** __gently__" and call["parse_mode"] == "md"

    @pytest.mark.asyncio
    async def test_a_public_username_is_resolved_to_its_id(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        rig.control.state["entities"] = {
            "@alice_x": {"type": "user", "id": 42, "first_name": "Alice"}
        }
        rig.control.write_state()
        await connected(rig, tmp_path)
        assert await current(rig).send_message(
            {"text": "hi", "interface_path": "telegram/@alice_x"}
        )
        assert sent(rig)[0]["peer"] == "42"
        assert (
            await current(rig).send_message(
                {"text": "hi", "interface_path": "telegram/@nobody_here"}
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_a_user_the_account_never_saw_is_a_clear_failure(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        rig.control.state["known_users"] = [42]
        rig.control.write_state()
        await connected(rig, tmp_path)
        assert (
            await current(rig).send_message(
                {"text": "hi", "interface_path": "telegram/999"}
            )
            is False
        )
        assert sent(rig) == []
        assert (
            await current(rig).send_message(
                {"text": "hi", "interface_path": "telegram/42"}
            )
            is True
        )

    @pytest.mark.asyncio
    async def test_an_unreachable_user_is_not_searched_for_again_at_once(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        rig.control.state["known_users"] = [42]
        rig.control.write_state()
        await connected(rig, tmp_path)
        await wait_for(
            lambda: rig.control.calls("get_dialogs"), what="the warm-up", rig=rig
        )
        before = len(rig.control.calls("get_dialogs"))
        assert (
            await current(rig).send_message(
                {"text": "hi", "interface_path": "telegram/999"}
            )
            is False
        )
        searched = len(rig.control.calls("get_dialogs"))
        assert searched > before  # the first miss looks around once
        assert (
            await current(rig).send_message(
                {"text": "hi", "interface_path": "telegram/999"}
            )
            is False
        )
        assert len(rig.control.calls("get_dialogs")) == searched  # the second does not

    @pytest.mark.asyncio
    async def test_a_deleted_original_does_not_cost_the_reply(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path)
        rig.control.fail_next("send_message", "MsgIdInvalidError")
        origin = SimpleNamespace(message_id="5", interface_path="telegram/42")
        assert await current(rig).send_message(
            {"text": "still delivered", "interface_path": "telegram/42"},
            original_message=origin,
        )
        first, second = sent(rig)[-1:], sent(rig)
        assert len(second) == 1 and second[0]["reply_to"] is None and first

    @pytest.mark.asyncio
    async def test_a_stale_topic_is_dropped_and_the_text_goes_to_the_chat(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path)
        rig.control.fail_next("send_message", "TopicIdInvalidError")
        assert await current(rig).send_message(
            {"text": "hi", "interface_path": "telegram/-100123/999"}
        )
        [call] = sent(rig)
        assert call["reply_to"] is None

    @pytest.mark.asyncio
    async def test_broken_markup_is_resent_as_plain_text(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path)
        rig.control.fail_next("send_message", "EntitiesTooLongError")
        assert await current(rig).send_message(
            {"text": "*oops", "interface_path": "telegram/42"}
        )
        [call] = sent(rig)
        assert call["parse_mode"] is None and call["text"] == "*oops"

    @pytest.mark.asyncio
    async def test_flood_control_is_waited_out_once(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path)
        rig.control.fail_next("send_message", "FloodWaitError", seconds=1)
        assert await current(rig).send_message(
            {"text": "patience", "interface_path": "telegram/42"}
        )
        assert len(sent(rig)) == 1

    @pytest.mark.asyncio
    async def test_invented_ids_are_discarded_and_foreign_paths_refused(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path)
        assert await current(rig).send_message(
            {
                "text": "hi",
                "interface_path": "telegram/-100123/no-thread-id",
                "reply_to": "-100123",
            }
        )
        assert sent(rig)[0]["reply_to"] is None
        assert (
            await current(rig).send_message(
                {"text": "x", "interface_path": "discord_bot/1/2"}
            )
            is False
        )
        assert await current(rig).send_message({"text": "x"}) is False
        assert len(sent(rig)) == 1

    @pytest.mark.asyncio
    async def test_long_text_is_split(self, rig: Rig, tmp_path: Path) -> None:
        await connected(rig, tmp_path)
        text = "\n".join(f"line {i:04d} " + "x" * 90 for i in range(120))
        assert await current(rig).send_message(
            {"text": text, "interface_path": "telegram/-9"}
        )
        calls = sent(rig)
        assert len(calls) >= 3 and all(len(c["text"]) <= 4096 for c in calls)
        assert "".join(c["text"] for c in calls) == text

    @pytest.mark.asyncio
    async def test_a_rejected_send_is_a_failed_delivery(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path)
        rig.control.fail_next("send_message", "PeerIdInvalidError")
        assert (
            await current(rig).send_message(
                {"text": "hi", "interface_path": "telegram/-5"}
            )
            is False
        )
        assert current(rig).failed == 1


class TestOutboundFiles:
    @pytest.mark.asyncio
    async def test_files_go_with_the_right_kind_and_the_caption_on_the_first(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path)
        picture, voice, notes = (rig.outgoing / n for n in ("p.png", "v.mp3", "n.txt"))
        picture.write_bytes(PNG)
        voice.write_bytes(b"ID3voice")
        notes.write_bytes(b"plain notes")
        ok = await current(rig).send_message(
            {"text": "here you go", "interface_path": "telegram/42", "media": [str(picture), str(voice), str(notes)], "send_as_voice": True}
        )  # fmt: skip
        assert ok is True
        photo, voice_call, doc = rig.control.calls("send_file")
        assert photo["caption"] == "here you go" and photo["extra"] == []
        assert voice_call["caption"] is None and voice_call["extra"] == ["voice_note"]
        assert (
            doc["extra"] == ["force_document"]
            and bytes.fromhex(doc["data_hex"]) == b"plain notes"
        )

    @pytest.mark.asyncio
    async def test_the_staged_copies_are_gone_afterwards(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path)
        picture = rig.outgoing / "p.png"
        picture.write_bytes(PNG)
        assert await current(rig).send_message(
            {"text": "", "interface_path": "telegram/42", "media": [str(picture)]}
        )
        outbox = rig.harness.layout.data_dir(EXT_ID) / "outbox"
        assert not outbox.exists() or not any(outbox.rglob("*"))
        assert picture.exists()


class TestSmallThings:
    @pytest.mark.asyncio
    async def test_typing_is_shown_in_the_right_topic(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path)
        await current(rig).send_activity("telegram/-100123/239", "record_audio")
        await current(rig).send_activity("telegram/42", "something_new")
        await wait_for(
            lambda: len(rig.control.requests("SetTypingRequest")) == 2,
            what="two actions",
            rig=rig,
        )
        first, second = rig.control.requests("SetTypingRequest")
        assert (
            first["action"] == "SendMessageRecordAudioAction"
            and first["top_msg_id"] == 239
        )
        assert (
            second["action"] == "SendMessageTypingAction" and "top_msg_id" not in second
        )

    @pytest.mark.asyncio
    async def test_a_reaction_is_sent(self, rig: Rig, tmp_path: Path) -> None:
        await connected(rig, tmp_path)
        message = SimpleNamespace(chat_id="-100123", message_id="9")
        assert await current(rig).add_reaction(message, "👀") is True
        [call] = rig.control.requests("SendReactionRequest")
        assert call["emoji"] == ["👀"] and call["msg_id"] == 9
        rig.control.fail_next("SendReactionRequest", "PeerIdInvalidError")
        assert await current(rig).add_reaction(message, "👀") is False

    @pytest.mark.asyncio
    async def test_chat_names_come_from_the_account(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        rig.control.state["entities"] = {"-100123": GROUP}
        rig.control.write_state()
        await connected(rig, tmp_path)
        resolver = rig.host.resolvers["telegram"]
        assert (await resolver("-100123", None))["chat_name"] == "Dev Room"
        assert await resolver("not-a-chat", None) == {}

    @pytest.mark.asyncio
    async def test_the_avatar_is_applied_only_when_the_operator_opted_in(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path)  # opt-in is off by default
        assert await current(rig).set_avatar(PNG, "image/png", "v2") is False
        assert rig.control.calls("upload_file") == []

    @pytest.mark.asyncio
    async def test_the_synth_avatar_is_applied_at_start_and_not_twice(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path, TELEGRAM_USE_SYNTH_AVATAR=True)
        await wait_for(
            lambda: rig.control.calls("upload_file"), what="avatar upload", rig=rig
        )
        assert rig.control.calls("upload_file")[0]["size"] == len(PNG)
        assert len(rig.control.requests("UploadProfilePhotoRequest")) == 1
        assert (
            await current(rig).set_avatar(PNG, "image/png", "v1") is False
        )  # same version
        assert await current(rig).set_avatar(PNG, "image/png", "v1", force=True) is True
        rig.control.state["has_photo"] = True
        rig.control.write_state()
        assert await current(rig).set_avatar(None) is True
        assert len(rig.control.requests("DeletePhotosRequest")) == 1

    @pytest.mark.asyncio
    async def test_a_picture_the_extension_did_not_set_is_never_removed(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path, TELEGRAM_USE_SYNTH_AVATAR=True)
        # nothing applied yet (no avatar at start) -> removal is refused
        storage = rig.harness.ports.storage
        assert isinstance(storage, MemoryStorage)
        storage._data[EXT_ID].pop("avatar_applied", None)  # noqa: SLF001
        assert await current(rig).set_avatar(None) is False
        assert rig.control.requests("DeletePhotosRequest") == []

    @pytest.mark.asyncio
    async def test_calls_are_unavailable_without_py_tgcalls_and_the_action_says_so(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        await connected(rig, tmp_path, TELEGRAM_CALLS_ENABLED=True)
        remote = current(rig)
        assert "call" not in remote.spec.capabilities and remote.spec.actions == {}
        assert (await remote.start_call("42"))["status"] == "failed"
