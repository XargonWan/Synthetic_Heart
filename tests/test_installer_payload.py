"""Guards on what the Windows installer is allowed to ship.

The exclude list in ``installer/synth-installer.iss`` is a hand-written string,
and getting it wrong is not a cosmetic bug: the repository root holds a real
``.env`` (gitignored, so it exists only on a developer's machine), ``data/``
holds the encrypted-endpoint secret, and the WebUI's TTS cache holds the
avatar's actual speech. All three were shipped by a rewrite of that list, which
is why these assertions exist.

These tests read the .iss as text. They do not compile it; the release workflow
does that, and the compiler's log is the authoritative payload record.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ISS = REPO_ROOT / "installer" / "synth-installer.iss"


@pytest.fixture(scope="module")
def iss_text() -> str:
    return ISS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def exclude_patterns(iss_text: str) -> set[str]:
    """The Excludes list of the application-tree entry, split into patterns."""
    match = re.search(r'Source: "\.\.\\\*";[^\n]*Excludes: "([^"]+)"', iss_text)
    assert match, "the application-tree [Files] entry is missing or has no Excludes"
    raw = match.group(1)
    # {#ExampleSkinsExclude} is an ISPP define; treat it as a wildcard fragment
    # so the split below stays meaningful.
    raw = re.sub(r"\{#[A-Za-z]+\}", "skins\\__define__\\*,", raw)
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


@pytest.mark.parametrize(
    "pattern",
    [
        ".env",
        ".venv*",
        ".git\\*",
        "data\\*",
        "logs\\*",
        "node_modules\\*",
        "mcp_servers\\*",
        "installer\\output\\*",
    ],
)
def test_private_and_generated_paths_are_excluded(
    exclude_patterns: set[str], pattern: str
) -> None:
    assert pattern.lower() in exclude_patterns, (
        f"{pattern} is not excluded: it is local state or generated content and "
        "must never be installed on someone else's machine"
    )


def test_the_env_template_still_ships(exclude_patterns: set[str]) -> None:
    """Excluding .env must not exclude .env.example, which users need."""
    assert ".env.example" not in exclude_patterns
    # A bare ".env*" would swallow the template as well.
    assert ".env*" not in exclude_patterns


def test_the_tts_cache_is_not_shipped(exclude_patterns: set[str]) -> None:
    """The WebUI's generated speech is private and was ~35 MB of the payload."""
    assert any("audio\\tts" in pattern for pattern in exclude_patterns)


def test_untracked_working_notes_are_not_shipped(exclude_patterns: set[str]) -> None:
    assert "one-click.md" in exclude_patterns
    assert "venice_no_response_report.md" in exclude_patterns


#: Inno Setup's own constants, plus the ones this installer is allowed to use.
#: An environment variable (``{%NAME}``) is always acceptable and is handled
#: separately, because it is a different mechanism with different syntax.
VALID_INNO_CONSTANTS = frozenset(
    {
        "app",
        "tmp",
        "sys",
        "sysnative",
        "win",
        "src",
        "sd",
        "localappdata",
        "userappdata",
        "userdocs",
        "userdesktop",
        "userprograms",
        "userstartmenu",
        "usercf",
        "userpf",
        "commonappdata",
        "commonprograms",
        "commondesktop",
        "commonpf",
        "commoncf",
        "autodesktop",
        "autoprograms",
        "group",
        "uninstallexe",
        "fonts",
        "dao",
    }
)


def test_only_real_inno_constants_are_used(iss_text: str) -> None:
    """``{userprofile}`` is not a constant, and only fails at runtime.

    ``ExpandConstant('{userprofile}')`` aborts the install with "unknown
    constant" *after* the files are copied and the prerequisites installed. The
    compile is perfectly clean, so nothing catches it before a user does. Any
    braced name that is not a documented constant must be an environment
    variable (``{%NAME}``), which this regex deliberately does not match.
    """
    used = set(re.findall(r"\{([a-z][a-z0-9_]*)\}", iss_text))
    unknown = sorted(name for name in used if name not in VALID_INNO_CONSTANTS)
    assert not unknown, (
        f"not Inno Setup constants: {unknown} - the user profile is "
        "{%USERPROFILE} and the temp folder is {%TEMP} or {tmp}"
    )


def test_no_inno_comment_swallows_itself(iss_text: str) -> None:
    """Inno's ``{ }`` comments do not nest, so an inner ``{`` ends one early.

    The rest of the line is then parsed as Pascal and the compiler reports a
    column number pointing at prose, which is a slow way to learn this. Writing
    the temp-folder constant inside a comment that explains the temp-folder
    constant is the natural thing to do, so it is worth a guard.
    """
    offenders = [
        (number, line.strip())
        for number, line in enumerate(iss_text.splitlines(), start=1)
        if line.lstrip().startswith("{") and line.count("{") > 1
    ]
    assert not offenders, f"a brace inside an Inno comment ends it early: {offenders}"


