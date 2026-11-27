"""Rift Vessel connectors that live in an extension (``[[provides.vessels]]``).

Core side: a connector appears in the Vessel registry while its extension is connected,
questions the Vessel awaits become ``vessel.call`` requests, questions it reads at once are
answered from the state snapshot the extension pushes, perception events reach the Vessel's
callback only while it is connected. SDK side: a real extension process serving a
:class:`synth_sdk.vessel.VesselConnector`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from core.extensions.api import register_core_api
from core.extensions.gateway import Gateway
from core.extensions.grants import MemoryGrantStore
from core.extensions.manifest import (
    Manifest,
    ManifestError,
    Permissions,
    Provides,
    VesselProvision,
    parse_manifest_text,
)
from core.extensions.memory_ports import memory_ports
from core.extensions.vessels import (
    CAP_VESSEL,
    attach_vessels,
    register_vessel_api,
)
from synth_sdk.rpc import ErrorCode, RpcError
from tests.test_extensions_gateway import Client, Env

WORLD = VesselProvision("tinyworld", "Tiny world", False, ("movement", "chat"))


def manifest(
    *vessels: VesselProvision, caps: tuple[str, ...] = (CAP_VESSEL,)
) -> Manifest:
    return Manifest(
        id="dev/world",
        name="world",
        version="1.0.0",
        core_api=">=0.1",
        permissions=Permissions(capabilities=caps),
        provides=Provides(vessels=vessels),
    )


class FakeHost:
    def __init__(self, taken: set[str] | None = None) -> None:
        self.connectors: dict[str, Any] = {}
        self.meta: dict[str, tuple[str, dict[str, bool]]] = {}
        self._taken = taken or set()

    def taken(self, name: str) -> bool:
        return name in self._taken

    def register(
        self, name: str, connector: Any, label: str, capabilities: dict[str, bool]
    ) -> None:
        self.connectors[name] = connector
        self.meta[name] = (label, capabilities)

    def unregister(self, name: str) -> None:
        self.connectors.pop(name, None)


class Rig:
    def __init__(self, env: Env, host: FakeHost) -> None:
        self.env, self.host = env, host
        self.warnings: list[str] = []
        self.asked: list[tuple[str, dict[str, Any]]] = []

    async def connect(
        self, m: Manifest, grant: tuple[str, ...] = (CAP_VESSEL,)
    ) -> Client:
        return await self.env.connect(m, grant=grant)

    def reply(self, client: Client, answers: dict[str, Any]) -> None:
        """Answer ``vessel.call`` by the method asked, and record what was asked."""
        original = client.peer._handler  # noqa: SLF001

        async def handler(method: str, params: Any, notification: bool) -> Any:
            if method == "vessel.call":
                self.asked.append((params["method"], dict(params["args"])))
                answer = answers.get(params["method"])
                if isinstance(answer, Exception):
                    raise answer
                return answer
            return await original(method, params, notification)

        client.peer._handler = handler  # noqa: SLF001

    def connector(self) -> Any:
        return self.host.connectors["tinyworld"]


@pytest_asyncio.fixture
async def rig() -> AsyncIterator[Rig]:
    ports, grants = memory_ports(), MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    register_vessel_api(gateway)
    host = FakeHost()
    the = Rig(Env(gateway, grants, ports), host)
    attach_vessels(gateway, host, warn=the.warnings.append)
    try:
        yield the
    finally:
        await the.env.close()


class TestManifest:
    BASE = (
        'schema = 1\n[extension]\nid = "dev/x"\nname = "x"\nversion = "1.0.0"\n'
        '[compat]\ncore_api = ">=0.1"\n[[provides.vessels]]\n'
    )

    def test_a_connector_is_declared_with_its_flags(self) -> None:
        m = parse_manifest_text(
            self.BASE
            + 'name = "tinyworld"\nlabel = "Tiny world"\nremembers_chat = true\n'
            'capabilities = ["movement", "chat"]\n'
        )
        (v,) = m.provides.vessels
        assert (v.name, v.label, v.remembers_chat) == ("tinyworld", "Tiny world", True)
        assert v.capabilities == ("movement", "chat")

    @pytest.mark.parametrize(
        "body, expect",
        [
            ('name = "Bad"', "lowercase identifier"),
            ('name = "w"\ncapabilities = ["fly"]', "unknown flags"),
            ('name = "w"\nsecret = 1', "unknown key"),
        ],
    )
    def test_bad_declarations_are_refused(self, body: str, expect: str) -> None:
        with pytest.raises(ManifestError) as info:
            parse_manifest_text(self.BASE + body)
        assert expect in str(info.value)


class TestAttach:
    @pytest.mark.asyncio
    async def test_a_connector_exists_while_its_extension_is_connected(
        self, rig: Rig
    ) -> None:
        await rig.connect(manifest(WORLD))
        assert rig.host.meta["tinyworld"] == (
            "Tiny world",
            {"movement": True, "chat": True},
        )
        await rig.env.close()
        for _ in range(100):
            if "tinyworld" not in rig.host.connectors:
                break
            await asyncio.sleep(0.02)
        assert "tinyworld" not in rig.host.connectors

    @pytest.mark.asyncio
    async def test_without_the_grant_nothing_is_provided(self, rig: Rig) -> None:
        await rig.connect(manifest(WORLD), grant=("log",))
        assert not rig.host.connectors

    @pytest.mark.asyncio
    async def test_a_name_another_source_holds_is_skipped(self, rig: Rig) -> None:
        rig.host._taken.add("tinyworld")  # noqa: SLF001
        await rig.connect(manifest(WORLD))
        assert not rig.host.connectors
        assert any("already provided" in w for w in rig.warnings)


class TestConnector:
    @pytest.mark.asyncio
    async def test_connect_acts_and_reads_the_snapshot_the_extension_pushed(
        self, rig: Rig
    ) -> None:
        client = await rig.connect(manifest(WORLD))
        rig.reply(
            client,
            {
                "connect": {
                    "ok": True,
                    "error": None,
                    "state": {"is_connected": True, "in_world_name": "Alpha"},
                },
                "act": {"ok": True, "detail": "walked", "data": {"steps": 3}},
            },
        )
        remote = rig.connector()
        assert remote.display_name == "Tiny world"
        assert remote.is_connected is False
        events: list[Any] = []
        assert await remote.connect({"host": "h"}, events.append) is True
        assert remote.is_connected is True and remote.get_in_world_name() == "Alpha"
        result = await remote.act("move", {"x": 1})
        assert (result.ok, result.detail, result.data) == (True, "walked", {"steps": 3})
        assert rig.asked[0] == ("connect", {"settings": {"host": "h"}})
        assert rig.asked[1] == ("act", {"action": "move", "payload": {"x": 1}})

    @pytest.mark.asyncio
    async def test_a_failed_connect_carries_the_reason(self, rig: Rig) -> None:
        client = await rig.connect(manifest(WORLD))
        rig.reply(client, {"connect": {"ok": False, "error": "server is down"}})
        remote = rig.connector()
        assert await remote.connect({}, lambda e: None) is False
        assert remote.last_error == "server is down"
        assert remote.is_connected is False

    @pytest.mark.asyncio
    async def test_an_action_the_extension_fails_does_not_raise(self, rig: Rig) -> None:
        client = await rig.connect(manifest(WORLD))
        rig.reply(client, {"act": RpcError(ErrorCode.INTERNAL_ERROR, "boom")})
        result = await rig.connector().act("move", {})
        assert result.ok is False and "boom" in (result.detail or "")

    @pytest.mark.asyncio
    async def test_what_the_extension_never_sent_gets_the_base_defaults(
        self, rig: Rig
    ) -> None:
        await rig.connect(manifest(WORLD))
        remote = rig.connector()
        assert remote.describe_surroundings() is None
        assert remote.idle_busy() is False
        assert remote.get_world_actions() == {}
        assert remote.get_knowledge_wiki_sources() == []
        assert remote.get_progression_stage() is None

    @pytest.mark.asyncio
    async def test_world_state_and_goals_are_converted(self, rig: Rig) -> None:
        client = await rig.connect(manifest(WORLD))
        rig.reply(
            client,
            {
                "get_world_state": {
                    "environment": "tinyworld",
                    "health": 12.5,
                    "possible_actions": ["move"],
                },
                "get_active_goal": {"id": 4, "text": "find water"},
                "motor_step": "junk",
            },
        )
        remote = rig.connector()
        state = await remote.get_world_state()
        assert state.health == 12.5 and state.possible_actions == ["move"]
        assert await remote.get_active_goal() == {"id": 4, "text": "find water"}
        assert await remote.motor_step(None) == {
            "acted": False,
            "reason": "no_motorics",
        }

    @pytest.mark.asyncio
    async def test_a_gone_extension_is_not_connected(self, rig: Rig) -> None:
        await rig.connect(manifest(WORLD))
        remote = rig.connector()
        remote.apply_state({"is_connected": True})
        assert remote.is_connected is True
        await rig.env.close()
        for _ in range(100):
            if not remote.is_connected:
                break
            await asyncio.sleep(0.02)
        assert remote.is_connected is False
        assert await remote.probe_external_liveness() is False


class TestFromTheExtension:
    @pytest.mark.asyncio
    async def test_events_reach_the_vessel_only_while_it_is_connected(
        self, rig: Rig
    ) -> None:
        client = await rig.connect(manifest(WORLD))
        rig.reply(client, {"connect": {"ok": True}})
        remote = rig.connector()
        event = {
            "event_type": "chat",
            "summary": "hello",
            "actor": "ana",
            "salience": 0.9,
        }
        assert (await client.call("vessel.perceive", name="tinyworld", event=event))[
            "accepted"
        ] is False
        got: list[Any] = []
        await remote.connect({}, got.append)
        assert (await client.call("vessel.perceive", name="tinyworld", event=event))[
            "accepted"
        ] is True
        (seen,) = got
        assert (seen.environment, seen.event_type, seen.actor, seen.salience) == (
            "tinyworld",
            "chat",
            "ana",
            0.9,
        )
        await remote.disconnect()
        assert (await client.call("vessel.perceive", name="tinyworld", event=event))[
            "accepted"
        ] is False

    @pytest.mark.asyncio
    async def test_an_async_callback_is_awaited(self, rig: Rig) -> None:
        client = await rig.connect(manifest(WORLD))
        rig.reply(client, {"connect": {"ok": True}})
        got: list[Any] = []

        async def callback(event: Any) -> None:
            got.append(event)

        await rig.connector().connect({}, callback)
        await client.call(
            "vessel.perceive", name="tinyworld", event={"event_type": "x"}
        )
        assert len(got) == 1

    @pytest.mark.asyncio
    async def test_another_name_is_refused(self, rig: Rig) -> None:
        client = await rig.connect(manifest(WORLD))
        await client.expect(
            ErrorCode.PERMISSION_DENIED,
            "vessel.perceive",
            name="minecraft",
            event={"event_type": "x"},
        )
        await client.expect(
            ErrorCode.PERMISSION_DENIED, "vessel.state", name="minecraft", state={}
        )

    @pytest.mark.asyncio
    async def test_only_known_snapshot_keys_are_kept_and_size_is_capped(
        self, rig: Rig
    ) -> None:
        client = await rig.connect(manifest(WORLD))
        result = await client.call(
            "vessel.state",
            name="tinyworld",
            state={"idle_busy": True, "evil": 1, "last_error": "oops"},
        )
        assert result["keys"] == ["idle_busy", "last_error"]
        remote = rig.connector()
        assert remote.idle_busy() is True and "evil" not in remote.mirror
        assert remote.last_error == "oops"
        await client.expect(
            ErrorCode.QUOTA_EXCEEDED,
            "vessel.state",
            name="tinyworld",
            state={"surroundings": "x" * 70_000},
        )


class TestCoreHost:
    def test_the_vessel_registry_loads_the_remote_connector_like_any_other(
        self,
    ) -> None:
        from core.extensions.vessels import CoreVesselHost
        from core.vessel_registry import VESSEL_REGISTRY

        host = CoreVesselHost()
        marker = object()
        VESSEL_REGISTRY.register_instance("builtin_probe", marker)
        try:
            assert host.taken("builtin_probe") is True
            assert host.taken("nothing_here") is False
            host.register("tinyworld_probe", marker, "Tiny", {"chat": True})
            try:
                assert "tinyworld_probe" in VESSEL_REGISTRY.get_available_connectors()
                assert VESSEL_REGISTRY.load_connector("tinyworld_probe") is marker
                assert VESSEL_REGISTRY.get_connector_meta("tinyworld_probe")[
                    "capabilities"
                ] == {"chat": True}
            finally:
                host.unregister("tinyworld_probe")
            assert "tinyworld_probe" not in VESSEL_REGISTRY.get_available_connectors()
        finally:
            VESSEL_REGISTRY.unregister_connector("builtin_probe")


# ------------------------------------------------------------ a real extension process

import shutil  # noqa: E402

from core.extensions.launcher import Launcher  # noqa: E402
from core.extensions.manifest import load_manifest  # noqa: E402
from core.extensions.sandbox import RuntimeLayout, SandboxPolicy  # noqa: E402
from core.extensions.supervisor import RestartPolicy, Supervisor  # noqa: E402
from tests.support.waiting import wait_until  # noqa: E402
from tests.test_extensions_runtime import Harness  # noqa: E402

WORLD_DIR = Path(__file__).resolve().parent / "support" / "world_extension"


@pytest.mark.asyncio
async def test_a_connector_written_with_the_sdk_works_through_a_real_process(
    tmp_path: Path,
) -> None:
    ports, grants = memory_ports(), MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    register_vessel_api(gateway)
    host = FakeHost()
    attach_vessels(gateway, host)
    layout = RuntimeLayout(tmp_path / "extensions")
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
        m = load_manifest(WORLD_DIR / "synth-extension.toml")
        directory = layout.install_dir(m.id)
        directory.mkdir(parents=True)
        shutil.copy(WORLD_DIR / "ext_main.py", directory / "ext_main.py")
        grants.grant(m.id, *m.permissions.capabilities)
        supervisor.register(m)
        await supervisor.start(m.id)
        await wait_until(lambda: "tinyworld" in host.connectors, 20)
        remote = host.connectors["tinyworld"]

        events: list[Any] = []
        assert await remote.connect({"host": "down"}, events.append) is False
        assert remote.last_error == "the server is down"

        assert await remote.connect({"host": "ok"}, events.append) is True
        await wait_until(lambda: len(events) == 1, 5)
        assert (events[0].event_type, events[0].actor) == ("chat", "ana")
        assert remote.is_connected is True
        assert remote.describe_surroundings() == "You are in the hall."
        assert remote.get_world_actions() == {"move": {"required_fields": ["to"]}}

        moved = await remote.act("move", {"to": "kitchen"})
        assert (moved.ok, moved.data) == (True, {"room": "kitchen"})
        # the extension said the snapshot changed: the Core's copy follows at once
        await wait_until(
            lambda: remote.describe_surroundings() == "You are in the kitchen.", 5
        )
        assert (await remote.act("dance", {})).ok is False
        assert await remote.get_active_goal() == {"text": "visit the kitchen"}

        await remote.disconnect()
        assert remote.is_connected is False
        assert harness.stderr == [] or all(
            "Traceback" not in line for _, line in harness.stderr
        )
    finally:
        await supervisor.stop_all()
        await gateway.close_all()
