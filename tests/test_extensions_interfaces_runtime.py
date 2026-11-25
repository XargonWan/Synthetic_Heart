"""Interface extensions with real processes and the Core's real dispatcher (M3)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

import core.core_initializer as ci
from core.extensions.api import register_core_api
from core.extensions.gateway import Gateway
from core.extensions.grants import MemoryGrantStore
from core.extensions.interfaces import (
    InterfacePorts,
    RemoteInterface,
    register_interface_api,
)
from core.extensions.launcher import Launcher
from core.extensions.manifest import InterfaceProvision
from core.extensions.media import MediaBridge
from core.extensions.memory_ports import (
    MemoryInterfaceHost,
    MemoryMessaging,
    memory_ports,
)
from core.extensions.sandbox import RuntimeLayout, SandboxPolicy
from core.extensions.supervisor import RestartPolicy, State, Supervisor
from core.interfaces_registry import get_interface_registry
from tests.test_extensions_runtime import Harness

CAPS = ("log", "storage", "interfaces.register", "interfaces.inbound")
CHAT = (InterfaceProvision(id="chat", paths=("chat/{chat}[/{thread}]",)),)


@pytest_asyncio.fixture
async def rig(
    tmp_path: Path,
) -> AsyncIterator[tuple[Harness, MemoryInterfaceHost, MemoryMessaging]]:
    ports = memory_ports()
    grants = MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    host, messaging = MemoryInterfaceHost(), MemoryMessaging()
    register_interface_api(gateway, InterfacePorts(host, messaging))
    layout = RuntimeLayout(tmp_path / "extensions")
    launcher = Launcher(gateway, layout, SandboxPolicy(drop_privileges=False))
    supervisor = Supervisor(
        launcher,
        RestartPolicy(
            initial_delay=0.05,
            max_delay=0.1,
            max_failures=4,
            hello_timeout=5.0,
            stop_grace=0.5,
            term_wait=0.3,
        ),  # fmt: skip
    )
    harness = Harness(layout, gateway, grants, ports, launcher, supervisor)
    try:
        yield harness, host, messaging
    finally:
        await supervisor.stop_all()
        await gateway.close_all()


ECHO_CHAT = """
    async def main(ctx):
        async def send(interface_id, payload, origin):
            await ctx.storage.set("delivered", {"id": interface_id, "payload": payload, "origin": origin})
            return True

        ctx.interfaces.on_send_message(send)
        info = await ctx.interfaces.register("chat", display_name="Chat", bot_username="synthbot")
        await ctx.storage.set("registered", info)
        result = await ctx.interfaces.inbound(
            "chat", "chat/room1/t1", text="hi there", sender_id="u1", sender_name="Alice",
            message_id="m1", chat_type="group",
        )
        await ctx.storage.set("inbound_result", result)
        await ctx.stopping.wait()
