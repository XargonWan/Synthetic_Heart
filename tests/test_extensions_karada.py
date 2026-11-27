"""Karada for extensions: an extension becomes a transport of the state server, receives its frames,
reads the state to catch up with and announces a presence."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from core.extensions.api import register_core_api
from core.extensions.gateway import Gateway
from core.extensions.grants import MemoryGrantStore
from core.extensions.karada import (
    CAP_PRESENCE,
    CAP_SUBSCRIBE,
    register_karada_api,
)
from core.extensions.manifest import Manifest, Permissions, Provides
from core.extensions.memory_ports import memory_ports
from synth_sdk.rpc import ErrorCode
from tests.support.waiting import wait_until
from tests.test_extensions_gateway import Client, Env


class FakeServer:
    """The state server, reduced to what the API uses."""

    def __init__(self) -> None:
        self.transports: list[Any] = []
        self._presence: str | None = None
        self.presence_calls: list[tuple[str | None, str]] = []

    def add_transport(self, transport: Any) -> None:
        self.transports.append(transport)

    def remove_transport(self, transport: Any) -> None:
        if transport in self.transports:
            self.transports.remove(transport)

    def snapshot(self) -> dict[str, Any]:
        return {"animation": {"state": "idle"}, "presence": self._presence}

    def presence(self) -> str | None:
        return self._presence

    async def set_presence(self, name: str | None, source: str) -> None:
        self.presence_calls.append((name, source))
        self._presence = name


def manifest(caps: tuple[str, ...] = (CAP_SUBSCRIBE, CAP_PRESENCE)) -> Manifest:
    return Manifest(
        id="dev/body",
        name="body",
        version="1.0.0",
        core_api=">=0.1",
        permissions=Permissions(capabilities=caps),
        provides=Provides(),
    )


class Rig:
    def __init__(self, env: Env, server: FakeServer) -> None:
        self.env, self.server = env, server
        self.frames: list[tuple[str, dict[str, Any]]] = []

    async def connect(
        self, grant: tuple[str, ...] = (CAP_SUBSCRIBE, CAP_PRESENCE)
    ) -> Client:
        client = await self.env.connect(manifest(), grant=grant)
        original = client.peer._handler  # noqa: SLF001

        async def handler(method: str, params: Any, notification: bool) -> Any:
            if method == "karada.frame":
                self.frames.append((params["kind"], params["payload"]))
                return None
            return await original(method, params, notification)

        client.peer._handler = handler  # noqa: SLF001
        return client


@pytest_asyncio.fixture
async def rig() -> AsyncIterator[Rig]:
    ports, grants = memory_ports(), MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    server = FakeServer()
    register_karada_api(gateway, server)
    the = Rig(Env(gateway, grants, ports), server)
    try:
        yield the
    finally:
        await the.env.close()


class TestSubscribe:
    @pytest.mark.asyncio
    async def test_subscribing_makes_the_extension_a_transport_and_returns_the_state(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        state = await client.call("karada.subscribe")
        assert state == {"animation": {"state": "idle"}, "presence": None}
        assert len(rig.server.transports) == 1
        await client.call("karada.subscribe")  # idempotent
        assert len(rig.server.transports) == 1

    @pytest.mark.asyncio
    async def test_every_broadcast_arrives_as_a_frame_in_order(self, rig: Rig) -> None:
        client = await rig.connect()
        await client.call("karada.subscribe")
        (transport,) = rig.server.transports
        await transport.broadcast_animation(
            {"type": "vrm_animation_v2", "state": "think"}
        )
        await transport.broadcast_audio({"type": "tts-play", "url": "/a.wav"})
        await transport.broadcast_face({"type": "vrm_face", "values": {"aa": 1}})
        await transport.broadcast_model({"type": "vrm_model", "name": "Rei"})
        await transport.broadcast_expression({"type": "vrm_expression_set"})
        await transport.broadcast_presence({"type": "vrm_presence", "state": "summon"})
        await transport.send_to_session("x", {"hello": 1})
        await wait_until(lambda: len(rig.frames) == 7, 5)
        assert [k for k, _ in rig.frames] == [
            "animation",
            "audio",
            "face",
            "model",
            "expression",
            "presence",
            "direct",
        ]
        assert rig.frames[1][1] == {"type": "tts-play", "url": "/a.wav"}
        assert transport.get_connected_sessions() == ["dev/body"]

    @pytest.mark.asyncio
    async def test_unsubscribing_stops_the_frames(self, rig: Rig) -> None:
        client = await rig.connect()
        await client.call("karada.subscribe")
        assert (await client.call("karada.unsubscribe"))["unsubscribed"] is True
        assert rig.server.transports == []
        assert (await client.call("karada.unsubscribe"))["unsubscribed"] is False

    @pytest.mark.asyncio
    async def test_a_session_that_ends_is_removed_from_the_server(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        await client.call("karada.subscribe")
        await rig.env.close()
        for _ in range(100):
            if not rig.server.transports:
                break
            await asyncio.sleep(0.02)
        assert rig.server.transports == []

    @pytest.mark.asyncio
    async def test_a_slow_extension_loses_the_oldest_frames_not_the_server(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        await client.call("karada.subscribe")
        (transport,) = rig.server.transports
        transport.stop()  # nobody drains the queue
        for n in range(100):
            await transport.broadcast_face({"n": n})
        assert transport._queue.qsize() == 64 and transport.dropped == 36  # noqa: SLF001
        assert transport._queue.get_nowait()[1]["n"] == 36  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_without_the_grant_it_is_refused(self, rig: Rig) -> None:
        client = await rig.connect(grant=("log",))
        await client.expect(ErrorCode.PERMISSION_DENIED, "karada.subscribe")
        assert rig.server.transports == []

    @pytest.mark.asyncio
    async def test_the_state_can_be_read_at_any_time(self, rig: Rig) -> None:
        client = await rig.connect()
        assert (await client.call("karada.state"))["presence"] is None


class TestPresence:
    @pytest.mark.asyncio
    async def test_an_extension_announces_and_clears_its_own_presence(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        assert (await client.call("karada.presence", state="summon"))[
            "presence"
        ] == "summon"
        assert rig.server.presence_calls[-1] == ("summon", "dev/body")
        assert (await client.call("karada.presence", state=None))["presence"] is None

    @pytest.mark.asyncio
    async def test_it_cannot_clear_a_presence_it_did_not_set(self, rig: Rig) -> None:
        rig.server._presence = "summon"  # noqa: SLF001 - someone else's
        client = await rig.connect()
        await client.expect(ErrorCode.PERMISSION_DENIED, "karada.presence", state=None)

    @pytest.mark.asyncio
    async def test_only_known_presences_are_accepted(self, rig: Rig) -> None:
        client = await rig.connect()
        await client.expect(
            ErrorCode.INVALID_PARAMS, "karada.presence", state="dancing"
        )

    @pytest.mark.asyncio
    async def test_the_presence_is_released_when_the_extension_goes(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        await client.call("karada.presence", state="summon")
        await rig.env.close()
        for _ in range(100):
            if rig.server.presence() is None:
                break
            await asyncio.sleep(0.02)
        assert rig.server.presence() is None


class TestRealStateServer:
    @pytest.mark.asyncio
    async def test_the_core_host_delivers_what_the_real_server_broadcasts(self) -> None:
        from core.animation_handler import get_karada_state_server
        from core.extensions.karada import CoreKaradaHost

        ports, grants = memory_ports(), MemoryGrantStore()
        gateway = Gateway(grants=grants)
        register_core_api(gateway, ports)
        register_karada_api(gateway, CoreKaradaHost())
        env = Env(gateway, grants, ports)
        the = Rig(env, FakeServer())
        try:
            client = await the.connect()
            state = await client.call("karada.subscribe")
            assert "animation" in state and state["presence"] is None
            server = get_karada_state_server()
            await client.call("karada.presence", state="summon")
            await wait_until(
                lambda: (
                    ("presence", {"type": "vrm_presence", "state": "summon"})
                    in the.frames
                ),
                5,
            )
            assert server.get_presence() == "summon"
            await client.call("karada.presence", state=None)
            await wait_until(
                lambda: (
                    ("presence", {"type": "vrm_presence", "state": "idle"})
                    in the.frames
                ),
                5,
            )
        finally:
            await env.close()
