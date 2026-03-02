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
    """If an engine.sample accepts two args we should send the prompt text."""

    class TwoArgEngine:
        def sample(self, text, voice):
            return b"ok:" + (voice or "").encode()

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
    """When no VOX engines provide audio, we should still get bytes via edge-tts."""
    # ensure no engines are reported
    monkeypatch.setattr(
        generate_model_samples.VOX_REGISTRY,
        "get_available_engines",
        lambda: [],
    )

    # create a fake edge_tts module with predictable behaviour
    class FakeComm:
        def __init__(self, text, voice, **kwargs):
            self.text = text
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

    # clear cache so our fake list_voices is invoked
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
    # different key yields different result (may be same voice if only 1 entry, so just check not None)
    c = generate_model_samples._edge_generate("foo", "baz", lang="en", gender="F")
    assert c is not None


def test_callback_passes_text_when_supported(monkeypatch):
    """If an engine.sample accepts two args we should send the prompt text."""

    class TwoArgEngine:
        def sample(self, text, voice):
            return b"ok:" + (voice or b"").encode() if isinstance(voice, str) else b"ok"

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
