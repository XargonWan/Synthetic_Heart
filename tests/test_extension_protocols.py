"""The protocol extensions (Harmony, Fish) and the way the Core lets an extension provide one.

Three layers: the Core side (a protocol appears while its extension is connected, operations
are sent to it, the rest fall back to the base protocol, audio travels as files), the two
extensions' own logic (against a fake HTTP service), and one real extension process.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from aiohttp import web

from core.external_endpoints.adapters.base import (
    BaseProtocolAdapter,
    ChatResponse,
    ModelInfo,
)
from core.external_endpoints.models import EndpointProtocol, ExternalEndpoint
from core.external_endpoints.preset_registry import load_presets
from core.extensions.api import register_core_api
from core.extensions.gateway import Gateway
from core.extensions.grants import MemoryGrantStore
from core.extensions.interfaces import InterfacePorts, register_interface_api
from core.extensions.launcher import Launcher
from core.extensions.manifest import (
    Manifest,
    Permissions,
    ProtocolProvision,
    Provides,
    load_manifest,
    parse_manifest_text,
)
from core.extensions.media import MediaBridge
from core.extensions.memory_ports import (
    MemoryInterfaceHost,
    MemoryMessaging,
    memory_ports,
)
from core.extensions.protocols import CAP_PROTOCOL, attach_protocols
from core.extensions.sandbox import RuntimeLayout, SandboxPolicy
from core.extensions.supervisor import RestartPolicy, Supervisor
from tests.support.fake_fluxer import wait_until
from tests.test_extensions_gateway import Client, Env
from tests.test_extensions_interface_extras import answering
from tests.test_extensions_runtime import Harness

ROOT = Path(__file__).resolve().parent.parent
HARMONY_DIR = ROOT / "extensions_src" / "protocol_harmony"
FISH_DIR = ROOT / "extensions_src" / "protocol_fish"


def load(directory: Path, names: tuple[str, ...]) -> dict[str, Any]:
    """Import an extension's modules the way its process does, then forget them: names
    like ``harmony`` and ``fish`` are too generic to leave in ``sys.modules``."""
    sys.path.insert(0, str(directory))
    try:
        return {n: importlib.import_module(n) for n in names}
    finally:
        sys.path.remove(str(directory))
        for n in names:
            sys.modules.pop(n, None)


harmony = load(HARMONY_DIR, ("harmony",))["harmony"]
fish = load(FISH_DIR, ("fish",))["fish"]


def endpoint(protocol: str, **kw: Any) -> ExternalEndpoint:
    base: dict[str, Any] = dict(
        id=1,
        name="ep",
        display_label="ep",
        protocol=EndpointProtocol(protocol),
        base_url="https://api.example.org/v1",
        api_key_enc=None,
        enabled=True,
        capabilities={},
        subsystem_map={},
        available_models=[],
        default_model="m1",
        probe_status="never",
        last_probe_at=None,
        extra_config={"timeout": 5},
    )
    base.update(kw)
    return ExternalEndpoint(**base)


# --------------------------------------------------------------------------- manifests


class TestManifests:
    @pytest.mark.parametrize("directory", [HARMONY_DIR, FISH_DIR])
    def test_the_extensions_declare_their_protocol_and_the_key_grant(
        self, directory: Path
    ) -> None:
        manifest = load_manifest(directory / "synth-extension.toml")
        (protocol,) = manifest.provides.protocols
        assert protocol.name == directory.name.removeprefix("protocol_")
        assert CAP_PROTOCOL in manifest.permissions.capabilities
        assert (directory / "guide.md").is_file() and (directory / "icon.svg").is_file()

    def test_harmony_answers_only_what_is_not_openai_shaped(self) -> None:
        (protocol,) = load_manifest(
            HARMONY_DIR / "synth-extension.toml"
        ).provides.protocols
        assert protocol.base == "openai"
        assert set(protocol.ops) == {"models", "tts", "stt"}

    def test_fish_is_a_voice_provider_without_chat_or_base(self) -> None:
        (protocol,) = load_manifest(
            FISH_DIR / "synth-extension.toml"
        ).provides.protocols
        assert protocol.base == "" and "chat" not in protocol.ops
        assert {
            "tts",
            "list_speakers",
            "fetch_model_detail",
            "parse_model_id_from_url",
        } <= set(protocol.ops)

    @pytest.mark.parametrize(
        "body, expect",
        [
            ('name = "Bad"\nops = ["tts"]', "lowercase identifier"),
            ('name = "p"\nops = ["fly"]', "unknown operations"),
            ('name = "p"\nops = []', "at least one operation"),
            ('name = "p"\nbase = "p"\nops = ["tts"]', "own base"),
            ('name = "p"\nops = ["tts"]\nkey = 1', "unknown key"),
        ],
    )
    def test_bad_declarations_are_refused(self, body: str, expect: str) -> None:
        from core.extensions.manifest import ManifestError

        text = (
            'schema = 1\n[extension]\nid = "dev/x"\nname = "x"\nversion = "1.0.0"\n'
            '[compat]\ncore_api = ">=0.1"\n[[provides.protocols]]\n' + body
        )
        with pytest.raises(ManifestError) as info:
            parse_manifest_text(text)
        assert expect in str(info.value)

    def test_the_presets_still_name_the_two_protocols(self) -> None:
        presets = {p["provider_id"]: p for p in load_presets()}
        assert presets["fish_audio"]["protocol"] == "fish"
        assert presets["fish_audio"]["category"] == "tts"
        assert presets["harmonyai"]["protocol"] == "harmony"


# ------------------------------------------------------------------------ the Core side


class FakeHost:
    def __init__(self, builtin: set[str] | None = None) -> None:
        self.factories: dict[str, Callable[[Any, str], Any]] = {}
        self._builtin = builtin or {"openai", "custom"}
        self.base_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.factories["openai"] = lambda ep, key: FakeBase(self, ep, key)

    def builtin(self, name: str) -> bool:
        return name in self._builtin

    def factory_of(self, name: str) -> Callable[[Any, str], Any] | None:
        return self.factories.get(name)

    def register(self, name: str, factory: Callable[[Any, str], Any]) -> None:
        self.factories[name] = factory

    def unregister(self, name: str) -> None:
        self.factories.pop(name, None)


class FakeBase(BaseProtocolAdapter):
    """The Core's base-protocol adapter: records what it was asked."""

    def __init__(self, host: FakeHost, ep: Any, key: str) -> None:
        self.host, self.ep, self.key = host, ep, key

    async def chat_completion(self, messages, model=None, stream=False, **kwargs):
        self.host.base_calls.append(("chat", (messages, model)))
        return ChatResponse(content="from the core", model=model or "")

    async def list_models(self) -> list[ModelInfo]:
        self.host.base_calls.append(("models", ()))
        return [ModelInfo(id="base", name="Base")]

    async def probe_capabilities(self, models=None):
        self.host.base_calls.append(("capabilities", ()))
        return {"cortex": True}

    async def health_check(self) -> bool:
        return True

    async def ping_test(self, model=None, timeout=15.0, models=None):
        self.host.base_calls.append(("ping", (model,)))
        return True, "pong"


