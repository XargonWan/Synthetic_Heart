"""The Telegram bot extension, end to end against a fake Telegram Bot API.

The extension runs as a real sandboxed process with python-telegram-bot loaded from
its own ``lib/``; the Core side is the real gateway, the real interface API, the real
media bridge and the in-memory ports. Telegram is a fake HTTP server that records
every call.
"""

from __future__ import annotations

import asyncio
import base64
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from core.extensions.api import register_core_api
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
    memory_ports,
)
from core.extensions.sandbox import RuntimeLayout, SandboxPolicy
from core.extensions.supervisor import RestartPolicy, Supervisor
from tests.support.fake_telegram import TOKEN, FakeTelegram, private_message
from tests.test_extensions_runtime import Harness

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def logged(h: Harness) -> list[tuple[str, str, str]]:
    log = h.ports.log
    assert isinstance(log, MemoryLog)
    return log.records


class Rig(SimpleNamespace):
    harness: Harness
    host: MemoryInterfaceHost
    messaging: MemoryMessaging
    root: Path


@pytest_asyncio.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    ports, grants = memory_ports(), MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    host, messaging = MemoryInterfaceHost(), MemoryMessaging()
    layout = RuntimeLayout(tmp_path / "extensions")
    outbox_root = tmp_path / "outgoing"
    outbox_root.mkdir()

    def resolve(raw: str) -> tuple[Path | None, str | None]:
        path = Path(raw).resolve()
        if outbox_root not in path.parents:
            return None, "Path is outside allowed roots"
        return (path, None) if path.is_file() else (None, "File does not exist")

    bridge = MediaBridge(
        data_dir=layout.data_dir,
        uid=lambda ext: None,
        inbound_root=tmp_path / "media",
        resolve_outbound=resolve,
    )
    register_interface_api(gateway, InterfacePorts(host, messaging, bridge))
    launcher = Launcher(gateway, layout, SandboxPolicy(drop_privileges=False))
    supervisor = Supervisor(
        launcher,
        RestartPolicy(
            initial_delay=0.05,
            max_delay=0.1,
            max_failures=4,
            hello_timeout=10.0,
            stop_grace=1.0,
            term_wait=0.5,
        ),  # fmt: skip
    )
    harness = Harness(layout, gateway, grants, ports, launcher, supervisor)
    try:
        yield Rig(harness=harness, host=host, messaging=messaging, root=outbox_root)
    finally:
        await supervisor.stop_all()
        await gateway.close_all()


SOURCE = Path(__file__).resolve().parent.parent / "extensions_src" / "telegram_bot"
EXT_ID = "official/telegram_bot"