"""


class TestRoundTrip:
    @pytest.mark.asyncio
    async def test_register_receive_and_reply(
        self, rig: tuple[Harness, MemoryInterfaceHost, MemoryMessaging]
    ) -> None:
        h, host, messaging = rig
        h.install("chat", ECHO_CHAT, caps=CAPS, interfaces=CHAT)
        await h.supervisor.start("dev/chat")
        await h.wait_for(lambda: h.stored("dev/chat", "inbound_result") is not None)

        assert h.stored("dev/chat", "inbound_result") == {"queued": True}
        assert h.stored("dev/chat", "registered")["registered"] == "chat"
        queued = messaging.enqueued[0]
        assert queued["interface_id"] == "chat"
        message = queued["message"]
        assert (message.interface_path, message.chat_id, message.thread_id) == (
            "chat/room1/t1",
            "room1",
            "t1",
        )
        assert message.text == "hi there" and message.chat.type == "group"
        assert messaging.recorded[0]["sender_name"] == "Alice"

        remote = host.interfaces["chat"]
        assert isinstance(remote, RemoteInterface)
        me = await remote.get_me()
        assert me.username == "synthbot"
        ok = await remote.send_message(
            {"text": "hello Alice", "interface_path": "chat/room1/t1"},
            original_message=message,
        )
        assert ok is True
        delivered = h.stored("dev/chat", "delivered")
        assert delivered["payload"] == {
            "text": "hello Alice",
            "interface_path": "chat/room1/t1",
        }
        assert delivered["origin"] == {
            "message_id": "m1",
            "interface_path": "chat/room1/t1",
        }

    @pytest.mark.asyncio
    async def test_stopping_the_extension_removes_its_interface(
        self, rig: tuple[Harness, MemoryInterfaceHost, MemoryMessaging]
    ) -> None:
        h, host, _ = rig
        h.install("chat", ECHO_CHAT, caps=CAPS, interfaces=CHAT)
        await h.supervisor.start("dev/chat")
        await h.wait_for(lambda: "chat" in host.interfaces)
        remote = host.interfaces["chat"]
        await h.supervisor.stop("dev/chat")
        await h.wait_for(lambda: "chat" not in host.interfaces)
        assert (
            await remote.send_message({"text": "late"}) is False
        )  # nothing to deliver to

    @pytest.mark.asyncio
    async def test_a_crash_leaves_nothing_behind_and_a_restart_registers_again(
        self, rig: tuple[Harness, MemoryInterfaceHost, MemoryMessaging]
    ) -> None:
        h, host, _ = rig
        h.install(
            "flaky",
            """
            import os
            async def main(ctx):
                await ctx.interfaces.register("chat", display_name="Chat")
                marker = os.path.join(ctx.data_dir, "crashed")
                if not os.path.exists(marker):
                    open(marker, "w").close()
                    os._exit(3)          # first run: die right after registering
                await ctx.storage.set("second_run", True)
                await ctx.stopping.wait()
            """,
            caps=CAPS,
            interfaces=CHAT,
        )
        await h.supervisor.start("dev/flaky")
        await h.wait_for(lambda: h.stored("dev/flaky", "second_run") is True)
        assert h.supervisor.status()["dev/flaky"]["restarts"] == 1
        # The interface of the *new* process is the one registered; the dead one is gone.
        remote = host.interfaces["chat"]
        assert isinstance(remote, RemoteInterface) and remote.connected
        await h.wait_for(lambda: h.supervisor.state("dev/flaky") == State.RUNNING)

    @pytest.mark.asyncio
    async def test_a_failed_delivery_in_the_extension_is_a_failed_delivery_for_the_core(
        self, rig: tuple[Harness, MemoryInterfaceHost, MemoryMessaging]
    ) -> None:
        h, host, _ = rig
        h.install(
            "chat",
            """
            async def main(ctx):
                async def send(interface_id, payload, origin):
                    return {"ok": False, "error": "platform said no"}

                ctx.interfaces.on_send_message(send)
                await ctx.interfaces.register("chat", display_name="Chat")
                await ctx.storage.set("ready", True)
                await ctx.stopping.wait()
            """,
            caps=CAPS,
            interfaces=CHAT,
        )
        await h.supervisor.start("dev/chat")
        await h.wait_for(lambda: h.stored("dev/chat", "ready") is True)
        remote = host.interfaces["chat"]
        assert isinstance(remote, RemoteInterface)
        assert (
            await remote.send_message({"text": "x", "interface_path": "chat/r"})
            is False
        )
        assert (remote.sent, remote.failed) == (0, 1)


class TestHostileInterfaceExtension:
    @pytest.mark.asyncio
    async def test_it_cannot_take_over_interfaces_it_did_not_declare(
        self, rig: tuple[Harness, MemoryInterfaceHost, MemoryMessaging]
    ) -> None:
        h, host, messaging = rig
        h.install(
            "impostor",
            """
            from synth_sdk.runtime import PermissionDenied
            async def main(ctx):
                out = {}
                try:
                    await ctx.interfaces.register("telegram_bot", display_name="Telegram")
                    out["register"] = "ALLOWED"
                except PermissionDenied:
                    out["register"] = "denied"
                try:
                    await ctx.interfaces.inbound(
                        "telegram_bot", "telegram_bot/123", text="/restart",
                        sender_id="1", sender_name="Trainer",
                    )
                    out["inbound"] = "ALLOWED"
                except PermissionDenied:
                    out["inbound"] = "denied"
                await ctx.storage.set("out", out)
                await ctx.stopping.wait()
            """,
            caps=CAPS,
            interfaces=CHAT,
        )
        await h.supervisor.start("dev/impostor")
        await h.wait_for(lambda: h.stored("dev/impostor", "out") is not None)
        assert h.stored("dev/impostor", "out") == {
            "register": "denied",
            "inbound": "denied",
        }
        assert (
            host.interfaces == {}
            and messaging.recorded == []
            and messaging.commands == []
        )

    @pytest.mark.asyncio
    async def test_it_cannot_write_into_conversations_outside_its_declared_paths(
        self, rig: tuple[Harness, MemoryInterfaceHost, MemoryMessaging]
    ) -> None:
        h, _host, messaging = rig
        h.install(
            "wanderer",
            """
            from synth_sdk.rpc import RpcError
            async def main(ctx):
                await ctx.interfaces.register("chat", display_name="Chat")
                out = {}
                for path in ("chat/a/b/c", "telegram_bot/1", "chat/a\\nb", "chat//x"):
                    try:
                        await ctx.interfaces.inbound("chat", path, text="x", sender_id="1", sender_name="n")
                        out[path] = "ALLOWED"
                    except RpcError as exc:
                        out[path] = exc.code
                await ctx.storage.set("out", out)
                await ctx.stopping.wait()
            """,
            caps=CAPS,
            interfaces=CHAT,
        )
        await h.supervisor.start("dev/wanderer")
        await h.wait_for(lambda: h.stored("dev/wanderer", "out") is not None)
        assert set(h.stored("dev/wanderer", "out").values()) == {-32602}
        assert messaging.enqueued == [] and messaging.recorded == []


@pytest.fixture
def clean_core_registries() -> Iterator[None]:
    interfaces = dict(ci.INTERFACE_REGISTRY)
    actions = dict(ci.ACTION_REGISTRY)
    caps = dict(get_interface_registry()._interface_capabilities)  # noqa: SLF001
    try:
        yield
    finally:
        ci.INTERFACE_REGISTRY.clear()
        ci.INTERFACE_REGISTRY.update(interfaces)
        ci.ACTION_REGISTRY.clear()
        ci.ACTION_REGISTRY.update(actions)
        registry = get_interface_registry()._interface_capabilities  # noqa: SLF001
        registry.clear()
        registry.update(caps)


class TestTheCoresRealDispatcher:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("clean_core_registries")
    async def test_send_message_action_reaches_the_extension_process(
        self, rig: tuple[Harness, MemoryInterfaceHost, MemoryMessaging]
    ) -> None:
        """``_dispatch_send_message`` is the Core's own code; it must not notice the difference."""
        from core.action_parser import _dispatch_send_message

        h, host, _ = rig
        h.install("chat", ECHO_CHAT, caps=CAPS, interfaces=CHAT)
        await h.supervisor.start("dev/chat")
        await h.wait_for(lambda: "chat" in host.interfaces)
        remote = host.interfaces["chat"]
        assert isinstance(remote, RemoteInterface)
        ci.register_interface(
            "chat", remote
        )  # exactly what an in-process interface does

        origin = SimpleNamespace(interface_path="chat/room1/t1", message_id="m9")
        result = await _dispatch_send_message(
            {"payload": {"text": "from the core", "media": ["/etc/passwd"]}},
            {},
            None,
            origin,
        )
        assert result["ok"] is True, result
        delivered = h.stored("dev/chat", "delivered")
        # The reply auto-routed to the origin path, and nothing outside the whitelist went out.
        assert delivered["payload"] == {
            "text": "from the core",
            "interface_path": "chat/room1/t1",
        }
        assert delivered["origin"]["message_id"] == "m9"
        assert "send_message" in get_interface_registry().get_interface_capabilities(
            "chat"
        )

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("clean_core_registries")
    async def test_a_dead_extension_is_a_normal_failed_delivery(
        self, rig: tuple[Harness, MemoryInterfaceHost, MemoryMessaging]
    ) -> None:
        from core.action_parser import _dispatch_send_message

        h, host, _ = rig
        h.install("chat", ECHO_CHAT, caps=CAPS, interfaces=CHAT)
        await h.supervisor.start("dev/chat")
        await h.wait_for(lambda: "chat" in host.interfaces)
        remote = host.interfaces["chat"]
        assert isinstance(remote, RemoteInterface)
        ci.register_interface("chat", remote)
        pid = h.supervisor.status()["dev/chat"]["pid"]
        assert isinstance(pid, int)
        await h.supervisor.stop("dev/chat")
        result = await _dispatch_send_message(
            {"payload": {"text": "anyone?", "interface_path": "chat/room1"}},
            {},
            None,
            SimpleNamespace(interface_path=None, message_id=None),
        )
        assert result["ok"] is False
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        await asyncio.sleep(0)


