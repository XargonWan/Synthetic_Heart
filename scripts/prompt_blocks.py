#!/usr/bin/env python3
"""Print the block-level composition of one Langfuse trace's assembled prompt.

Usage
-----
    python scripts/prompt_blocks.py <trace_id> [--json]
    python scripts/prompt_blocks.py <trace_id> --sections   # just the section table

Reads the checkout's ``.env`` for the Langfuse credentials (``LANGFUSE_HOST`` or
``LANGFUSE_BASE_URL``/``LANGFUSE_BASEURL`` plus ``LANGFUSE_PUBLIC_KEY`` and
``LANGFUSE_SECRET_KEY``).

Why this exists: the whole point of the instruction-budget work is that the
shared instruction block dominates the system message on every route. That is a
claim about a *rendered* prompt, so it has to be measured on a rendered prompt,
not reasoned from the source. This prints, per message, the size and the offsets
of the known prompt sections, plus the token accounting the endpoint reported.

Section detection is anchored to line starts (``re.MULTILINE``) because several
section names also appear as cross-references inside the instruction text — a
bare substring search reports the reference, not the block.

Read-only. Never prints a credential, and never prints prompt content.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import requests

UA = "synth-diag/1.0"

# (label, compiled pattern). Order is the order they appear in a rendered prompt;
# each is searched for *after* the previous section start so a later duplicate of
# an earlier marker cannot reorder the table.
#
# Two anchoring styles, deliberately: the instruction-scaffold markers
# (`=== … ===`, `GRILLO INTERNAL MODE`) live inside a MINIFIED single-line string,
# so they can only be found unanchored; the context-block markers (`[SYSTEM: …]`)
# are newline-joined and are anchored to a line start — which also avoids the
# in-text cross-references (the instructions MENTION `[SYSTEM: REALITY ANCHOR]`
# mid-line long before the block itself appears).
SECTIONS: list[tuple[str, re.Pattern[str]]] = [
    ("persona", re.compile(r"=== CRITICAL SYSTEM IDENTITY ===")),
    ("instructions", re.compile(r"=== JSON RESPONSE INSTRUCTIONS ===")),
    ("actions", re.compile(r"=== AVAILABLE ACTIONS ===")),
    ("reality_anchor", re.compile(r"^\[SYSTEM: REALITY ANCHOR\]$", re.M)),
    ("temporal_context", re.compile(r"^\[Temporal context\]$", re.M)),
    ("persona_background", re.compile(r"^\[Persona background\]$", re.M)),
    ("self_growth", re.compile(r"^\[Self-growth\]$", re.M)),
    (
        "recent_other_chats",
        re.compile(r"^\[Recent context from other conversations\]$", re.M),
    ),
    ("thoughts", re.compile(r"^\[Thoughts and diary entries\]$", re.M)),
    ("memory_honesty", re.compile(r"^\[Memory honesty notice\]$", re.M)),
    ("memories", re.compile(r"^\[Relevant memories\]$", re.M)),
    ("session_state", re.compile(r"^\[Session state\]$", re.M)),
    ("participants", re.compile(r"^\[People in this conversation\]$", re.M)),
    ("grillo_guard", re.compile(r"GRILLO INTERNAL MODE:")),
    ("vessel_guidance", re.compile(r"LIVE VESSEL STATE is attached")),
    ("grillo_friendly", re.compile(r"^INSTRUCTIONS \(friendly\):", re.M)),
    ("grillo_proactivity", re.compile(r"^PROACTIVITY \(activation frames\):", re.M)),
    ("eligible_targets", re.compile(r"^ELIGIBLE TARGETS", re.M)),
]

# Markers appended at the *end* of a payload, after everything above.
TAIL_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    (
        "json_format_reminder",
        re.compile(r"\n\nRespond with ONLY valid JSON — your ENTIRE reply"),
    ),
]


def read_env(path: Path) -> dict[str, str]:
    """Parse a dotenv file into a dict (values may be quoted)."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def base_url(env: dict[str, str]) -> str:
    for key in ("LANGFUSE_HOST", "LANGFUSE_BASE_URL", "LANGFUSE_BASEURL"):
        if env.get(key):
            return env[key].rstrip("/")
    return ""


