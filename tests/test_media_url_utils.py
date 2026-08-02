"""Tests for core.media_url_utils.derive_audio_url.

Covers the bug where a Vox output directory outside the ``/static`` tree
(e.g. ``/config/media/tts``) produced a broken ``/static/audio/tts/<name>``
URL (HTTP 404). The fix serves such files under the alternate ``/media`` mount.
"""

from __future__ import annotations

from pathlib import Path

import core.media_url_utils as mu


def test_static_path_preserved() -> None:
    url = mu.derive_audio_url("res/synth_webui/static/audio/tts/vox_123.wav")
    assert url == "/static/audio/tts/vox_123.wav"


def test_absolute_static_path_preserved() -> None:
    url = mu.derive_audio_url("/app/res/synth_webui/static/audio/tts/vox_9.wav")
    assert url == "/static/audio/tts/vox_9.wav"


def test_out_of_static_path_uses_media_mount(monkeypatch) -> None:
    # Configured output dir lives outside the static tree.
    out_dir = Path("/config/media/tts")
    monkeypatch.setattr(mu, "get_vox_output_dir", lambda: out_dir)

    url = mu.derive_audio_url("/config/media/tts/vox_abc_0.wav")
    assert url == "/media/tts/vox_abc_0.wav"


def test_out_of_static_unknown_dir_fallback(monkeypatch) -> None:
    # A path not under the configured dir and with no static segment falls
    # back under /media/tts (never the broken /static fallback).
    monkeypatch.setattr(mu, "get_vox_output_dir", lambda: Path("/config/media/tts"))

    url = mu.derive_audio_url("/tmp/somewhere/vox_x.wav")
    assert url == "/media/tts/vox_x.wav"


def test_vox_output_is_outside_static(monkeypatch) -> None:
    monkeypatch.setattr(mu, "get_vox_output_dir", lambda: Path("/config/media/tts"))
    assert mu.vox_output_is_outside_static() is True

    monkeypatch.setattr(
        mu, "get_vox_output_dir", lambda: Path("res/synth_webui/static/audio/tts")
    )
    assert mu.vox_output_is_outside_static() is False
