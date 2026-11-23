#!/usr/bin/env python3
"""Compute the version from the commit history and the branch name.

Starts from the newest plain ``vX.Y.Z`` tag reachable from HEAD (none: 0.0.0 and the
whole history), then walks every non-merge commit after it, oldest to newest:
  breaking change ("type!:" or a BREAKING CHANGE footer) -> major, minor/patch reset
  feat                                                    -> minor, patch reset
  anything else (fix, chore, doc, ...)                    -> patch
Branches other than main get the branch name as a suffix: 1.3.1-my-branch.

A tag sitting on HEAD itself is not a starting point, so running this again on a commit
that was already released gives the version it was released with.

Prints the version; under GitHub Actions it also writes ``version=`` to $GITHUB_OUTPUT.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

MAIN_BRANCHES = ("main", "master")
BREAKING = re.compile(r"^\w+(\([^)]*\))?!:|^BREAKING[ -]CHANGE:", re.MULTILINE)
FEAT = re.compile(r"^feat(\([^)]*\))?:")
RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def git(*args: str) -> str:
    """Run git and return its standard output."""
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def branch_name() -> str:
    """The branch being built: the pull request's head, the pushed ref, or HEAD."""
    name = os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME")
    return name or git("rev-parse", "--abbrev-ref", "HEAD").strip()


def previous_release() -> tuple[tuple[int, int, int], str] | None:
    """The highest plain vX.Y.Z tag reachable from HEAD, not counting one on HEAD."""
    found = []
    for tag in git("tag", "--merged", "HEAD", "--no-contains", "HEAD").split():
        if m := RELEASE_TAG.match(tag):
            found.append(((int(m[1]), int(m[2]), int(m[3])), tag))
    return max(found) if found else None


def compute() -> str:
    """The version of HEAD, with the branch suffix when it is not a main branch."""
    previous = previous_release()
    major, minor, patch = previous[0] if previous else (0, 0, 0)
    log = git(
        "log",
        "--reverse",
        "--topo-order",
        "--no-merges",
        "--format=%B%x00",
        f"{previous[1]}..HEAD" if previous else "HEAD",
    )
    for message in filter(None, (m.strip() for m in log.split("\0"))):
        if BREAKING.search(message):
            major, minor, patch = major + 1, 0, 0
        elif FEAT.match(message):
            minor, patch = minor + 1, 0
        else:
            patch += 1
    version = f"{major}.{minor}.{patch}"

    branch = branch_name()
    if branch not in MAIN_BRANCHES:
        slug = re.sub(r"[^a-z0-9-]+", "-", branch.lower()).strip("-")
        version += f"-{slug}"
    return version


if __name__ == "__main__":
    result = compute()
    print(result)
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"version={result}\n")
    sys.exit(0)