@pytest.fixture(scope="session")
def ptb_lib(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """python-telegram-bot installed the way a package build does: into ``lib/``."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is needed to build the extension's lib/")
    target = tmp_path_factory.mktemp("ptb") / "lib"
    done = subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--offline",
            "--target",
            str(target),
            "python-telegram-bot==22.6",
        ],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        pytest.skip(f"cannot build lib/ offline: {done.stderr.strip()[-200:]}")
    return target


@pytest_asyncio.fixture
async def telegram() -> AsyncIterator[FakeTelegram]:
    server = FakeTelegram()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def install_telegram(
    h: Harness, lib: Path, server: FakeTelegram, **config: object
) -> None:
    manifest = load_manifest(SOURCE / "synth-extension.toml")
    assert manifest.id == EXT_ID
    directory = h.layout.install_dir(EXT_ID)
    directory.mkdir(parents=True)
    for name in ("ext_main.py", "convert.py"):
        shutil.copy(SOURCE / name, directory / name)
    (directory / "lib").symlink_to(lib)
    h.grants.grant(EXT_ID, *manifest.permissions.capabilities)
    config_port = h.ports.config
    assert isinstance(config_port, MemoryConfig)
    config_port.values[EXT_ID] = {
        "BOTFATHER_TOKEN": TOKEN,
        "TELEGRAM_API_URL": server.url,
        "POLL_TIMEOUT": 1,
        **config,
    }
    h.supervisor.register(manifest)


async def start(
    rig_: Rig, lib: Path, server: FakeTelegram, **config: object
) -> RemoteInterface:
    h = rig_.harness
    install_telegram(h, lib, server, **config)
    await h.supervisor.start(EXT_ID)
    try:
        await h.wait_for(lambda: "telegram_bot" in rig_.host.interfaces, timeout=20)
    except TimeoutError:
        pytest.fail(
            f"not registered; status={h.supervisor.status()} log={logged(h)[-3:]}"
        )
    remote = rig_.host.interfaces["telegram_bot"]
    assert isinstance(remote, RemoteInterface)
    return remote


class TestRegistration:
    @pytest.mark.asyncio
    async def test_registers_the_interface_with_the_bot_identity(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        remote = await start(rig, ptb_lib, telegram)
        me = await remote.get_me()
        assert me.username == "synth_test_bot" and str(me.id) == "123456"

    @pytest.mark.asyncio
    async def test_without_a_token_the_extension_stays_idle_and_registers_nothing(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        h, host = rig.harness, rig.host
        install_telegram(h, ptb_lib, telegram, BOTFATHER_TOKEN="")
        await h.supervisor.start(EXT_ID)
        await h.wait_for(lambda: any("not configured" in m for _, _, m in logged(h)))
        assert "telegram_bot" not in host.interfaces
        assert telegram.calls == []

    @pytest.mark.asyncio
    async def test_a_refused_token_is_reported_not_retried_in_a_loop(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        h, host = rig.harness, rig.host
        install_telegram(h, ptb_lib, telegram, BOTFATHER_TOKEN="1:wrong")
        await h.supervisor.start(EXT_ID)
        await h.wait_for(
            lambda: any("refused" in m for _, _, m in logged(h)), timeout=20
        )
        assert "telegram_bot" not in host.interfaces
        assert h.supervisor.status()[EXT_ID]["restarts"] == 0


async def wait_for(predicate: Any, timeout: float = 15.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout)


class TestInbound:
    @pytest.mark.asyncio
    async def test_a_private_message_reaches_the_core(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        await start(rig, ptb_lib, telegram)
        telegram.push_update(private_message("hello there", chat_id=42, message_id=9))
        await wait_for(lambda: rig.messaging.enqueued)
        item = rig.messaging.enqueued[0]
        message = item["message"]
        assert item["interface_id"] == "telegram_bot"
        assert message.interface_path == "telegram_bot/42"
        assert (message.text, message.chat_id, message.message_id) == (
            "hello there",
            "42",
            "9",
        )
        assert message.thread_id is None and message.chat.type == "private"
        assert rig.messaging.recorded[0]["sender_name"] == "Alice"
        assert rig.messaging.recorded[0]["sender_id"] == "42"
        assert (
            message.from_user.username == "alice"
        )  # the @handle, not the display name
        assert rig.messaging.aliases and rig.messaging.aliases[0][0] == "telegram_bot"

    @pytest.mark.asyncio
    async def test_a_forum_topic_becomes_the_thread_level_of_the_path(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        await start(rig, ptb_lib, telegram)
        telegram.member_count = 5
        telegram.push_update(
            {
                "message_id": 11,
                "date": 1,
                "chat": {
                    "id": -100123,
                    "type": "supergroup",
                    "title": "Dev Room",
                    "is_forum": True,
                },
                "from": {"id": 5, "is_bot": False, "first_name": "Bob"},
                "text": "@synth_test_bot status?",
                "message_thread_id": 77,
                "is_topic_message": True,
            }
        )
        await wait_for(lambda: rig.messaging.enqueued)
        message = rig.messaging.enqueued[0]["message"]
        assert message.interface_path == "telegram_bot/-100123/77"
        assert (message.chat_id, message.thread_id) == ("-100123", "77")
        assert message.chat.type == "group" and message.chat.title == "Dev Room"
        # Telegram's member count is not a human count, so none is claimed.
        assert not hasattr(message, "human_count")
        assert telegram.sent("getChatMemberCount") == []

    @pytest.mark.asyncio
    async def test_a_reply_in_an_ordinary_group_is_not_a_thread(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        await start(rig, ptb_lib, telegram)
        telegram.push_update(
            {
                "message_id": 12,
                "date": 1,
                "chat": {"id": -555, "type": "group", "title": "Friends"},
                "from": {"id": 5, "is_bot": False, "first_name": "Bob"},
                "text": "agreed",
                "message_thread_id": 3,  # set by Telegram on plain replies
                "reply_to_message": {
                    "message_id": 3,
                    "date": 1,
                    "chat": {"id": -555, "type": "group", "title": "Friends"},
                    "from": {"id": 6, "is_bot": False, "first_name": "Carol"},
                    "text": "pizza tonight?",
                },
            }
        )
        await wait_for(lambda: rig.messaging.enqueued)
        message = rig.messaging.enqueued[0]["message"]
        assert (
            message.interface_path == "telegram_bot/-555" and message.thread_id is None
        )
        quoted = message.reply_to_message
        assert quoted.text == "pizza tonight?" and quoted.message_id == "3"
        assert quoted.from_user.full_name == "Carol"

    @pytest.mark.asyncio
    async def test_slash_commands_are_handled_by_the_core(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        await start(rig, ptb_lib, telegram)
        telegram.push_update(private_message("/help"))
        await wait_for(lambda: rig.messaging.commands)
        assert rig.messaging.commands[0][0] == "/help"
        assert rig.messaging.enqueued == []

    @pytest.mark.asyncio
    async def test_other_bots_and_empty_service_messages_are_ignored(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        await start(rig, ptb_lib, telegram)
        bot_message = private_message("beep")
        bot_message["from"]["is_bot"] = True
        telegram.push_update(bot_message)
        service = private_message("")
        del service["text"]
        telegram.push_update(service)
        telegram.push_update(private_message("real one"))
        await wait_for(lambda: rig.messaging.enqueued)
        assert [m["message"].text for m in rig.messaging.enqueued] == ["real one"]

    @pytest.mark.asyncio
    async def test_a_photo_is_downloaded_and_handed_over_as_the_cores_copy(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        await start(rig, ptb_lib, telegram)
        telegram.files["BIG"] = PNG
        telegram.files["small"] = b"x"
        telegram.push_update(
            private_message(
                "",
                caption="look at this",
                photo=[
                    {
                        "file_id": "small",
                        "file_unique_id": "s",
                        "width": 1,
                        "height": 1,
                        "file_size": 1,
                    },
                    {
                        "file_id": "BIG",
                        "file_unique_id": "b",
                        "width": 9,
                        "height": 9,
                        "file_size": len(PNG),
                    },
                ],
            )
        )
        await wait_for(lambda: rig.messaging.enqueued)
        message = rig.messaging.enqueued[0]["message"]
        assert message.text == "look at this"
        [att] = message.attachments
        assert (
            att["mime_type"] == "image/jpeg" and Path(att["path"]).read_bytes() == PNG
        )
        # The extension cleaned its own inbox once the Core had its copy.
        inbox = rig.harness.layout.data_dir(EXT_ID) / "inbox"
        await wait_for(lambda: not any(inbox.iterdir()))
        # Only the largest size was downloaded.
        assert [c.params["file_id"] for c in telegram.sent("getFile")] == ["BIG"]

    @pytest.mark.asyncio
    async def test_a_voice_note_is_marked_as_spoken(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        await start(rig, ptb_lib, telegram)
        telegram.files["v1"] = b"OggS"
        telegram.push_update(
            private_message(
                "",
                voice={
                    "file_id": "v1",
                    "file_unique_id": "v",
                    "duration": 2,
                    "mime_type": "audio/ogg",
                    "file_size": 4,
                },
            )
        )
        telegram.push_update(private_message("typed words", message_id=8))
        await wait_for(lambda: len(rig.messaging.enqueued) == 2)
        spoken, typed = (m["message"] for m in rig.messaging.enqueued)
        assert spoken.is_voice_input is True
        assert spoken.attachments[0]["mime_type"] == "audio/ogg"
        assert not hasattr(typed, "is_voice_input")

    @pytest.mark.asyncio
    async def test_a_file_that_cannot_be_downloaded_does_not_lose_the_text(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        await start(rig, ptb_lib, telegram)
        telegram.push_update(
            private_message(
                "see attached",
                document={
                    "file_id": "gone",
                    "file_unique_id": "g",
                    "file_name": "a.pdf",
                    "file_size": 10,
                },
            )
        )
        await wait_for(lambda: rig.messaging.enqueued)
        message = rig.messaging.enqueued[0]["message"]
        assert message.text == "see attached" and not getattr(
            message, "attachments", None
        )

    @pytest.mark.asyncio
    async def test_an_oversized_file_is_not_downloaded(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        await start(rig, ptb_lib, telegram)
        telegram.push_update(
            private_message(
                "huge",
                document={
                    "file_id": "huge",
                    "file_unique_id": "h",
                    "file_name": "h.bin",
                    "file_size": 30 * 1024 * 1024,
                },
            )
        )
        await wait_for(lambda: rig.messaging.enqueued)
        assert telegram.sent("getFile") == []

    @pytest.mark.asyncio
    async def test_polling_survives_a_failing_bot_api(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        await start(rig, ptb_lib, telegram)
        telegram.fail("getUpdates", "Bad Gateway", code=502)
        telegram.push_update(private_message("after the outage"))
        await wait_for(lambda: rig.messaging.enqueued, timeout=20)
        assert rig.messaging.enqueued[0]["message"].text == "after the outage"
        assert rig.harness.supervisor.status()[EXT_ID]["restarts"] == 0


class TestOutbound:
    @pytest.mark.asyncio
    async def test_a_reply_quotes_the_incoming_message_in_the_same_topic(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        remote = await start(rig, ptb_lib, telegram)
        origin = SimpleNamespace(
            message_id="31", interface_path="telegram_bot/-100123/77"
        )
        ok = await remote.send_message(
            {"text": "all green", "interface_path": "telegram_bot/-100123"},
            original_message=origin,
        )
        assert ok is True
        [call] = telegram.sent("sendMessage")
        assert (
            call.params["chat_id"] == -100123
            or str(call.params["chat_id"]) == "-100123"
        )
        assert call.params["text"] == "all green"
        assert str(call.params["message_thread_id"]) == "77"
        assert (
            str(call.params["reply_parameters"]).find("31") >= 0
            or str(call.params.get("reply_to_message_id")) == "31"
        )
        assert "parse_mode" not in call.params  # groups are sent as plain text

    @pytest.mark.asyncio
    async def test_private_chats_use_markdown(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        remote = await start(rig, ptb_lib, telegram)
        assert await remote.send_message(
            {"text": "*waves*", "interface_path": "telegram_bot/42"}
        )
        [call] = telegram.sent("sendMessage")
        assert call.params["parse_mode"] == "Markdown"

    @pytest.mark.asyncio
    async def test_broken_markdown_is_resent_as_plain_text(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        remote = await start(rig, ptb_lib, telegram)
        telegram.fail(
            "sendMessage",
            "Bad Request: can't parse entities: Can't find end of the entity",
        )
        assert await remote.send_message(
            {"text": "snake_case *oops", "interface_path": "telegram_bot/42"}
        )
        first, second = [c for c in telegram.calls if c.method == "sendMessage"]
        assert (
            first.params.get("parse_mode") == "Markdown"
            and "parse_mode" not in second.params
        )

    @pytest.mark.asyncio
    async def test_a_deleted_original_does_not_cost_the_reply(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        remote = await start(rig, ptb_lib, telegram)
        telegram.fail("sendMessage", "Bad Request: message to be replied not found")
        origin = SimpleNamespace(message_id="5", interface_path="telegram_bot/42")
        assert await remote.send_message(
            {"text": "still delivered", "interface_path": "telegram_bot/42"},
            original_message=origin,
        )
        assert len(telegram.sent("sendMessage")) == 1

    @pytest.mark.asyncio
    async def test_a_stale_topic_is_dropped_and_the_text_goes_to_the_chat(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        remote = await start(rig, ptb_lib, telegram)
        telegram.fail("sendMessage", "Bad Request: message thread not found")
        assert await remote.send_message(
            {"text": "hi", "interface_path": "telegram_bot/-100123/999"}
        )
        [call] = telegram.sent("sendMessage")
        assert "message_thread_id" not in call.params

    @pytest.mark.asyncio
    async def test_invented_ids_are_discarded_not_sent(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        remote = await start(rig, ptb_lib, telegram)
        # reply_to equal to the chat id, and a non-numeric topic, as models write them.
        assert await remote.send_message(
            {
                "text": "hi",
                "interface_path": "telegram_bot/-100123/no-thread-id",
                "reply_to": "-100123",
            }
        )
        [call] = telegram.sent("sendMessage")
        assert "message_thread_id" not in call.params
        assert (
            "reply_parameters" not in call.params
            and "reply_to_message_id" not in call.params
        )

    @pytest.mark.asyncio
    async def test_text_longer_than_telegram_allows_is_split(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        remote = await start(rig, ptb_lib, telegram)
        text = "\n".join(f"line {i:04d} " + "x" * 90 for i in range(120))  # ~12 KB
        assert await remote.send_message(
            {"text": text, "interface_path": "telegram_bot/-9"}
        )
        calls = telegram.sent("sendMessage")
        assert len(calls) >= 3 and all(len(c.params["text"]) <= 4096 for c in calls)
        assert "".join(c.params["text"] for c in calls) == text

    @pytest.mark.asyncio
    async def test_a_rejected_send_is_a_failed_delivery_for_the_core(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        remote = await start(rig, ptb_lib, telegram)
        telegram.fail("sendMessage", "Bad Request: chat not found")
        assert (
            await remote.send_message(
                {"text": "hi", "interface_path": "telegram_bot/1"}
            )
            is False
        )
        telegram.fail("sendMessage", "Forbidden: bot was blocked by the user", code=403)
        assert (
            await remote.send_message(
                {"text": "hi", "interface_path": "telegram_bot/1"}
            )
            is False
        )
        assert remote.failed == 2

    @pytest.mark.asyncio
    async def test_flood_control_is_waited_out_once(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        remote = await start(rig, ptb_lib, telegram)
        telegram.fail(
            "sendMessage",
            "Too Many Requests: retry after 1",
            code=429,
            parameters={"retry_after": 1},
        )
        assert await remote.send_message(
            {"text": "patience", "interface_path": "telegram_bot/1"}
        )
        assert len(telegram.sent("sendMessage")) == 1

    @pytest.mark.asyncio
    async def test_a_foreign_or_missing_path_is_refused_without_calling_telegram(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        remote = await start(rig, ptb_lib, telegram)
        assert (
            await remote.send_message(
                {"text": "x", "interface_path": "discord_bot/1/2"}
            )
            is False
        )
        assert (
            await remote.send_message(
                {"text": "x", "interface_path": "telegram_bot/not-a-number"}
            )
            is False
        )
        assert await remote.send_message({"text": "x"}) is False
        assert telegram.sent("sendMessage") == []


class TestOutboundFiles:
    @pytest.mark.asyncio
    async def test_files_are_sent_with_the_right_method_and_the_caption_on_the_first(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        remote = await start(rig, ptb_lib, telegram)
        picture, voice, notes = (rig.root / n for n in ("p.png", "v.mp3", "n.txt"))
        picture.write_bytes(PNG)
        voice.write_bytes(b"ID3voice")
        notes.write_bytes(b"plain notes")
        ok = await remote.send_message(
            {
                "text": "here you go",
                "interface_path": "telegram_bot/42",
                "media": [str(picture), str(voice), str(notes)],
                "send_as_voice": True,
            }
        )
        assert ok is True
        photo, voice_call, doc = (
            telegram.sent("sendPhoto")[0],
            telegram.sent("sendVoice")[0],
            telegram.sent("sendDocument")[0],
        )
        assert photo.files["photo"] == PNG and photo.params["caption"] == "here you go"
        assert (
            voice_call.files["voice"] == b"ID3voice"
            and "caption" not in voice_call.params
        )
        assert doc.files["document"] == b"plain notes" and "caption" not in doc.params

    @pytest.mark.asyncio
    async def test_the_staged_copies_are_gone_afterwards(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        remote = await start(rig, ptb_lib, telegram)
        picture = rig.root / "p.png"
        picture.write_bytes(PNG)
        assert await remote.send_message(
            {"text": "", "interface_path": "telegram_bot/42", "media": [str(picture)]}
        )
        outbox = rig.harness.layout.data_dir(EXT_ID) / "outbox"
        assert not outbox.exists() or not any(outbox.rglob("*"))
        assert picture.exists()  # the original is untouched

    @pytest.mark.asyncio
    async def test_a_caption_over_the_limit_goes_as_its_own_message(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        remote = await start(rig, ptb_lib, telegram)
        picture = rig.root / "p.png"
        picture.write_bytes(PNG)
        long_text = "y" * 1500
        assert await remote.send_message(
            {
                "text": long_text,
                "interface_path": "telegram_bot/42",
                "media": [str(picture)],
            }
        )
        [text_call] = telegram.sent("sendMessage")
        [photo] = telegram.sent("sendPhoto")
        assert text_call.params["text"] == long_text and "caption" not in photo.params


class TestNames:
    @pytest.mark.asyncio
    async def test_the_chat_title_is_resolved_through_the_bot(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        await start(rig, ptb_lib, telegram)
        telegram.chats[-100123] = {
            "id": -100123,
            "type": "supergroup",
            "title": "Dev Room",
        }
        resolver = rig.host.resolvers["telegram_bot"]
        names = await resolver("-100123", None)
        assert names["chat_name"] == "Dev Room"
        assert await resolver("not-a-chat", None) == {}
        assert await resolver("-777", None) == {}  # a chat with no title


class TestActivity:
    @pytest.mark.asyncio
    async def test_typing_and_recording_are_shown_in_the_right_topic(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        remote = await start(rig, ptb_lib, telegram)
        await remote.send_activity("telegram_bot/-100123/239", "record_audio")
        await remote.send_activity("telegram_bot/31321637", "typing")
        await remote.send_activity("telegram_bot/31321637", "something_new")
        await wait_for(lambda: len(telegram.sent("sendChatAction")) == 3)
        first, second, third = telegram.sent("sendChatAction")
        assert (first.params["action"], str(first.params["chat_id"])) == (
            "record_voice",
            "-100123",
        )
        assert str(first.params["message_thread_id"]) == "239"
        assert (
            second.params["action"] == "typing"
            and "message_thread_id" not in second.params
        )
        assert third.params["action"] == "typing"  # unknown kinds are shown as typing

    @pytest.mark.asyncio
    async def test_a_foreign_path_or_a_failing_call_is_ignored(
        self, rig: Rig, ptb_lib: Path, telegram: FakeTelegram
    ) -> None:
        remote = await start(rig, ptb_lib, telegram)
        await remote.send_activity("discord_bot/1/2", "typing")
        telegram.fail("sendChatAction", "Bad Request: chat not found")
        await remote.send_activity("telegram_bot/1", "typing")  # must not raise
        assert telegram.sent("sendChatAction") == []
