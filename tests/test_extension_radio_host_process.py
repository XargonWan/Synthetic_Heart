"""The Radio DJ extension as a real process: a fake station, the real gateway and sandbox.

The whole path of one remark: the station says a song is ending, the extension asks the
Core for a background turn, the model answers with ``radio_speak`` (played here by the
test through the real ``RemoteInterface``), the Core's voice makes the clip, ffmpeg
converts it, the WebDJ receives it, the stage mirrors it and the Radio window can replay it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
import wave
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
    CONTEXT_KEY,
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
from core.extensions.recon import attach_recon
from core.extensions.sandbox import RuntimeLayout, SandboxPolicy
from core.extensions.supervisor import RestartPolicy, Supervisor
from core.extensions.turns import register_turn_api
from core.extensions.voice import register_voice_api
from tests.support.fake_azuracast import FakeAzuraCast
from tests.support.fake_fluxer import wait_until
from tests.test_extensions_runtime import Harness
from tests.test_extensions_turns_voice_recon import FakeReconHost, FakeTurns

SOURCE = Path(__file__).resolve().parent.parent / "extensions_src" / "radio_host"
EXT_ID = "official/radio_host"
FILES = (
    "ext_main.py",
    "radio.py",
    "jingle.py",
    "azuracast_client.py",
    "track_monitor.py",
    "radiolog.py",
)


class CoreVoice:
    """What the Core's voice does: a clip made on request, and a record of the stage."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.played: list[tuple[str, str, str]] = []

    async def synthesize(self, text: str, engine: str) -> str | None:
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / "voice.wav"
        with wave.open(str(path), "wb") as clip:
            clip.setnchannels(1)
            clip.setsampwidth(2)
            clip.setframerate(24000)
            clip.writeframes(b"\x00\x10" * 12000)  # half a second
        return str(path)

    async def play(self, audio_path: str, text: str, engine: str) -> bool:
        self.played.append((audio_path, text, engine))
        return True


class Rig(SimpleNamespace):
    harness: Harness
    host: MemoryInterfaceHost
    turns: FakeTurns
    voice: CoreVoice
    recon: FakeReconHost
    station: FakeAzuraCast


@pytest.fixture(scope="session")
def libs(tmp_path_factory: pytest.TempPathFactory) -> Path:
    uv = shutil.which("uv")
    if uv is None or shutil.which("ffmpeg") is None:
        pytest.skip("uv and ffmpeg are needed")
    target = tmp_path_factory.mktemp("radio_lib") / "lib"
    done = subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--offline",
            "--target",
            str(target),
            "httpx>=0.27",
            "websockets>=12",
        ],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        pytest.skip(f"cannot build lib/ offline: {done.stderr.strip()[-200:]}")
    return target


@pytest_asyncio.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    station = FakeAzuraCast()
    await station.start()
    ports, grants = memory_ports(), MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    host = MemoryInterfaceHost()
    layout = RuntimeLayout(tmp_path / "extensions")
    media = MediaBridge(
        data_dir=layout.data_dir,
        uid=lambda ext: None,
        inbound_root=tmp_path / "media",
        resolve_outbound=lambda raw: (None, "refused"),
    )
    register_interface_api(gateway, InterfacePorts(host, MemoryMessaging(), media))
    turns, voice, recon = (
        FakeTurns(),
        CoreVoice(tmp_path / "core_voice"),
        FakeReconHost(),
    )
    register_turn_api(gateway, turns)
    register_voice_api(gateway, voice, media)
    attach_recon(gateway, recon)
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
        yield Rig(
            harness=harness,
            host=host,
            turns=turns,
            voice=voice,
            recon=recon,
            station=station,
        )
    finally:
        await supervisor.stop_all()
        await gateway.close_all()
        await station.stop()


def install(rig: Rig, lib: Path, **config: Any) -> None:
    h = rig.harness
    manifest = load_manifest(SOURCE / "synth-extension.toml")
    assert manifest.id == EXT_ID
    directory = h.layout.install_dir(EXT_ID)
    directory.mkdir(parents=True)
    for name in FILES:
        shutil.copy(SOURCE / name, directory / name)
    (directory / "lib").symlink_to(lib)
    h.grants.grant(EXT_ID, *manifest.permissions.capabilities)
    config_port = h.ports.config
    assert isinstance(config_port, MemoryConfig)
    config_port.values[EXT_ID] = {
        "AZURACAST_BASE_URL": rig.station.base,
        "AZURACAST_API_KEY": "test-key",
        "AZURACAST_STATION_ID": "main",
        "RADIO_HOST_POLL_INTERVAL_S": 1,
        "AZURACAST_STREAMER_USERNAME": "SyntH",
        **config,
    }
    h.supervisor.register(manifest)


async def start(rig: Rig, lib: Path, **config: Any) -> RemoteInterface:
    install(rig, lib, **config)
    await rig.harness.supervisor.start(EXT_ID)
    try:
        await wait_until(lambda: "radio_host" in rig.host.interfaces, 20)
    except TimeoutError:
        pytest.fail(
            f"not up: status={rig.harness.supervisor.status()} stderr={rig.harness.stderr}"
        )
    remote = rig.host.interfaces["radio_host"]
    assert isinstance(remote, RemoteInterface)
    return remote