def manifest(
    *protocols: ProtocolProvision, caps: tuple[str, ...] = (CAP_PROTOCOL,)
) -> Manifest:
    return Manifest(
        id="dev/proto",
        name="proto",
        version="1.0.0",
        core_api=">=0.1",
        permissions=Permissions(capabilities=caps),
        provides=Provides(protocols=protocols),
    )


HARMONY = ProtocolProvision("harmony", "openai", ("models", "tts", "stt"))
FISHY = ProtocolProvision(
    "fishy", "", ("models", "tts", "list_speakers", "parse_model_id_from_url")
)


class Rig:
    def __init__(self, env: Env, host: FakeHost, tmp: Path) -> None:
        self.env, self.host, self.tmp = env, host, tmp
        self.warnings: list[str] = []

    async def connect(
        self, m: Manifest, grant: tuple[str, ...] = (CAP_PROTOCOL,)
    ) -> Client:
        return await self.env.connect(m, grant=grant)

    def adapter(self, name: str, **kw: Any) -> Any:
        return self.host.factories[name](endpoint(name, **kw), "SECRET")


@pytest_asyncio.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    ports, grants = memory_ports(), MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    host = FakeHost()
    media = MediaBridge(
        data_dir=lambda ext: tmp_path / "data" / ext,
        uid=lambda ext: None,
        inbound_root=tmp_path / "media",
        resolve_outbound=lambda raw: (None, "refused"),
    )
    (tmp_path / "data" / "dev/proto" / "inbox").mkdir(parents=True)
    the = Rig(Env(gateway, grants, ports), host, tmp_path)
    attach_protocols(gateway, host, media, warn=the.warnings.append)
    try:
        yield the
    finally:
        await the.env.close()


