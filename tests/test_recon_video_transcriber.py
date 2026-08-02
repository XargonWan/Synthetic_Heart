from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from plugins.recon.recon_video_transcriber import ReconVideoTranscriberPlugin


class _Msg:
    """Minimal message stub carrying an optional raw_data dict."""

    def __init__(self, raw_data: dict | None = None) -> None:
        self.raw_data = raw_data
        self.text = ""
        self.interface_path = "test/interface"


class _AurisResult:
    def __init__(self, text: str | None) -> None:
        self.text = text


class _IrisResult:
    def __init__(self, description: str | None) -> None:
        self.description = description


class _FakeAuris:
    def __init__(self, text: str | None = "spoken words") -> None:
        self._text = text

    async def transcribe_audio(self, *_a: Any, **_k: Any) -> _AurisResult:
        return _AurisResult(self._text)


class _FakeIris:
    def __init__(self, desc: str | None = "a person talking") -> None:
        self._desc = desc

    async def describe_media(self, *_a: Any, **_k: Any) -> _IrisResult:
        return _IrisResult(self._desc)


@pytest.fixture
def enable_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force all config flags to their enabled defaults."""
    from core.config_manager import config_registry

    def _get_value(key: str, default: Any = None, **_k: Any) -> Any:
        return {
            "RECON_VIDEO_TRANSCRIBER_RECON_ENABLED": True,
            "RECON_VIDEO_INCLUDE_VISION": True,
            "RECON_VIDEO_MAX_SECONDS": 1800,
            "RECON_VIDEO_SNIPPET_MAX_CHARS": 12000,
        }.get(key, default)

    monkeypatch.setattr(config_registry, "get_value", _get_value)


def _install_fake_registry(
    monkeypatch: pytest.MonkeyPatch,
    *,
    auris: Any = None,
    iris: Any = None,
) -> None:
    """Provide a fake core.core_initializer.PLUGIN_REGISTRY."""
    registry = {"auris_plugin": auris, "iris_plugin": iris}
    fake_mod = types.ModuleType("core.core_initializer")
    fake_mod.PLUGIN_REGISTRY = registry  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.core_initializer", fake_mod)


# ----------------------------------------------------------------------
# URL extraction
# ----------------------------------------------------------------------


def test_urls_from_data_nested_dict() -> None:
    plugin = ReconVideoTranscriberPlugin()
    data = {
        "video_media": {
            "youtube_urls": [
                "https://www.youtube.com/watch?v=aoP81h68Xkk",
                "not a url",
            ]
        }
    }
    urls = plugin._urls_from_data(data)
    assert urls == ["https://www.youtube.com/watch?v=aoP81h68Xkk"]


def test_urls_from_data_flat_list() -> None:
    plugin = ReconVideoTranscriberPlugin()
    data = {"youtube_urls": ["https://youtu.be/aoP81h68Xkk"]}
    urls = plugin._urls_from_data(data)
    assert urls == ["https://youtu.be/aoP81h68Xkk"]


def test_urls_from_raw_text_json_block() -> None:
    plugin = ReconVideoTranscriberPlugin()
    raw = (
        'Sure, here is the answer: {"video_media": {"youtube_urls": '
        '["https://www.youtube.com/watch?v=aoP81h68Xkk"]}} done.'
    )
    urls = plugin._urls_from_raw_text(raw)
    assert urls == ["https://www.youtube.com/watch?v=aoP81h68Xkk"]


def test_urls_from_raw_text_empty() -> None:
    plugin = ReconVideoTranscriberPlugin()
    assert plugin._urls_from_raw_text(None) == []
    assert plugin._urls_from_raw_text("no json here") == []


# ----------------------------------------------------------------------
# Enable gate
# ----------------------------------------------------------------------


async def test_disabled_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config_manager import config_registry

    monkeypatch.setattr(
        config_registry,
        "get_value",
        lambda key, default=None, **_k: (
            False if key == "RECON_VIDEO_TRANSCRIBER_RECON_ENABLED" else default
        ),
    )
    plugin = ReconVideoTranscriberPlugin()
    result = await plugin.parse_recon_response(
        {"video_media": {"youtube_urls": ["https://youtu.be/aoP81h68Xkk"]}}
    )
    assert result == []


async def test_no_source_returns_empty(enable_plugin: None) -> None:
    plugin = ReconVideoTranscriberPlugin()
    result = await plugin.parse_recon_response({"video_media": {"youtube_urls": []}})
    assert result == []


# ----------------------------------------------------------------------
# YouTube processing
# ----------------------------------------------------------------------


async def test_youtube_subtitle_fast_path(
    enable_plugin: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import plugins.recon.recon_video_transcriber as mod

    class _Fetched:
        title = "Test Video"
        duration_s = 60
        subtitle_text = "hello from the subtitles"
        audio_path = None
        temp_files: list[str] = []

        def cleanup(self) -> None:
            pass

    monkeypatch.setattr(mod, "fetch_youtube", lambda *a, **k: _Fetched())
    plugin = ReconVideoTranscriberPlugin()
    result = await plugin.parse_recon_response(
        {
            "video_media": {
                "youtube_urls": ["https://www.youtube.com/watch?v=aoP81h68Xkk"]
            }
        }
    )
    assert len(result) == 1
    contrib = result[0]
    assert contrib["type"] == "snippet"
    assert contrib["source"] == "recon_video_transcriber"
    assert "hello from the subtitles" in contrib["content"]
    assert "Test Video" in contrib["content"]


async def test_youtube_audio_stt_fallback(
    enable_plugin: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import plugins.recon.recon_video_transcriber as mod

    class _Fetched:
        title = "Audio Only"
        duration_s = 60
        subtitle_text = None
        audio_path = "/tmp/fake.wav"
        temp_files: list[str] = []

        def cleanup(self) -> None:
            pass

    monkeypatch.setattr(mod, "fetch_youtube", lambda *a, **k: _Fetched())
    _install_fake_registry(monkeypatch, auris=_FakeAuris("transcribed audio"))
    plugin = ReconVideoTranscriberPlugin()
    result = await plugin.parse_recon_response(
        {"video_media": {"youtube_urls": ["https://youtu.be/aoP81h68Xkk"]}}
    )
    assert len(result) == 1
    assert "transcribed audio" in result[0]["content"]


async def test_youtube_fetch_none_returns_empty(
    enable_plugin: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import plugins.recon.recon_video_transcriber as mod

    monkeypatch.setattr(mod, "fetch_youtube", lambda *a, **k: None)
    plugin = ReconVideoTranscriberPlugin()
    result = await plugin.parse_recon_response(
        {"video_media": {"youtube_urls": ["https://youtu.be/aoP81h68Xkk"]}}
    )
    assert result == []


# ----------------------------------------------------------------------
# Local video processing
# ----------------------------------------------------------------------


async def test_local_video_audio_and_vision(
    enable_plugin: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import plugins.recon.recon_video_transcriber as mod

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake video")
    audio = tmp_path / "extracted.wav"
    audio.write_bytes(b"fake audio")

    monkeypatch.setattr(mod, "extract_audio_from_video", lambda *a, **k: str(audio))
    _install_fake_registry(
        monkeypatch,
        auris=_FakeAuris("what was said"),
        iris=_FakeIris("what was seen"),
    )
    plugin = ReconVideoTranscriberPlugin()
    msg = _Msg(raw_data={"media_path": str(video)})
    result = await plugin.parse_recon_response(
        {"video_media": {"youtube_urls": []}}, message=msg
    )
    assert len(result) == 1
    content = result[0]["content"]
    assert "what was said" in content
    assert "what was seen" in content
    # Extracted audio should be cleaned up.
    assert not audio.exists()


async def test_local_video_path_ignores_non_video(
    enable_plugin: None, tmp_path: Any
) -> None:
    plugin = ReconVideoTranscriberPlugin()
    doc = tmp_path / "notes.txt"
    doc.write_text("hi")
    msg = _Msg(raw_data={"media_path": str(doc)})
    assert plugin._local_video_path(msg) is None


async def test_local_video_path_missing_file(
    enable_plugin: None,
) -> None:
    plugin = ReconVideoTranscriberPlugin()
    msg = _Msg(raw_data={"media_path": "/does/not/exist.mp4"})
    assert plugin._local_video_path(msg) is None


# ----------------------------------------------------------------------
# Truncation
# ----------------------------------------------------------------------


def test_truncate_respects_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config_manager import config_registry

    monkeypatch.setattr(
        config_registry,
        "get_value",
        lambda key, default=None, **_k: (
            10 if key == "RECON_VIDEO_SNIPPET_MAX_CHARS" else default
        ),
    )
    plugin = ReconVideoTranscriberPlugin()
    out = plugin._truncate("x" * 100)
    assert out.startswith("x" * 10)
    assert "troncato" in out


def test_truncate_no_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config_manager import config_registry

    monkeypatch.setattr(
        config_registry,
        "get_value",
        lambda key, default=None, **_k: (
            0 if key == "RECON_VIDEO_SNIPPET_MAX_CHARS" else default
        ),
    )
    plugin = ReconVideoTranscriberPlugin()
    text = "y" * 50
    assert plugin._truncate(text) == text
