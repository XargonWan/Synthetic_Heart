"""The tray icon: it has to run, and it has to be able to say why it did not.

Two failures cost a round trip to a real machine, and both were silence:

* ``DETACHED_PROCESS`` was used to start PowerShell. A console program given no
  console at all starts, exits with 0 and does nothing, so the icon never appeared
  and nothing anywhere recorded it.
* the script crashed a second later on ``$matches`` being overwritten by a later
  ``-notmatch`` — an error that would have been invisible without its log.

These tests pin the launch flags, the fact that the script's output is kept, and that
the script really does reach its message loop when run the way the launcher runs it.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "scripts" / "start_synth.py"
TRAY_SCRIPT = REPO_ROOT / "scripts" / "synth_tray.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("powershell.exe")

IS_WINDOWS = os.name == "nt"


def _strip_comments(text: str) -> str:
    """The tray's code without its comments (prose reads like code to a regex)."""
    code = re.sub(r"<#.*?#>", "", text, flags=re.DOTALL)
    return "\n".join(
        line.split(" #", 1)[0]
        for line in code.splitlines()
        if not line.lstrip().startswith("#")
    )


def _process_exists(pid: int) -> bool:
    """Ask Windows whether a pid is alive, rather than believing our own child."""
    completed = subprocess.run(  # noqa: S603
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return str(pid) in completed.stdout


def _read_log(path: Path) -> str:
    """Read a log the tray may be appending to at this instant."""
    for _ in range(40):
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except (PermissionError, FileNotFoundError):
            time.sleep(0.05)
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _load_launcher():
    spec = importlib.util.spec_from_file_location("start_synth_tray_test", LAUNCHER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["start_synth_tray_test"] = module
    spec.loader.exec_module(module)
    return module


def _spawn_tray_source() -> str:
    """The body of spawn_tray, which is where the launch flags live."""
    source = LAUNCHER.read_text(encoding="utf-8")
    start = source.index("def spawn_tray(")
    end = source.index("def wants_tray(", start)
    return source[start:end]


def test_the_tray_is_not_started_with_detached_process() -> None:
    """DETACHED_PROCESS makes a console program a no-op, which is how the icon vanished.

    Measured on Windows: ``powershell -Command '... | Set-Content marker'`` writes the
    marker with no flags and with CREATE_NO_WINDOW, and writes nothing at all with
    DETACHED_PROCESS. The tray is a PowerShell script, so using it there means the icon
    silently never appears.
    """
    body = _spawn_tray_source()
    # Comments are excluded on purpose: the code explains why it does not use it.
    code = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )
    assert "DETACHED_PROCESS" not in code, (
        "spawn_tray must not start PowerShell detached: it exits without running"
    )
    assert "CREATE_NO_WINDOW" in code


def test_the_trays_output_is_kept_rather_than_discarded() -> None:
    """When an icon does not appear, its own output is the only witness.

    It was sent to DEVNULL once, and a silent tray then needed a trip to the user's
    machine to explain. Both the launcher's decision log and the script's output have
    to land somewhere readable.
    """
    body = _spawn_tray_source()
    assert "tray.out.log" in body
    assert "_launch_log(" in body
    # stdin may be closed, but stdout and stderr must not go to nowhere.
    assert 'stdout": subprocess.DEVNULL' not in body
    assert 'stderr": subprocess.DEVNULL' not in body


def test_the_tray_is_given_a_log_file() -> None:
    """The script is told where to write, so its log sits with the application's."""
    launcher_source = LAUNCHER.read_text(encoding="utf-8")
    assert '"-LogFile"' in launcher_source
    assert '"tray.log"' in launcher_source
    tray_source = TRAY_SCRIPT.read_text(encoding="utf-8")
    assert "Add-Content -LiteralPath $LogFile" in tray_source
    # The whole point: an error inside the script is recorded, not swallowed.
    assert "FATAL" in tray_source


def test_a_refused_tray_records_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No icon is a decision with a reason, and the reason is written down.

    Here the reason is a missing script: the root is pointed at an empty directory, so
    the launcher has to decline. Whatever the reason is, it has to reach the log.
    """
    module = _load_launcher()
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    assert module.tray_command(tmp_path / ".env") is None
    launch_log = tmp_path / "logs" / "synth_launch.log"
    assert launch_log.is_file(), "a refused tray left no trace"
    text = launch_log.read_text(encoding="utf-8")
    assert "tray:" in text
    if IS_WINDOWS:
        assert "is missing" in text


@pytest.mark.skipif(
    not (IS_WINDOWS and POWERSHELL),
    reason="needs a Windows desktop with PowerShell",
)
def test_the_tray_script_is_syntactically_valid() -> None:
    """A parse error in the tray is an icon that never appears, with no other symptom."""
    if not TRAY_SCRIPT.is_file() or not POWERSHELL:
        pytest.skip("no tray script or no PowerShell in this checkout")
    parser = (
        "$errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{TRAY_SCRIPT}', [ref]$null, [ref]$errors) | Out-Null; "
        "if ($errors) { $errors | ForEach-Object { $_.Message }; exit 1 } else { 'ok' }"
    )
    completed = subprocess.run(  # noqa: S603
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", parser],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.skipif(
    not (IS_WINDOWS and POWERSHELL),
    reason="needs a Windows desktop with PowerShell",
)
def test_every_helper_the_tray_calls_is_defined_in_it() -> None:
    """A missing helper is a menu entry that fails only when someone clicks it.

    The Shut down and Restart entries called ``Update-State`` for a while without it
    existing anywhere: PowerShell reports that at click time, in a window that is not
    open, which is exactly the kind of silence this file exists to prevent. PowerShell
    itself is the judge of which commands are its own.
    """
    if not TRAY_SCRIPT.is_file() or not POWERSHELL:
        pytest.skip("no tray script or no PowerShell in this checkout")
    body = TRAY_SCRIPT.read_text(encoding="utf-8")
    # Comments are prose, and prose reads like a command name: the help block's
    # "Notification-area (tray) icon" is not a call to anything.
    code = _strip_comments(body)
    defined = set(re.findall(r"function\s+([A-Za-z][\w-]*)", code))
    # Command position only: a statement start, straight after an opening brace, or a
    # pipeline stage.
    candidates = set(
        re.findall(r"(?:^\s*|\{\s*)([A-Z][A-Za-z]*-[A-Za-z][\w]*)", code, re.MULTILINE)
    ) | set(re.findall(r"\|\s*([A-Z][A-Za-z]*-[A-Za-z][\w]*)", code))
    unknown = sorted(candidates - defined)
    if not unknown:
        return
    probe = "; ".join(
        f"if (Get-Command -Name '{name}' -ErrorAction SilentlyContinue) {{ '{name}' }}"
        for name in unknown
    )
    known = subprocess.run(  # noqa: S603
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", probe],
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout
    builtin = {name for name in unknown if name in known}
    missing = sorted(set(unknown) - builtin)
    assert not missing, (
        f"the tray calls {missing}, which it does not define and PowerShell does not "
        "provide"
    )


@pytest.mark.skipif(
    not (IS_WINDOWS and POWERSHELL),
    reason="needs a Windows desktop with PowerShell",
)
def test_the_tray_script_reaches_its_message_loop(tmp_path: Path) -> None:
    """Run the real script the way scripts/start_synth.py runs it.

    This is the test that would have caught both silent failures: a crash shows up as
    FATAL in the log, and a process that never ran shows up as no log at all.
    """
    if not TRAY_SCRIPT.is_file():
        pytest.skip("synth_tray.ps1 is not in this checkout")
    icon = REPO_ROOT / "installer" / "synth-tray.ico"
    if not icon.is_file():
        pytest.skip("the tray icon is not in this checkout")

    # Its own AppRoot, so this cannot collide with an icon that is really running.
    (tmp_path / "installer").mkdir(exist_ok=True)
    shutil.copy(icon, tmp_path / "installer" / "synth-tray.ico")
    log = tmp_path / "logs" / "tray.log"

    def read_log() -> str:
        """Read the log, tolerating the instant the tray has it open to append.

        Windows can refuse the read while the writer holds it, which is a collision
        between watching and writing, not a tray that failed.
        """
        for _ in range(20):
            try:
                return log.read_text(encoding="utf-8", errors="replace")
            except PermissionError:
                time.sleep(0.05)
        return log.read_text(encoding="utf-8", errors="replace")

    command = [
        POWERSHELL,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(TRAY_SCRIPT),
        "-AppRoot",
        str(tmp_path),
        "-EnvFile",
        str(tmp_path / "missing.env"),
        "-LogFile",
        str(log),
        # No balloon: this runs during the test suite, on the tester's desktop.
        "-NoBalloon",
    ]
    process = subprocess.Popen(  # noqa: S603
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        text = ""
        deadline = time.time() + 60.0
        while time.time() < deadline:
            if log.is_file():
                text = read_log()
                if "entering the message loop" in text or "FATAL" in text:
                    break
            time.sleep(0.25)

        assert "FATAL" not in text, f"the tray failed:\n{text}"
        assert "notification icon created and made visible" in text, (
            f"the tray never made its icon visible:\n{text}"
        )
        assert "entering the message loop" in text, (
            f"the tray never reached its message loop:\n{text}"
        )
        assert process.poll() is None, "the tray exited instead of staying up"
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=15)


def _click_handler_bodies() -> list[str]:
    """The body of every ``Add_Click({ ... })`` in the tray, by brace matching."""
    code = _strip_comments(TRAY_SCRIPT.read_text(encoding="utf-8"))
    bodies = []
    for match in re.finditer(r"Add_Click\(\{", code):
        depth = 1
        index = match.end()
        start = index
        while index < len(code) and depth:
            if code[index] == "{":
                depth += 1
            elif code[index] == "}":
                depth -= 1
            index += 1
        bodies.append(code[start : index - 1])
    return bodies


@pytest.mark.skipif(
    not (IS_WINDOWS and POWERSHELL),
    reason="needs a Windows desktop with PowerShell",
)
def test_no_menu_entry_waits_for_the_launcher() -> None:
    """A click must not hold the tray's single thread, or the menu goes dead.

    The tray is one thread running a WinForms message loop, and the handlers run on it.
    ``Start-Process -Wait`` in the Shut down entry therefore froze the icon and its menu
    for as long as the application took to die: the menu opened, and nothing in it did
    anything. The launcher is called and reported on by the state poll instead.
    """
    if not TRAY_SCRIPT.is_file():
        pytest.skip("synth_tray.ps1 is not in this checkout")
    bodies = _click_handler_bodies()
    assert len(bodies) >= 4, f"expected the tray's menu handlers, found {len(bodies)}"
    for body in bodies:
        assert "-Wait" not in body, f"a menu entry waits for the launcher:\n{body}"
        assert "Start-Sleep" not in body, (
            f"a menu entry sleeps in the message loop:\n{body}"
        )


@pytest.mark.skipif(
    not (IS_WINDOWS and POWERSHELL),
    reason="needs a Windows desktop with PowerShell",
)
def test_the_icon_goes_away_when_the_application_does(tmp_path: Path) -> None:
    """The icon's lifetime is the application's: nothing else removes a stale one.

    Reported from a real install: after Shut down the icon stayed, its menu kept opening,
    and every entry in it was about an application that was no longer running. This runs
    the real script against a stand-in WebUI, takes the stand-in away, and requires the
    tray to notice, say so, and end by itself.
    """
    if not TRAY_SCRIPT.is_file():
        pytest.skip("synth_tray.ps1 is not in this checkout")

    # A stand-in WebUI: every request gets a 200. That is all the tray asks of it.
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = int(listener.getsockname()[1])

    def serve() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return  # the socket was closed: that is the shutdown
            with conn:
                try:
                    conn.recv(4096)
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
                    )
                except OSError:
                    pass

    threading.Thread(target=serve, daemon=True).start()

    (tmp_path / "logs").mkdir(exist_ok=True)
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"SYNTH_WEBUI_HOST=127.0.0.1\nSYNTH_WEBUI_HTTP_PORT={port}\nSYNTH_WEBUI_TLS=0\n",
        encoding="utf-8",
    )
    log = tmp_path / "logs" / "tray.log"

    process = subprocess.Popen(  # noqa: S603
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(TRAY_SCRIPT),
            "-AppRoot",
            str(tmp_path),
            "-EnvFile",
            str(env_file),
            "-LogFile",
            str(log),
            "-NoBalloon",
            "-PollSeconds",
            "1",
            "-StartupGraceSeconds",
            "30",
            "-StopGraceSeconds",
            "3",
        ],
        # No stdio redirection: the tray keeps its own account in tray.log, and the
        # subprocess stubs do not accept DEVNULL in this call shape.
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        deadline = time.time() + 40.0
        while time.time() < deadline and "state: running" not in _read_log(log):
            time.sleep(0.25)
        text = _read_log(log)
        assert "state: running" in text, (
            f"the tray never saw the WebUI answering:\n{text}"
        )

        # This is what a Shut down looks like from outside: the port stops answering.
        listener.close()

        deadline = time.time() + 30.0
        while time.time() < deadline and _process_exists(process.pid):
            time.sleep(0.25)
        text = _read_log(log)
        assert "removing the icon" in text, f"the tray kept a stale icon:\n{text}"
        assert not _process_exists(process.pid), (
            f"the tray outlived the application:\n{text}"
        )
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=15)
        try:
            listener.close()
        except Exception:  # noqa: BLE001 - cleanup must never mask the test result
            pass
