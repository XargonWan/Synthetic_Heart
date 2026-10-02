#!/usr/bin/env python3
"""Start SyntH the way a desktop user expects: one click, no console window.

This is what the installers' shortcuts and the Linux desktop entry invoke.  It:

* starts ``main.py`` in the background with the project virtual environment;
* on Windows uses ``pythonw.exe`` so no console window appears or lingers;
* redirects output into the normal log file;
* records a pid file so the same shortcut can stop it;
* shows a notification-area icon on Windows, so a windowless launch is visible;
* waits until the WebUI actually answers, then opens the browser on it.

Only the standard library is used, so it also works before ``uv sync``.

Usage::

    python scripts/start_synth.py              # start (or focus) and open the WebUI
    python scripts/start_synth.py --foreground # run in this terminal
    python scripts/start_synth.py --status     # is it up?
    python scripts/start_synth.py --stop       # stop the background instance
    python scripts/start_synth.py --console    # keep a console window (debugging)
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

#: Sibling script, stdlib-only. Imported by path rather than as a package
#: because ``scripts/`` is a folder of standalone tools, not an importable
#: package.
from healthcheck import check  # type: ignore[unresolved-import]  # noqa: E402


# ---------------------------------------------------------------------------
# Paths and interpreter selection
# ---------------------------------------------------------------------------


def _load_app_paths() -> object:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_synth_app_paths", REPO_ROOT / "core" / "app_paths.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError("cannot load core/app_paths.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_APP_PATHS = _load_app_paths()


def data_root() -> Path:
    return _APP_PATHS.data_root()  # type: ignore[attr-defined]


def log_dir() -> Path:
    return _APP_PATHS.log_dir()  # type: ignore[attr-defined]


def pid_path() -> Path:
    return data_root() / "synth.pid"


def venv_python(*, windowed: bool) -> Path | None:
    """Return the interpreter inside the project virtual environment.

    ``windowed=True`` prefers ``pythonw.exe``, which is what keeps a Windows
    desktop launch from flashing a console window.
    """
    candidates: list[Path] = []
    if os.name == "nt":
        base = REPO_ROOT / ".venv" / "Scripts"
        if windowed:
            candidates.append(base / "pythonw.exe")
        candidates.extend([base / "python.exe", base / "pythonw.exe"])
    else:
        candidates.append(REPO_ROOT / ".venv" / "bin" / "python")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# pid handling
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Processes: is it alive, stop it, and find our own
# ---------------------------------------------------------------------------
#
# On Windows, os.kill(pid, 0) cannot answer "is this alive?" from a process that has
# no console, which is exactly how this launcher is started on a desktop install
# (pythonw.exe, and the tray calls --stop with it). Measured here:
#
#     console interpreter:      os.kill(live_pid, 0)  -> no exception
#     windowless child:         os.kill(live_pid, 0)  -> OSError 22 (WinError 87)
#     pythonw, piped stdio:     os.kill(live_pid, 0)  -> OSError 9  (WinError 6)
#
# So pid_alive() called a running SyntH dead, stop() therefore reported "SyntH is not
# running." into a console nobody has, and the tray's Shut down did nothing at all
# while Synth kept serving. The Win32 calls below do not care whether the caller has
# a console.

_IS_WINDOWS = os.name == "nt"

if _IS_WINDOWS:  # pragma: no cover - exercised on Windows only
    import ctypes
    from ctypes import wintypes

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_TERMINATE = 0x0001
    _STILL_ACTIVE = 259
    _ERROR_INVALID_PARAMETER = 87

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def pid_alive(pid: int) -> bool:
    """Return True when a process with ``pid`` is running.

    Deliberately not ``os.kill(pid, 0)`` on Windows: from a process without a console
    that raises for a live process, which is how a running SyntH came to be reported
    as stopped.
    """
    if pid <= 0:
        return False
    if _IS_WINDOWS:  # pragma: no cover - exercised on Windows only
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # ERROR_INVALID_PARAMETER means there is no such process. Anything else
            # (access denied, for instance) means it exists but is not ours to query.
            return ctypes.get_last_error() != _ERROR_INVALID_PARAMETER
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == _STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        # Windows raises a PermissionError-shaped error for a live system process we
        # do not own; treat anything non-OSError as "exists".
        return True
    return True


def terminate_pid(pid: int) -> tuple[bool, str]:
    """Stop one process. Returns ``(asked, detail)``, with an empty detail on success.

    Windows again skips ``os.kill``: TerminateProcess is what it ends up calling
    anyway, and this way a caller with no console gets the same behaviour as one with.
    """
    if pid <= 0:
        return False, "not a process id"
    if _IS_WINDOWS:  # pragma: no cover - exercised on Windows only
        handle = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
        if not handle:
            return False, f"OpenProcess failed (WinError {ctypes.get_last_error()})"
        try:
            if not _kernel32.TerminateProcess(handle, 1):
                error = ctypes.get_last_error()
                return False, f"TerminateProcess failed (WinError {error})"
            return True, ""
        finally:
            _kernel32.CloseHandle(handle)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return False, str(exc)
    return True, ""


def owns_app(command_line: str) -> bool:
    """Whether a command line is this install's application.

    Structural: an interpreter from this install's virtual environment running this
    install's ``main.py``. Scoped to the install root on purpose, so a second SyntH on
    the same machine (another checkout, another install) is never touched.
    """
    if not command_line:
        return False
    normalised = command_line.replace("\\", "/").lower()
    root = str(REPO_ROOT).replace("\\", "/").lower().rstrip("/")
    return root in normalised and "main.py" in normalised


def _windows_processes() -> list[tuple[int, str]]:
    """Every process as ``(pid, command line)``, via PowerShell.

    PowerShell ships with Windows and the tray already depends on it, so this needs no
    extra library. Run windowless: this may be called from pythonw.
    """
    powershell = shutil.which("powershell") or shutil.which("powershell.exe")
    if not powershell:
        return []
    script = (
        "Get-CimInstance Win32_Process | "
        'ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }'
    )
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "timeout": 30,
    }
    if _IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    completed = subprocess.run(  # noqa: S603
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script], **kwargs
    )
    pairs: list[tuple[int, str]] = []
    for line in completed.stdout.splitlines():
        pid_text, _, command_line = line.partition("\t")
        try:
            pairs.append((int(pid_text.strip()), command_line.strip()))
        except ValueError:
            continue
    return pairs


def _proc_processes() -> list[tuple[int, str]]:
    """Every process as ``(pid, command line)``, from ``/proc`` (Linux)."""
    pairs: list[tuple[int, str]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
        except Exception:
            continue
        pairs.append((int(entry.name), raw.replace("\x00", " ").strip()))
    return pairs


def _ps_processes() -> list[tuple[int, str]]:
    """Every process as ``(pid, command line)``, from ``ps`` (other POSIX)."""
    completed = subprocess.run(  # noqa: S603
        ["ps", "-eo", "pid=,args="],
        capture_output=True,
        text=True,
        timeout=30,
    )
    pairs: list[tuple[int, str]] = []
    for line in completed.stdout.splitlines():
        pid_text, _, command_line = line.strip().partition(" ")
        try:
            pairs.append((int(pid_text), command_line.strip()))
        except ValueError:
            continue
    return pairs


def app_pids() -> list[int]:
    """Every pid that is this install's application, ignoring the pid file.

    The pid file is a convenience, not the truth: it goes missing, it goes stale, and
    Windows recycles process ids. Asking what is actually running is what makes
    "Shut down" work even when the file is wrong.
    """
    try:
        if _IS_WINDOWS:
            pairs = _windows_processes()
        elif Path("/proc").is_dir():
            pairs = _proc_processes()
        else:
            pairs = _ps_processes()
    except Exception as exc:
        _launch_log(f"stop: could not list processes ({exc!r})")
        return []
    mine = os.getpid()
    return [pid for pid, line in pairs if pid != mine and owns_app(line)]


def read_pid() -> int | None:
    path = pid_path()
    if not path.is_file():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def write_pid(pid: int) -> None:
    path = pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid), encoding="utf-8")


def clear_pid() -> None:
    try:
        pid_path().unlink()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# start / stop / status
# ---------------------------------------------------------------------------


def build_command(interpreter: Path | str, *, console: bool) -> list[str]:
    """Return the command line that runs the application."""
    return [str(interpreter), str(REPO_ROOT / "main.py")]


def spawn_background(interpreter: Path | str, log_path: Path, *, console: bool) -> int:
    """Start the application detached and return its pid."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("a", encoding="utf-8", errors="replace")
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    kwargs: dict[str, Any] = {
        "cwd": str(REPO_ROOT),
        "stdout": handle,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "env": env,
    }
    if os.name == "nt":
        # CREATE_NO_WINDOW, never DETACHED_PROCESS: a console program given no console
        # at all starts, exits 0 and does nothing. pythonw.exe ignores the flag and
        # python.exe gets a hidden console it can run in, so this is safe for both.
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
        # Keep the log file handle alive for the child's lifetime by holding a
        # reference; the child owns the descriptor after Popen returns.
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(build_command(interpreter, console=console), **kwargs)  # noqa: S603
    write_pid(process.pid)
    return process.pid