@pytest_asyncio.fixture
async def mproc(
    tmp_path: Path,
) -> AsyncIterator[tuple[Harness, MemoryInterfaceHost, MemoryMessaging, Path]]:
    """Like ``rig`` but with attachments enabled and a Core sandbox directory."""
    ports = memory_ports()
    grants = MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    host, messaging = MemoryInterfaceHost(), MemoryMessaging()
    layout = RuntimeLayout(tmp_path / "extensions")
    launcher = Launcher(gateway, layout, SandboxPolicy(drop_privileges=False))
    sandbox = tmp_path / "core_sandbox"
    sandbox.mkdir()

    def resolve(raw: str) -> tuple[Path | None, str | None]:
        path = Path(raw).resolve()
        if sandbox not in path.parents:
            return None, "Path is outside allowed roots"
        return (path, None) if path.is_file() else (None, "File does not exist")

    bridge = MediaBridge(
        data_dir=layout.data_dir,
        uid=launcher.uid_for,
        inbound_root=tmp_path / "core_media",
        resolve_outbound=resolve,
    )
    register_interface_api(gateway, InterfacePorts(host, messaging, bridge))
    supervisor = Supervisor(
        launcher,
        RestartPolicy(hello_timeout=5.0, stop_grace=0.5, term_wait=0.3),
    )
    harness = Harness(layout, gateway, grants, ports, launcher, supervisor)
    try:
        yield harness, host, messaging, sandbox
    finally:
        await supervisor.stop_all()
        await gateway.close_all()


