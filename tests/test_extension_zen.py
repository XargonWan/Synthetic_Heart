"""The Zen extension: it registers one endpoint, politely."""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from core.extensions.api import register_core_api
from core.extensions.endpoints import EndpointInfo, register_endpoint_api
from core.extensions.gateway import Gateway
from core.extensions.grants import MemoryGrantStore
from core.extensions.launcher import Launcher
from core.extensions.manifest import load_manifest
from core.extensions.memory_ports import MemoryConfig, memory_ports
from core.extensions.sandbox import RuntimeLayout, SandboxPolicy
from core.extensions.supervisor import RestartPolicy, Supervisor
from tests.support.fake_fluxer import wait_until
from tests.test_extension_openai_api import free_port  # noqa: F401  (helper reuse)
from tests.test_extensions_endpoints import FakePort
from tests.test_extensions_runtime import Harness

SOURCE = Path(__file__).resolve().parent.parent / "extensions_src" / "zen"
EXT_ID = "official/zen"


@pytest_asyncio.fixture
async def rig(tmp_path: Path) -> AsyncIterator[SimpleNamespace]:
    ports, grants = memory_ports(), MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    port = FakePort()
    register_endpoint_api(gateway, port)
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
        yield SimpleNamespace(harness=harness, port=port)
    finally:
        await supervisor.stop_all()
        await gateway.close_all()


async def run(rig: SimpleNamespace, **config: Any) -> None:
    h = rig.harness
    manifest = load_manifest(SOURCE / "synth-extension.toml")
    directory = h.layout.install_dir(EXT_ID)
    directory.mkdir(parents=True)
    for name in ("ext_main.py", "convert.py"):
        shutil.copy(SOURCE / name, directory / name)
    h.grants.grant(EXT_ID, *manifest.permissions.capabilities)
    config_port = h.ports.config
    assert isinstance(config_port, MemoryConfig)
    config_port.values[EXT_ID] = dict(config)
    h.supervisor.register(manifest)
    await h.supervisor.start(EXT_ID)
    await wait_until(lambda: h.supervisor.status()[EXT_ID]["state"] == "running", 15)
    await asyncio.sleep(0.5)  # main() has made its calls


import asyncio  # noqa: E402

URL = "http://synth-zen-llm-engine:8000"


@pytest.mark.asyncio
async def test_a_fresh_install_gets_the_endpoint_with_zens_settings(
    rig: SimpleNamespace,
) -> None:
    await run(rig)
    row = rig.port.rows["zen-llm-engine"]
    assert row.base_url == URL
    assert row.extra_config == {"timeout": 300, "downstream_char_budget": 24000}
    [(kind, args)] = rig.port.calls
    assert kind == "add"
    assert args[1] == "Zen LLM Engine"
    assert args[5] == {
        "cortex": True,
        "vox": False,
        "auris": False,
        "live": False,
        "vision": True,
    }


@pytest.mark.asyncio
async def test_the_engine_url_setting_is_used(rig: SimpleNamespace) -> None:
    await run(rig, ZEN_BASE_URL="http://zen.lan:9000")
    assert rig.port.rows["zen-llm-engine"].base_url == "http://zen.lan:9000"


@pytest.mark.asyncio
async def test_it_is_not_added_behind_a_configured_operator(
    rig: SimpleNamespace,
) -> None:
    rig.port.rows["openai"] = EndpointInfo(1, "openai", "https://api.openai.com", True)
    await run(rig)
    assert "zen-llm-engine" not in rig.port.rows and rig.port.calls == []


@pytest.mark.asyncio
async def test_an_existing_endpoint_keeps_what_the_operator_set(
    rig: SimpleNamespace,
) -> None:
    rig.port.rows["zen-llm-engine"] = EndpointInfo(
        4, "zen-llm-engine", "http://custom:1", False, {"timeout": 60}
    )
    await run(rig)
    row = rig.port.rows["zen-llm-engine"]
    assert row.base_url == "http://custom:1"  # not repointed
    assert row.extra_config == {"timeout": 60, "downstream_char_budget": 24000}


@pytest.mark.asyncio
async def test_a_stale_container_hostname_is_corrected(rig: SimpleNamespace) -> None:
    rig.port.rows["zen-llm-engine"] = EndpointInfo(
        4,
        "zen-llm-engine",
        "http://synth-selenium-llm-engine:8000",
        False,
        {"timeout": 300, "downstream_char_budget": 24000},
    )
    await run(rig)
    assert rig.port.rows["zen-llm-engine"].base_url == URL


@pytest.mark.asyncio
async def test_a_keyed_endpoint_is_left_alone(rig: SimpleNamespace) -> None:
    rig.port.rows["zen-llm-engine"] = EndpointInfo(
        4, "zen-llm-engine", "http://synth-selenium-llm-engine:8000", True
    )
    await run(rig)
    assert rig.port.calls == []
    status = rig.harness.supervisor.status()["official/zen"]
    assert status["state"] == "running" and status["restarts"] == 0


def test_the_manifest_declares_only_its_own_endpoint() -> None:
    manifest = load_manifest(SOURCE / "synth-extension.toml")
    assert manifest.provides.endpoints == ("zen-llm-engine",)
    assert "endpoints.manage" in manifest.permissions.capabilities
