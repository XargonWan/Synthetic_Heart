"""Tests for the native-path helpers and the container-path guard.

``core/app_paths.py`` is the single place that decides where an application-owned
file goes when the container's ``/app`` / ``/config`` layout is absent (a native
Windows or bare-metal install). These tests pin the precedence rules that the
rest of the code relies on, and run the ``check_dockerisms`` guard so a new
container-only default fails locally instead of on a user's machine.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core import app_paths


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every environment variable the resolver reads."""
    for name in (
        "SYNTH_APP_ROOT",
        "SYNTH_DATA_ROOT",
        "SYNTH_LOG_DIR",
        "SYNTH_STATE_DIR",
        "SYNTH_WEBUI_CERT_DIR",
        "SYNTH_EXPOSED_STORAGE_ROOT",
        "SYNTH_RADIO_AUDIO_DIR",
        "AGENT_FS_ROOTS",
        "AGENT_FS_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_app_root_is_the_repository_root() -> None:
    root = app_paths.app_root()
    assert (root / "core" / "app_paths.py").is_file()
    assert (root / "main.py").is_file()


def test_app_root_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SYNTH_APP_ROOT", str(tmp_path))
    assert app_paths.app_root() == tmp_path.resolve()


def test_data_root_prefers_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SYNTH_DATA_ROOT", str(tmp_path / "state"))
    assert app_paths.data_root() == tmp_path / "state"


def test_data_root_falls_back_inside_the_app_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no writable /config, state must land under the application root."""
    monkeypatch.setenv("SYNTH_APP_ROOT", str(tmp_path))
    monkeypatch.setattr(app_paths, "CONTAINER_DATA_ROOT", tmp_path / "nope" / "\0bad")
    resolved = app_paths.data_root()
    assert resolved == tmp_path / "data"


def test_data_root_uses_a_writable_container_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    container = tmp_path / "config"
    container.mkdir()
    monkeypatch.setenv("SYNTH_APP_ROOT", str(tmp_path / "app"))
    monkeypatch.setattr(app_paths, "CONTAINER_DATA_ROOT", container)
    assert app_paths.data_root() == container


def test_data_root_ignores_a_missing_container_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An absent container mount must not be adopted just because it is named."""
    monkeypatch.setenv("SYNTH_APP_ROOT", str(tmp_path))
    monkeypatch.setattr(app_paths, "CONTAINER_DATA_ROOT", tmp_path / "absent")
    assert app_paths.data_root() == tmp_path / "data"


def test_data_root_never_returns_a_drive_relative_path(tmp_path: Path) -> None:
    """On Windows ``/config`` means ``\\config``: never an acceptable data root.

    The regression this guards: a Docker-era default captured state into
    ``D:\\config`` (the root of whatever drive the process started on).
    """
    monkeypatch_free_default = app_paths.data_root()
    if os.name != "nt":
        assert monkeypatch_free_default.is_absolute()
        return
    assert not Path("/config").is_absolute()
    assert monkeypatch_free_default.is_absolute()
    assert app_paths.app_root() in monkeypatch_free_default.parents


def test_resolving_paths_creates_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Path resolution is read-only; it must not litter the filesystem."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    monkeypatch.setenv("SYNTH_APP_ROOT", str(app_dir))
    monkeypatch.setattr(app_paths, "CONTAINER_DATA_ROOT", tmp_path / "absent-config")

    app_paths.data_root()
    app_paths.log_dir()
    app_paths.cert_dir()
    app_paths.exposed_storage_root()
    app_paths.state_path("x.json")
    app_paths.agent_fs_roots()

    assert list(app_dir.iterdir()) == [], "resolving a path must not create it"


def test_agent_fs_roots_default_to_the_app_root() -> None:
    roots = app_paths.agent_fs_roots()
    assert roots, "agent sandbox must never be empty without an explicit override"
    assert roots[0] == app_paths.app_root()
    assert roots[1] == app_paths.app_root() / "logs"
    # The bug this guards: on Windows the literal "/app" resolves to C:\app.
    assert not str(roots[0]).startswith("/app")


def test_agent_fs_roots_honour_explicit_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    monkeypatch.setenv("AGENT_FS_ROOT", str(first))
    monkeypatch.setenv("SYNTH_LOG_DIR", str(second))
    assert app_paths.agent_fs_roots() == [first.resolve(), second.resolve()]


def test_agent_fs_roots_split_on_pathsep(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A multi-root list must use os.pathsep, not a hardcoded colon."""
    first = tmp_path / "one"
    second = tmp_path / "two"
    monkeypatch.setenv("AGENT_FS_ROOTS", f"{first}{os.pathsep}{second}")
    assert app_paths.agent_fs_roots() == [first.resolve(), second.resolve()]


def test_cert_dir_and_exposed_storage_live_under_the_data_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SYNTH_DATA_ROOT", str(tmp_path))
    assert app_paths.cert_dir() == tmp_path / "ssl"
    assert app_paths.exposed_storage_root() == tmp_path / "storage"
    assert app_paths.state_path("x.json") == tmp_path / "x.json"


def test_explicit_cert_dir_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SYNTH_WEBUI_CERT_DIR", str(tmp_path / "certs"))
    assert app_paths.cert_dir() == tmp_path / "certs"


def test_in_container_honours_the_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYNTH_IN_CONTAINER", "0")
    assert app_paths.in_container() is False
    monkeypatch.setenv("SYNTH_IN_CONTAINER", "true")
    assert app_paths.in_container() is True


def test_in_container_is_false_for_a_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repository checkout is never ``/app``, so it is not a container."""
    monkeypatch.delenv("SYNTH_IN_CONTAINER", raising=False)
    if os.name == "nt":
        assert app_paths.in_container() is False
    if app_paths.app_root() != Path("/app"):
        assert app_paths.in_container() is False


def test_bind_host_defaults_to_loopback_natively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SYNTH_WEBUI_HOST", raising=False)
    monkeypatch.setenv("SYNTH_IN_CONTAINER", "0")
    assert app_paths.default_bind_host() == "127.0.0.1"


def test_bind_host_defaults_to_all_interfaces_in_a_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SYNTH_WEBUI_HOST", raising=False)
    monkeypatch.setenv("SYNTH_IN_CONTAINER", "1")
    assert app_paths.default_bind_host() == "0.0.0.0"


def test_bind_host_explicit_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNTH_IN_CONTAINER", "0")
    monkeypatch.setenv("SYNTH_WEBUI_HOST", "192.168.1.10")
    assert app_paths.default_bind_host() == "192.168.1.10"
    monkeypatch.delenv("SYNTH_WEBUI_HOST")
    monkeypatch.setenv("OLLAMA_HOST", "10.0.0.5")
    assert app_paths.default_bind_host("OLLAMA_HOST") == "10.0.0.5"


def test_check_dockerisms_guard_is_clean() -> None:
    """The repository ships no un-allowlisted container-only path defaults."""
    script = app_paths.app_root() / "scripts" / "check_dockerisms.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        cwd=str(app_paths.app_root()),
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_radio_audio_dir_is_not_container_only() -> None:
    """The radio plugin resolves its audio dir instead of hardcoding /app."""
    module = importlib.import_module("plugins.radio_host.radio_host_plugin")
    audio_dir = Path(module.AUDIO_STORAGE_DIR)
    if app_paths.usable_container_dir(Path("/app")):
        assert audio_dir == Path("/app/tmp_tts/radio_host")
    else:
        assert app_paths.app_root() in audio_dir.parents
