"""The Fluxer extension against a fake Fluxer instance (REST + gateway).

Fluxer is the second, differently shaped interface: a chat is a *channel* inside an
optional *guild* (``fluxer_bot/<guild>/<channel>`` or ``fluxer_bot/<channel>``), not
``<chat>[/<thread>]``. It exercises the contract with nothing Telegram-shaped in it.
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
from core.extensions.avatar import register_avatar_api
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
    MemoryMessaging,
    memory_ports,
)
from core.extensions.sandbox import RuntimeLayout, SandboxPolicy
from core.extensions.supervisor import RestartPolicy, Supervisor
from tests.support.fake_fluxer import TOKEN, FakeFluxer, message, wait_until
from tests.test_extensions_runtime import Harness

SOURCE = Path(__file__).resolve().parent.parent / "extensions_src" / "fluxer"
EXT_ID = "official/fluxer"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class Rig(SimpleNamespace):
    harness: Harness
    host: MemoryInterfaceHost
    messaging: MemoryMessaging
    outgoing: Path


@pytest.fixture(scope="session")
def aiohttp_lib(tmp_path_factory: pytest.TempPathFactory) -> Path:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is needed to build the extension's lib/")
    target = tmp_path_factory.mktemp("aio") / "lib"
    done = subprocess.run(
        [uv, "pip", "install", "--offline", "--target", str(target), "aiohttp>=3.9"],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        pytest.skip(f"cannot build lib/ offline: {done.stderr.strip()[-200:]}")
    return target


@pytest_asyncio.fixture
async def fluxer() -> AsyncIterator[FakeFluxer]:
    server = FakeFluxer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


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
    register_interface_api(gateway, InterfacePorts(host, messaging, media))
    register_avatar_api(gateway, lambda: (PNG, "v1"))
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
        ),
    )
    harness = Harness(layout, gateway, grants, ports, launcher, supervisor)
    try:
        yield Rig(harness=harness, host=host, messaging=messaging, outgoing=outgoing)
    finally:
        await supervisor.stop_all()
        await gateway.close_all()


def install(rig: Rig, lib: Path, server: FakeFluxer, **config: Any) -> None:
    h = rig.harness
    manifest = load_manifest(SOURCE / "synth-extension.toml")
    assert manifest.id == EXT_ID
    directory = h.layout.install_dir(EXT_ID)
    directory.mkdir(parents=True)
    for name in ("ext_main.py", "convert.py", "client.py"):
        shutil.copy(SOURCE / name, directory / name)
    (directory / "lib").symlink_to(lib)
    h.grants.grant(EXT_ID, *manifest.permissions.capabilities)
    config_port = h.ports.config
    assert isinstance(config_port, MemoryConfig)
    config_port.values[EXT_ID] = {
        "FLUXER_TOKEN": TOKEN,
        "FLUXER_API_BASE_URL": server.base,
        "FLUXER_GATEWAY_URL": server.gateway_url,
        "FLUXER_API_VERSION": 1,
        **config,
    }
    h.supervisor.register(manifest)


async def start(
    rig: Rig, lib: Path, server: FakeFluxer, **config: Any
) -> RemoteInterface:
    install(rig, lib, server, **config)
    await rig.harness.supervisor.start(EXT_ID)
    try:
        await wait_until(
            lambda: "fluxer_bot" in rig.host.interfaces and server.identified, 20
        )
    except TimeoutError:
        pytest.fail(
            f"not up: status={rig.harness.supervisor.status()} calls={server.calls}"
        )
    remote = rig.host.interfaces["fluxer_bot"]
    assert isinstance(remote, RemoteInterface)
    await wait_until(lambda: server.sockets_open() > 0)
    await asyncio.sleep(0.15)  # READY has been dispatched and the bot id learnt
    return remote


class TestInbound:
    @pytest.mark.asyncio
    async def test_a_server_message_is_a_guild_channel_path(
        self, rig: Rig, aiohttp_lib: Path, fluxer: FakeFluxer
    ) -> None:
        await start(rig, aiohttp_lib, fluxer)
        await fluxer.dispatch("MESSAGE_CREATE", message("hello there"))
        await wait_until(lambda: rig.messaging.enqueued)
        wrapped = rig.messaging.enqueued[0]["message"]
        assert wrapped.interface_path == "fluxer_bot/100/500"
        # The chat is the channel, not the first level of the path.
        assert (wrapped.chat_id, wrapped.thread_id, wrapped.message_id) == (
            "500",
            None,
            "300",
        )
        assert (
            wrapped.chat.type == "group" and wrapped.from_user.full_name == "Alice A."
        )
        assert rig.messaging.recorded[0]["sender_name"] == "alice"

    @pytest.mark.asyncio
    async def test_a_direct_message_has_only_the_channel(
        self, rig: Rig, aiohttp_lib: Path, fluxer: FakeFluxer
    ) -> None:
        await start(rig, aiohttp_lib, fluxer)
        await fluxer.dispatch(
            "MESSAGE_CREATE", message("psst", guild=None, channel="777")
        )
        await wait_until(lambda: rig.messaging.enqueued)
        wrapped = rig.messaging.enqueued[0]["message"]
        assert (wrapped.interface_path, wrapped.chat_id, wrapped.chat.type) == (
            "fluxer_bot/777",
            "777",
            "private",
        )

    @pytest.mark.asyncio
    async def test_a_reply_carries_who_and_what_was_quoted(
        self, rig: Rig, aiohttp_lib: Path, fluxer: FakeFluxer
    ) -> None:
        await start(rig, aiohttp_lib, fluxer)
        quoted = {
            "id": "299",
            "content": "pizza?",
            "author": {"id": "7", "username": "bob", "global_name": "Bob B."},
        }
        await fluxer.dispatch(
            "MESSAGE_CREATE", message("yes", referenced_message=quoted)
        )
        await wait_until(lambda: rig.messaging.enqueued)
        reply = rig.messaging.enqueued[0]["message"].reply_to_message
        assert (reply.text, reply.message_id, reply.from_user.username) == (
            "pizza?",
            "299",
            "bob",
        )

    @pytest.mark.asyncio
    async def test_our_own_messages_other_bots_and_empty_ones_are_ignored(
        self, rig: Rig, aiohttp_lib: Path, fluxer: FakeFluxer
    ) -> None:
        await start(rig, aiohttp_lib, fluxer)
        await fluxer.dispatch("MESSAGE_CREATE", message("echo", author_id="9000"))
        await fluxer.dispatch(
            "MESSAGE_CREATE",
            message("beep", author_id="55", username="robot")
            | {"author": {"id": "55", "username": "robot", "bot": True}},
        )
        await fluxer.dispatch("MESSAGE_CREATE", message("   "))
        await fluxer.dispatch("MESSAGE_CREATE", message("real", message_id="301"))
        await wait_until(lambda: rig.messaging.enqueued)
        await asyncio.sleep(0.2)
        assert [m["message"].text for m in rig.messaging.enqueued] == ["real"]

    @pytest.mark.asyncio
    async def test_commands_are_answered_by_the_core(
        self, rig: Rig, aiohttp_lib: Path, fluxer: FakeFluxer
    ) -> None:
        await start(rig, aiohttp_lib, fluxer)
        await fluxer.dispatch("MESSAGE_CREATE", message("/help"))
        await wait_until(lambda: rig.messaging.commands)
        assert rig.messaging.commands[0][0] == "/help" and rig.messaging.enqueued == []

    @pytest.mark.asyncio
    async def test_a_message_with_a_timestamp_keeps_its_time(
        self, rig: Rig, aiohttp_lib: Path, fluxer: FakeFluxer
    ) -> None:
        await start(rig, aiohttp_lib, fluxer)
        await fluxer.dispatch("MESSAGE_CREATE", message("dated"))
        await wait_until(lambda: rig.messaging.recorded)
        assert rig.messaging.recorded[0]["timestamp"].year == 2026


class TestOutbound:
    @pytest.mark.asyncio
    async def test_a_reply_goes_to_the_channel_and_quotes_the_message(
        self, rig: Rig, aiohttp_lib: Path, fluxer: FakeFluxer
    ) -> None:
        remote = await start(rig, aiohttp_lib, fluxer)
        origin = SimpleNamespace(message_id="300", interface_path="fluxer_bot/100/500")
        assert await remote.send_message(
            {"text": "hi!", "interface_path": "fluxer_bot/100/500"},
            original_message=origin,
        )
        [call] = fluxer.sent()
        assert call["channel"] == "500"
        assert call["body"] == {
            "content": "hi!",
            "message_reference": {"message_id": "300"},
        }

    @pytest.mark.asyncio
    async def test_a_dm_path_and_a_guild_path_reach_their_channel(
        self, rig: Rig, aiohttp_lib: Path, fluxer: FakeFluxer
    ) -> None:
        remote = await start(rig, aiohttp_lib, fluxer)
        assert await remote.send_message(
            {"text": "a", "interface_path": "fluxer_bot/777"}
        )
        assert await remote.send_message(
            {"text": "b", "interface_path": "fluxer_bot/100/500"}
        )
        assert [c["channel"] for c in fluxer.sent()] == ["777", "500"]
        assert all("message_reference" not in c["body"] for c in fluxer.sent())

    @pytest.mark.asyncio
    async def test_a_foreign_or_unusable_path_is_refused_without_calling_fluxer(
        self, rig: Rig, aiohttp_lib: Path, fluxer: FakeFluxer
    ) -> None:
        remote = await start(rig, aiohttp_lib, fluxer)
        for path in ("telegram/1", "fluxer_bot/not-a-channel", "fluxer_bot", None):
            assert (
                await remote.send_message(
                    {"text": "x", "interface_path": path} if path else {"text": "x"}
                )
                is False
            )
        assert fluxer.sent() == []

    @pytest.mark.asyncio
    async def test_a_quote_that_does_not_exist_does_not_cost_the_message(
        self, rig: Rig, aiohttp_lib: Path, fluxer: FakeFluxer
    ) -> None:
        remote = await start(rig, aiohttp_lib, fluxer)
        fluxer.fail("POST messages", 400, {"message": "Unknown message"})
        origin = SimpleNamespace(message_id="1", interface_path="fluxer_bot/100/500")
        assert await remote.send_message(
            {"text": "still here", "interface_path": "fluxer_bot/100/500"},
            original_message=origin,
        )
        [call] = fluxer.sent()
        assert call["body"] == {"content": "still here"}

    @pytest.mark.asyncio
    async def test_the_channel_id_is_never_quoted_as_a_message(
        self, rig: Rig, aiohttp_lib: Path, fluxer: FakeFluxer
    ) -> None:
        remote = await start(rig, aiohttp_lib, fluxer)
        assert await remote.send_message(
            {"text": "x", "interface_path": "fluxer_bot/100/500", "reply_to": "500"}
        )
        assert "message_reference" not in fluxer.sent()[0]["body"]

    @pytest.mark.asyncio
    async def test_rate_limits_are_waited_out(
        self, rig: Rig, aiohttp_lib: Path, fluxer: FakeFluxer
    ) -> None:
        remote = await start(rig, aiohttp_lib, fluxer)
        fluxer.fail("POST messages", 429, {"retry_after": 0.05})
        assert await remote.send_message(
            {"text": "patience", "interface_path": "fluxer_bot/500"}
        )
        assert len(fluxer.sent()) == 1

    @pytest.mark.asyncio
    async def test_a_rejected_send_is_a_failed_delivery(
        self, rig: Rig, aiohttp_lib: Path, fluxer: FakeFluxer
    ) -> None:
        remote = await start(rig, aiohttp_lib, fluxer)
        fluxer.fail("POST messages", 403, {"message": "Missing Access"})
        assert (
            await remote.send_message({"text": "x", "interface_path": "fluxer_bot/500"})
            is False
        )
        assert remote.failed == 1

    @pytest.mark.asyncio
    async def test_long_text_is_split_under_the_limit(
        self, rig: Rig, aiohttp_lib: Path, fluxer: FakeFluxer
    ) -> None:
        remote = await start(rig, aiohttp_lib, fluxer)
        text = "\n".join(f"line {i:04d} " + "x" * 90 for i in range(60))
        assert await remote.send_message(
            {"text": text, "interface_path": "fluxer_bot/500"}
        )
        parts = [c["body"]["content"] for c in fluxer.sent()]
        assert (
            len(parts) >= 3
            and all(len(p) <= 2000 for p in parts)
            and "".join(parts) == text
        )

    @pytest.mark.asyncio
    async def test_files_are_uploaded_with_the_caption_on_the_first(
        self, rig: Rig, aiohttp_lib: Path, fluxer: FakeFluxer
    ) -> None:
        remote = await start(rig, aiohttp_lib, fluxer)
        first, second = rig.outgoing / "a.png", rig.outgoing / "b.txt"
        first.write_bytes(PNG)
        second.write_bytes(b"notes")
        assert await remote.send_message(
            {
                "text": "here",
                "interface_path": "fluxer_bot/100/500",
                "media": [str(first), str(second)],
            }
        )
        one, two = fluxer.sent("files")
        assert (
            one["payload"] == {"content": "here"}
            and one["files"]["files[0]"] == PNG
            and one["names"]["files[0]"].endswith("a.png")
        )
        assert two["payload"] == {} and two["files"]["files[0]"] == b"notes"
        outbox = rig.harness.layout.data_dir(EXT_ID) / "outbox"
        assert not outbox.exists() or not any(outbox.rglob("*"))


class TestGateway:
    @pytest.mark.asyncio
    async def test_it_identifies_with_the_raw_token_and_keeps_the_bot_prefix_for_rest_only(
        self, rig: Rig, aiohttp_lib: Path, fluxer: FakeFluxer
    ) -> None:
        await start(rig, aiohttp_lib, fluxer, FLUXER_TOKEN="Bot " + TOKEN)
        assert fluxer.identified[0]["token"] == TOKEN
        remote = rig.host.interfaces["fluxer_bot"]
        assert isinstance(remote, RemoteInterface)
        assert await remote.send_message(
            {"text": "x", "interface_path": "fluxer_bot/500"}
        )  # REST accepted "Bot <token>"

    @pytest.mark.asyncio
    async def test_a_rejected_token_stops_retrying_and_disables_the_interface(
        self, rig: Rig, aiohttp_lib: Path, fluxer: FakeFluxer
    ) -> None:
        install(rig, aiohttp_lib, fluxer, FLUXER_TOKEN="wrong")
        await rig.harness.supervisor.start(EXT_ID)
        await wait_until(lambda: "fluxer_bot" in rig.host.interfaces)
        await wait_until(lambda: not rig.host.interfaces["fluxer_bot"].is_enabled, 20)  # type: ignore[attr-defined]
        await asyncio.sleep(0.4)
        assert len(fluxer.identified) == 1  # no reconnect storm
        assert rig.harness.supervisor.status()[EXT_ID]["restarts"] == 0

    @pytest.mark.asyncio
    async def test_a_reconnect_request_opens_a_new_session_and_messages_keep_flowing(
        self,
        rig: Rig,
        aiohttp_lib: Path,
        fluxer: FakeFluxer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await start(rig, aiohttp_lib, fluxer)
        await fluxer.reconnect()
        await wait_until(lambda: len(fluxer.identified) + len(fluxer.resumed) >= 2, 20)
        await wait_until(lambda: fluxer.sockets_open() > 0)
        await asyncio.sleep(0.2)
        await fluxer.dispatch("MESSAGE_CREATE", message("after the reconnect"))
        await wait_until(lambda: rig.messaging.enqueued, 15)
        assert rig.messaging.enqueued[0]["message"].text == "after the reconnect"
        assert fluxer.resumed or len(fluxer.identified) >= 2

    @pytest.mark.asyncio
    async def test_without_a_token_it_stays_idle_and_registers_nothing(
        self, rig: Rig, aiohttp_lib: Path, fluxer: FakeFluxer
    ) -> None:
        install(rig, aiohttp_lib, fluxer, FLUXER_TOKEN="")
        await rig.harness.supervisor.start(EXT_ID)
        await asyncio.sleep(0.6)
        assert "fluxer_bot" not in rig.host.interfaces and fluxer.identified == []


class TestSmallThings:
    @pytest.mark.asyncio
    async def test_the_avatar_follows_the_operators_choice(
        self, rig: Rig, aiohttp_lib: Path, fluxer: FakeFluxer
    ) -> None:
        remote = await start(rig, aiohttp_lib, fluxer)
        assert remote.uses_synth_avatar() is True
        assert await remote.set_avatar(PNG, "image/png", "v2") is True
        assert fluxer.sent("avatar")[0]["body"]["avatar"].startswith(
            "data:image/png;base64,"
        )
        assert await remote.set_avatar(None) is True
        assert fluxer.sent("avatar")[1]["body"] == {"avatar": None}
        fluxer.fail("PATCH me", 500)
        assert await remote.set_avatar(PNG, "image/png", "v3") is False

    @pytest.mark.asyncio
    async def test_the_avatar_can_be_turned_off(
        self, rig: Rig, aiohttp_lib: Path, fluxer: FakeFluxer
    ) -> None:
        remote = await start(rig, aiohttp_lib, fluxer, FLUXER_USE_SYNTH_AVATAR=False)
        assert await remote.set_avatar(PNG, "image/png", "v2") is False
        assert fluxer.sent("avatar") == []

    @pytest.mark.asyncio
    async def test_the_chat_name_is_the_channel_id(
        self, rig: Rig, aiohttp_lib: Path, fluxer: FakeFluxer
    ) -> None:
        await start(rig, aiohttp_lib, fluxer)
        assert await rig.host.resolvers["fluxer_bot"]("500", None) == {
            "chat_name": "500"
        }

    @pytest.mark.asyncio
    async def test_the_bot_identity_is_registered(
        self, rig: Rig, aiohttp_lib: Path, fluxer: FakeFluxer
    ) -> None:
        remote = await start(rig, aiohttp_lib, fluxer)
        assert (await remote.get_me()).id == "9000"
