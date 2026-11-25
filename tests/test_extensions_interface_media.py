"""Attachments through the interface API (platform milestone M3b)."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from synth_sdk.rpc import ErrorCode, RpcError

from core.extensions.api import register_core_api
from core.extensions.gateway import Gateway
from core.extensions.grants import MemoryGrantStore
from core.extensions.interfaces import (
    InterfacePorts,
    RemoteInterface,
    register_interface_api,
)
from core.extensions.media import MediaBridge, MediaLimits
from core.extensions.memory_ports import (
    MemoryInterfaceHost,
    MemoryMessaging,
    memory_ports,
)
from tests.test_extensions_gateway import Client, Env
from tests.test_extensions_interfaces import (
    CAPS,
    iface_manifest,
    inbound_args,
)

# A real 1x1 PNG, so the Core's own extractor treats it as an image.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
EXT = "dev/chat"


class MediaRig:
    def __init__(
        self,
        root: Path,
        env: Env,
        host: MemoryInterfaceHost,
        messaging: MemoryMessaging,
        bridge: MediaBridge,
    ) -> None:
        self.root, self.env, self.host, self.messaging, self.bridge = (
            root,
            env,
            host,
            messaging,
            bridge,
        )
        self.sandbox = root / "sandbox"
        self.sandbox.mkdir()
        self.data = root / "data" / EXT
        self.data.mkdir(parents=True)

    async def connect(self, caps: list[str] | None = None) -> Client:
        client = await self.env.connect(iface_manifest(), grant=CAPS)
        await client.call(
            "interfaces.register",
            interface_id="chat",
            display_name="Chat",
            **({"capabilities": caps} if caps is not None else {}),
        )
        return client

    def received(self, client: Client, answer: Any = None) -> list[dict[str, Any]]:
        """Capture ``interface.send_message`` calls, reading each attachment while it exists."""
        seen: list[dict[str, Any]] = []
        original = client.peer._handler  # noqa: SLF001

        async def handler(method: str, params: Any, notification: bool) -> Any:
            if method == "interface.send_message":
                entry = dict(params)
                entry["attachment_bytes"] = [
                    Path(a["path"]).read_bytes()
                    for a in params["payload"].get("attachments", [])
                ]
                seen.append(entry)
                return {"ok": True} if answer is None else answer
            return await original(method, params, notification)

        client.peer._handler = handler  # noqa: SLF001
        return seen


@pytest_asyncio.fixture
async def mrig(tmp_path: Path) -> AsyncIterator[MediaRig]:
    ports, grants = memory_ports(), MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    host, messaging = MemoryInterfaceHost(), MemoryMessaging()
    sandbox = tmp_path / "sandbox"

    def resolve(raw: str) -> tuple[Path | None, str | None]:
        path = Path(raw).resolve()
        if sandbox not in path.parents:
            return None, "Path is outside allowed roots"
        return (path, None) if path.is_file() else (None, "File does not exist")

    bridge = MediaBridge(
        data_dir=lambda ext: tmp_path / "data" / ext,
        uid=lambda ext: None,
        inbound_root=tmp_path / "media",
        resolve_outbound=resolve,
        limits=MediaLimits(max_files=3),
    )
    register_interface_api(gateway, InterfacePorts(host, messaging, bridge))
    env = Env(gateway, grants, ports)
    rig = MediaRig(tmp_path, env, host, messaging, bridge)
    try:
        yield rig
    finally:
        await env.close()


def attachment(path: Path, **kw: Any) -> dict[str, Any]:
    return {"path": str(path), **kw}


def inbox(rig: MediaRig, name: str, data: bytes = PNG) -> Path:
    path = rig.data / "inbox" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    return path


class TestInboundAttachments:
    @pytest.mark.asyncio
    async def test_the_message_carries_the_cores_copies(self, mrig: MediaRig) -> None:
        client = await mrig.connect()
        source = inbox(mrig, "photo.png")
        result = await client.call(
            "interfaces.inbound",
            **inbound_args(
                text="look",
                attachments=[
                    attachment(source, filename="photo.png", mime_type="image/png")
                ],
            ),
        )
        assert result == {"queued": True}
        message = mrig.messaging.enqueued[0]["message"]
        [att] = message.attachments
        assert att["mime_type"] == "image/png" and att["filename"] == "photo.png"
        copy = Path(att["path"])
        assert copy != source and copy.read_bytes() == PNG
        assert mrig.messaging.recorded[0]["metadata"]["attachments"] == [
            {"filename": "photo.png", "mime_type": "image/png", "size": len(PNG)}
        ]

    @pytest.mark.asyncio
    async def test_the_cores_own_extractor_reads_what_we_produce(
        self, mrig: MediaRig
    ) -> None:
        """The Core already understands ``message.attachments``; no special handling."""
        from core.plugin_instance import _extract_multimodal_attachments

        client = await mrig.connect()
        await client.call(
            "interfaces.inbound",
            **inbound_args(
                text="",
                attachments=[attachment(inbox(mrig, "p.png"), mime_type="image/png")],
            ),
        )
        message = mrig.messaging.enqueued[0]["message"]
        extracted = await _extract_multimodal_attachments(None, message, "chat")
        assert len(extracted) == 1
        assert extracted[0]["mime_type"] == "image/png"
        assert base64.b64decode(extracted[0]["data"]) == PNG

    @pytest.mark.asyncio
    async def test_a_message_with_no_text_is_fine(self, mrig: MediaRig) -> None:
        client = await mrig.connect()
        await client.call(
            "interfaces.inbound",
            **inbound_args(text="", attachments=[attachment(inbox(mrig, "a.png"))]),
        )
        assert mrig.messaging.enqueued[0]["message"].text == ""

    @pytest.mark.asyncio
    async def test_a_path_outside_the_data_directory_refuses_the_whole_message(
        self, mrig: MediaRig
    ) -> None:
        secret = mrig.root / ".env"
        secret.write_text("DB_PASSWORD=hunter2")
        (mrig.data / "innocent.png").symlink_to(secret)
        client = await mrig.connect()
        for bad in (str(secret), "/etc/passwd", str(mrig.data / "innocent.png")):
            err = await client.expect(
                ErrorCode.INVALID_PARAMS,
                "interfaces.inbound",
                **inbound_args(
                    text="x",
                    attachments=[attachment(inbox(mrig, "ok.png")), {"path": bad}],
                ),
            )
            assert "attachment refused" in err.message
            assert "hunter2" not in err.message
        assert mrig.messaging.enqueued == [] and mrig.messaging.recorded == []
        assert [
            p for p in (mrig.root / "media").rglob("*") if p.is_file()
        ] == []  # all or nothing
        assert any(r.outcome == "invalid_params" for r in mrig.env.gateway.audit)

    @pytest.mark.asyncio
    async def test_shape_and_count_are_validated(self, mrig: MediaRig) -> None:
        client = await mrig.connect()
        files = [attachment(inbox(mrig, f"{i}.png")) for i in range(4)]
        await client.expect(
            ErrorCode.INVALID_PARAMS,
            "interfaces.inbound",
            **inbound_args(attachments=files),
        )
        await client.expect(
            ErrorCode.INVALID_PARAMS,
            "interfaces.inbound",
            **inbound_args(attachments="/etc/passwd"),
        )
        await client.expect(
            ErrorCode.INVALID_PARAMS,
            "interfaces.inbound",
            **inbound_args(attachments=["/etc/passwd"]),
        )
        assert mrig.messaging.enqueued == []

    @pytest.mark.asyncio
    async def test_without_a_media_bridge_attachments_are_refused(self) -> None:
        ports, grants = memory_ports(), MemoryGrantStore()
        gateway = Gateway(grants=grants)
        host, messaging = MemoryInterfaceHost(), MemoryMessaging()
        register_interface_api(gateway, InterfacePorts(host, messaging))
        env = Env(gateway, grants, ports)
        try:
            client = await env.connect(iface_manifest(), grant=CAPS)
            await client.call(
                "interfaces.register", interface_id="chat", display_name="Chat"
            )
            err = await client.expect(
                ErrorCode.INVALID_PARAMS,
                "interfaces.inbound",
                **inbound_args(attachments=[{"path": "/tmp/x"}]),
            )
            assert "not enabled" in err.message
            assert messaging.enqueued == []
        finally:
            await env.close()


class TestOutboundAttachments:
    async def _remote(
        self, mrig: MediaRig, caps: list[str]
    ) -> tuple[Client, RemoteInterface, list[dict[str, Any]]]:
        client = await mrig.connect(caps)
        seen = mrig.received(client)
        remote = mrig.host.interfaces["chat"]
        assert isinstance(remote, RemoteInterface)
        return client, remote, seen

    @pytest.mark.asyncio
    async def test_the_extension_receives_readable_copies_that_are_deleted_afterwards(
        self, mrig: MediaRig
    ) -> None:
        _client, remote, seen = await self._remote(mrig, ["send_message", "send_file"])
        report = mrig.sandbox / "report.pdf"
        report.write_bytes(b"%PDF-1.4 fake")
        ok = await remote.send_message(
            {"text": "here you go", "interface_path": "chat/r", "media": [str(report)]}
        )
        assert ok is True
        [call] = seen
        [entry] = call["payload"]["attachments"]
        assert (entry["name"], entry["kind"], entry["mime_type"]) == (
            "report.pdf",
            "document",
            "application/pdf",
        )
        assert call["attachment_bytes"] == [b"%PDF-1.4 fake"]
        assert call["payload"]["text"] == "here you go"
        assert "media" not in call["payload"]  # Core paths never go out
        assert not Path(entry["path"]).exists()  # deleted when the call returned
        assert not any((mrig.data / "outbox").iterdir())

    @pytest.mark.asyncio
    async def test_a_voice_note_is_an_audio_attachment_with_the_flag(
        self, mrig: MediaRig
    ) -> None:
        _client, remote, seen = await self._remote(mrig, ["send_message", "send_voice"])
        clip = mrig.sandbox / "vox_1.mp3"
        clip.write_bytes(b"ID3audio")
        assert await remote.send_message(
            {
                "text": "",
                "interface_path": "chat/r",
                "media": [str(clip)],
                "send_as_voice": True,
            }
        )
        [call] = seen
        assert call["payload"]["send_as_voice"] is True
        assert call["payload"]["attachments"][0]["kind"] == "audio"

    @pytest.mark.asyncio
    async def test_files_the_sandbox_refuses_are_not_sent_but_the_text_still_is(
        self, mrig: MediaRig
    ) -> None:
        _client, remote, seen = await self._remote(mrig, ["send_message", "send_file"])
        assert await remote.send_message(
            {
                "text": "no file for you",
                "interface_path": "chat/r",
                "media": ["/etc/passwd"],
            }
        )
        [call] = seen
        assert call["payload"]["text"] == "no file for you"
        assert "attachments" not in call["payload"]

    @pytest.mark.asyncio
    async def test_nothing_to_send_is_a_failed_delivery(self, mrig: MediaRig) -> None:
        _client, remote, seen = await self._remote(mrig, ["send_message", "send_file"])
        assert (
            await remote.send_message(
                {"interface_path": "chat/r", "media": ["/etc/passwd"]}
            )
            is False
        )
        assert seen == [] and remote.failed == 1

    @pytest.mark.asyncio
    async def test_without_the_media_capability_media_is_ignored(
        self, mrig: MediaRig
    ) -> None:
        _client, remote, seen = await self._remote(mrig, ["send_message"])
        f = mrig.sandbox / "a.png"
        f.write_bytes(PNG)
        assert await remote.send_message(
            {"text": "hi", "interface_path": "chat/r", "media": [str(f)]}
        )
        assert "attachments" not in seen[0]["payload"]
        assert not (mrig.data / "outbox").exists()

    @pytest.mark.asyncio
    async def test_staged_files_are_removed_even_when_the_extension_fails(
        self, mrig: MediaRig
    ) -> None:
        client = await mrig.connect(["send_message", "send_file"])
        original = client.peer._handler  # noqa: SLF001

        async def failing(method: str, params: Any, notification: bool) -> Any:
            if method == "interface.send_message":
                raise RpcError(ErrorCode.INTERNAL_ERROR, "boom")
            return await original(method, params, notification)

        client.peer._handler = failing  # noqa: SLF001
        remote = mrig.host.interfaces["chat"]
        assert isinstance(remote, RemoteInterface)
        f = mrig.sandbox / "a.png"
        f.write_bytes(PNG)
        assert (
            await remote.send_message(
                {"text": "x", "interface_path": "chat/r", "media": [str(f)]}
            )
            is False
        )
        assert not any((mrig.data / "outbox").iterdir())
        assert f.exists()  # the original is never touched

    @pytest.mark.asyncio
    async def test_the_cores_capability_drops_follow_what_was_declared(
        self, mrig: MediaRig
    ) -> None:
        """The Core strips media/voice from a reply and tells the model, per capability."""
        from core.message_registry import resolve_capability_drops

        client, remote, _seen = await self._remote(
            mrig, ["send_message", "send_file", "send_voice"]
        )
        wanted = {"media": ["x"], "send_as_voice": True}
        assert resolve_capability_drops(remote, wanted, "chat") == []

        await client.call(
            "interfaces.register",
            interface_id="chat",
            display_name="Chat",
            capabilities=["send_message"],
        )
        text_only = mrig.host.interfaces["chat"]
        drops = resolve_capability_drops(text_only, wanted, "chat")
        assert {d["feature"] for d in drops} == {"media", "send_as_voice"}