def as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def sections_of(text: str) -> list[tuple[int, int, str]]:
    """Return (start, end, label) for every section present in ``text``."""
    found: list[tuple[int, str]] = []
    cursor = 0
    for label, pattern in SECTIONS:
        match = pattern.search(text, cursor)
        if match is None:
            # A section may appear before an earlier-searched one (e.g. the Grillo
            # guard is prepended ahead of the persona) — search from the start.
            match = pattern.search(text)
        if match is None:
            continue
        found.append((match.start(), label))
        cursor = match.start()
    for label, pattern in TAIL_MARKERS:
        match = pattern.search(text)
        if match is not None:
            found.append((match.start(), label))
    found.sort()
    out: list[tuple[int, int, str]] = []
    for i, (start, label) in enumerate(found):
        end = found[i + 1][0] if i + 1 < len(found) else len(text)
        out.append((start, end, label))
    return out


def fetch_observations(trace_id: str, env: dict[str, str]) -> list[dict[str, Any]]:
    base = base_url(env)
    auth = (env.get("LANGFUSE_PUBLIC_KEY", ""), env.get("LANGFUSE_SECRET_KEY", ""))
    if not base or not auth[0] or not auth[1]:
        raise SystemExit(
            "Langfuse not configured: need LANGFUSE_HOST + LANGFUSE_PUBLIC_KEY + "
            "LANGFUSE_SECRET_KEY in .env"
        )
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "application/json"})
    response = session.get(
        f"{base}/api/public/observations",
        params={"traceId": trace_id, "limit": 50},
        auth=auth,
        timeout=60,
    )
    response.raise_for_status()
    return response.json().get("data") or []


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        raise SystemExit(2)
    trace_id = args[0]
    as_json = "--json" in sys.argv

    env = read_env(Path(".env"))
    observations = fetch_observations(trace_id, env)
    if not observations:
        raise SystemExit(f"no observations for trace {trace_id}")

    report: dict[str, Any] = {"trace_id": trace_id, "observations": []}
    for observation in observations:
        payload = observation.get("input")
        if not isinstance(payload, dict):
            continue
        usage = observation.get("usage") or {}
        messages = payload.get("messages") or []
        entry: dict[str, Any] = {
            "name": observation.get("name"),
            "model": observation.get("model"),
            "prompt_tokens": usage.get("input"),
            "completion_tokens": usage.get("output"),
            "messages": [],
        }
        for index, message in enumerate(messages):
            content = message.get("content")
            if isinstance(content, list):
                content = json.dumps(content, ensure_ascii=False)
            content = content or ""
            message_entry: dict[str, Any] = {
                "index": index,
                "role": message.get("role"),
                "chars": len(content),
                "sections": [
                    {"start": start, "end": end, "chars": end - start, "label": label}
                    for start, end, label in sections_of(content)
                ],
            }
            entry["messages"].append(message_entry)
        entry["total_chars"] = sum(m["chars"] for m in entry["messages"])
        report["observations"].append(entry)

    if as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    for entry in report["observations"]:
        print(
            f"=== {entry['name']} model={entry['model']} "
            f"prompt_tokens={entry['prompt_tokens']} "
            f"completion_tokens={entry['completion_tokens']}"
        )
        for message in entry["messages"]:
            print(
                f"  [{message['index']}] role={message['role']:9s} "
                f"chars={message['chars']}"
            )
            for section in message["sections"]:
                print(
                    f"        {section['start']:6d}..{section['end']:6d}  "
                    f"({section['chars']:6d})  {section['label']}"
                )
        print(f"  TOTAL chars={entry['total_chars']}")


if __name__ == "__main__":
    main()
