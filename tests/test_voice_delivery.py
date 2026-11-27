"""A spoken reply that goes to a chat instead of a call is sent in the format the chat takes."""

from __future__ import annotations

import io
import shutil
import wave
from pathlib import Path
from typing import Any

import pytest

from core import interface_traits as it
from core import voice_delivery
from core.extensions.manifest import ManifestError, parse_manifest_text

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg is not installed"
)


def wav(path: Path, seconds: float = 0.5) -> Path:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(24000)
        out.writeframes(b"\x10\x00\x20\x00" * int(24000 * seconds / 2))
    path.write_bytes(buffer.getvalue())
    return path


class Target:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.files: list[bytes] = []

    async def send_message(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)
        self.files.append(Path(payload["media"][0]).read_bytes())


@pytest.fixture(autouse=True)
def declared():
    it.register_traits("voicechat", it.InterfaceTraits(voice_format="ogg_opus"))
    it.register_traits("plainchat", it.InterfaceTraits())
    yield
    it.clear_declared_traits()


@pytest.mark.asyncio
@needs_ffmpeg
async def test_speech_to_a_chat_that_wants_opus_is_converted_and_sent_as_a_voice_note(
    tmp_path: Path,
) -> None:
    clip = wav(tmp_path / "vox_1.wav")
    target = Target()
    await voice_delivery.send_speech(target, "voicechat", "voicechat/42", clip, "Ciao")
    (payload,) = target.sent
    assert payload["send_as_voice"] is True and payload["text"] == "Ciao"
    assert payload["media"][0].endswith(".ogg")
    assert target.files[0][:4] == b"OggS"  # what Telegram needs for a voice note
    assert not Path(payload["media"][0]).exists()  # the converted copy is cleaned up
    assert clip.exists()  # the engine's clip is not ours to delete


@pytest.mark.asyncio
async def test_an_interface_that_declares_no_format_gets_the_clip_as_it_is(
    tmp_path: Path,
) -> None:
    clip = wav(tmp_path / "vox_2.wav")
    target = Target()
    await voice_delivery.send_speech(target, "plainchat", "plainchat/1", clip, None)
    assert target.sent[0]["media"] == [str(clip)] and target.sent[0]["send_as_voice"]


@pytest.mark.asyncio
async def test_without_ffmpeg_the_original_clip_is_sent_not_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(voice_delivery.shutil, "which", lambda name: None)
    clip = wav(tmp_path / "vox_3.wav")
    target = Target()
    await voice_delivery.send_speech(target, "voicechat", "voicechat/1", clip, "x")
    assert target.sent[0]["media"] == [str(clip)]


@pytest.mark.asyncio
@needs_ffmpeg
async def test_a_clip_ffmpeg_cannot_read_is_sent_as_it_is(tmp_path: Path) -> None:
    clip = tmp_path / "vox_4.wav"
    clip.write_bytes(b"this is not audio")
    target = Target()
    await voice_delivery.send_speech(target, "voicechat", "voicechat/1", clip, "x")
    assert target.sent[0]["media"] == [str(clip)]
    assert not (tmp_path / "vox_4.ogg").exists()


@pytest.mark.asyncio
async def test_a_clip_already_in_the_wanted_container_is_not_converted(
    tmp_path: Path,
) -> None:
    clip = tmp_path / "vox_5.ogg"
    clip.write_bytes(b"OggS-already")
    assert await voice_delivery.convert_for("voicechat", clip) is None


class TestManifest:
    BASE = (
        'schema = 1\n[extension]\nid = "dev/x"\nname = "x"\nversion = "1.0.0"\n'
        '[compat]\ncore_api = ">=0.1"\n[[provides.interfaces]]\nid = "chatty"\n'
        'paths = ["chatty/{chat}"]\n'
    )

    def test_the_format_is_declared(self) -> None:
        manifest = parse_manifest_text(self.BASE + 'voice_format = "ogg_opus"\n')
        assert manifest.provides.interfaces[0].voice_format == "ogg_opus"

    def test_an_unknown_format_is_refused(self) -> None:
        with pytest.raises(ManifestError, match="voice_format"):
            parse_manifest_text(self.BASE + 'voice_format = "wma"\n')
