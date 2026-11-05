"""Invariants for the Windows prerequisite script.

These are text-level checks, not a substitute for running the script on Windows
(which the release workflow does). They exist because the script's failure modes
are about *what it reports*, and a silent failure is the one thing a hidden
installer cannot afford: a real run died with exit code 1 and a log that stopped
mid-sentence, and there was nothing on disk to say why.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PREREQS = REPO_ROOT / "scripts" / "install_prereqs.ps1"


def script_text() -> str:
    # utf-8-sig: the file carries a BOM, as PowerShell scripts written on Windows do.
    return PREREQS.read_text(encoding="utf-8-sig")


def test_unexpected_failures_are_logged_rather_than_swallowed() -> None:
    """`$ErrorActionPreference = 'Stop'` plus no trap is a silent exit code 1.

    That is exactly what a live run produced: the installer reported exit code 1
    and the log ended at the last progress line, because an exception terminated
    the script before any handler could record it.
    """
    text = script_text()
    assert "$ErrorActionPreference = 'Stop'" in text, "the premise of this test changed"

    trap = re.search(r"^trap \{.*?^\}", text, re.DOTALL | re.MULTILINE)
    assert trap, "an unhandled exception must not be allowed to end the script silently"
    assert "Write-Log" in trap.group(0), "the trap must record the reason in the log"
    assert "exit 1" in trap.group(0)


def test_the_cached_archive_is_checked_before_it_is_unpacked() -> None:
    """A download killed part-way leaves a truncated zip that only fails later."""
    text = script_text()
    check = text.index("Test-UsableArchive $zipPath")
    unpack = text.index("Expand-Zip $zipPath $staging")
    assert check < unpack, "the cached archive must be validated before unpacking it"
    assert "Remove-Item $zipPath" in text, (
        "an unusable archive must be discarded, not reused"
    )


def test_the_slow_steps_say_what_they_are_doing() -> None:
    """Unpacking a 320 MB archive is slow, and silence reads as a hang."""
    text = script_text()
    assert "unpacking PostgreSQL into a temporary folder" in text
    assert "unpacked in" in text
    assert "installing into" in text
    assert "installed in" in text


def test_the_archive_extractor_is_not_expand_archive() -> None:
    """PS 5.1's Expand-Archive pushes every entry through PowerShell objects.

    On a 300 MB archive that is minutes of work and a lot of memory; the .NET
    extractor streams to disk and reports a real error when the file is bad.
    """
    text = script_text()
    assert "Expand-Archive -" not in text, "use Expand-Zip (the .NET extractor) instead"
    assert "System.IO.Compression.ZipFile" in text


def test_the_log_path_is_one_the_installer_can_name() -> None:
    """The installer prints the log path in its error dialog, so it must be TEMP."""
    text = script_text()
    assert "Join-Path $env:TEMP 'synth_prereqs.log'" in text


def _pgvector_function(text: str) -> str:
    match = re.search(
        r"function Ensure-Pgvector \{.*?^\}", text, re.DOTALL | re.MULTILINE
    )
    assert match, "Ensure-Pgvector is gone; nothing deploys the extension any more"
    return match.group(0)


def test_pgvector_is_deployed_where_postgres_will_look_for_it() -> None:
    """`CREATE EXTENSION vector` resolves against $libdir and share/extension.

    A file in the wrong directory is indistinguishable from a missing one, and the
    install would quietly fall back to in-memory SOUL memory instead of failing.
    """
    fn = _pgvector_function(script_text())
    assert "Join-Path $PgRoot 'lib'" in fn, "vector.dll must land in lib/"
    assert "share\\extension" in fn, (
        "the control and sql files must land in share/extension"
    )


def test_a_missing_pgvector_build_only_warns() -> None:
    """A source checkout has no vendored build and must still produce an installer."""
    fn = _pgvector_function(script_text())
    assert "pgvector is not bundled" in fn, "the user has to be told what is missing"
    assert "return $false" in fn, "and the install must continue without it"


def test_the_installer_ships_the_vendored_build_when_it_exists() -> None:
    """Packing the files is what turns a CI build into an installed extension."""
    iss = (REPO_ROOT / "installer" / "synth-installer.iss").read_text(encoding="utf-8")
    assert 'Source: "vendor\\pgvector\\*"' in iss, "the vendored build is not packed"
    assert "skipifsourcedoesntexist" in iss, (
        "a source checkout has no vendored build; the compile must still succeed"
    )
    assert "{app}\\installer\\vendor\\pgvector" in iss, (
        "the prereqs script looks for the files under the install root"
    )


def test_a_vendored_pgvector_build_is_complete() -> None:
    """When a build is present it must be loadable, not merely present.

    CI enforces the same size floor on the DLL; this catches a half-copied
    directory before it reaches an installer.
    """
    vendor = REPO_ROOT / "installer" / "vendor" / "pgvector"
    builds = (
        sorted(p for p in vendor.glob("pg*") if p.is_dir()) if vendor.is_dir() else []
    )
    if not builds:
        pytest.skip("no vendored pgvector build in this checkout; CI produces it")
    for build in builds:
        dll = build / "vector.dll"
        assert dll.is_file(), f"{build.name} has no vector.dll"
        assert dll.stat().st_size > 100_000, (
            f"{build.name}/vector.dll is only {dll.stat().st_size} bytes; "
            "the build did not link properly"
        )
        assert (build / "vector.control").is_file(), (
            f"{build.name} has no vector.control"
        )
        assert list(build.glob("vector--*.sql")), f"{build.name} has no vector--*.sql"
