"""The KittenTTS voice extension, against a fake ``kittentts`` and a fake ``ctx``."""

from __future__ import annotations

import asyncio
import importlib
import io
import sys
import types
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from core.extensions.manifest import load_manifest

SOURCE = Path(__file__).resolve().parent.parent / "extensions_src" / "vox_kitten"


def _load() -> tuple[Any, Any]:
    sys.path.insert(0, str(SOURCE))
    try:
        return importlib.import_module("kitten"), importlib.import_module("ext_main")
    finally:
        sys.path.remove(str(SOURCE))
        for name in ("kitten", "ext_main"):
            sys.modules.pop(name, None)


kitten, ext_main = _load()


class FakeModel:
    def __init__(self) -> None:
        self.phonemizer: Any = "original"


class FakeKittenTTS:
    """What ``kittentts.KittenTTS(repo)`` returns: a model with a phonemizer and generate()."""

    instances: list[FakeKittenTTS] = []
    fail = False

    def __init__(self, repo: str) -> None:
        self.repo = repo
        self.model = FakeModel()
        self.calls: list[dict[str, Any]] = []
        self.seen_phonemizers: list[Any] = []
        FakeKittenTTS.instances.append(self)

    def generate(self, text: str, voice: str, speed: float) -> Any:
        self.calls.append({"text": text, "voice": voice, "speed": speed})
        self.seen_phonemizers.append(self.model.phonemizer)
        if FakeKittenTTS.fail:
            raise RuntimeError("model blew up")
        t = np.linspace(0, 1, 2400)
        return np.sin(2 * np.pi * 220 * t) * 0.25  # quiet: normalisation must lift it


class FakeEspeak:
    made: list[str] = []

    def __init__(
        self, language: str, preserve_punctuation: bool, with_stress: bool
    ) -> None:
        if language == "xx":
            raise RuntimeError("no such language")
        self.language = language
        FakeEspeak.made.append(language)


@pytest.fixture(autouse=True)
def fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeKittenTTS.instances = []
    FakeKittenTTS.fail = False
    FakeEspeak.made = []
    kitten._models.clear()
    backend = types.ModuleType("phonemizer.backend")
    backend.EspeakBackend = FakeEspeak  # type: ignore[attr-defined]
    package = types.ModuleType("phonemizer")
    package.backend = backend  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "phonemizer", package)
    monkeypatch.setitem(sys.modules, "phonemizer.backend", backend)
    kitten.Log.target = None


def speak(text: str = "Ciao", voice: str | None = None, **kwargs: Any) -> bytes | None:
    return kitten.synthesize(
        text,
        voice,
        kwargs,
        default_model="kitten-tts-mini-0.8",
        language_models={},
        speed=1.3,
        factory=FakeKittenTTS,
    )


def wav_info(data: bytes) -> tuple[int, int, int, np.ndarray]:
    with wave.open(io.BytesIO(data), "rb") as clip:
        frames = np.frombuffer(clip.readframes(clip.getnframes()), dtype=np.int16)
        return clip.getnchannels(), clip.getsampwidth(), clip.getframerate(), frames


def test_the_manifest_declares_the_engine_endpoint_and_protocol() -> None:
    m = load_manifest(SOURCE / "synth-extension.toml")
    assert m.provides.endpoints == ("kitten",)
    (protocol,) = m.provides.protocols
    assert (
        protocol.name == "kitten"
        and "tts" in protocol.ops
        and "list_speakers" in protocol.ops
    )
    assert {"endpoints.manage", "endpoints.protocol"} <= set(m.permissions.capabilities)
    assert {c.key for c in m.config} == {
        "KITTEN_MODEL",
        "KITTEN_SPEED",
        "KITTEN_LANGUAGE_MODELS",
    }


def test_speech_is_a_mono_16_bit_wav_peak_normalised_to_minus_one_dbfs() -> None:
    channels, width, rate, frames = wav_info(speak() or b"")
    assert (channels, width, rate) == (1, 2, kitten.SAMPLE_RATE)
    assert abs(int(np.max(np.abs(frames))) - round(0.891 * 32767)) <= 2


def test_the_voice_model_and_speed_reach_the_model() -> None:
    speak("Hi", "Luna", model="kitten-tts-nano-0.8", speed=1.0)
    (engine,) = FakeKittenTTS.instances
    assert (
        engine.repo == "KittenML/kitten-tts-nano-0.8"
    )  # the catalogue id became its repo
    assert engine.calls == [{"text": "Hi", "voice": "Luna", "speed": 1.0}]


def test_defaults_voice_speed_and_a_raw_repo_are_honoured() -> None:
    speak("Hi", model="someone/else-repo")
    (engine,) = FakeKittenTTS.instances
    assert engine.repo == "someone/else-repo"
    assert engine.calls[0]["voice"] == "Bella" and engine.calls[0]["speed"] == 1.3


def test_a_model_is_loaded_once() -> None:
    speak("a")
    speak("b")
    assert len(FakeKittenTTS.instances) == 1


def test_a_language_uses_its_own_model_when_mapped() -> None:
    kitten.synthesize(
        "Ciao",
        None,
        {"language": "it"},
        default_model="kitten-tts-mini-0.8",
        language_models={"it": "kitten-tts-nano-it-0.1"},
        speed=1.3,
        factory=FakeKittenTTS,
    )
    assert FakeKittenTTS.instances[0].repo == "kitten-tts-nano-it-0.1"


