"""The APIs a speaking, self-starting extension needs: turns, voice and Recon."""

from __future__ import annotations

import asyncio
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
    CONTEXT_KEY,
    InterfacePorts,
    register_interface_api,
    turn_context_for,
)
from core.extensions.manifest import (
    InterfaceProvision,
    Manifest,
    Permissions,
    Provides,
    ReconProvision,
    parse_manifest_text,
)
from core.extensions.media import MediaBridge
from core.extensions.memory_ports import (
    MemoryInterfaceHost,
    MemoryMessaging,
    memory_ports,
)
from core.extensions.recon import CAP_RECON, RemoteRecon, attach_recon
from core.extensions.turns import CAP_TURNS, RATE_LIMIT, register_turn_api
from core.extensions.voice import (
    CAP_PLAY,
    CAP_SYNTHESIZE,
    CAP_TRANSCRIBE,
    register_voice_api,
)
from tests.test_extensions_gateway import Client, Env
from tests.test_extensions_interface_extras import answering
from tests.test_extensions_interfaces import PATTERNS, register

CAPS = (
    "interfaces.register",
    "interfaces.inbound",
    CAP_TURNS,
    CAP_SYNTHESIZE,
    CAP_PLAY,
    CAP_TRANSCRIBE,
    CAP_RECON,
)
SPEAK = {
    "description": "Speak on air.",
    "fields": {
        "text": {"type": "string"},
        "style": {"type": "string", "optional": True},
    },
}


def manifest(**provides: Any) -> Manifest:
    return Manifest(
        id="dev/radio",
        name="radio",
        version="1.0.0",
        core_api=">=0.1",
        permissions=Permissions(capabilities=CAPS),
        provides=Provides(
            interfaces=(
                InterfaceProvision(id="chat", paths=PATTERNS, actions=("speak",)),
            ),
            **provides,
        ),
    )


class FakeTurns:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def request(self, **kwargs: Any) -> None:
        self.requests.append(kwargs)


class FakeVoice:
    def __init__(self, audio: Path | None) -> None:
        self.audio = audio
        self.spoken: list[tuple[str, str]] = []
        self.played: list[tuple[str, str, str]] = []
        self.transcribed: list[tuple[str, str]] = []

    async def synthesize(self, text: str, engine: str) -> str | None:
        self.spoken.append((text, engine))
        return str(self.audio) if self.audio else None

    async def play(self, audio_path: str, text: str, engine: str) -> bool:
        self.played.append((audio_path, text, engine))
        return True

    heard: dict[str, Any] | None = {"text": " hello there ", "language": "en"}

    async def transcribe(
        self, audio_path: str, mime_type: str
    ) -> dict[str, Any] | None:
        self.transcribed.append((audio_path, mime_type))
        return self.heard


class FakeReconHost:
    def __init__(self, taken: set[str] | None = None) -> None:
        self.plugins: dict[str, Any] = {}
        self.taken = taken or set()

    def add(self, name: str, plugin: object) -> None:
        self.plugins[name] = plugin

    def remove(self, name: str, plugin: object) -> None:
        if self.plugins.get(name) is plugin:
            del self.plugins[name]

    def taken_keys(self) -> set[str]:
        return set(self.taken)


class Rig:
    def __init__(self, env: Env, tmp: Path) -> None:
        self.env, self.tmp = env, tmp
        self.turns = FakeTurns()
        self.core_audio = tmp / "core_made.wav"
        self.core_audio.write_bytes(b"RIFF" + b"\x00" * 60)
        self.voice = FakeVoice(self.core_audio)
        self.recon = FakeReconHost()
        self.clock = 0.0

    async def connect(
        self,
        m: Manifest | None = None,
        *,
        grant: tuple[str, ...] = CAPS,
    ) -> Client:
        m = m or manifest()
        client = await self.env.connect(m, grant=grant)
        await register(client, actions={"speak": SPEAK})
        return client


@pytest_asyncio.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    ports, grants = memory_ports(), MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    register_interface_api(
        gateway, InterfacePorts(MemoryInterfaceHost(), MemoryMessaging())
    )
    env = Env(gateway, grants, ports)
    the = Rig(env, tmp_path)
    register_turn_api(gateway, the.turns, clock=lambda: the.clock)
    bridge = MediaBridge(
        data_dir=lambda ext: tmp_path / "data" / ext,
        uid=lambda ext: None,
        inbound_root=tmp_path / "media",
        resolve_outbound=lambda raw: (None, "refused"),
    )
    (tmp_path / "data" / "dev/radio").mkdir(parents=True)
    register_voice_api(gateway, the.voice, bridge)
    attach_recon(gateway, the.recon)
    try:
        yield the
    finally:
        await env.close()


