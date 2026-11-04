import sys
import types


from scripts import generate_model_samples
from core.model_manager import MODEL_MANAGER, ModelSpec, VoiceSpec, SAMPLE_TEXT_BY_LANG


# ---------------------------------------------------------------------------
# VoiceSpec & ModelSpec tests
# ---------------------------------------------------------------------------


def test_voice_spec_defaults():
    v = VoiceSpec(name="Bella")
    assert v.gender == "N"
    assert v.languages == ["*"]


def test_model_spec_derives_voices_from_meta():
    meta = [VoiceSpec("A", gender="F"), VoiceSpec("B", gender="M")]
    s = ModelSpec(
        model_id="test",
        plugin_id="test_plugin",
        display_name="Test",
        description="test",
        voices_meta=meta,
        supported_languages=["en", "it"],
    )
    assert s.voices == ["A", "B"]
    assert s.language == "en"


def test_sample_text_by_lang_has_key_languages():
    for lang in ("en", "it", "de", "fr", "es", "ja", "zh", "ko"):
        assert lang in SAMPLE_TEXT_BY_LANG, f"Missing sample text for lang={lang}"


def test_sample_path_uses_lang_suffix(tmp_path, monkeypatch):
    """_sample_path should produce <voice>_<lang>.mp3 files."""
    monkeypatch.setattr(
        "core.model_manager._SAMPLES_STATIC_DIR",
        tmp_path,
    )
    monkeypatch.setattr(
        MODEL_MANAGER,
        "_models",
        {"m1": ModelSpec("m1", "p1", "N", "d", voices=["Bella"])},
    )
    path_en = MODEL_MANAGER._sample_path("m1", "Bella", "en")
    path_it = MODEL_MANAGER._sample_path("m1", "Bella", "it")
    assert path_en.name == "Bella_en.mp3"
    assert path_it.name == "Bella_it.mp3"
    assert path_en != path_it


def test_legacy_sample_migration(tmp_path, monkeypatch):
    """Old Bella.mp3 should be renamed to Bella_en.mp3 on first access."""
    import core.model_manager as mm_mod

    monkeypatch.setattr(mm_mod, "_SAMPLES_STATIC_DIR", tmp_path)
    monkeypatch.setattr(
        MODEL_MANAGER,
        "_models",
        {"m1": ModelSpec("m1", "p1", "N", "d", voices=["Bella"])},
    )

    model_dir = tmp_path / "m1"
    model_dir.mkdir()
    old_file = model_dir / "Bella.mp3"
    old_file.write_bytes(b"legacy")

    # Migration runs on first call to list_samples / sample_exists
    MODEL_MANAGER._migrate_legacy_samples("m1")

    new_file = model_dir / "Bella_en.mp3"
    assert new_file.exists(), "Legacy file should be renamed to Bella_en.mp3"
    assert not old_file.exists(), "Old Bella.mp3 should be gone"


# ---------------------------------------------------------------------------
# generate_model_samples tests (updated API)
# ---------------------------------------------------------------------------


def test_generate_callback_falls_back_to_edge(monkeypatch, tmp_path):
    """When no VOX engines provide audio, we should still get bytes via edge-tts."""
    monkeypatch.setattr(
        generate_model_samples.VOX_REGISTRY,
        "get_available_engines",
        lambda: [],
    )

    class FakeComm:
        def __init__(self, text, voice, **kwargs):
            self.voice = voice

        async def save(self, fname):
            with open(fname, "wb") as f:
                f.write(f"fake:{self.voice}".encode())

    async def fake_list_voices():
        return [
            {"ShortName": "en-US-AriaNeural", "Locale": "en-US", "Gender": "Female"},
            {"ShortName": "en-US-GuyNeural", "Locale": "en-US", "Gender": "Male"},
            {"ShortName": "fr-FR-DeniseNeural", "Locale": "fr-FR", "Gender": "Female"},
        ]

    fake_edge = types.SimpleNamespace(
        Communicate=FakeComm, list_voices=fake_list_voices
    )
    monkeypatch.setitem(sys.modules, "edge_tts", fake_edge)
    generate_model_samples._edge_voice_cache.clear()

    cb = generate_model_samples._make_generate_callback(gender="F", lang="en")
    data = cb("hello world", "voice1")
    assert data is not None, "fallback should return something"
    # should have chosen an English female voice
    assert b"en-US-AriaNeural" in data


