"""The endpoints API: an extension fronting an engine as an external endpoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from synth_sdk.rpc import ErrorCode

from core.extensions.endpoints import CAP_ENDPOINTS, EndpointInfo, register_endpoint_api
from core.extensions.gateway import Gateway
from core.extensions.grants import MemoryGrantStore
from core.extensions.manifest import (
    Manifest,
    Permissions,
    ProtocolProvision,
    Provides,
)
from core.extensions.memory_ports import memory_ports
from tests.test_extensions_gateway import Client, Env


class FakePort:
    def __init__(self) -> None:
        self.rows: dict[str, EndpointInfo] = {}
        self.calls: list[tuple[str, Any]] = []
        self._next = 1

    async def get(self, name: str) -> EndpointInfo | None:
        return self.rows.get(name)

    async def count_cortex(self) -> int:
        return sum(1 for row in self.rows.values() if row.extra_config.get("_cortex"))

    async def add(
        self,
        name: str,
        display_label: str,
        base_url: str,
        protocol: str,
        extra_config: dict[str, Any],
        subsystem_map: dict[str, bool],
    ) -> int:
        self.calls.append(
            (
                "add",
                (name, display_label, base_url, protocol, extra_config, subsystem_map),
            )
        )
        row = EndpointInfo(self._next, name, base_url, False, dict(extra_config))
        self._next += 1
        self.rows[name] = row
        return row.id

    async def update(
        self,
        endpoint_id: int,
        *,
        base_url: str | None = None,
        extra_config: dict[str, Any] | None = None,
    ) -> None:
        self.calls.append(("update", (endpoint_id, base_url, extra_config)))
        row = next(r for r in self.rows.values() if r.id == endpoint_id)
        if base_url is not None:
            row.base_url = base_url
        if extra_config is not None:
            row.extra_config = dict(extra_config)


def manifest(*names: str, caps: tuple[str, ...] = (CAP_ENDPOINTS,)) -> Manifest:
    return Manifest(
        id="dev/engine",
        name="engine",
        version="1.0.0",
        core_api=">=0.1",
        permissions=Permissions(capabilities=caps),
        provides=Provides(endpoints=names),
    )


@pytest_asyncio.fixture
async def rig() -> AsyncIterator[tuple[Env, FakePort]]:
    ports, grants = memory_ports(), MemoryGrantStore()
    gateway = Gateway(grants=grants)
    port = FakePort()
    register_endpoint_api(gateway, port)
    env = Env(gateway, grants, ports)
    try:
        yield env, port
    finally:
        await env.close()


async def connect(env: Env, *names: str, grant: bool = True) -> Client:
    m = manifest(*names)
    return await env.connect(m, grant=(CAP_ENDPOINTS,) if grant else ())


URL = "http://engine:8000"


@pytest.mark.asyncio
async def test_a_declared_endpoint_is_created(rig: tuple[Env, FakePort]) -> None:
    env, port = rig
    client = await connect(env, "my-engine")
    got = await client.call(
        "endpoints.ensure",
        name="my-engine",
        base_url=URL,
        display_label="My engine",
        extra_config={"timeout": 300},
        subsystem_map={"cortex": True, "vox": False},
    )
    assert got == {"created": True, "id": 1}
    assert port.calls == [
        (
            "add",
            (
                "my-engine",
                "My engine",
                URL,
                "openai",
                {"timeout": 300},
                {"cortex": True, "vox": False},
            ),
        )
    ]
    assert await client.call("endpoints.get", name="my-engine") == {
        "exists": True,
        "id": 1,
        "base_url": URL,
        "has_key": False,
    }


@pytest.mark.asyncio
async def test_an_undeclared_name_is_refused(rig: tuple[Env, FakePort]) -> None:
    env, port = rig
    port.rows["openai"] = EndpointInfo(7, "openai", "https://api.openai.com", True)
    client = await connect(env, "my-engine")
    await client.expect(ErrorCode.PERMISSION_DENIED, "endpoints.get", name="openai")
    await client.expect(
        ErrorCode.PERMISSION_DENIED, "endpoints.ensure", name="openai", base_url=URL
    )
    assert port.calls == []


@pytest.mark.asyncio
async def test_without_the_grant_nothing_works(rig: tuple[Env, FakePort]) -> None:
    env, port = rig
    client = await connect(env, "my-engine", grant=False)
    await client.expect(ErrorCode.PERMISSION_DENIED, "endpoints.get", name="my-engine")
    assert port.calls == []


@pytest.mark.asyncio
async def test_an_endpoint_with_a_key_is_never_touched(
    rig: tuple[Env, FakePort],
) -> None:
    env, port = rig
    port.rows["my-engine"] = EndpointInfo(
        3, "my-engine", "https://operator.example", True
    )
    client = await connect(env, "my-engine")
    await client.expect(
        ErrorCode.PERMISSION_DENIED,
        "endpoints.ensure",
        name="my-engine",
        base_url="http://evil.example",
        update_base_url=True,
    )
    assert (
        port.calls == []
        and port.rows["my-engine"].base_url == "https://operator.example"
    )
    assert (await client.call("endpoints.get", name="my-engine"))["has_key"] is True


@pytest.mark.asyncio
async def test_an_existing_endpoint_only_gains_what_it_lacks(
    rig: tuple[Env, FakePort],
) -> None:
    env, port = rig
    port.rows["my-engine"] = EndpointInfo(
        2, "my-engine", "http://operator:9", False, {"timeout": 60}
    )
    client = await connect(env, "my-engine")
    got = await client.call(
        "endpoints.ensure",
        name="my-engine",
        base_url=URL,
        extra_config={"timeout": 300, "downstream_char_budget": 24000},
    )
    assert got == {
        "created": False,
        "id": 2,
        "added": ["downstream_char_budget"],
        "base_url_changed": False,
    }
    row = port.rows["my-engine"]
    assert row.base_url == "http://operator:9"  # not repointed without asking
    assert row.extra_config == {"timeout": 60, "downstream_char_budget": 24000}


@pytest.mark.asyncio
async def test_the_base_url_changes_only_when_asked(rig: tuple[Env, FakePort]) -> None:
    env, port = rig
    port.rows["my-engine"] = EndpointInfo(2, "my-engine", "http://old:1", False)
    client = await connect(env, "my-engine")
    got = await client.call(
        "endpoints.ensure", name="my-engine", base_url=URL, update_base_url=True
    )
    assert got["base_url_changed"] is True and port.rows["my-engine"].base_url == URL
    again = await client.call(
        "endpoints.ensure", name="my-engine", base_url=URL, update_base_url=True
    )
    assert again["base_url_changed"] is False
    assert [c[0] for c in port.calls] == ["update"]  # the second call changed nothing


@pytest.mark.asyncio
async def test_it_is_not_added_behind_a_configured_operator(
    rig: tuple[Env, FakePort],
) -> None:
    env, port = rig
    port.rows["other"] = EndpointInfo(1, "other", "http://x", False, {"_cortex": True})
    client = await connect(env, "my-engine")
    got = await client.call(
        "endpoints.ensure", name="my-engine", base_url=URL, only_if_no_cortex=True
    )
    assert got["created"] is False and "my-engine" not in port.rows


@pytest.mark.asyncio
async def test_a_voice_or_hearing_engine_is_not_a_configured_operator(
    rig: tuple[Env, FakePort],
) -> None:
    """A fresh install already holds the local voice and hearing endpoints: they must not stop
    the Zen endpoint from being created."""
    env, port = rig
    port.rows["kitten"] = EndpointInfo(1, "kitten", "", False)
    client = await connect(env, "my-engine")
    got = await client.call(
        "endpoints.ensure", name="my-engine", base_url=URL, only_if_no_cortex=True
    )
    assert got["created"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        {"base_url": "ftp://x"},
        {"base_url": "http://user:pw@host"},
        {"base_url": "not a url"},
        {"extra_config": {"_managed_by": "x"}},
        {"extra_config": {"k" * 65: 1}},
        {"extra_config": {"big": "x" * 5000}},
        {"subsystem_map": {"warp": True}},
        {"subsystem_map": {"cortex": "yes"}},
        {"protocol": "gemini"},
        {"name": "Bad Name"},
    ],
)
async def test_bad_parameters_are_refused(
    rig: tuple[Env, FakePort], bad: dict[str, Any]
) -> None:
    env, port = rig
    client = await connect(env, "my-engine")
    params: dict[str, Any] = {"name": "my-engine", "base_url": URL, **bad}
    await client.expect(ErrorCode.INVALID_PARAMS, "endpoints.ensure", **params)
    assert port.calls == []


@pytest.mark.asyncio
async def test_a_local_engine_endpoint_has_no_address_and_its_own_protocol(
    rig: tuple[Env, FakePort],
) -> None:
    """An engine that runs inside the extension (a local voice) has nothing to connect to."""
    env, port = rig
    m = manifest("local-voice")
    m = Manifest(
        id=m.id,
        name=m.name,
        version=m.version,
        core_api=m.core_api,
        permissions=m.permissions,
        provides=Provides(
            endpoints=("local-voice",),
            protocols=(ProtocolProvision("localvoice", "", ("tts",)),),
        ),
    )
    client = await env.connect(m, grant=(CAP_ENDPOINTS,))
    got = await client.call(
        "endpoints.ensure",
        name="local-voice",
        base_url="",
        protocol="localvoice",
        subsystem_map={"vox": True},
    )
    assert got["created"] is True
    assert port.calls[0][1][2:4] == ("", "localvoice")
    # a protocol the extension does not provide (nor the OpenAI one) is refused
    await client.expect(
        ErrorCode.INVALID_PARAMS,
        "endpoints.ensure",
        name="local-voice",
        base_url="",
        protocol="harmony",
    )
    # an address, when given, must still be http(s)
    await client.expect(
        ErrorCode.INVALID_PARAMS,
        "endpoints.ensure",
        name="local-voice",
        base_url="ftp://x",
        protocol="localvoice",
    )