def test_the_finish_page_opens_the_setup_page(iss_text: str) -> None:
    """The [Run] entry's label promises the setup page, so it must ask for it.

    Without `--setup` the launcher opens the plain WebUI, and a new user meets an
    avatar scene with nothing telling them what to do next.
    """
    run_section = iss_text.split("[Run]", 1)[1].split("[UninstallRun]", 1)[0]
    assert "--setup" in run_section


def test_no_local_draft_at_the_root_can_be_shipped(iss_text: str) -> None:
    """Anything the repository does not track must be named in the exclude list.

    The tree is packed by a hand-written exclude list, and the repository root is
    where local drafts live. A 55 KB PR description was sitting there untracked and
    would have been packed into the installer and shipped.
    """
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "*.md"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    except Exception:
        pytest.skip("not a git checkout, so untracked files cannot be identified")
    tracked_root = {name for name in tracked if "/" not in name}

    for path in sorted(REPO_ROOT.glob("*.md")):
        if path.name in tracked_root:
            continue
        assert path.name in iss_text, (
            f"{path.name} exists only on this machine and is not in the installer's "
            "Excludes list, so it would be shipped: add it, or commit it if it is "
            "meant to be part of the distribution."
        )


def test_the_data_question_is_asked_by_the_uninstaller(iss_text: str) -> None:
    """The uninstaller asks; setup asks nothing about a hypothetical uninstall.

    Setup used to carry it as an unchecked task, which asked the user to decide about a
    future uninstall while they were still installing, and then stored the answer until
    it was needed. The question is asked when it is a decision about something that is
    actually happening. Keeping the data stays the default and is what a silent
    uninstall does, because keeping is the recoverable direction: deleting it is not.
    """
    tasks = iss_text.split("[Tasks]", 1)[1].split("\n[", 1)[0]
    assert "cleanslate" not in tasks, (
        "setup still asks about a future uninstall; that question belongs in the "
        "uninstaller, not in the install"
    )

    uninstall_delete = iss_text.split("[UninstallDelete]", 1)[1].split("\n[", 1)[0]
    for name in ("{app}\\data", "{app}\\.env"):
        assert f'Name: "{name}"' not in uninstall_delete, (
            f"{name} is an unconditional uninstall delete, so it would be removed "
            "without ever asking"
        )

    code = iss_text.split("[Code]", 1)[1]
    assert "function InitializeUninstall(): Boolean;" in code
    assert "procedure CurUninstallStepChanged(" in code
    # Keeping is the default, and the question is asked only when a caller asks for it.
    assert "DeleteDataOnUninstall := False;" in code
    assert "ExpandConstant('{param:SYNTHASK|0}') = '1'" in code
    # One window, on every path a user can actually take. Inno's confirmation cannot be
    # suppressed from script (there is no directive for it, and UninstallSilent is a
    # read-only function), so the paths a user clicks launch the uninstaller *silently*,
    # which is what makes Inno skip its own confirmation, and carry /SYNTHASK=1, which is
    # what makes this script's window appear in its place. Without that pairing the same
    # question arrives twice, which is the bug being fixed.
    assert "if not UninstallSilent() then" not in code, (
        "the window is gated on silent mode again, which would both hide it from the "
        "Add/Remove Programs path and show it to a scripted uninstall"
    )
    assert 'Parameters: "/SILENT /SYNTHASK=1"' in iss_text, (
        "the Start-menu uninstall entry must launch uninstaller silently, so Inno's own "
        "confirmation does not appear after ours"
    )
    assert "UninstallString" in code and "/SILENT /SYNTHASK=1" in code, (
        "Add/Remove Programs still runs the uninstaller interactively, so it asks twice"
    )
    assert "RegWriteStringValue(HKCU," in code, (
        "a per-user install (PrivilegesRequired=lowest) has its entry under HKCU"
    )
    # Built from {app} rather than {uninstallexe}. They are the same path - the constant
    # is {app}\unins000.exe - but the probe showed that what it resolves to follows {app},
    # so naming the install directory directly is what keeps the entry tied to where the
    # application actually is. {uninstallexe} is not used anywhere in this write.
    assert r"ExpandConstant('{app}') + '\unins000.exe" in code
    assert "ExpandConstant('{uninstallexe}')" not in code
    # The key is spelled out because a script cannot read AppId back, so the GUID must be
    # proven to stay in step: Inno hangs its uninstall entry off AppId, and a mismatch
    # would rewrite a key that does not exist, silently.
    appid = next(line for line in iss_text.splitlines() if line.startswith("AppId="))
    guid = appid.split("=", 1)[1].strip().strip("{}").strip()
    assert guid in code, (
        f"the rewritten uninstall key does not carry the AppId GUID ({guid}), so it "
        "would write to a key Inno never created"
    )
    # The question is one window, with a checkbox that starts clear. It used to be a
    # message box whose default button was Yes, so the way most people leave a dialog -
    # clicking through it - deleted the persona, the chats and the database. A clear
    # checkbox cannot do that, and the default answer is the recoverable one.
    assert "CreateCustomForm" in code, "the uninstaller must ask in its own window"
    assert "DataCheck.Checked := False;" in code
    assert "Form.ShowModal() <> mrOk" in code, "Cancel must leave everything installed"
    assert "MB_YESNO" not in code, (
        "the data question is a message box again: its default button is what made a "
        "click-through delete everything"
    )
    # Deleted only after the app and its cluster have been stopped: a running cluster
    # holds its files open, which would leave the install half deleted. The whole
    # condition is asserted, not the token, because the comment above it names the step
    # too and a token would be satisfied by prose.
    assert (
        "if (CurUninstallStep = usPostUninstall) and DeleteDataOnUninstall then" in code
    )
    assert "DelTree(ExpandConstant('{app}\\data'), True, True, True);" in code
    assert "DeleteFile(ExpandConstant('{app}\\.env'));" in code
    # "Delete all of my data" has to mean the folder, not two paths: leftovers would be
    # inherited by the next install.
    assert "DelTree(ExpandConstant('{app}'), True, True, True);" in code
    # Inno's own confirmation runs after that window, and its stock wording claims "all
    # of its components" and "successfully removed", neither of which is true here. Only
    # the message values are checked: the comment above them quotes the stock wording.
    messages = iss_text.split("[Messages]", 1)[1].split("\n[", 1)[0]
    message_values = "\n".join(
        line for line in messages.splitlines() if not line.lstrip().startswith(";")
    )
    assert "ConfirmUninstall={#AppName} will be removed now." in message_values
    assert "all of its components" not in message_values


