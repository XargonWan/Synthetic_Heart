"""Tests for the native health probe and launcher helpers.

``scripts/healthcheck.py`` and ``scripts/start_synth.py`` are what the installers'
shortcuts call, so their decision logic is pinned here: URL derivation, scheme
fallback, pid bookkeeping and interpreter selection. One test does start a process,
because the flags a child is started with are the difference between it running and
silently doing nothing.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, relative: str) -> object:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


healthcheck = _load("_synth_healthcheck_under_test", "scripts/healthcheck.py")
start_synth = _load("_synth_start_under_test", "scripts/start_synth.py")


def _windowless_python() -> str:
    """The interpreter a desktop shortcut and the tray actually use: no console.

    Falls back to the running interpreter where there is no separate windowed one
    (Linux), which is itself always console-backed.
    """
    return str(start_synth.venv_python(windowed=True) or sys.executable)


def _spawn_windowless(command: list[str]) -> subprocess.Popen:
    """Start a child the way the launcher starts SyntH: no console, own group."""
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(  # noqa: S603
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )


def _process_exists(pid: int) -> bool:
    """Ask the OS whether ``pid`` is running, independently of the launcher.

    Deliberately not ``Popen.poll()``: inside a pytest process, polling a child that
    was terminated through this process's own Win32 calls keeps reporting it as
    running while the OS says it is gone, so the poll would hide a working stop.
    """
    if os.name == "nt":
        done = subprocess.run(  # noqa: S603
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | "
                "Select-Object -ExpandProperty Id",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return bool(done.stdout.strip())
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# healthcheck: configuration parsing
# ---------------------------------------------------------------------------


def test_parse_env_file_reads_values_and_skips_comments(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\nDB_PORT=5432\nSYNTH_WEBUI_TLS='1'\nEMPTY=\n", encoding="utf-8"
    )
    values = healthcheck.parse_env_file(env)
    assert values == {"DB_PORT": "5432", "SYNTH_WEBUI_TLS": "1", "EMPTY": ""}


def test_parse_env_file_on_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert healthcheck.parse_env_file(tmp_path / "absent") == {}


def test_effective_env_lets_the_real_environment_win(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("DB_PORT=5432\n", encoding="utf-8")
    monkeypatch.setenv("DB_PORT", "6543")
    assert healthcheck.effective_env(env)["DB_PORT"] == "6543"


# ---------------------------------------------------------------------------
# healthcheck: WebUI URL derivation
# ---------------------------------------------------------------------------


def test_webui_url_uses_plain_http_for_a_loopback_native_install() -> None:
    assert (
        healthcheck._webui_url(
            {"SYNTH_WEBUI_TLS": "0", "SYNTH_WEBUI_HTTP_PORT": "8080"}
        )
        == "http://127.0.0.1:8080/"
    )


def test_webui_url_uses_https_when_configured() -> None:
    values = {"SYNTH_WEBUI_TLS": "1", "SYNTH_WEBUI_HTTPS_PORT": "8443"}
    assert healthcheck._webui_url(values) == "https://127.0.0.1:8443/"


def test_webui_url_falls_back_to_the_http_port_for_tls() -> None:
    """With TLS on and no HTTPS port, the app serves TLS on the HTTP port."""
    values = {"SYNTH_WEBUI_TLS": "1", "SYNTH_WEBUI_HTTP_PORT": "8080"}
    assert healthcheck._webui_url(values) == "https://127.0.0.1:8080/"


def test_webui_url_maps_the_all_interfaces_bind_to_loopback() -> None:
    values = {"SYNTH_WEBUI_TLS": "0", "SYNTH_WEBUI_HOST": "0.0.0.0"}
    assert healthcheck._webui_url(values) == "http://127.0.0.1:8080/"


def test_webui_candidates_probe_both_schemes() -> None:
    candidates = healthcheck._webui_candidates({"SYNTH_WEBUI_TLS": "1"})
    assert candidates[0][0].startswith("https://")
    assert candidates[0][1] is True
    assert any(url.startswith("http://") for url, _ in candidates)
    assert not any(is_configured for url, is_configured in candidates[1:])


def test_probe_webui_reports_a_scheme_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server up on the wrong scheme is up, with a note — not 'down'."""
    monkeypatch.setattr(
        healthcheck,
        "http_reachable",
        lambda url, timeout=5.0: (url.startswith("http://"), "200"),
    )
    result = healthcheck._probe_webui(
        {"SYNTH_WEBUI_TLS": "1", "SYNTH_WEBUI_HTTP_PORT": "8080"}
    )
    assert result["ok"] is True
    assert result["scheme_mismatch"] is True
    assert str(result["url"]).startswith("http://")


