"""The release cycle's two scripts: the version and the notes, run on a real repository.

``scripts/version.py`` and ``scripts/release_notes.py`` are what CI runs on every push, so
they are run here the same way, as processes, in a throwaway git repository whose history is
written to order.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
    ).stdout


def _commit(repo: Path, message: str) -> None:
    _git(repo, "commit", "--allow-empty", "-m", message)


def _script(repo: Path, name: str, *args: str, branch: str = "main") -> str:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("GITHUB_REF_NAME", "GITHUB_HEAD_REF", "GITHUB_OUTPUT")
    }
    env["GITHUB_REF_NAME"] = branch
    return subprocess.run(
        [sys.executable, str(SCRIPTS / name), *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    return tmp_path


def test_without_a_tag_the_whole_history_counts(repo: Path) -> None:
    _commit(repo, "feat(a): first")
    _commit(repo, "fix(a): second")
    _commit(repo, "chore: third")
    _commit(repo, "feat(b): fourth")
    _commit(repo, "doc: fifth")
    assert _script(repo, "version.py").strip() == "0.2.1"


def test_a_breaking_change_bumps_the_major(repo: Path) -> None:
    _commit(repo, "feat(a): first")
    _commit(repo, "fix(api)!: drop a field")
    assert _script(repo, "version.py").strip() == "1.0.0"
    _commit(repo, "fix: something\n\nBREAKING CHANGE: it is different now")
    assert _script(repo, "version.py").strip() == "2.0.0"


def test_counting_starts_from_the_last_release_tag(repo: Path) -> None:
    for _ in range(5):
        _commit(repo, "feat: old")
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "fix: one")
    _commit(repo, "feat: two")
    _commit(repo, "fix: three")
    assert _script(repo, "version.py").strip() == "1.1.1"


def test_only_a_plain_version_tag_is_a_release(repo: Path) -> None:
    _commit(repo, "feat: a")
    _git(repo, "tag", "checkpoint")
    _git(repo, "tag", "v9.9.9-develop")
    _commit(repo, "feat: b")
    assert _script(repo, "version.py").strip() == "0.2.0"


def test_a_branch_carries_its_name(repo: Path) -> None:
    _commit(repo, "feat: a")
    assert _script(repo, "version.py", branch="feat/Ret_roXR").strip() == (
        "0.1.0-feat-ret-roxr"
    )


def test_running_again_on_a_released_commit_gives_the_same_version(
    repo: Path,
) -> None:
    _git(repo, "commit", "--allow-empty", "-m", "feat: a")
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "feat: b")
    _commit(repo, "fix: c")
    released = _script(repo, "version.py").strip()
    assert released == "1.1.1"
    _git(repo, "tag", f"v{released}")
    assert _script(repo, "version.py").strip() == released


def test_the_notes_list_features_and_fixes_since_the_last_release(
    repo: Path,
) -> None:
    _commit(repo, "feat: before the release")
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "feat(vessel): walk everywhere")
    _commit(repo, "fix(core): stop crashing")
    _commit(repo, "minor(ui): nicer button")
    _commit(repo, "chore(ci): tidy")
    _commit(repo, "doc: words")
    _commit(repo, "no convention here")
    _commit(repo, "refactor(x)!: break it")
    notes = _script(repo, "release_notes.py", "--repo", "o/n", "--version", "1.2.0")

    changes, _, fixes = notes.partition("## Fixes")
    assert "## Changes" in changes
    assert "vessel: walk everywhere" in changes
    assert "x: break it" in changes  # a breaking change is a change
    assert "core: stop crashing" in fixes
    assert "ui: nicer button" in fixes
    assert "no convention here" in fixes  # version.py counts it as a patch
    for left_out in ("tidy", "words", "before the release"):
        assert left_out not in notes
    assert "https://github.com/o/n/commit/" in notes
    assert "**See changes**: https://github.com/o/n/compare/v1.0.0...v1.2.0" in notes


def test_the_header_is_put_first_with_the_version_filled_in(repo: Path) -> None:
    _commit(repo, "feat: a")
    header = repo / "header.md"
    header.write_text("Image: {version}\n", encoding="utf-8")
    body = repo / "RELEASE.md"
    _script(
        repo,
        "release_notes.py",
        "--repo",
        "o/n",
        "--version",
        "0.1.0",
        "--header",
        str(header),
        "--body",
        str(body),
    )
    text = body.read_text(encoding="utf-8")
    assert text.startswith("Image: 0.1.0\n\n## Changes")


def test_a_release_with_nothing_to_list_says_so(repo: Path) -> None:
    _commit(repo, "chore: only housekeeping")
    notes = _script(repo, "release_notes.py", "--repo", "o/n", "--version", "0.0.1")
    assert "Nothing worth noting" in notes


def test_the_notes_of_a_rerun_are_still_the_notes_of_that_release(
    repo: Path,
) -> None:
    """The tag the release put on HEAD must not become 'the previous release'."""
    _commit(repo, "feat: a")
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "feat: b")
    _git(repo, "tag", "v1.1.0")
    notes = _script(repo, "release_notes.py", "--repo", "o/n", "--version", "1.1.0")
    assert "b [" in notes and "compare/v1.0.0...v1.1.0" in notes
