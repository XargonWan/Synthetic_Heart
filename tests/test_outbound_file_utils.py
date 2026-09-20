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


def test_agent_fs_roots_keeps_windows_drive_letters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``C:/a:D:/b`` is two roots, not four fragments split on the drive colon."""
    root_a = tmp_path / "root_a"
    root_b = tmp_path / "root_b"
    root_a.mkdir()
    root_b.mkdir()
    monkeypatch.setenv("AGENT_FS_ROOTS", f"{root_a}:{root_b}")

    roots = ofu.allowed_file_roots()
    assert root_a.resolve() in roots
    assert root_b.resolve() in roots

    f = root_b / "note.txt"
    f.write_text("hi")
    resolved, err = ofu.resolve_safe_outbound_path(str(f))
    assert err is None
    assert resolved == f.resolve()


def test_allowed_file_roots_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_FS_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_FS_ROOT", "/app")
    monkeypatch.setenv("SYNTH_LOG_DIR", "/app/logs")
    roots = ofu.allowed_file_roots()
    assert Path("/app").resolve() in roots
    assert Path("/app/logs").resolve() in roots


def test_allowed_file_roots_default_to_the_app_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no override the sandbox root is the application tree, not "/app".

    In the container the app lives at /app, so the two are the same directory and
    nothing changes. In a bare checkout "/app" does not exist and every outbound
    attachment was rejected with "Path is outside allowed roots" — the text still
    arrived, the media was silently dropped (broke every voice note on Telegram
    from a Windows dev tree).
    """
    monkeypatch.delenv("AGENT_FS_ROOTS", raising=False)
    monkeypatch.delenv("AGENT_FS_ROOT", raising=False)
    monkeypatch.delenv("SYNTH_LOG_DIR", raising=False)

    app_root = Path(ofu.__file__).resolve().parent.parent
    roots = ofu.allowed_file_roots()
    assert app_root in roots
    assert (app_root / "logs") in roots

    # An ordinary file inside the app tree must be deliverable.
    resolved, err = ofu.resolve_safe_outbound_path(str(Path(ofu.__file__).resolve()))
    assert err is None
    assert resolved is not None


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