def test_edge_generate_deterministic(monkeypatch):
    """The voice choice should be deterministic based on the (voice, lang) key."""

    class FakeComm:
        def __init__(self, text, voice, **kwargs):
            self.voice = voice

        async def save(self, fname):
            with open(fname, "wb") as f:
                f.write(f"{self.voice}".encode())

    async def fake_list_voices():
        return [
            {"ShortName": "one", "Locale": "en-US", "Gender": "Female"},
            {"ShortName": "two", "Locale": "en-US", "Gender": "Female"},
            {"ShortName": "three", "Locale": "en-US", "Gender": "Female"},
        ]

    fake_edge = types.SimpleNamespace(
        Communicate=FakeComm, list_voices=fake_list_voices
    )
    monkeypatch.setitem(sys.modules, "edge_tts", fake_edge)
    generate_model_samples._edge_voice_cache.clear()

    a = generate_model_samples._edge_generate("foo", "bar", lang="en", gender="F")
    assert generate_model_samples._edge_voice_cache, "voice cache must not be empty"
    b = generate_model_samples._edge_generate("foo", "bar", lang="en", gender="F")
    assert a == b
    c = generate_model_samples._edge_generate("foo", "baz", lang="en", gender="F")
    assert c is not None


def test_callback_passes_text_when_supported(monkeypatch):
    """The voice is ``sample``'s first argument; the prompt text rides as a hint."""

    class TwoArgEngine:
        def sample(self, speaker, text_hint=None):
            assert speaker == "voice2", "the voice must be passed as the speaker"
            assert text_hint == "hello"
            return b"ok:" + (speaker or "").encode()

    monkeypatch.setattr(
        generate_model_samples.VOX_REGISTRY,
        "get_available_engines",
        lambda: ["foo"],
    )
    monkeypatch.setattr(
        generate_model_samples.VOX_REGISTRY,
        "load_engine",
        lambda name: TwoArgEngine(),
    )

    cb = generate_model_samples._make_generate_callback(gender="F", lang="en")
    data = cb("hello", "voice2")
    assert data is not None
    assert data.startswith(b"ok")


def test_callback_ignores_text_when_not_supported(monkeypatch):
    """Engines with single-arg sample should still work."""

    class OneArgEngine:
        def sample(self, voice):
            return b"yes"

    monkeypatch.setattr(
        generate_model_samples.VOX_REGISTRY,
        "get_available_engines",
        lambda: ["bar"],
    )
    monkeypatch.setattr(
        generate_model_samples.VOX_REGISTRY,
        "load_engine",
        lambda name: OneArgEngine(),
    )

    cb = generate_model_samples._make_generate_callback(gender="M", lang="en")
    data = cb("foo", "voice3")
    assert data.startswith(b"yes")


def test_tweak_audio_changes_for_different_voices():
    """Audio bytes should differ when voice string is different."""
    base = b"dummy-audio-bytes"

    v1 = generate_model_samples._tweak_audio(base, "alice", "en")
    v2 = generate_model_samples._tweak_audio(base, "bob", "en")
    assert v1 != v2, "different voices should produce different bytes"


# ---------------------------------------------------------------------------
# A catalog sample must be spoken by the voice (and model) it is shipped for
# ---------------------------------------------------------------------------


class _RecordingKitten:
    """Mirrors ``KittenVoxEngine.sample``: the voice is the FIRST argument."""

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str | None, str | None]] = []

    def sample(self, speaker, text_hint=None, model_id=None):  # noqa: ANN001, ANN201
        self.calls.append((speaker, text_hint, model_id))
        return b"RIFFfake"


def _install_engine(monkeypatch, name: str, engine) -> None:
    monkeypatch.setattr(
        generate_model_samples.VOX_REGISTRY, "get_available_engines", lambda: [name]
    )
    monkeypatch.setattr(
        generate_model_samples.VOX_REGISTRY, "load_engine", lambda _n: engine
    )
    monkeypatch.setattr(generate_model_samples, "_convert_to_mp3", lambda d: d)


def test_engine_sample_receives_voice_first_and_model(monkeypatch) -> None:
    engine = _RecordingKitten()
    _install_engine(monkeypatch, "kitten", engine)

    data = generate_model_samples.vox_engine_sample(
        "Hello there", "Bruno", "kitten-tts-micro-0.8"
    )

    assert data == b"RIFFfake"
    assert engine.calls == [("Bruno", "Hello there", "kitten-tts-micro-0.8")]


def test_engine_sample_is_not_pitch_shifted(monkeypatch) -> None:
    """Only the edge-tts stand-in is tweaked; a real voice is left untouched."""
    _install_engine(monkeypatch, "kitten", _RecordingKitten())

    def _boom(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("engine output must not be tweaked")

    monkeypatch.setattr(generate_model_samples, "_tweak_audio", _boom)
    cb = generate_model_samples._make_generate_callback(model_id="kitten-tts-nano-0.8")
    assert cb("Hi", "Luna") == b"RIFFfake"


def test_engine_with_voice_only_signature_is_supported(monkeypatch) -> None:
    class _VoiceOnly:
        def sample(self, speaker):  # noqa: ANN001, ANN201
            return f"voice:{speaker}".encode()

    _install_engine(monkeypatch, "other", _VoiceOnly())
    assert generate_model_samples.vox_engine_sample("text", "Kiki") == b"voice:Kiki"
