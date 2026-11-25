"""``core.info``: what the Core says about itself to an extension."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from core.extensions.gateway import Gateway
from core.extensions.grants import MemoryGrantStore
from core.extensions.info import CAP_INFO, register_info_api
from core.extensions.manifest import Manifest, Permissions
from core.extensions.memory_ports import memory_ports
from tests.test_extensions_gateway import Client, Env
from tests.test_extensions_runtime import h  # noqa: F401  (fixture)


def manifest(*caps: str, ext: str = "dev/api") -> Manifest:
    return Manifest(
        id=ext,
        name="api",
        version="1.0.0",
        core_api=">=0.1",
        permissions=Permissions(capabilities=caps),
    )


@pytest_asyncio.fixture
async def env() -> AsyncIterator[tuple[Env, dict[str, Any]]]:
    ports, grants = memory_ports(), MemoryGrantStore()
    gateway = Gateway(grants=grants)
    state: dict[str, Any] = {
        "synth_name": "Rei",
        "capabilities": {"text": True, "vision": False, "audio": 1},
        "in_container": True,
    }
    register_info_api(gateway, lambda: state)
    e = Env(gateway, grants, ports)
    try:
        yield e, state
    finally:
        await e.close()


async def connect(
    e: Env, *caps: str, grant: tuple[str, ...] | None = None, ext: str = "dev/api"
) -> Client:
    return await e.connect(
        manifest(*caps, ext=ext), grant=caps if grant is None else grant
    )


@pytest.mark.asyncio
async def test_returns_name_container_flag_and_only_the_true_capabilities(
    env: tuple[Env, dict[str, Any]],
) -> None:
    e, _ = env
    client = await connect(e, CAP_INFO)
    got = await client.call("core.info")
    assert got == {
        "synth_name": "Rei",
        "capabilities": {"text": True, "audio": True},
        "in_container": True,
    }


@pytest.mark.asyncio
async def test_needs_the_capability_declared_and_granted(
    env: tuple[Env, dict[str, Any]],
) -> None:
    e, _ = env
    undeclared = await connect(e)
    with pytest.raises(Exception) as err:
        await undeclared.call("core.info")
    assert "permission" in str(err.value).lower() or "capab" in str(err.value).lower()
    ungranted = await connect(e, CAP_INFO, grant=(), ext="dev/other")
    with pytest.raises(Exception):
        await ungranted.call("core.info")


@pytest.mark.asyncio
async def test_a_malformed_loader_result_is_sanitised(
    env: tuple[Env, dict[str, Any]],
) -> None:
    e, state = env
    state.update(synth_name=None, capabilities="nope", in_container=0)
    client = await connect(e, CAP_INFO)
    assert await client.call("core.info") == {
        "synth_name": "",
        "capabilities": {},
        "in_container": False,
    }