def test_probe_webui_reports_down_when_nothing_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        healthcheck, "http_reachable", lambda url, timeout=5.0: (False, "no")
    )
    result = healthcheck._probe_webui({"SYNTH_WEBUI_TLS": "0"})
    assert result["ok"] is False
    assert result["scheme_mismatch"] is False


# ---------------------------------------------------------------------------
# start_synth: launcher internals
# ---------------------------------------------------------------------------


def test_build_command_runs_main_py_with_the_given_interpreter() -> None:
    command = start_synth.build_command(Path("/python"), console=False)
    assert command[0] == str(Path("/python"))
    assert command[1].endswith("main.py")


def test_pid_file_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(start_synth, "pid_path", lambda: tmp_path / "synth.pid")
    assert start_synth.read_pid() is None
    start_synth.write_pid(4321)
    assert start_synth.read_pid() == 4321
    start_synth.clear_pid()
    assert start_synth.read_pid() is None


def test_pid_alive_for_this_process() -> None:
    assert start_synth.pid_alive(os.getpid()) is True


def test_pid_alive_for_an_impossible_pid() -> None:
    assert start_synth.pid_alive(999_999_999) is False


def test_pid_alive_is_right_from_a_process_with_no_console(tmp_path: Path) -> None:
    """A live SyntH must not be reported as stopped by a console-less caller.

    The tray calls ``--stop`` through ``pythonw.exe``. Measured before this test
    existed: from a process with no console, ``os.kill(live_pid, 0)`` raises
    ``OSError`` (WinError 6 "The handle is invalid", or 87 "The parameter is
    incorrect" with DEVNULL stdio), so ``pid_alive`` answered False for a running
    SyntH, ``stop()`` printed "SyntH is not running." into a console nobody has, and
    the tray's Shut down did nothing at all.
    """
    answer_file = tmp_path / "answer.json"
    script = tmp_path / "liveness.py"
    script.write_text(
        "import importlib.util, json, os, sys\n"
        "from pathlib import Path\n"
        f"root = Path(r'{REPO_ROOT}')\n"
        "spec = importlib.util.spec_from_file_location(\n"
        "    'ss', root / 'scripts' / 'start_synth.py'\n"
        ")\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules['ss'] = module\n"
        "spec.loader.exec_module(module)\n"
        "answer = {\n"
        "    'self': module.pid_alive(os.getpid()),\n"
        "    'impossible': module.pid_alive(999999999),\n"
        "}\n"
        f"Path(r'{answer_file}').write_text(json.dumps(answer), encoding='utf-8')\n",
        encoding="utf-8",
    )
    _spawn_windowless([_windowless_python(), str(script)]).wait(timeout=60)

    assert answer_file.is_file(), "the console-less child produced no answer"
    answer = json.loads(answer_file.read_text(encoding="utf-8"))
    assert answer["self"] is True, (
        "a live process was reported as dead without a console"
    )
    assert answer["impossible"] is False


def test_the_sweep_only_claims_this_installs_application(tmp_path: Path) -> None:
    """Finding the app by what it is must not reach another install on the machine."""
    outside = tmp_path / "elsewhere"
    lines = {
        f"{REPO_ROOT}\\.venv\\Scripts\\pythonw.exe {REPO_ROOT}\\main.py": True,
        f'"{REPO_ROOT}\\.venv\\Scripts\\python.exe" {REPO_ROOT}\\main.py --setup': True,
        f"{REPO_ROOT}/.venv/bin/python {REPO_ROOT}/main.py": True,
        f"{outside}\\.venv\\Scripts\\python.exe {outside}\\main.py": False,
        r"D:\dev\D18\.venv\Scripts\python.exe main.py": False,
        f"{REPO_ROOT}\\scripts\\start_synth.py --stop": False,
        f"powershell.exe -AppRoot {REPO_ROOT}": False,
        "": False,
    }
    for command_line, expected in lines.items():
        assert start_synth.owns_app(command_line) is expected, command_line