class TestAttach:
    @pytest.mark.asyncio
    async def test_a_protocol_exists_while_its_extension_is_connected(
        self, rig: Rig
    ) -> None:
        assert "harmony" not in rig.host.factories
        await rig.connect(manifest(HARMONY))
        assert "harmony" in rig.host.factories
        await rig.env.close()
        for _ in range(100):
            if "harmony" not in rig.host.factories:
                break
            await asyncio.sleep(0.02)
        assert "harmony" not in rig.host.factories

    @pytest.mark.asyncio
    async def test_without_the_grant_nothing_is_provided(self, rig: Rig) -> None:
        await rig.connect(manifest(HARMONY), grant=("log",))
        assert "harmony" not in rig.host.factories

    @pytest.mark.asyncio
    async def test_a_builtin_protocol_cannot_be_taken_over(self, rig: Rig) -> None:
        original = rig.host.factories["openai"]
        await rig.connect(manifest(ProtocolProvision("openai", "", ("tts",))))
        assert rig.host.factories["openai"] is original
        assert any("built into the Core" in w for w in rig.warnings)

    @pytest.mark.asyncio
    async def test_a_missing_base_protocol_skips_the_dialect(self, rig: Rig) -> None:
        await rig.connect(manifest(ProtocolProvision("odd", "nothere", ("tts",))))
        assert "odd" not in rig.host.factories
        assert any("needs the 'nothere' protocol" in w for w in rig.warnings)