def test_both_provisioning_steps_leave_a_log(iss_text: str) -> None:
    """Both steps run with their window hidden, so each must write a log.

    Otherwise a failure reaches the user as a bare exit code, which is exactly
    what the installer's own error dialog has to explain.
    """
    assert "synth_prereqs.log" in iss_text
    assert "synth_bootstrap.log" in iss_text
    assert "--log-file" in iss_text, "bootstrap is invoked without a log file"
    assert "{%TEMP}" in iss_text, "the reported log path must be the real TEMP folder"


def test_the_installer_is_user_scoped(iss_text: str) -> None:
    """No admin, no UAC: that was the main complaint about the old installer."""
    assert "PrivilegesRequired=lowest" in iss_text
    assert "PrivilegesRequired=admin" not in iss_text


def test_there_is_exactly_one_choice_to_make(iss_text: str) -> None:
    """One option, no component picker: the whole point of the rewrite."""
    assert "[Components]" not in iss_text
    assert "[Types]" not in iss_text
    assert "DisableDirPage=yes" in iss_text


def test_nothing_opens_a_console_window(iss_text: str) -> None:
    """Every helper runs through pythonw.exe or hidden PowerShell."""
    assert "start_synth.bat" not in iss_text
    assert "pythonw.exe" in iss_text
    run_entries = re.findall(r"^Filename:.*$", iss_text, flags=re.MULTILINE)
    assert run_entries, "expected [Run]/[UninstallRun] entries"
    for entry in run_entries:
        assert ".bat" not in entry and ".cmd" not in entry, entry


def test_the_version_comes_from_the_build_not_a_file(iss_text: str) -> None:
    """GitVersion is the source of truth; there is no version file to read."""
    assert "version.txt" not in iss_text
    assert "#ifndef AppVersion" in iss_text


def test_every_file_the_installer_references_exists() -> None:
    """A missing icon or template would only fail at compile time on a release."""
    for relative in (
        "installer/synth.ico",
        "installer/wizard-large.bmp",
        "installer/wizard-small.bmp",
        "installer/build_installer.ps1",
        "installer/vendor/README.md",
        "core/webui_templates/setup.html",
        "scripts/start_synth.py",
        "scripts/bootstrap.py",
        "scripts/install_prereqs.ps1",
    ):
        assert (REPO_ROOT / relative).is_file(), f"{relative} is referenced but missing"


def test_pgvector_is_vendored_by_the_release_build(iss_text: str) -> None:
    """Without it the install still works, but SOUL memory search is memory-only."""
    assert "vendor\\pgvector" in iss_text
    assert "skipifsourcedoesntexist" in iss_text


def test_the_uninstaller_stops_the_database_first(iss_text: str) -> None:
    """Windows will not delete a directory a running process holds open."""
    assert "--stop-cluster" in iss_text
    assert "start_synth.py" in iss_text and "--stop" in iss_text