async def web_call(rig: Rig, path: str, **query: str) -> dict[str, Any]:
    session = rig.harness.gateway.sessions[EXT_ID]
    answer = await session.call(
        "web.request",
        {"method": "GET", "path": path, "query": query, "headers": {}, "body": ""},
        timeout=5,
    )
    return {
        "status": answer["status"],
        "headers": answer["headers"],
        "body": base64.b64decode(answer["body"]),
    }


@pytest.mark.asyncio
async def test_the_interface_is_a_background_one_with_its_own_speaking_action(
    rig: Rig, libs: Path
) -> None:
    remote = await start(rig, libs)
    assert set(remote.spec.actions) == {"radio_speak", "radio_update_metadata"}
    assert remote.spec.patterns == ("radio_host/{chat}",)
    # the Recon question is on offer while it runs
    (plugin,) = rig.recon.plugins.values()
    assert plugin.get_recon_key() == "radio_status"


@pytest.mark.asyncio
async def test_a_remark_from_the_songs_end_to_the_air_the_stage_and_the_window(
    rig: Rig, libs: Path
) -> None:
    remote = await start(rig, libs)

    # 1. the song is about to end: the extension asks the Core for a turn
    await wait_until(lambda: rig.turns.requests, 25)
    request = rig.turns.requests[0]
    assert request["extension_id"] == EXT_ID and request["interface_id"] == "radio_host"
    assert (
        request["allowed_actions"] == ["radio_speak"]
        and request["priority"] == "elevated"
    )
    assert "JUST FINISHED: 'I Tried'" in request["text"]
    assert request["context"]["radio_pregenerate_prev_title"] == "I Tried"

    # 2. the model answers with its action; the turn's context comes back with it
    memory = {
        CONTEXT_KEY: {
            "extension_id": EXT_ID,
            "interface_id": "radio_host",
            "context": request["context"],
        }
    }
    done = await remote.execute_action(
        {"type": "radio_speak", "payload": {"text": "Bye, I Tried!", "style": "outro"}},
        memory,
    )
    assert done["status"] == "success"

    # 3. it airs through the WebDJ when the song ends, and the stage mirrors it
    await wait_until(lambda: rig.voice.played, 30)
    station = rig.station
    assert station.webdj_subprotocols == ["webcast"]
    hello = next(m for m in station.webdj_text if m["type"] == "hello")
    assert hello["data"]["user"] == "SyntH" and hello["data"]["mime"].startswith(
        "audio/webm"
    )
    assert any(
        m["type"] == "metadata" and "is speaking" in m["data"]["title"]
        for m in station.webdj_text
    )
    assert station.webdj_bytes > 100
    (_clip, caption, _engine) = rig.voice.played[0]
    assert caption == "Bye, I Tried!"

    # 4. the window lists it and can replay it
    data = json.loads((await web_call(rig, "data"))["body"])
    assert data["configured"] and data["online"] and data["station_name"] == "Radio Dee"
    (row,) = data["activities"]
    assert row["banter_text"] == "Bye, I Tried!" and row["status"] == "success"
    audio = await web_call(rig, "audio", id=str(row["id"]))
    assert audio["status"] == 200 and audio["headers"]["content-type"] == "audio/webm"
    assert len(audio["body"]) > 100
    stored = rig.harness.stored(EXT_ID, "activities")
    assert stored[0]["id"] == row["id"]


@pytest.mark.asyncio
async def test_the_recon_question_adds_the_live_status(rig: Rig, libs: Path) -> None:
    await start(rig, libs)
    await wait_until(lambda: rig.station.api_calls, 15)
    (plugin,) = rig.recon.plugins.values()
    asked = await plugin.parse_recon_response(
        {"radio_status": True}, text="what is on air?"
    )
    (snippet,) = asked
    assert (
        "Bone Thugs - I Tried" in snippet["content"]
        and "Radio Dee" in snippet["content"]
    )
    assert snippet["source"] == EXT_ID
    assert await plugin.parse_recon_response({"radio_status": False}) == []


@pytest.mark.asyncio
async def test_without_settings_it_starts_idle_and_the_window_says_so(
    rig: Rig, libs: Path
) -> None:
    await start(rig, libs, AZURACAST_BASE_URL="", AZURACAST_API_KEY="")
    data = json.loads((await web_call(rig, "data"))["body"])
    assert data["configured"] is False and data["online"] is False
    await asyncio.sleep(0.3)
    assert rig.station.api_calls == [] and rig.turns.requests == []


@pytest.mark.asyncio
async def test_the_station_credentials_never_leave_the_extension(
    rig: Rig, libs: Path
) -> None:
    await start(rig, libs)
    data = (await web_call(rig, "data"))["body"].decode()
    assert "test-key" not in data and "synthradio" not in data