def test_the_phonemizer_follows_the_language_and_is_always_restored() -> None:
    speak("Ciao", language="it")
    (engine,) = FakeKittenTTS.instances
    assert isinstance(engine.seen_phonemizers[0], FakeEspeak)
    assert engine.seen_phonemizers[0].language == "it"
    assert engine.model.phonemizer == "original"  # put back
    FakeKittenTTS.fail = True
    assert speak("again", language="fr") is None  # a failing model: None, not a crash
    assert engine.model.phonemizer == "original"  # restored even then


def test_an_unknown_espeak_language_falls_back_to_english() -> None:
    engine = kitten.LocalKittenTTS("repo", FakeKittenTTS)
    assert engine._phonemizer("xx") is engine._phonemizer("en-us")  # noqa: SLF001
    assert FakeEspeak.made == ["en-us"]


@pytest.mark.parametrize(
    ("tag", "code"),
    [
        (None, "en-us"),
        ("it", "it"),
        ("it-IT", "it"),
        ("ja", "it"),
        ("zz", "en-us"),
        ("EN", "en-us"),
    ],
)
def test_language_tags_map_to_espeak_codes(tag: str | None, code: str) -> None:
    assert kitten.espeak_lang_code(tag) == code


def test_the_language_model_setting_is_read_strictly() -> None:
    assert kitten.parse_language_models('{"it": "m1", "en": 3}') == {
        "it": "m1",
        "en": "3",
    }
    for bad in ("", "{", "[1]", None, "null"):
        assert kitten.parse_language_models(bad) == {}


def test_voices_and_models_are_described_for_the_picker() -> None:
    assert [v["reference_id"] for v in kitten.speakers()][:3] == [
        "Bella",
        "Jasper",
        "Luna",
    ]
    assert all(v["language"] == "*" for v in kitten.speakers())
    ids = [m["id"] for m in kitten.model_infos()]
    assert kitten.DEFAULT_MODEL in ids and len(ids) == 3
    assert all(m["capabilities"] == {"vox": True} for m in kitten.model_infos())


class FakeCtx:
    def __init__(self, tmp: Path, config: dict[str, Any] | None = None) -> None:
        self.data_dir = tmp
        self.values = config or {}
        self.stopping = asyncio.Event()
        self.handler: Any = None
        self.ensured: list[tuple[Any, ...]] = []
        ctx = self

        class Protocols:
            def on_request(self, handler: Any) -> None:
                ctx.handler = handler

        class Endpoints:
            async def ensure(
                self, name: str, base_url: str, **kwargs: Any
            ) -> dict[str, Any]:
                ctx.ensured.append((name, base_url, kwargs))
                return {"created": True}

        class Config:
            async def all(self) -> dict[str, Any]:
                return dict(ctx.values)

        self.protocols, self.endpoints, self.config = Protocols(), Endpoints(), Config()
        self.log = SimpleNamespace(
            info=lambda m: None, error=lambda m: None, warning=lambda m: None
        )

    def inbox_path(self, name: str) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir / name


@pytest.mark.asyncio
async def test_the_engine_endpoint_is_created_local_with_the_voice_subsystem(
    tmp_path: Path,
) -> None:
    ctx = FakeCtx(tmp_path)
    ctx.stopping.set()
    await ext_main.main(ctx)
    ((name, base_url, kwargs),) = ctx.ensured
    assert (name, base_url) == ("kitten", "")
    assert kwargs["protocol"] == "kitten" and kwargs["subsystem_map"] == {"vox": True}
    assert kwargs["extra_config"]["tts_output_format"] == "wav"


@pytest.mark.asyncio
async def test_operations_answer_without_the_model_and_speech_is_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeCtx(
        tmp_path, {"KITTEN_MODEL": "kitten-tts-nano-0.8", "KITTEN_SPEED": "1.1"}
    )
    ctx.stopping.set()
    await ext_main.main(ctx)
    ask = ctx.handler
    assert [m["id"] for m in await ask("kitten", "models", {}, "", {})][
        0
    ] == kitten.DEFAULT_MODEL
    assert await ask("kitten", "capabilities", {}, "", {}) == {
        "cortex": False,
        "vox": True,
        "auris": False,
        "live": False,
    }
    assert await ask("kitten", "health", {}, "", {}) is True
    assert (await ask("kitten", "ping", {}, "", {}))["ok"] is False
    assert (await ask("kitten", "list_speakers", {}, "", {}))[0][
        "reference_id"
    ] == "Bella"
    with pytest.raises(ValueError, match="not provided"):
        await ask("kitten", "chat", {}, "", {})

    monkeypatch.setattr(
        kitten,
        "load_model",
        lambda model_id, factory=None: kitten.LocalKittenTTS(model_id, FakeKittenTTS),
    )
    got = await ask(
        "kitten", "tts", {}, "", {"text": "Ciao", "voice": "Hugo", "kwargs": {}}
    )
    assert Path(got["path"]).read_bytes()[:4] == b"RIFF"
    engine = FakeKittenTTS.instances[0]
    assert engine.calls[0] == {"text": "Ciao", "voice": "Hugo", "speed": 1.1}
    assert (
        engine.repo == "kitten-tts-nano-0.8"
    )  # the configured model, not a catalogue lookup here


@pytest.mark.asyncio
async def test_a_failed_synthesis_is_an_empty_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeCtx(tmp_path)
    ctx.stopping.set()
    await ext_main.main(ctx)
    monkeypatch.setattr(
        kitten,
        "load_model",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model")),
    )
    assert await ctx.handler("kitten", "tts", {}, "", {"text": "x", "kwargs": {}}) == {}