class TestOperations:
    @pytest.mark.asyncio
    async def test_a_listed_operation_goes_to_the_extension_with_the_key(
        self, rig: Rig
    ) -> None:
        client = await rig.connect(manifest(HARMONY))
        seen = answering(
            client,
            {
                "protocol.request": [
                    {
                        "id": "m1",
                        "name": "Model One",
                        "capabilities": {"vox": 1},
                        "languages": [{"code": "EN", "name": "English"}],
                    },
                    {"name": "no id: dropped"},
                    "junk",
                ]
            },
        )
        models = await rig.adapter("harmony").list_models()
        assert [m.id for m in models] == ["m1"]
        assert models[0].capabilities == {"vox": True}
        assert models[0].languages == [{"code": "EN", "name": "English"}]
        (_, params) = seen[0]
        assert params["protocol"] == "harmony" and params["op"] == "models"
        assert params["api_key"] == "SECRET"
        assert params["endpoint"] == {
            "name": "ep",
            "base_url": "https://api.example.org/v1",
            "default_model": "m1",
            "extra_config": {"timeout": 5},
        }
        assert rig.host.base_calls == []  # the Core's adapter was not asked

    @pytest.mark.asyncio
    async def test_an_unlisted_operation_falls_back_to_the_base_protocol(
        self, rig: Rig
    ) -> None:
        client = await rig.connect(manifest(HARMONY))
        seen = answering(client, {"protocol.request": []})
        adapter = rig.adapter("harmony")
        reply = await adapter.chat_completion(
            [{"role": "user", "content": "hi"}], model="x"
        )
        assert reply.content == "from the core"
        assert await adapter.probe_capabilities([]) == {"cortex": True}
        assert await adapter.ping_test(model="x") == (True, "pong")
        assert await adapter.health_check() is True
        assert seen == []  # nothing went to the extension
        assert [name for name, _ in rig.host.base_calls] == [
            "chat",
            "capabilities",
            "ping",
        ]

    @pytest.mark.asyncio
    async def test_without_a_base_an_unlisted_operation_is_unsupported(
        self, rig: Rig
    ) -> None:
        await rig.connect(manifest(FISHY))
        with pytest.raises(NotImplementedError, match="does not implement"):
            await rig.adapter("fishy").chat_completion([], model=None)

    @pytest.mark.asyncio
    async def test_a_voice_comes_back_as_a_file_the_core_reads_and_deletes(
        self, rig: Rig
    ) -> None:
        client = await rig.connect(manifest(HARMONY))
        clip = rig.tmp / "data" / "dev/proto" / "inbox" / "speech.audio"
        clip.write_bytes(b"RIFFaudio")
        seen = answering(client, {"protocol.request": {"path": str(clip)}})
        audio = await rig.adapter("harmony").generate_tts(
            "hello", "voice1", model="m", format="wav"
        )
        assert audio == b"RIFFaudio" and not clip.exists()
        assert seen[0][1]["args"] == {
            "text": "hello",
            "voice": "voice1",
            "kwargs": {"model": "m", "format": "wav"},
        }

    @pytest.mark.asyncio
    async def test_the_extension_cannot_make_the_core_read_another_file(
        self, rig: Rig
    ) -> None:
        client = await rig.connect(manifest(HARMONY))
        secret = rig.tmp / "secret.env"
        secret.write_text("KEY=1")
        for bad in (str(secret), "/etc/passwd", "../../etc/passwd"):
            answering(client, {"protocol.request": {"path": bad}})
            assert await rig.adapter("harmony").generate_tts("x") is None
        assert secret.exists()

    @pytest.mark.asyncio
    async def test_no_audio_is_none_not_an_error(self, rig: Rig) -> None:
        client = await rig.connect(manifest(HARMONY))
        answering(client, {"protocol.request": {}})
        assert await rig.adapter("harmony").generate_tts("x") is None

    @pytest.mark.asyncio
    async def test_audio_to_transcribe_is_staged_for_the_extension_and_removed(
        self, rig: Rig
    ) -> None:
        client = await rig.connect(manifest(HARMONY))
        staged: list[tuple[str, bytes]] = []
        original = client.peer._handler  # noqa: SLF001

        async def handler(method: str, params: Any, notification: bool) -> Any:
            if method == "protocol.request":
                path = Path(params["args"]["path"])
                staged.append((str(path), path.read_bytes()))
                return {"text": "ciao a tutti"}
            return await original(method, params, notification)

        client.peer._handler = handler  # noqa: SLF001
        text = await rig.adapter("harmony").transcribe_audio(
            b"\x01\x02audio", "audio/wav", model="stt"
        )
        assert text == "ciao a tutti"
        (path, content) = staged[0]
        assert content == b"\x01\x02audio"
        assert rig.tmp / "data" / "dev/proto" / "outbox" in Path(path).parents
        assert not Path(path).exists()  # cleaned up when the call returned

    @pytest.mark.asyncio
    async def test_a_request_too_large_for_a_message_is_refused_before_sending(
        self, rig: Rig
    ) -> None:
        client = await rig.connect(manifest(ProtocolProvision("chatty", "", ("chat",))))
        seen = answering(client, {"protocol.request": {"content": "x"}})
        with pytest.raises(Exception, match="too large"):
            await rig.adapter("chatty").chat_completion(
                [{"role": "user", "content": "y" * 900_000}]
            )
        assert seen == []

    @pytest.mark.asyncio
    async def test_the_voice_picker_extras_exist_only_when_declared(
        self, rig: Rig
    ) -> None:
        client = await rig.connect(manifest(FISHY, HARMONY))
        harmony_adapter, fishy_adapter = rig.adapter("harmony"), rig.adapter("fishy")
        # a picker looks for these by name: Harmony has none, the fishy one has two
        assert getattr(harmony_adapter, "list_speakers", None) is None
        assert getattr(fishy_adapter, "fetch_model_detail", None) is None
        assert callable(fishy_adapter.list_speakers)
        answering(
            client, {"protocol.request": [{"reference_id": "a", "title": "A"}, "x"]}
        )
        assert await fishy_adapter.list_speakers() == [
            {"reference_id": "a", "title": "A"}
        ]
        answering(client, {"protocol.request": "abc123"})
        assert (
            await fishy_adapter.parse_model_id_from_url("https://x/?modelId=abc123")
            == "abc123"
        )

    @pytest.mark.asyncio
    async def test_an_adapter_of_a_gone_extension_says_so(self, rig: Rig) -> None:
        await rig.connect(manifest(HARMONY))
        factory = rig.host.factories["harmony"]
        await rig.env.close()
        with pytest.raises(ValueError, match="is not running"):
            factory(endpoint("harmony"), "k")