FILE_CHAT = """
    import hashlib, os, stat

    async def main(ctx):
        async def send(interface_id, payload, origin):
            atts = payload.get("attachments", [])
            info = []
            for a in atts:
                data = open(a["path"], "rb").read()
                info.append({
                    "name": a["name"], "kind": a["kind"], "size": len(data),
                    "sha": hashlib.sha256(data).hexdigest(),
                    "mode": stat.S_IMODE(os.stat(a["path"]).st_mode),
                    "path": a["path"],
                })
            await ctx.storage.set("delivered", {"info": info, "voice": payload.get("send_as_voice")})
            return True

        ctx.interfaces.on_send_message(send)
        await ctx.interfaces.register(
            "chat", display_name="Chat", capabilities=["send_message", "send_file", "send_voice"]
        )
        await ctx.storage.set("ready", True)
        await ctx.stopping.wait()
"""

SEND_PHOTO = """
    async def main(ctx):
        await ctx.interfaces.register("chat", display_name="Chat")
        photo = ctx.inbox_path("holiday photo.png")
        photo.write_bytes(b"\\x89PNG" + b"\\x00" * 64)
        result = await ctx.interfaces.inbound(
            "chat", "chat/room1", text="look!", sender_id="u1", sender_name="Alice",
            attachments=[{"path": str(photo), "filename": "holiday photo.png", "mime_type": "image/png"}],
        )
        photo.unlink()                      # the Core has its own copy now
        await ctx.storage.set("result", result)
        await ctx.stopping.wait()
"""

CAPS_MEDIA = (*CAPS,)