def request(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "interface_id": "chat",
        "path": "chat/room1",
        "text": "Say something about the song.",
        "allowed_actions": ["speak"],
    }
    base.update(kw)
    return base


class TestTurns:
    @pytest.mark.asyncio
    async def test_a_turn_is_queued_with_its_context_and_a_named_priority(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        got = await client.call(
            "turns.request",
            **request(
                priority="elevated", context={"track": "A"}, history_recent_max=3
            ),
        )
        assert got == {"queued": True}
        (queued,) = rig.turns.requests
        assert queued["extension_id"] == "dev/radio"
        assert queued["priority"] == "elevated"
        assert queued["context"] == {"track": "A"}
        assert queued["allowed_actions"] == ["speak"]
        assert queued["history_recent_max"] == 3
        assert queued["sender_name"] == "Chat"  # the interface's name by default

    @pytest.mark.asyncio
    async def test_a_priority_above_the_named_bands_does_not_exist(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        for bad in ("trainer", "emergency", "9"):
            await client.expect(
                ErrorCode.INVALID_PARAMS, "turns.request", **request(priority=bad)
            )
        assert rig.turns.requests == []

    @pytest.mark.asyncio
    async def test_only_actions_of_the_interface_itself_may_be_allowed(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        err = await client.expect(
            ErrorCode.INVALID_PARAMS,
            "turns.request",
            **request(allowed_actions=["speak", "agent_run_shell"]),
        )
        assert "agent_run_shell" in err.message
        await client.expect(
            ErrorCode.INVALID_PARAMS, "turns.request", **request(allowed_actions=[])
        )
        assert rig.turns.requests == []

    @pytest.mark.asyncio
    async def test_it_must_be_an_interface_the_extension_registered(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        await client.expect(
            ErrorCode.PERMISSION_DENIED,
            "turns.request",
            **request(interface_id="telegram_bot", path="telegram_bot/1"),
        )
        await client.expect(
            ErrorCode.INVALID_PARAMS,
            "turns.request",
            **request(path="other/1"),
        )
        assert rig.turns.requests == []

    @pytest.mark.asyncio
    async def test_the_capability_must_be_granted(self, rig: Rig) -> None:
        client = await rig.connect(grant=("interfaces.register", "interfaces.inbound"))
        await client.expect(ErrorCode.PERMISSION_DENIED, "turns.request", **request())

    @pytest.mark.asyncio
    async def test_context_is_small_json(self, rig: Rig) -> None:
        client = await rig.connect()
        await client.expect(
            ErrorCode.QUOTA_EXCEEDED,
            "turns.request",
            **request(context={"x": "y" * 5000}),
        )
        await client.expect(
            ErrorCode.INVALID_PARAMS, "turns.request", **request(text="   ")
        )

    @pytest.mark.asyncio
    async def test_an_extension_cannot_flood_the_model(self, rig: Rig) -> None:
        client = await rig.connect()
        for _ in range(RATE_LIMIT):
            await client.call("turns.request", **request())
        await client.expect(ErrorCode.QUOTA_EXCEEDED, "turns.request", **request())
        rig.clock += 61  # the window moves on
        await client.call("turns.request", **request())
        assert len(rig.turns.requests) == RATE_LIMIT + 1

    @pytest.mark.asyncio
    async def test_the_answer_meets_its_context_again(self, rig: Rig) -> None:
        client = await rig.connect()
        seen = answering(client, {"interface.action": {"status": "success"}})
        remote = rig.env.gateway.sessions["dev/radio"].state["interfaces"]["chat"]
        context_memory = {
            CONTEXT_KEY: {
                "extension_id": "dev/radio",
                "interface_id": "chat",
                "context": {"prev": "A", "next": "B"},
            }
        }
        await remote.execute_action(
            {"type": "speak", "payload": {"text": "hi"}}, context_memory
        )
        assert seen[0][1]["context"] == {"prev": "A", "next": "B"}

    def test_another_extensions_context_is_never_handed_over(self) -> None:
        memory = {CONTEXT_KEY: {"extension_id": "dev/other", "context": {"a": 1}}}
        assert turn_context_for(memory, "dev/radio") == {}
        assert turn_context_for(None, "dev/radio") == {}
        assert turn_context_for({CONTEXT_KEY: "junk"}, "dev/radio") == {}


class TestVoice:
    @pytest.mark.asyncio
    async def test_the_synth_voice_comes_back_as_a_file_of_the_extension(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        got = await client.call("voice.synthesize", text="Hello there", engine="kitten")
        assert got["available"] is True
        path = Path(got["path"])
        assert rig.tmp / "data" / "dev/radio" / "outbox" in path.parents
        assert path.read_bytes() == rig.core_audio.read_bytes()
        assert rig.voice.spoken == [("Hello there", "kitten")]

    @pytest.mark.asyncio
    async def test_no_engine_is_a_plain_answer_not_an_error(self, rig: Rig) -> None:
        rig.voice.audio = None
        client = await rig.connect()
        assert await client.call("voice.synthesize", text="Hi") == {"available": False}

    @pytest.mark.asyncio
    async def test_engine_names_are_plain(self, rig: Rig) -> None:
        client = await rig.connect()
        await client.expect(
            ErrorCode.INVALID_PARAMS,
            "voice.synthesize",
            text="Hi",
            engine="../../etc/passwd",
        )
        await client.expect(ErrorCode.INVALID_PARAMS, "voice.synthesize", text="  ")

    @pytest.mark.asyncio
    async def test_a_clip_of_the_extension_is_played_from_a_copy(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        clip = rig.tmp / "data" / "dev/radio" / "banter.wav"
        clip.write_bytes(b"RIFF" + b"\x01" * 40)
        got = await client.call("voice.play", path=str(clip), text="caption")
        assert got == {"delivered": True}
        (path, text, engine) = rig.voice.played[0]
        assert Path(path) != clip and Path(path).read_bytes() == clip.read_bytes()
        assert (text, engine) == ("caption", "")

    @pytest.mark.asyncio
    async def test_only_the_extensions_own_files_can_be_played(self, rig: Rig) -> None:
        client = await rig.connect()
        secret = rig.tmp / "secret.env"
        secret.write_text("KEY=1")
        for bad in (str(secret), "/etc/passwd", "../../etc/passwd"):
            err = await client.expect(ErrorCode.INVALID_PARAMS, "voice.play", path=bad)
            assert "refused" in err.message
        assert rig.voice.played == []

    @pytest.mark.asyncio
    async def test_a_recording_of_the_extension_is_heard_from_a_copy(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        clip = rig.tmp / "data" / "dev/radio" / "player.ogg"
        clip.write_bytes(b"OggS" + b"\x01" * 40)
        got = await client.call(
            "voice.transcribe", path=str(clip), mime_type="audio/ogg"
        )
        assert got == {"available": True, "text": "hello there", "language": "en"}
        (path, mime) = rig.voice.transcribed[0]
        assert Path(path) != clip and Path(path).read_bytes() == clip.read_bytes()
        assert mime == "audio/ogg"

    @pytest.mark.asyncio
    async def test_nothing_heard_is_a_plain_answer_not_an_error(self, rig: Rig) -> None:
        rig.voice.heard = None
        client = await rig.connect()
        clip = rig.tmp / "data" / "dev/radio" / "silence.wav"
        clip.write_bytes(b"RIFF" + b"\x00" * 40)
        assert await client.call("voice.transcribe", path=str(clip)) == {
            "available": False
        }

    @pytest.mark.asyncio
    async def test_only_the_extensions_own_recordings_can_be_heard(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        secret = rig.tmp / "secret.env"
        secret.write_text("KEY=1")
        for bad in (str(secret), "/etc/passwd"):
            await client.expect(ErrorCode.INVALID_PARAMS, "voice.transcribe", path=bad)
        await client.expect(
            ErrorCode.INVALID_PARAMS,
            "voice.transcribe",
            path=str(secret),
            mime_type="text/html",
        )
        assert rig.voice.transcribed == []

    @pytest.mark.asyncio
    async def test_both_need_their_grant(self, rig: Rig) -> None:
        client = await rig.connect(grant=("interfaces.register",))
        await client.expect(ErrorCode.PERMISSION_DENIED, "voice.synthesize", text="x")
        await client.expect(ErrorCode.PERMISSION_DENIED, "voice.play", path="x")
        await client.expect(ErrorCode.PERMISSION_DENIED, "voice.transcribe", path="x")


RECON = (ReconProvision("radio_status", "Is the message about the radio?", 5),)


class TestRecon:
    @pytest.mark.asyncio
    async def test_a_declared_question_is_offered_to_recon_while_connected(
        self, rig: Rig
    ) -> None:
        client = await rig.connect(manifest(recon=RECON))
        (plugin,) = rig.recon.plugins.values()
        assert isinstance(plugin, RemoteRecon)
        assert plugin.get_recon_key() == "radio_status"
        assert plugin.get_recon_instruction() == "Is the message about the radio?"
        assert plugin.recon_priority == 5
        assert plugin.get_supported_actions() == {}
        await rig.env.close()
        for _ in range(50):  # the session cleanup runs after the connection closes
            if not rig.recon.plugins:
                break
            await asyncio.sleep(0.02)
        assert rig.recon.plugins == {}
        del client

    @pytest.mark.asyncio
    async def test_the_model_answer_goes_to_the_extension_and_snippets_come_back(
        self, rig: Rig
    ) -> None:
        client = await rig.connect(manifest(recon=RECON))
        seen = answering(
            client,
            {
                "recon.contribute": [
                    {"content": "Now playing: X"},
                    {"content": "  "},
                    {"content": 5},
                    {"content": "y" * 5000},
                    {"content": "three"},
                    {"content": "four"},
                ]
            },
        )
        (plugin,) = rig.recon.plugins.values()
        got = await plugin.parse_recon_response(
            {"radio_status": True},
            context_memory={"interface_path": "chat/room1"},
            text="what is on air?",
        )
        assert seen == [
            (
                "recon.contribute",
                {
                    "key": "radio_status",
                    "answer": True,
                    "text": "what is on air?",
                    "interface_path": "chat/room1",
                },
            )
        ]
        assert [g["content"][:12] for g in got] == ["Now playing:", "y" * 12, "three"]
        assert all(
            g["type"] == "snippet" and g["source"] == "dev/radio" and g["priority"] == 5
            for g in got
        )
        assert len(got[1]["content"]) == 2000

    @pytest.mark.asyncio
    async def test_a_failing_extension_only_costs_its_own_snippet(
        self, rig: Rig
    ) -> None:
        client = await rig.connect(manifest(recon=RECON))
        answering(
            client, {"recon.contribute": RpcError(ErrorCode.INTERNAL_ERROR, "boom")}
        )
        (plugin,) = rig.recon.plugins.values()
        assert await plugin.parse_recon_response({"radio_status": True}) == []

    @pytest.mark.asyncio
    async def test_without_the_grant_the_question_is_not_asked(self, rig: Rig) -> None:
        await rig.connect(manifest(recon=RECON), grant=("interfaces.register",))
        assert rig.recon.plugins == {}

    @pytest.mark.asyncio
    async def test_a_key_the_core_already_uses_is_not_shadowed(self, rig: Rig) -> None:
        rig.recon.taken = {"radio_status"}
        await rig.connect(manifest(recon=RECON))
        assert rig.recon.plugins == {}
        bad = (ReconProvision("language_hint", "Which language?", 1),)
        rig.recon.taken = set()
        client2 = await rig.env.connect(
            Manifest(
                id="dev/other",
                name="other",
                version="1.0.0",
                core_api=">=0.1",
                permissions=Permissions(capabilities=(CAP_RECON,)),
                provides=Provides(recon=bad),
            ),
            grant=(CAP_RECON,),
        )
        assert rig.recon.plugins == {}
        del client2


class TestManifest:
    def test_recon_questions_are_validated(self) -> None:
        good = parse_manifest_text(
            """
schema = 1
[extension]
id = "dev/x"
name = "x"
version = "1.0.0"
[compat]
core_api = ">=0.1"
[[provides.recon]]
key = "radio_status"
instruction = "Is it about the radio?"
priority = 5
"""
        )
        assert good.provides.recon == (
            ReconProvision("radio_status", "Is it about the radio?", 5),
        )

    @pytest.mark.parametrize(
        "body, expect",
        [
            ('key = "Bad Key"\ninstruction = "q"', "lowercase identifier"),
            ('key = "k1"', "instruction"),
            ('key = "k1"\ninstruction = "q"\npriority = 99', "0 to 10"),
            ('key = "k1"\ninstruction = "q"\nextra = 1', "unknown key"),
        ],
    )
    def test_bad_questions_are_refused(self, body: str, expect: str) -> None:
        from core.extensions.manifest import ManifestError

        text = f"""
schema = 1
[extension]
id = "dev/x"
name = "x"
version = "1.0.0"
[compat]
core_api = ">=0.1"
[[provides.recon]]
{body}
"""
        with pytest.raises(ManifestError) as info:
            parse_manifest_text(text)
        assert expect in str(info.value)
