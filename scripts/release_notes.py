#!/usr/bin/env python3
"""Write the notes of a release from the commits made since the previous release.

The previous release is the highest vX.Y.Z tag reachable from HEAD, not counting one on HEAD itself (none: the whole history). Each commit becomes a line
without its type, with its scope in front and its hash, a link to the commit, at the end:

  ## Changes   feat, and a breaking change (the major and minor of scripts/version.py)
  ## Fixes     fix, perf, minor, patch, and a subject that is not a conventional commit (the patch)
  (left out)   chore, doc, docs, res, style, test, refactor, ci, build

  release_notes.py --repo OWNER/NAME --version 1.2.3 \\
      [--header FILE] [--body OUT] [--changelog OUT] [--module OUT]

--body is the text of the GitHub release: the header file ({version} in it is the version), the sections and a link to
compare the two releases.
--changelog is the sections alone (empty when there is nothing to list), and --module the same as a Python module
(CHANGELOG = "...") that a build bundles, so a build can show what changed in the version it is running.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import version as versioning  # noqa: E402  # ty: ignore[unresolved-import]

SUBJECT = re.compile(
    r"^(?P<type>\w+)(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?:\s*(?P<text>.+)$"
)
BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)
CHANGES = ("feat",)
FIXES = ("fix", "perf", "minor", "patch")
NOTHING = "Nothing worth noting in this version."


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def previous_release() -> str | None:
    """The tag of the previous release (see version.py), or None for the first one."""
    found = versioning.previous_release()
    return found[1] if found else None


def commits_since(tag: str | None) -> list[tuple[str, str]]:
    """(hash, whole message) of every non-merge commit since the tag, newest first."""
    log = git(
        "log", "--no-merges", "--format=%H%x1f%B%x00", f"{tag}..HEAD" if tag else "HEAD"
    )
    out = []
    for entry in filter(None, (e.strip() for e in log.split("\0"))):
        sha, _, message = entry.partition("\x1f")
        out.append((sha, message.strip()))
    return out


def sort(
    commits: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """The lines of Changes and of Fixes, each as (text, hash); the commits arrive newest first."""
    changes, fixes = [], []
    for sha, message in commits:
        subject = message.splitlines()[0] if message else ""
        m = SUBJECT.match(subject)
        if (
            m is None
        ):  # not a conventional commit: counted as a patch by scripts/version.py, so listed as a fix
            fixes.append((subject, sha))
            continue
        text = f"{m['scope']}: {m['text']}" if m["scope"] else m["text"]
        if m["bang"] or BREAKING_FOOTER.search(message):
            changes.append((text, sha))
        elif m["type"] in CHANGES:
            changes.append((text, sha))
        elif m["type"] in FIXES:
            fixes.append((text, sha))
    return changes, fixes


def line(text: str, sha: str, repo: str) -> str:
    return f"- {text} [[{sha[:7]}](https://github.com/{repo}/commit/{sha})]"


def sections(commits: list[tuple[str, str]], repo: str) -> str:
    changes, fixes = sort(commits)
    parts = []
    for title, found in (("Changes", changes), ("Fixes", fixes)):
        if found:
            parts.append(
                f"## {title}\n\n"
                + "\n".join(line(text, sha, repo) for text, sha in found)
            )
    return "\n\n".join(parts)


def body(header: str, notes: str, repo: str, version: str, previous: str | None) -> str:
    notes = notes or NOTHING
    parts = [header.strip(), notes] if header.strip() else [notes]
    if previous:
        parts.append(
            f"**See changes**: https://github.com/{repo}/compare/{previous}...v{version}"
        )
    return "\n\n".join(parts) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--repo", required=True, help="OWNER/NAME, for the links")
    ap.add_argument(
        "--version", required=True, help="the version being released, without the v"
    )
    ap.add_argument(
        "--header", type=Path, help="text put before the sections in the release body"
    )
    ap.add_argument("--body", type=Path)
    ap.add_argument("--changelog", type=Path)
    ap.add_argument("--module", type=Path)
    args = ap.parse_args()

    previous = previous_release()
    notes = sections(commits_since(previous), args.repo)
    header = (
        args.header.read_text(encoding="utf-8").replace("{version}", args.version)
        if args.header and args.header.exists()
        else ""
    )
    if args.body:
        args.body.write_text(
            body(header, notes, args.repo, args.version, previous), encoding="utf-8"
        )
    if args.changelog:
        args.changelog.write_text(notes + "\n" if notes else "", encoding="utf-8")
    if args.module:
        args.module.write_text(
            f'"""Written by scripts/release_notes.py at build time."""\n\nCHANGELOG = {notes!r}\n',
            encoding="utf-8",
        )
    if not (args.body or args.changelog or args.module):
        print(body(header, notes, args.repo, args.version, previous), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
