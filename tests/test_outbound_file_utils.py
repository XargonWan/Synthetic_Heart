"""Tests for core.outbound_file_utils (outbound attachment path safety + MIME)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core import outbound_file_utils as ofu


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the allowed roots at an isolated temp directory."""
    root = tmp_path / "sandbox"
    root.mkdir()
    monkeypatch.setenv("AGENT_FS_ROOTS", str(root))
    return root


def test_allowed_file_roots_from_env(sandbox: Path) -> None:
    roots = ofu.allowed_file_roots()
    assert sandbox.resolve() in roots


def test_allowed_file_roots_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_FS_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_FS_ROOT", "/app")
    monkeypatch.setenv("SYNTH_LOG_DIR", "/app/logs")
    roots = ofu.allowed_file_roots()
    assert Path("/app").resolve() in roots
    assert Path("/app/logs").resolve() in roots


def test_resolve_safe_outbound_path_absolute_inside(sandbox: Path) -> None:
    f = sandbox / "doc.txt"
    f.write_text("hello")
    resolved, err = ofu.resolve_safe_outbound_path(str(f))
    assert err is None
    assert resolved == f.resolve()


def test_resolve_safe_outbound_path_relative(sandbox: Path) -> None:
    f = sandbox / "rel.txt"
    f.write_text("hi")
    resolved, err = ofu.resolve_safe_outbound_path("rel.txt")
    assert err is None
    assert resolved == f.resolve()


def test_resolve_safe_outbound_path_empty() -> None:
    resolved, err = ofu.resolve_safe_outbound_path("")
    assert resolved is None
    assert err == "Missing path"


def test_resolve_safe_outbound_path_traversal_blocked(sandbox: Path) -> None:
    # A ../ escape attempt must be rejected even if the target exists.
    outside = sandbox.parent / "secret.txt"
    outside.write_text("nope")
    resolved, err = ofu.resolve_safe_outbound_path(str(sandbox / ".." / "secret.txt"))
    assert resolved is None
    assert err == "Path is outside allowed roots"


def test_resolve_safe_outbound_path_missing_file(sandbox: Path) -> None:
    resolved, err = ofu.resolve_safe_outbound_path(str(sandbox / "nope.txt"))
    assert resolved is None
    assert err == "File does not exist"


def test_resolve_safe_outbound_path_directory_rejected(sandbox: Path) -> None:
    d = sandbox / "subdir"
    d.mkdir()
    resolved, err = ofu.resolve_safe_outbound_path(str(d))
    assert resolved is None
    assert err == "Path is not a regular file"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("photo.jpg", ofu.MEDIA_IMAGE),
        ("photo.jpeg", ofu.MEDIA_IMAGE),
        ("clip.mp4", ofu.MEDIA_VIDEO),
        ("song.mp3", ofu.MEDIA_AUDIO),
        ("voice.ogg", ofu.MEDIA_AUDIO),
        ("report.pdf", ofu.MEDIA_DOCUMENT),
        ("archive.bin", ofu.MEDIA_DOCUMENT),
    ],
)
def test_classify_media(name: str, expected: str) -> None:
    assert ofu.classify_media(name) == expected


def test_guess_mime_type_fallback() -> None:
    assert ofu.guess_mime_type("weird.unknownext") == "application/octet-stream"


def test_guess_mime_type_known() -> None:
    assert ofu.guess_mime_type("photo.png") == "image/png"