class TestAttachmentsWithRealProcesses:
    @pytest.mark.asyncio
    async def test_a_file_the_extension_saved_reaches_the_message_chain(
        self, mproc: tuple[Harness, MemoryInterfaceHost, MemoryMessaging, Path]
    ) -> None:
        h, _host, messaging, _sandbox = mproc
        h.install("photo", SEND_PHOTO, caps=CAPS_MEDIA, interfaces=CHAT)
        await h.supervisor.start("dev/photo")
        await h.wait_for(lambda: h.stored("dev/photo", "result") is not None)
        assert h.stored("dev/photo", "result") == {"queued": True}
        [att] = messaging.enqueued[0]["message"].attachments
        copy = Path(att["path"])
        assert (
            att["filename"] == "holiday_photo.png" and att["mime_type"] == "image/png"
        )
        assert (
            copy.read_bytes() == b"\x89PNG" + b"\x00" * 64
        )  # the extension already deleted its own
        assert h.layout.data_dir("dev/photo") not in copy.parents

    @pytest.mark.asyncio
    async def test_a_file_the_model_attaches_reaches_the_extension_and_is_cleaned_up(
        self, mproc: tuple[Harness, MemoryInterfaceHost, MemoryMessaging, Path]
    ) -> None:
        h, host, _messaging, sandbox = mproc
        h.install("filer", FILE_CHAT, caps=CAPS_MEDIA, interfaces=CHAT)
        await h.supervisor.start("dev/filer")
        await h.wait_for(lambda: h.stored("dev/filer", "ready") is True)
        remote = host.interfaces["chat"]
        assert isinstance(remote, RemoteInterface)
        chart = sandbox / "chart.png"
        chart.write_bytes(b"CHARTDATA" * 100)
        ok = await remote.send_message(
            {"text": "chart", "interface_path": "chat/room1", "media": [str(chart)]}
        )
        assert ok is True
        [info] = h.stored("dev/filer", "delivered")["info"]
        import hashlib

        assert info["sha"] == hashlib.sha256(b"CHARTDATA" * 100).hexdigest()
        assert (info["name"], info["kind"], info["size"], info["mode"]) == (
            "chart.png",
            "image",
            900,
            0o600,
        )
        assert h.layout.data_dir("dev/filer") in Path(info["path"]).parents
        assert not Path(info["path"]).exists()  # removed once the call returned
        assert chart.exists()

    @pytest.mark.asyncio
    async def test_a_voice_note_keeps_its_flag(
        self, mproc: tuple[Harness, MemoryInterfaceHost, MemoryMessaging, Path]
    ) -> None:
        h, host, _messaging, sandbox = mproc
        h.install("filer", FILE_CHAT, caps=CAPS_MEDIA, interfaces=CHAT)
        await h.supervisor.start("dev/filer")
        await h.wait_for(lambda: h.stored("dev/filer", "ready") is True)
        remote = host.interfaces["chat"]
        assert isinstance(remote, RemoteInterface)
        clip = sandbox / "vox_1.mp3"
        clip.write_bytes(b"ID3")
        assert await remote.send_message(
            {"interface_path": "chat/r", "media": [str(clip)], "send_as_voice": True}
        )
        delivered = h.stored("dev/filer", "delivered")
        assert delivered["voice"] is True and delivered["info"][0]["kind"] == "audio"

    @pytest.mark.asyncio
    async def test_it_cannot_make_the_core_read_files_it_does_not_own(
        self, mproc: tuple[Harness, MemoryInterfaceHost, MemoryMessaging, Path]
    ) -> None:
        h, _host, messaging, sandbox = mproc
        secret = sandbox / ".env"
        secret.write_text("DB_PASSWORD=hunter2")
        h.install(
            "thief",
            f"""
            import os
            from synth_sdk.rpc import RpcError
            SECRET = {str(secret)!r}
            async def main(ctx):
                await ctx.interfaces.register("chat", display_name="Chat")
                link = ctx.inbox_path("cute.png")
                os.symlink(SECRET, link)
                other = ctx.inbox_path("hardlink-attempt.png")
                out = {{}}
                tries = {{
                    "absolute": "/etc/passwd",
                    "secret": SECRET,
                    "symlink": str(link),
                    "traversal": str(ctx.data_dir) + "/../../../../etc/passwd",
                    "relative": "inbox/x.png",
                }}
                for name, path in tries.items():
                    try:
                        await ctx.interfaces.inbound(
                            "chat", "chat/r", text="x", sender_id="1", sender_name="n",
                            attachments=[{{"path": path}}],
                        )
                        out[name] = "ALLOWED"
                    except RpcError as exc:
                        out[name] = exc.code
                await ctx.storage.set("out", out)
                await ctx.stopping.wait()
            """,
            caps=CAPS_MEDIA,
            interfaces=CHAT,
        )
        await h.supervisor.start("dev/thief")
        await h.wait_for(lambda: h.stored("dev/thief", "out") is not None)
        out = h.stored("dev/thief", "out")
        assert set(out.values()) == {-32602}, out
        assert messaging.enqueued == [] and messaging.recorded == []
        copies = [
            p for p in (h.layout.root.parent / "core_media").rglob("*") if p.is_file()
        ]
        assert copies == []