# ------------------------------------------------------ the extensions' own logic


REAL_CLIENT = httpx.AsyncClient


def mock_client(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Route the extension's HTTP calls to ``handler`` and record the requests."""
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    real = REAL_CLIENT
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda *a, **kw: real(
            transport=httpx.MockTransport(wrapped), timeout=kw.get("timeout")
        ),
    )
    return seen


HARMONY_MODELS = {
    "data": [
        {
            "model_id": "kitten",
            "display_name": "Kitten TTS",
            "model_type": "tts",
            "input_modalities": ["text"],
            "output_modalities": ["audio"],
            "speech_options": {"languages": [{"code": "EN", "name": "English"}, "IT"]},
        },
        {
            "model_id": "whisper",
            "model_type": "stt",
            "capabilities": {"supportsVision": True},
        },
        {
            "model_id": "chat",
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
        },
        {"name": "no id"},
    ]
}


class TestHarmony:
    @pytest.mark.asyncio
    async def test_the_model_listing_maps_rich_descriptors_to_subsystems(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = mock_client(
            monkeypatch, harmony, lambda r: httpx.Response(200, json=HARMONY_MODELS)
        )
        models = await harmony.Harmony(
            {"base_url": "https://h.example/v1"}, "KEY"
        ).models()
        by_id = {m["id"]: m for m in models}
        assert set(by_id) == {"kitten", "whisper", "chat"}
        assert by_id["kitten"]["name"] == "Kitten TTS"
        assert by_id["kitten"]["capabilities"] == {"vox": True}
        assert by_id["kitten"]["languages"] == [
            {"code": "EN", "name": "English"},
            {"code": "IT", "name": "IT"},
        ]
        assert by_id["whisper"]["capabilities"] == {
            "auris": True,
            "supportsvision": True,
            "vision": True,
        }
        assert by_id["chat"]["capabilities"] == {"cortex": True, "vision": True}
        assert str(seen[0].url) == "https://h.example/v1/models"
        assert seen[0].headers["authorization"] == "Bearer KEY"

    @pytest.mark.asyncio
    async def test_paths_follow_the_base_url_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = mock_client(monkeypatch, harmony, lambda r: httpx.Response(404, json={}))
        await harmony.Harmony({"base_url": "https://h.example"}, "K").models()
        assert [str(r.url) for r in seen] == [
            "https://h.example/v1/models",
            "https://h.example/models",
        ]

    @pytest.mark.asyncio
    async def test_an_unreachable_endpoint_invents_nothing_an_empty_one_gets_a_placeholder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        mock_client(monkeypatch, harmony, down)
        assert (
            await harmony.Harmony({"base_url": "https://h.example/v1"}, "K").models()
            == []
        )
        mock_client(
            monkeypatch, harmony, lambda r: httpx.Response(200, json={"data": []})
        )
        placeholder = await harmony.Harmony(
            {"base_url": "https://h.example/v1"}, "K"
        ).models()
        assert [m["id"] for m in placeholder] == ["default"]

    @pytest.mark.asyncio
    async def test_speech_is_json_with_a_mode_and_comes_back_base64(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audio = b"\xff\xfbmp3-bytes"
        seen = mock_client(
            monkeypatch,
            harmony,
            lambda r: httpx.Response(
                200, json={"data": base64.b64encode(audio).decode()}
            ),
        )
        got = await harmony.Harmony({"base_url": "https://h.example/v1"}, "K").tts(
            "Ciao", "v1", {"model": "kitten", "language": "IT", "sample_rate": 22050}
        )
        assert got == audio
        body = json.loads(seen[0].content)
        assert body == {
            "model": "kitten",
            "input": "Ciao",
            "mode": "single_speaker_tts",
            "output_options": {"format": "mp3", "sample_rate": 22050},
            "language": "IT",
            "voice": "v1",
        }
        assert str(seen[0].url) == "https://h.example/v1/audio/speech"

    @pytest.mark.asyncio
    async def test_an_unsupported_language_is_retried_once_in_a_supported_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audio = b"clip"
        calls: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append(body)
            if body.get("language") == "XX":
                return httpx.Response(
                    200,
                    json={
                        "object": "error",
                        "message": "The model `m` only supports the following languages: ZH, EN, JA.",
                    },
                )
            return httpx.Response(200, json={"data": base64.b64encode(audio).decode()})

        mock_client(monkeypatch, harmony, handler)
        got = await harmony.Harmony({"base_url": "https://h.example/v1"}, "K").tts(
            "hi", None, {"model": "m", "language": "XX"}
        )
        assert got == audio
        assert [c.get("language") for c in calls] == ["XX", "EN"]

    @pytest.mark.asyncio
    async def test_speech_and_transcription_need_a_model(self) -> None:
        client = harmony.Harmony({"base_url": "https://h.example/v1"}, "K")
        assert await client.tts("x", None, {}) is None
        assert await client.stt(b"a", {}) is None

    @pytest.mark.asyncio
    async def test_transcription_sends_base64_audio_and_reads_the_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = mock_client(
            monkeypatch, harmony, lambda r: httpx.Response(200, json={"text": "ciao"})
        )
        text = await harmony.Harmony({"base_url": "https://h.example/v1"}, "K").stt(
            b"\x00\x01", {"model": "whisper", "get_language": True}
        )
        assert text == "ciao"
        body = json.loads(seen[0].content)
        assert body == {
            "model": "whisper",
            "input_audio": base64.b64encode(b"\x00\x01").decode(),
            "get_language": True,
        }

    def test_a_base_url_is_required(self) -> None:
        with pytest.raises(harmony.HarmonyError):
            harmony.Harmony({"base_url": ""}, "K")

    def test_the_fallback_language_only_comes_from_an_error_that_lists_them(
        self,
    ) -> None:
        pick = harmony.pick_fallback_language
        error = {
            "error": "x",
            "message": "only supports the following languages: ZH, JA.",
        }
        assert pick(error, "XX") == "ZH"
        assert pick(error, "ja") is None  # already supported: something else failed
        assert pick({"data": "ok"}, "XX") is None and pick("junk", "XX") is None


class TestFish:
    @pytest.mark.asyncio
    async def test_speech_uses_the_reference_id_schema_and_the_model_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = mock_client(
            monkeypatch, fish, lambda r: httpx.Response(200, content=b"fake-audio")
        )
        client = fish.Fish(
            {
                "base_url": "https://api.fish.audio/v1/tts",
                "extra_config": {
                    "tts_model": "s2.1-pro-free",
                    "tts_output_format": "wav",
                    "tts_reference_id": "abc123",
                },
            },
            "fa_test",
        )
        assert await client.tts("Ciao", None, {}) == b"fake-audio"
        assert str(seen[0].url) == "https://api.fish.audio/v1/tts"
        assert json.loads(seen[0].content) == {
            "text": "Ciao",
            "format": "wav",
            "reference_id": "abc123",
        }
        assert seen[0].headers["authorization"] == "Bearer fa_test"
        assert seen[0].headers["model"] == "s2.1-pro-free"

    @pytest.mark.asyncio
    async def test_defaults_an_explicit_voice_and_an_unknown_format(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = mock_client(
            monkeypatch, fish, lambda r: httpx.Response(200, content=b"a")
        )
        await fish.Fish({"base_url": ""}, "k").tts("Hi", "voice-42", {"format": "ogg"})
        body = json.loads(seen[0].content)
        assert body["reference_id"] == "voice-42" and body["format"] == "wav"
        assert seen[0].headers["model"] == fish.DEFAULT_MODEL
        assert str(seen[0].url) == fish.DEFAULT_BASE_URL

    @pytest.mark.asyncio
    async def test_a_failed_request_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_client(monkeypatch, fish, lambda r: httpx.Response(402, text="no credit"))
        assert await fish.Fish({"base_url": ""}, "k").tts("Hi", None, {}) is None

    def test_the_catalogue_and_the_url_parser(self) -> None:
        assert "s2.1-pro-free" in [m["id"] for m in fish.models()]
        assert all(m["capabilities"] == {"vox": True} for m in fish.models())
        parse = fish.parse_model_id_from_url
        assert parse("abc123") == "abc123"
        assert parse("https://fish.audio/app/text-to-speech/?modelId=xyz") == "xyz"
        assert parse("https://fish.audio/model/tail") == "tail"
        assert parse("") is None

    @pytest.mark.asyncio
    async def test_voices_are_listed_by_tier_without_duplicates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("self") == "true":
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {"_id": "mine", "title": "Mine"},
                            {"_id": "dup", "title": "Dup"},
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"_id": "pub", "title": "Public", "languages": ["it"]},
                        {"_id": "dup", "title": "Dup"},
                    ]
                },
            )

        seen = mock_client(monkeypatch, fish, handler)
        voices = await fish.Fish(
            {
                "base_url": "https://api.fish.audio/v1/tts",
                "extra_config": {
                    "manual_voices": [
                        {"reference_id": "dup", "title": "Manual"},
                        "junk",
                    ]
                },
            },
            "k",
        ).list_speakers()
        assert [(v["reference_id"], v["tier"]) for v in voices] == [
            ("dup", 1),
            ("mine", 2),
            ("pub", 3),
        ]
        assert voices[0]["title"] == "Manual" and voices[2]["language"] == "it"
        assert {str(r.url).split("?")[0] for r in seen} == {
            "https://api.fish.audio/model"
        }

    @pytest.mark.asyncio
    async def test_a_voice_is_described_for_adding_by_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_client(
            monkeypatch,
            fish,
            lambda r: httpx.Response(
                200, json={"_id": "v9", "title": "Nine", "languages": ["en"]}
            ),
        )
        detail = await fish.Fish({"base_url": ""}, "k").fetch_model_detail("v9")
        assert detail == {"reference_id": "v9", "title": "Nine", "language": "en"}
        mock_client(monkeypatch, fish, lambda r: httpx.Response(404, text="no"))
        assert await fish.Fish({"base_url": ""}, "k").fetch_model_detail("v9") is None

    def test_a_key_is_required_to_synthesise(self) -> None:
        with pytest.raises(fish.FishError):
            fish.Fish({"base_url": ""}, "").need_key()


