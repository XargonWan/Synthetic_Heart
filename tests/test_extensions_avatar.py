"""The synth avatar API: any extension can read it and stay in sync with it."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
import pytest_asyncio

from core import synth_avatar
from core.extensions.api import register_core_api
from core.extensions.avatar import (
    CAP_AVATAR,
    MAX_AVATAR_BYTES,
    TOPIC_CHANGED,
    register_avatar_api,
)
from core.extensions.gateway import Gateway
from core.extensions.grants import MemoryGrantStore
from core.extensions.manifest import Manifest, Permissions
from core.extensions.memory_ports import memory_ports
from tests.test_extensions_gateway import Client, Env
from tests.test_extensions_runtime import h  # noqa: F401  (fixture)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def manifest(*caps: str) -> Manifest:
    return Manifest(
        id="dev/dash",
        name="dash",
        version="1.0.0",
        core_api=">=0.1",
        permissions=Permissions(capabilities=caps),
    )


class Rig:
    def __init__(self, env: Env, announce: Callable[[str | None], None]) -> None:
        self.env, self.announce = env, announce
        self.current: tuple[bytes, str] | None = (PNG, "v1")


@pytest_asyncio.fixture
async def rig() -> AsyncIterator[Rig]:
    ports, grants = memory_ports(), MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    holder: dict[str, Rig] = {}
    announce = register_avatar_api(gateway, lambda: holder["r"].current)
    env = Env(gateway, grants, ports)
    holder["r"] = Rig(env, announce)
    try:
        yield holder["r"]
    finally:
        await env.close()


async def connect(rig: Rig, *caps: str) -> Client:
    return await rig.env.connect(manifest(*caps), grant=caps)


class TestGet:
    @pytest.mark.asyncio
    async def test_any_extension_with_the_capability_gets_the_avatar(
        self, rig: Rig
    ) -> None:
        client = await connect(
            rig, CAP_AVATAR
        )  # not an interface: no interfaces.* at all
        got = await client.call("avatar.get")
        assert (
            got["available"] and got["version"] == "v1" and got["mime"] == "image/png"
        )
        assert base64.b64decode(got["image"]) == PNG

    @pytest.mark.asyncio
    async def test_none_set_or_too_big_means_not_available(self, rig: Rig) -> None:
        client = await connect(rig, CAP_AVATAR)
        rig.current = None
        assert await client.call("avatar.get") == {"available": False}
        rig.current = (b"0" * (MAX_AVATAR_BYTES + 1), "big")
        assert await client.call("avatar.get") == {"available": False}

    @pytest.mark.asyncio
    async def test_the_capability_is_required(self, rig: Rig) -> None:
        from synth_sdk.rpc import ErrorCode

        client = await connect(rig)
        await client.expect(ErrorCode.PERMISSION_DENIED, "avatar.get")


class TestChanges:
    @pytest.mark.asyncio
    async def test_a_subscriber_is_told_when_the_avatar_changes(self, rig: Rig) -> None:
        client = await connect(rig, CAP_AVATAR, "events")
        got: list[Any] = []
        original = client.peer._handler  # noqa: SLF001

        async def handler(method: str, params: Any, notification: bool) -> Any:
            if method == "events.deliver":
                got.append(dict(params))
                return {}
            return await original(method, params, notification)

        client.peer._handler = handler  # noqa: SLF001
        await client.call("events.subscribe", topic=TOPIC_CHANGED)
        rig.announce("v2")
        rig.announce(None)
        await asyncio.sleep(0.1)
        assert [g["payload"] for g in got] == [{"version": "v2"}, {"version": None}]
        assert {g["topic"] for g in got} == {TOPIC_CHANGED}

    @pytest.mark.asyncio
    async def test_someone_who_did_not_subscribe_hears_nothing(self, rig: Rig) -> None:
        client = await connect(rig, CAP_AVATAR, "events")
        got: list[Any] = []
        original = client.peer._handler  # noqa: SLF001

        async def handler(method: str, params: Any, notification: bool) -> Any:
            if method == "events.deliver":
                got.append(params)
                return {}
            return await original(method, params, notification)

        client.peer._handler = handler  # noqa: SLF001
        rig.announce("v2")
        await asyncio.sleep(0.05)
        assert got == []


class TestCoreSide:
    @pytest.mark.asyncio
    async def test_a_real_change_notifies_listeners_but_a_catch_up_does_not(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        told: list[str | None] = []
        listener = told.append
        monkeypatch.setattr(synth_avatar, "load_avatar", lambda: (PNG, "v7"))
        monkeypatch.setattr(synth_avatar, "_avatar_interfaces", lambda: {})
        synth_avatar.add_change_listener(listener)
        synth_avatar.add_change_listener(listener)  # twice is once
        try:
            await synth_avatar.broadcast_avatar_changed()
            await synth_avatar.broadcast_avatar_changed(force=True)  # "Resend avatar"
            await synth_avatar.broadcast_avatar_changed(only="x", catch_up=True)
            await synth_avatar.broadcast_avatar_changed(catch_up=True)
        finally:
            synth_avatar.remove_change_listener(listener)
        assert told == ["v7", "v7"]

    @pytest.mark.asyncio
    async def test_a_removal_is_announced_as_none_and_a_broken_listener_breaks_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        told: list[str | None] = []

        def broken(version: str | None) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(synth_avatar, "load_avatar", lambda: None)
        monkeypatch.setattr(synth_avatar, "_avatar_interfaces", lambda: {})
        synth_avatar.add_change_listener(broken)
        synth_avatar.add_change_listener(told.append)
        try:
            await synth_avatar.broadcast_avatar_changed()
        finally:
            synth_avatar.remove_change_listener(broken)
            synth_avatar.remove_change_listener(told.append)
        assert told == [None]


class TestSdkSync:
    """``ctx.avatar.sync`` in a real extension process."""

    @pytest.mark.asyncio
    async def test_an_extension_keeps_a_copy_in_sync_through_the_sdk(
        self,
        h: Any,  # noqa: F811
    ) -> None:
        state: dict[str, Any] = {"current": (PNG, "v1")}
        announce = register_avatar_api(h.gateway, lambda: state["current"])
        h.install(
            "dash",
            """
            async def main(ctx):
                seen = []
                async def mirror(image, version):
                    seen.append([None if image is None else len(image), version])
                    await ctx.storage.set("seen", seen)
                await ctx.avatar.sync(mirror)
                await ctx.stopping.wait()
            """,
            caps=("avatar", "events", "storage"),
        )
        await h.supervisor.start("dev/dash")
        await h.wait_for(lambda: h.stored("dev/dash", "seen") == [[len(PNG), "v1"]])
        state["current"] = (PNG + b"x", "v2")
        announce("v2")
        await h.wait_for(lambda: len(h.stored("dev/dash", "seen") or []) == 2)
        state["current"] = None
        announce(None)
        await h.wait_for(lambda: len(h.stored("dev/dash", "seen") or []) == 3)
        assert h.stored("dev/dash", "seen") == [
            [len(PNG), "v1"],
            [len(PNG) + 1, "v2"],
            [None, None],
        ]