def _launch_log(message: str) -> None:
    """Append a line to ``logs/synth_launch.log``.

    A native install runs the launcher with ``pythonw.exe``, so it has no console and
    no one to print to. Anything worth saying has to go to a file, which is how a tray
    that refuses to start becomes a line of evidence instead of a silent desktop.
    """
    try:
        directory = REPO_ROOT / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(directory / "synth_launch.log", "a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except Exception:
        pass


def tray_command(env_file: Path) -> list[str] | None:
    """Return the command that shows the notification-area icon, if it can run.

    Windows only: elsewhere SyntH is a service or a foreground process, and there is
    no notification area to put an icon in. It runs on the system PowerShell so the
    tray needs no dependency of its own. Reasons for declining are logged, because
    "no icon" and "no icon because PowerShell is missing" look identical otherwise.
    """
    if os.name != "nt":
        _launch_log("tray: not Windows, so there is no notification area")
        return None
    script = REPO_ROOT / "scripts" / "synth_tray.ps1"
    if not script.is_file():
        _launch_log(f"tray: {script} is missing")
        return None
    powershell = shutil.which("powershell") or shutil.which("powershell.exe")
    if not powershell:
        _launch_log("tray: powershell.exe is not on PATH")
        return None
    return [
        powershell,
        "-NoProfile",
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-AppRoot",
        str(REPO_ROOT),
        "-EnvFile",
        str(env_file),
        "-LogFile",
        str(REPO_ROOT / "logs" / "tray.log"),
    ]


def spawn_tray(env_file: Path) -> bool:
    """Show the tray icon, reporting whether it started.

    Called before waiting for the WebUI, so the icon and its "starting" balloon are
    already on screen while SyntH boots. On a native install the launcher's own
    window closes immediately, and without this the machine looks like it did
    nothing at all. Best-effort: a machine without PowerShell still starts SyntH.
    """
    command = tray_command(env_file)
    if not command:
        return False
    # The tray's own output goes to a file rather than nowhere. It was DISCARDED once,
    # and a tray that never appeared cost a round trip to the user's machine because
    # nothing anywhere had recorded why.
    sink = None
    try:
        logs = REPO_ROOT / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        sink = open(logs / "tray.out.log", "ab")
    except Exception as exc:
        _launch_log(f"tray: cannot open logs/tray.out.log ({exc!r}); output discarded")
    kwargs: dict[str, Any] = {
        "cwd": str(REPO_ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": sink if sink else subprocess.DEVNULL,
        "stderr": subprocess.STDOUT if sink else subprocess.DEVNULL,
    }
    if os.name == "nt":
        # NOT DETACHED_PROCESS. A console program given no console at all starts and
        # exits immediately without running: measured here, powershell.exe spawned with
        # DETACHED_PROCESS wrote nothing and returned 0, while CREATE_NO_WINDOW did the
        # work and still showed no window. That silent no-op is exactly why the tray
        # icon never appeared. CREATE_NO_WINDOW gives it a hidden console it can use.
        kwargs["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **kwargs)  # noqa: S603
    except Exception as exc:
        _launch_log(f"tray: could not start it: {exc!r}")
        if sink:
            sink.close()
        return False
    if sink:
        # The child holds its own handle now.
        sink.close()
    _launch_log(f"tray: started pid={process.pid}")
    return True


def wants_tray(args: argparse.Namespace) -> bool:
    """Whether this launch should show the tray icon.

    On by default for a desktop launch on Windows, because that is the launch the
    installer creates; a foreground run has its console instead, and ``--no-tray``
    turns it off for anything scripting the launcher.
    """
    if args.no_tray:
        return False
    if args.tray:
        return True
    return os.name == "nt" and not args.foreground


def stop(quiet: bool = False) -> int:
    """Stop the background instance, and say honestly whether it stopped.

    The pid file is a fast path, never the whole answer: it can be missing, stale, or
    name a pid Windows has recycled. The application's own processes are therefore
    consulted as well, scoped to this install, and the outcome is written to the
    launcher's log either way. A "Shut down" that silently did nothing is what this
    replaces.
    """
    recorded = read_pid()
    targets: list[int] = []
    if recorded and pid_alive(recorded):
        targets.append(recorded)
    for pid in app_pids():
        if pid not in targets:
            targets.append(pid)

    if not targets:
        clear_pid()
        _launch_log(f"stop: nothing was running (pid file said {recorded!r})")
        if not quiet:
            print("SyntH is not running.")
        return 0

    _launch_log(f"stop: stopping {targets} (pid file said {recorded!r})")
    failures: list[str] = []
    for pid in targets:
        asked, detail = terminate_pid(pid)
        if not asked:
            failures.append(f"{pid}: {detail}")

    for _ in range(30):
        if not any(pid_alive(pid) for pid in targets):
            break
        time.sleep(0.5)

    remaining = [pid for pid in targets if pid_alive(pid)]
    clear_pid()

    if remaining:
        _launch_log(
            f"stop: still running after the request: {remaining}"
            + (f"; failures: {failures}" if failures else "")
        )
        if not quiet:
            detail = f" ({'; '.join(failures)})" if failures else ""
            print(f"could not stop {remaining}{detail}")
        return 1

    _launch_log(f"stop: stopped {targets}")
    if not quiet:
        print("SyntH stopped.")
    return 0


def wait_for_webui(env_file: Path, *, timeout: float, quiet: bool) -> tuple[bool, str]:
    """Poll until the WebUI answers; return ``(ok, url)``."""
    deadline = time.time() + timeout
    last_url = ""
    while time.time() < deadline:
        report = check(env_file)
        last_url = str(report.get("url") or "")
        if report.get("ok"):
            return True, last_url
        if not quiet:
            print(f"      starting... ({int(deadline - time.time())}s left)")
        time.sleep(2)
    return False, last_url


def _open_browser(url: str) -> None:
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:
        pass


#: Where the first-run page lives. The WebUI serves it, and it is a plain page
#: over the existing config and endpoint APIs, so it needs nothing extra.
SETUP_PATH = "/setup"


def setup_url(base: str) -> str:
    """The setup page on *base*, tolerating a missing or trailing-slash URL."""
    base = (base or "").rstrip("/")
    if not base:
        return SETUP_PATH
    return base + SETUP_PATH


def _tail(path: Path, lines: int = 15) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(content[-lines:])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start, stop or inspect SyntH.")
    parser.add_argument(
        "--foreground", action="store_true", help="run in this terminal"
    )
    parser.add_argument(
        "--stop", action="store_true", help="stop the background instance"
    )
    parser.add_argument(
        "--status", action="store_true", help="report whether SyntH is up"
    )
    parser.add_argument("--console", action="store_true", help="allow a console window")
    parser.add_argument(
        "--no-browser", action="store_true", help="do not open the WebUI"
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="open the setup page instead of the WebUI (starts SyntH if needed)",
    )
    parser.add_argument(
        "--tray",
        action="store_true",
        help="show the notification-area icon (default on for a Windows desktop launch)",
    )
    parser.add_argument(
        "--no-tray",
        action="store_true",
        help="do not show the notification-area icon",
    )
    parser.add_argument(
        "--timeout", type=float, default=240.0, help="startup wait in seconds"
    )
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    args = parser.parse_args(argv)

    env_file = Path(args.env_file).expanduser()

    if args.status:
        report = check(env_file)
        if report.get("ok"):
            print(f"SyntH is running: {report.get('url')}")
            return 0
        # Silence is how a status command looks broken. A user who is told nothing
        # cannot tell "not running" from "the command did not work".
        print("SyntH is not running.")
        return 1

    if args.stop:
        return stop()

    # Already running? Then this invocation is just "open my Synth".
    report = check(env_file)
    if report.get("ok"):
        url = str(report.get("url") or "")
        if args.setup:
            url = setup_url(url)
        print(f"SyntH is already running: {url}")
        # Also a launch, from the user's point of view: if the icon is missing
        # (a fresh session, or the icon was hidden) it comes back, and the tray's
        # own single-instance guard keeps a second one from appearing.
        if wants_tray(args):
            spawn_tray(env_file)
        if args.setup or not args.no_browser:
            _open_browser(url)
        return 0

    interpreter = (
        sys.executable if args.foreground else venv_python(windowed=not args.console)
    )
    if not interpreter:
        interpreter = venv_python(windowed=False)
    if not interpreter:
        print(
            "SyntH is not installed yet: no virtual environment found.\n"
            "Run the installer, or: uv sync",
            file=sys.stderr,
        )
        return 2

    if args.foreground:
        os.execv(str(interpreter), build_command(interpreter, console=True))
        return 0  # pragma: no cover - execv does not return

    log_path = log_dir() / "synth.log"
    print("Starting SyntH...")
    print(f"  interpreter: {interpreter}")
    print(f"  log:         {log_path}")
    pid = spawn_background(interpreter, log_path, console=args.console)
    print(f"  pid:         {pid}")

    # Before the wait, not after: this is what tells the user something is happening
    # while the WebUI is still coming up.
    if wants_tray(args) and spawn_tray(env_file):
        print("  tray icon:   shown")

    ok, url = wait_for_webui(env_file, timeout=args.timeout, quiet=False)
    if ok:
        if args.setup:
            url = setup_url(url)
        print(f"\nSyntH is running: {url}")
        if args.setup or not args.no_browser:
            _open_browser(url)
        return 0

    print("\nSyntH did not come up in time.", file=sys.stderr)
    tail = _tail(log_path)
    if tail:
        print("\nLast log lines:\n" + tail, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
