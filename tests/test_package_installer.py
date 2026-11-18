"""Tests for first-use engine package installation (``core.package_installer``)."""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

import core.package_installer as pi

MODULE = "vosk"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    pi.reset_for_tests()
    monkeypatch.setattr(pi, "extra_packages_dir", lambda: tmp_path / "pylibs")
    monkeypatch.setattr(pi, "_enabled", lambda: True)
    monkeypatch.setattr(pi.shutil, "which", lambda name: "/usr/bin/uv")
    before = list(sys.path)
    yield
    sys.path[:] = before
    sys.modules.pop(MODULE, None)
    pi.reset_for_tests()


def _no_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the module is not importable until a fake install makes it so."""
    state = {"installed": False}
    monkeypatch.setattr(pi, "_importable", lambda module: state["installed"])
    monkeypatch.setattr(pi, "_state", state, raising=False)


def test_already_importable_installs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pi, "_importable", lambda module: True)

    def _boom(*a: Any, **k: Any) -> None:
        raise AssertionError("must not install")

    monkeypatch.setattr(pi.subprocess, "run", _boom)
    assert pi.ensure_package(MODULE) is True


def test_installs_allowlisted_requirement_into_persistent_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _no_module(monkeypatch)
    seen: list[list[str]] = []

    def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        (tmp_path / "pylibs").mkdir(exist_ok=True)
        pi._state["installed"] = True  # type: ignore[attr-defined]
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(pi.subprocess, "run", _run)

    assert pi.ensure_package(MODULE) is True
    cmd = seen[0]
    assert cmd[:3] == ["/usr/bin/uv", "pip", "install"]
    assert cmd[cmd.index("--python") + 1] == sys.executable
    assert cmd[cmd.index("--target") + 1] == str(tmp_path / "pylibs")
    assert cmd[-1] == "vosk>=0.3.45"
    # Appended to sys.path, never prepended: it cannot shadow the app environment.
    assert sys.path[-1] == str(tmp_path / "pylibs")


def test_unknown_module_is_never_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pi, "_importable", lambda module: False)
    monkeypatch.setattr(
        pi.subprocess, "run", lambda *a, **k: pytest.fail("must not install")
    )
    assert pi.ensure_package("totally_unlisted_pkg") is False


def test_switch_off_means_no_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pi, "_importable", lambda module: False)
    monkeypatch.setattr(pi, "_enabled", lambda: False)
    monkeypatch.setattr(
        pi.subprocess, "run", lambda *a, **k: pytest.fail("must not install")
    )
    assert pi.ensure_package(MODULE) is False


def test_failed_install_is_not_retried_within_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pi, "_importable", lambda module: False)
    calls: list[int] = []

    def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(1)
        return subprocess.CompletedProcess(cmd, 1, "", "no network")

    monkeypatch.setattr(pi.subprocess, "run", _run)
    assert pi.ensure_package(MODULE) is False
    assert pi.ensure_package(MODULE) is False
    assert len(calls) == 1


def test_missing_uv_fails_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pi, "_importable", lambda module: False)
    monkeypatch.setattr(pi.shutil, "which", lambda name: None)
    assert pi.ensure_package(MODULE) is False


def test_vosk_loader_installs_on_first_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_load_model`` asks the installer before importing vosk."""
    from plugins.auris_engines import vosk_engine

    sys.modules.pop(MODULE, None)
    monkeypatch.setitem(sys.modules, MODULE, None)  # import vosk -> ImportError
    model_dir = tmp_path / "model"
    model_dir.mkdir()

    fake = types.ModuleType(MODULE)
    fake.SetLogLevel = lambda level: None  # type: ignore[attr-defined]
    fake.Model = lambda path: ("model", path)  # type: ignore[attr-defined]
    asked: list[str] = []

    def _ensure(module: str) -> bool:
        asked.append(module)
        sys.modules[MODULE] = fake  # what a successful install makes importable
        return True

    monkeypatch.setattr(pi, "ensure_package", _ensure)
    monkeypatch.setattr(vosk_engine, "_MODEL_CACHE", {})

    assert vosk_engine._load_model(model_dir) == ("model", str(model_dir))
    assert asked == [MODULE]


def test_language_detection_asks_for_faster_whisper_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without it Vosk always falls back to English: install it on first use."""
    from plugins.auris_engines import vosk_engine

    asked: list[str] = []

    def _ensure(module: str) -> bool:
        asked.append(module)
        return False

    monkeypatch.setattr(pi, "ensure_package", _ensure)
    monkeypatch.setattr(vosk_engine, "_WHISPER_LID_MODEL", None)
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # import -> ImportError

    assert vosk_engine._get_whisper_lid_model() is None
    assert asked == ["faster_whisper"]
