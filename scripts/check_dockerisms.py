#!/usr/bin/env python
"""Fail on new container-only paths in code that must also run natively.

SyntH was Docker-only for most of its life, so ``/app`` and ``/config`` defaults
crept into code that a native install (Windows, bare-metal Linux) also executes.
On Windows ``/app`` resolves to ``C:\\app`` and ``/config`` to ``C:\\config``:
neither exists, so those defaults silently break at runtime instead of failing
loudly.

This scanner is the mechanism that keeps the list from growing again. It is
deliberately narrow: it looks for the literal container paths in string
literals, and every remaining hit has to be listed in ``ALLOWLIST`` with a
reason. Verification-only mentions in comments and docstrings are ignored.

Usage::

    python scripts/check_dockerisms.py            # report, exit 1 on new hits
    python scripts/check_dockerisms.py --list     # show the allowlist
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Globs that make up the shipped application. Tests and docs are excluded: a
#: test may legitimately simulate the container layout.
SCAN_GLOBS = (
    "core/**/*.py",
    "plugins/**/*.py",
    "interface/**/*.py",
    "engines/**/*.py",
    "scripts/**/*.py",
    "main.py",
)

#: (relative path, literal) pairs that are intentional. Keep the reason short and
#: factual; anything that is a real bug should be fixed, not listed here.
ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "core/app_paths.py",
        "/app",
    ): "the container log dir this module deliberately probes",
    (
        "core/app_paths.py",
        "/config",
    ): "the container data root this module deliberately probes",
    ("core/config.py", "/app"): "extra .env fallback, tried after the repo-local file",
    (
        "core/logging_utils.py",
        "/app",
    ): "extra .env fallback, tried after the repo-local file",
    (
        "core/external_endpoints/crypto.py",
        "/config",
    ): "container secret path, falls back to ~/.synthetic_heart",
    (
        "plugins/radio_host/radio_host_plugin.py",
        "/app",
    ): "container dir probed before the native fallback",
    ("core/legacy_state.py", "/app"): "names the leftover paths this module adopts",
    ("core/legacy_state.py", "/config"): "names the leftover paths this module adopts",
}

#: Files that are about the container paths themselves and must not be scanned.
SELF_EXCLUDE: frozenset[str] = frozenset({"scripts/check_dockerisms.py"})

#: Literals that must never appear in a *live* default.
PATTERNS = (
    ("/app", re.compile(r"""(?P<q>["'])/app(?:/[^"']*)?["']""")),
    ("/config", re.compile(r"""(?P<q>["'])/config(?:/[^"']*)?["']""")),
)


def _is_comment_or_docstring_line(line: str) -> bool:
    stripped = line.strip()
    return (
        stripped.startswith("#")
        or stripped.startswith('"""')
        or stripped.startswith("'''")
    )


def scan() -> list[tuple[str, int, str, str]]:
    """Return a list of (path, lineno, literal, line) for un-allowlisted hits."""
    findings: list[tuple[str, int, str, str]] = []
    for glob in SCAN_GLOBS:
        for path in sorted(REPO_ROOT.glob(glob)):
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in SELF_EXCLUDE:
                continue
            for lineno, line in enumerate(lines, start=1):
                if _is_comment_or_docstring_line(line):
                    continue
                for literal, pattern in PATTERNS:
                    match = pattern.search(line)
                    if not match:
                        continue
                    if (rel, literal) in ALLOWLIST:
                        continue
                    findings.append((rel, lineno, literal, line.strip()))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list", action="store_true", help="print the allowlist and exit"
    )
    args = parser.parse_args(argv)

    if args.list:
        for (path, literal), reason in sorted(ALLOWLIST.items()):
            print(f"{path}: {literal} — {reason}")
        return 0

    findings = scan()
    if not findings:
        print("check_dockerisms: no un-allowlisted container-only paths found.")
        return 0

    print("check_dockerisms: container-only paths found in live code:")
    for rel, lineno, literal, line in findings:
        print(f"  {rel}:{lineno}: {literal}  ->  {line}")
    print(
        "\nResolve these through core.app_paths (app_root/data_root/log_dir/"
        "cert_dir/agent_fs_roots), or add them to ALLOWLIST with a reason."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
