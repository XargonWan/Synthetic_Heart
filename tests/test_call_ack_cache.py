"""Tests for the call "message received" signal (``core.call_ack_cache``)."""

from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest

import core.call_ack_cache as cac


@pytest.fixture
def out_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    import core.media_url_utils as mu

    directory = tmp_path / "tts"
    directory.mkdir()
    monkeypatch.setattr(mu, "get_vox_output_dir", lambda: directory)
    return directory


def test_chime_is_a_short_audible_mono_wav() -> None:
    with wave.open(io.BytesIO(cac.render_chime()), "rb") as wav:
        assert wav.getnchannels() == 1 and wav.getsampwidth() == 2
        assert wav.getframerate() == cac.SAMPLE_RATE
        seconds = wav.getnframes() / wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    assert 0.3 < seconds < 1.0
    samples = memoryview(frames).cast("h")
    peak = max(abs(s) for s in samples)
    assert 0.2 * 32767 < peak < 0.8 * 32767  # audible, not clipping
    # Starts and ends near silence: no click.
    assert abs(samples[0]) < 500 and abs(samples[-1]) < 500


def test_get_writes_the_file_once_under_the_served_audio_dir(out_dir: Path) -> None:
    cache = cac.get_call_ack_cache()
    path = cache.get()
    assert path is not None and path.parent == out_dir / "call_ack"
    assert path.read_bytes() == cac.render_chime()

    mtime = path.stat().st_mtime_ns
    cache.clear()
    assert cache.get() == path
    assert path.stat().st_mtime_ns == mtime  # reused, not rewritten
    # Language is irrelevant: it is not a spoken clip.
    assert cache.get("it") == path


def test_get_removes_clips_left_by_older_versions_but_keeps_the_signals(
    out_dir: Path,
) -> None:
    stale_dir = out_dir / "call_ack"
    stale_dir.mkdir()
    (stale_dir / "it_abc123.wav").write_bytes(b"old spoken hmm")
    cache = cac.get_call_ack_cache()
    for kind in ("ack", "start", "end"):
        assert cache.get_signal(kind) is not None
    assert sorted(p.name for p in stale_dir.glob("*.wav")) == [
        "chime-v1.wav",
        "hangup-v1.wav",
        "pickup-v1.wav",
    ]


def test_pickup_rises_and_hangup_falls_and_all_are_short() -> None:
    import wave

    def pitch_order(kind: str) -> list[float]:
        return [note[0] for note in cac._SIGNALS[kind]]

    assert pitch_order("start") == sorted(pitch_order("start"))  # rising
    assert pitch_order("end") == sorted(pitch_order("end"), reverse=True)  # falling
    for kind in ("ack", "start", "end"):
        with wave.open(io.BytesIO(cac.render_signal(kind)), "rb") as wav:
            seconds = wav.getnframes() / wav.getframerate()
        assert 0.3 < seconds < 1.0, kind
    assert cac.render_signal("start") != cac.render_signal("end")


def test_unwritable_dir_fails_soft(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import core.media_url_utils as mu

    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    monkeypatch.setattr(mu, "get_vox_output_dir", lambda: blocker)
    assert cac.get_call_ack_cache().get() is None


@pytest.mark.asyncio
async def test_prepare_returns_the_signal(out_dir: Path) -> None:
    ready = await cac.get_call_ack_cache().prepare(["it", "en"])
    assert {"ack", "start", "end", "default"} <= set(ready)
    assert all(path.is_file() for path in ready.values())