# ------------------------------------------------------------ a real extension process


@pytest.fixture(scope="session")
def harmony_lib(tmp_path_factory: pytest.TempPathFactory) -> Path:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is needed to build the extension's lib/")
    target = tmp_path_factory.mktemp("harmony_lib") / "lib"
    done = subprocess.run(
        [uv, "pip", "install", "--offline", "--target", str(target), "httpx>=0.27"],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        pytest.skip(f"cannot build lib/ offline: {done.stderr.strip()[-200:]}")
    return target


class FakeHarmonyService:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any], str]] = []
        self._runner: web.AppRunner | None = None
        self.base = ""

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/v1/models", self._models)
        app.router.add_post("/v1/audio/speech", self._speech)
        app.router.add_post("/v1/audio/transcriptions", self._transcribe)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]  # noqa: SLF001
        self.base = f"http://127.0.0.1:{port}/v1"

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    async def _models(self, request: web.Request) -> web.Response:
        self.requests.append(("models", {}, request.headers.get("Authorization", "")))
        return web.json_response(HARMONY_MODELS)

    async def _speech(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.requests.append(("speech", body, request.headers.get("Authorization", "")))
        return web.json_response({"data": base64.b64encode(b"HARMONY-AUDIO").decode()})

    async def _transcribe(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.requests.append(("stt", body, request.headers.get("Authorization", "")))
        heard = base64.b64decode(body["input_audio"]).decode("utf-8", "replace")
        return web.json_response({"text": f"heard: {heard}"})


@pytest.mark.asyncio
async def test_a_harmony_endpoint_works_end_to_end_through_the_real_extension(
    tmp_path: Path, harmony_lib: Path
) -> None:
    service = FakeHarmonyService()
    await service.start()
    ports, grants = memory_ports(), MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    layout = RuntimeLayout(tmp_path / "extensions")
    media = MediaBridge(
        data_dir=layout.data_dir,
        uid=lambda ext: None,
        inbound_root=tmp_path / "media",
        resolve_outbound=lambda raw: (None, "refused"),
    )
    register_interface_api(
        gateway, InterfacePorts(MemoryInterfaceHost(), MemoryMessaging(), media)
    )
    host = FakeHost()
    attach_protocols(gateway, host, media)
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
        manifest_ = load_manifest(HARMONY_DIR / "synth-extension.toml")
        directory = layout.install_dir(manifest_.id)
        directory.mkdir(parents=True)
        for name in ("ext_main.py", "harmony.py"):
            shutil.copy(HARMONY_DIR / name, directory / name)
        (directory / "lib").symlink_to(harmony_lib)
        grants.grant(manifest_.id, *manifest_.permissions.capabilities)
        supervisor.register(manifest_)
        await supervisor.start(manifest_.id)
        await wait_until(lambda: "harmony" in host.factories, 20)

        adapter = host.factories["harmony"](
            endpoint("harmony", base_url=service.base, extra_config={"timeout": 5}),
            "SECRET-KEY",
        )
        models = await adapter.list_models()
        assert {m.id for m in models} == {"kitten", "whisper", "chat"}
        assert next(m for m in models if m.id == "kitten").capabilities == {"vox": True}

        assert (
            await adapter.generate_tts("Ciao", None, model="kitten") == b"HARMONY-AUDIO"
        )
        assert (
            await adapter.transcribe_audio(b"hello there", "audio/wav", model="whisper")
            == "heard: hello there"
        )

        # the key reached Harmony as a bearer token, and no leftover files remain
        assert {auth for _, _, auth in service.requests} == {"Bearer SECRET-KEY"}
        assert [kind for kind, _, _ in service.requests] == ["models", "speech", "stt"]
        data_dir = layout.data_dir(manifest_.id)
        assert (
            list((data_dir / "inbox").glob("*")) == []
            if (data_dir / "inbox").exists()
            else True
        )
        assert harness.stderr == [] or all(
            "Traceback" not in line for _, line in harness.stderr
        )
    finally:
        await supervisor.stop_all()
        await gateway.close_all()
        await service.stop()