def test_stop_ends_the_process_it_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(start_synth, "pid_path", lambda: tmp_path / "synth.pid")
    # Hermetic: the sweep is exercised by its own test, not against whatever happens
    # to be running on this machine.
    monkeypatch.setattr(start_synth, "app_pids", lambda: [])
    stand_in = _spawn_windowless([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        time.sleep(0.5)
        start_synth.write_pid(stand_in.pid)
        assert start_synth.stop(quiet=True) == 0
        assert not _process_exists(stand_in.pid), (
            "the recorded process survived the stop"
        )
        assert start_synth.read_pid() is None
    finally:
        if stand_in.poll() is None:
            stand_in.kill()
            stand_in.wait(timeout=10)


def test_stop_finds_the_application_without_a_pid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing or stale pid file must not turn Shut down into a no-op."""
    monkeypatch.setattr(start_synth, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(start_synth, "pid_path", lambda: tmp_path / "absent.pid")
    fake_main = tmp_path / "main.py"
    fake_main.write_text(
        "import time\nwhile True:\n    time.sleep(1)\n", encoding="utf-8"
    )
    orphan = _spawn_windowless([sys.executable, str(fake_main)])
    try:
        time.sleep(0.5)
        assert orphan.pid in start_synth.app_pids(), (
            "the sweep did not recognise this install's own application"
        )
        assert start_synth.stop(quiet=True) == 0
        assert not _process_exists(orphan.pid), "the application survived the stop"
    finally:
        start_synth.clear_pid()
        if orphan.poll() is None:
            orphan.kill()
            orphan.wait(timeout=10)


def test_stop_is_a_no_op_without_a_pid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The shortcut must never kill something it did not start.

    ``app_pids`` is stubbed out here on purpose: the sweep looks at the real machine,
    and a test run from a checkout that happens to have a SyntH running would
    otherwise stop it.
    """
    monkeypatch.setattr(start_synth, "pid_path", lambda: tmp_path / "synth.pid")
    called: list[tuple[int, int]] = []
    monkeypatch.setattr(start_synth, "read_pid", lambda: None)
    monkeypatch.setattr(start_synth, "app_pids", lambda: [])
    monkeypatch.setattr(
        start_synth, "terminate_pid", lambda pid: called.append(pid) or (True, "")
    )
    assert start_synth.stop() == 0
    assert called == []
    assert "not running" in capsys.readouterr().out


def test_venv_python_prefers_the_windowed_interpreter_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(start_synth, "REPO_ROOT", tmp_path)
    scripts_dir = tmp_path / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    scripts_dir.mkdir(parents=True)
    plain = scripts_dir / ("python.exe" if os.name == "nt" else "python")
    plain.write_text("", encoding="utf-8")
    assert start_synth.venv_python(windowed=False) == plain
    if os.name == "nt":
        windowed = scripts_dir / "pythonw.exe"
        windowed.write_text("", encoding="utf-8")
        assert start_synth.venv_python(windowed=True) == windowed
    else:
        assert start_synth.venv_python(windowed=True) == plain


def test_venv_python_is_none_without_an_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(start_synth, "REPO_ROOT", tmp_path)
    assert start_synth.venv_python(windowed=False) is None
    assert start_synth.venv_python(windowed=True) is None


def test_venv_python_never_consults_an_absolute_posix_path_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard the drive-relative trap: no ``/app``-style assumption in the launcher."""
    source = (REPO_ROOT / "scripts" / "start_synth.py").read_text(encoding="utf-8")
    assert '"/app' not in source
    assert "'/app" not in source


# ---------------------------------------------------------------------------
# The tray icon: a windowless launch has to look like something happened
# ---------------------------------------------------------------------------


def _args(**overrides: object) -> object:
    base = {"tray": False, "no_tray": False, "foreground": False}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_the_tray_is_shown_by_default_on_a_windows_desktop_launch() -> None:
    """The installer's shortcut and its finish action both take this path."""
    expected = os.name == "nt"
    assert start_synth.wants_tray(_args()) is expected


def test_a_foreground_run_uses_its_console_instead_of_the_tray() -> None:
    assert start_synth.wants_tray(_args(foreground=True)) is False


def test_no_tray_and_tray_win_over_the_default() -> None:
    assert start_synth.wants_tray(_args(no_tray=True, tray=True)) is False
    assert start_synth.wants_tray(_args(tray=True)) is True


def test_the_tray_command_points_at_the_script_and_the_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(start_synth.os, "name", "nt")
    monkeypatch.setattr(start_synth, "REPO_ROOT", tmp_path)
    script = tmp_path / "scripts" / "synth_tray.ps1"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        start_synth.shutil, "which", lambda name: "C:/ps/powershell.exe"
    )

    command = start_synth.tray_command(tmp_path / ".env")

    assert command is not None
    assert command[0] == "C:/ps/powershell.exe"
    assert str(script) in command
    # The tray reads the same .env, so it probes the port and scheme the app was
    # actually configured with rather than a hard-coded 8080.
    assert str(tmp_path / ".env") in command
    assert "-WindowStyle" in command and "Hidden" in command


def test_there_is_no_tray_outside_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(start_synth.os, "name", "posix")
    assert start_synth.tray_command(tmp_path / ".env") is None
    assert start_synth.spawn_tray(tmp_path / ".env") is False


def test_a_missing_tray_script_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source tree without the script still starts SyntH."""
    monkeypatch.setattr(start_synth.os, "name", "nt")
    monkeypatch.setattr(start_synth, "REPO_ROOT", tmp_path)
    assert start_synth.tray_command(tmp_path / ".env") is None


def test_spawn_tray_launches_it_detached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(start_synth.os, "name", "nt")
    monkeypatch.setattr(
        start_synth, "tray_command", lambda env_file: ["powershell", "-File", "tray"]
    )
    launched: list[tuple[list[str], dict]] = []

    class _Process:
        pid = 4242

    def fake_popen(command, **kwargs):
        launched.append((list(command), kwargs))
        return _Process()

    monkeypatch.setattr(start_synth.subprocess, "Popen", fake_popen)

    assert start_synth.spawn_tray(tmp_path / ".env") is True
    assert launched and launched[0][0] == ["powershell", "-File", "tray"]
    # Detached: the launcher exits after opening the browser and must not take the
    # icon down with it.
    assert launched[0][1].get("creationflags")


def test_the_tray_script_offers_the_actions_the_icon_promises() -> None:
    """The menu is the feature: pin its entries against a rename or a rewrite."""
    script = REPO_ROOT / "scripts" / "synth_tray.ps1"
    assert script.is_file(), "the launcher references this path"
    text = script.read_text(encoding="utf-8")

    for label in ("Open SyntH", "Restart", "Shut down", "Check for updates"):
        assert label in text, f"missing tray menu entry: {label}"
    # Reads the install's own .env instead of assuming a port.
    assert "SYNTH_WEBUI_HTTP_PORT" in text
    # Single instance, so repeated launches cannot stack icons.
    assert "Mutex" in text
    # The transparent artwork, not the black-tiled squircle.
    assert "synth-tray.ico" in text


def test_no_child_of_the_launcher_is_started_console_detached() -> None:
    """DETACHED_PROCESS turns a console program into a no-op on Windows.

    Measured on a real machine: ``powershell -Command '... | Set-Content marker'``
    writes the marker with no flags and with CREATE_NO_WINDOW, and writes nothing at
    all with DETACHED_PROCESS, exiting 0 either way. The launcher starts both the
    application and the tray, and python.exe and powershell.exe are console programs,
    so using it there means a process that starts, disappears and leaves no trace.
    """
    source = (REPO_ROOT / "scripts" / "start_synth.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    assert "DETACHED_PROCESS" not in code, (
        "a detached console program runs nothing at all on Windows"
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows-only process creation flags")
def test_a_python_child_really_runs_with_the_launchers_flags(tmp_path: Path) -> None:
    """Prove the flags the launcher uses let a child actually do its work.

    The tray icon failing to appear was caused by exactly this: flags that made the
    child start and exit without running. Asserting the flags by name is not enough,
    because the failure mode is silence, so this starts a real interpreter with them.
    """
    marker = tmp_path / "marker.txt"
    interpreter = sys.executable or shutil.which("python")
    script = f"from pathlib import Path; Path(r'{marker}').write_text('ran', encoding='utf-8')"
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen(  # noqa: S603
        [interpreter, "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    try:
        deadline = time.time() + 30.0
        while time.time() < deadline and not marker.is_file():
            time.sleep(0.1)
        assert marker.is_file(), "a child started with these flags did not run"
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=15)


def test_status_reports_that_nothing_is_running(capsys, tmp_path) -> None:
    """`synth --status` printed nothing at all, which reads as a broken command.

    Reported from a fresh Debian VM: `synth --status` showed nothing and `--stop`
    said not running, and there was no way to tell the silent case from the failed
    one.
    """
    code = start_synth.main(["--status", "--env-file", str(tmp_path / "absent.env")])
    out = capsys.readouterr().out
    assert "not running" in out.lower(), f"--status printed {out!r}"
    assert code == 1
