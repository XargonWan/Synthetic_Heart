"""The installer build script's compiler lookup.

`Find-Iscc` returning the first character of the path instead of the path is not a
theoretical failure: it is what shipped, and it is why the Windows installer job on CI
died with "The term 'C' is not recognized" while the script cheerfully printed
"compiler: C". PowerShell unwraps a single pipeline result out of its array, and indexing
a string gives a character. These tests pin the lookup itself, with the candidate
locations pointed at temporary directories so they do not depend on what is installed on
the machine running them.

Two of the three locations can be pointed elsewhere and one cannot: Windows re-derives
`ProgramFiles` for every new process, so a child handed a fake one still reports the real
path (verified for python, cmd and powershell children alike). `ProgramFiles(x86)` and
`LOCALAPPDATA` are ordinary named variables and do take the value they are given, which is
enough, because the failure was positional: exercising one candidate at a time finds it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "installer" / "build_installer.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
REAL_INNO_ELSEWHERE = (
    Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    / "Inno Setup 6"
    / "ISCC.exe"
)

pytestmark = pytest.mark.skipif(
    POWERSHELL is None,
    reason="build_installer.ps1 is PowerShell, and there is no PowerShell here",
)


def _find_iscc_function() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"^function Find-Iscc \{.*?^\}", text, re.MULTILINE | re.DOTALL)
    assert match, "installer/build_installer.ps1 no longer defines Find-Iscc"
    return match.group(0)


def _env_with(overrides: dict[str, str]) -> dict[str, str]:
    """An environment where each override replaces its existing name regardless of case.

    Windows environment names are case-insensitive but a Python dict is not, so adding
    "LOCALAPPDATA" next to an existing "LocalAppData" would leave two entries in the env
    block and Windows could read either.
    """
    environment = dict(os.environ)
    for key, value in overrides.items():
        for existing in [k for k in environment if k.lower() == key.lower()]:
            del environment[existing]
        environment[key] = value
    return environment


def _run_find_iscc(tmp_path: Path, *, locations: list[str]) -> str:
    """Run the real Find-Iscc with the fakeable roots pointed at temporary directories.

    `locations` names which locations hold an Inno Setup, as the script sees them: "x86"
    or "localappdata". "programs" is deliberately not among them, see the module docstring.
    """
    roots = {
        "x86": (tmp_path / "pf86", Path("Inno Setup 6") / "ISCC.exe"),
        "localappdata": (
            tmp_path / "la",
            Path("Programs") / "Inno Setup 6" / "ISCC.exe",
        ),
    }
    for name, (root, relative) in roots.items():
        root.mkdir(parents=True, exist_ok=True)
        if name in locations:
            iscc = root / relative
            iscc.parent.mkdir(parents=True, exist_ok=True)
            iscc.write_text("stub")

    script = tmp_path / "find.ps1"
    script.write_text(
        f"{_find_iscc_function()}\nWrite-Output (Find-Iscc)\n", encoding="utf-8"
    )
    environment = {
        "ProgramFiles(x86)": str(roots["x86"][0]),
        "LOCALAPPDATA": str(roots["localappdata"][0]),
    }
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=_env_with(environment),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def test_one_installed_inno_setup_yields_its_full_path(tmp_path: Path) -> None:
    """The ordinary case, and the one that broke: a machine has one Inno Setup.

    Returning "C" here means Inno Setup is never run at all, so the build always fails.
    """
    found = _run_find_iscc(tmp_path, locations=["x86"])
    assert found == str(tmp_path / "pf86" / "Inno Setup 6" / "ISCC.exe"), (
        f"Find-Iscc returned {found!r} for a single installed Inno Setup, which is not a usable path"
    )


def test_a_single_match_in_the_last_location_is_also_a_full_path(
    tmp_path: Path,
) -> None:
    """The unwrapping happens for whichever location matched, not only the first one."""
    found = _run_find_iscc(tmp_path, locations=["localappdata"])
    assert found == str(tmp_path / "la" / "Programs" / "Inno Setup 6" / "ISCC.exe")


def test_the_first_location_wins_when_several_exist(tmp_path: Path) -> None:
    found = _run_find_iscc(tmp_path, locations=["x86", "localappdata"])
    assert found == str(tmp_path / "pf86" / "Inno Setup 6" / "ISCC.exe")


@pytest.mark.skipif(
    REAL_INNO_ELSEWHERE.exists(),
    reason="this machine really does have a second Inno Setup, which Find-Iscc is meant to find",
)
def test_a_machine_without_inno_setup_says_so(tmp_path: Path) -> None:
    """Not a path and not a crash: the caller prints its install instructions."""
    assert _run_find_iscc(tmp_path, locations=[]) == ""


def _run_resolve_version(tmp_path: Path, repo: Path) -> str:
    """Resolve-Version on its own, run against a repository of our own making."""
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("function Resolve-Version")
    body = text[start : text.index("\n}", start) + 2]
    probe = tmp_path / "resolve.ps1"
    probe.write_text(
        f"$repoRoot = '{repo}'\n{body}\nWrite-Output (Resolve-Version)\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def _tagged_repo(tmp_path: Path, *tags: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=120)
    for tag in tags:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=test",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "x",
            ],
            check=True,
            timeout=120,
        )
        subprocess.run(["git", "-C", str(repo), "tag", tag], check=True, timeout=120)
    return repo


def test_a_tag_that_is_not_a_version_is_not_used_as_one(tmp_path: Path) -> None:
    """This repository's newest tag is "legacy", and it was passed to ISCC as the version.

    ISCC answered "Value of [Setup] section directive VersionInfoVersion is invalid",
    which names the field and not the cause: the build died pointing at a line of the .iss
    that was perfectly correct. A tag is only a version when it looks like one.
    """
    repo = _tagged_repo(tmp_path, "legacy")
    assert _run_resolve_version(tmp_path, repo) == "0.0.0-dev", (
        "a tag that is not a version was used as one"
    )


def test_a_version_tag_is_used_as_the_version(tmp_path: Path) -> None:
    """And the ordinary case still works, with or without the leading v."""
    assert _run_resolve_version(tmp_path, _tagged_repo(tmp_path, "v1.2.3")) == "1.2.3"


def _run_version_info_version(tmp_path: Path, app_version: str) -> str:
    """Resolve-VersionInfoVersion on its own, for one AppVersion."""
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("function Resolve-VersionInfoVersion")
    body = text[start : text.index("\n}", start) + 2]
    probe = tmp_path / "versioninfo.ps1"
    probe.write_text(
        f"{body}\nWrite-Output (Resolve-VersionInfoVersion -AppVersion '{app_version}')\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def test_a_letter_suffix_keeps_the_windows_version_numeric(tmp_path: Path) -> None:
    """`v1.0.0a` is a build of the 1.0.0 line: the asset is named 1.0.0a, the property says 1.0.0.

    Windows version resources are numeric x.y.z, and the .iss passed the string through
    unchanged when it carried no hyphen, so the tag that names this release could not build.
    """
    assert _run_version_info_version(tmp_path, "1.0.0a") == "1.0.0", (
        "a letter suffix reached VersionInfoVersion unchanged, which ISCC refuses"
    )


def test_a_gitversion_prerelease_is_still_trimmed_at_the_hyphen(tmp_path: Path) -> None:
    """The rule the .iss used to own, now in one place and still applied to pre-releases."""
    assert _run_version_info_version(tmp_path, "1.2.3-feat.4") == "1.2.3"


def test_an_ordinary_version_is_passed_through(tmp_path: Path) -> None:
    assert _run_version_info_version(tmp_path, "1.2.3") == "1.2.3"


def test_something_that_is_not_a_version_falls_back_to_zero(tmp_path: Path) -> None:
    """Resolve-Version already refuses a tag like "legacy"; this is the last line of defence."""
    assert _run_version_info_version(tmp_path, "legacy") == "0.0.0"


def test_the_script_actually_passes_the_numeric_define() -> None:
    """The .iss falls back to the hyphen rule, so an unpassed define is a silent partial fix."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert '"/DAppVersionNumeric=$versionInfoVersion"' in text, (
        "build_installer.ps1 derives the numeric version and then does not pass it to ISCC"
    )


def test_the_iss_accepts_the_numeric_define_and_keeps_the_fallback() -> None:
    """Both halves of the pairing: the override, and the rule it replaces."""
    iss = (
        Path(__file__).resolve().parents[1] / "installer" / "synth-installer.iss"
    ).read_text(encoding="utf-8")
    assert "#ifndef AppVersionNumeric" in iss
    assert "VersionInfoVersion={#AppVersionNumeric}" in iss
