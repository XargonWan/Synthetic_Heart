#!/usr/bin/env python3
"""Summarise an ``ab_compare.py`` run: prompt make-up, size, and where the time went.

Total latency is mostly the LLM front end (a web session in the A/B setup), which is
the same for both arms and noisy. What the refactoring can change is everything
*before* the model is called, so each request's time is split using the arms' logs:

``llm_s``       from "-> LLM plugin: handing off" to "<- LLM plugin: completed"
``overhead_s``  total latency minus ``llm_s``: queueing, recall, prompt assembly and
                the reply handling

Requests are matched to log lines by order (both arms are queried one request at a
time, and only the OpenAI-compatible API produces ``interface=ollama_serve``).

Example:
    uv run python scripts/ab_report.py ~/synth-clone/ab-results/ab-123.jsonl \
        --arm A=~/synth-clone/logs-a --arm B=~/synth-clone/logs-b
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

_STAMP = r"\[(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?: [+-]\d{4})?\]"
_HANDOFF_RE = re.compile(
    _STAMP + r".*\[flow\] -> LLM plugin: handing off chat_id=(?P<chat>\S+) "
    r"interface=ollama_serve prompt_len=(?P<len>\d+)"
)
_DONE_RE = re.compile(
    _STAMP + r".*\[flow\] <- LLM plugin: completed for chat_id=(?P<chat>\S+)"
)


def _ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def llm_seconds(log_file: Path, count: int) -> list[float | None]:
    """The LLM time of the last ``count`` ollama_serve requests in ``log_file``."""
    started: dict[str, datetime] = {}
    order: list[str] = []
    durations: dict[str, float] = {}
    with log_file.open(errors="replace") as handle:
        for line in handle:
            if "[flow]" not in line:
                continue
            match = _HANDOFF_RE.search(line)
            if match:
                started[match["chat"]] = _ts(match["ts"])
                order.append(match["chat"])
                continue
            match = _DONE_RE.search(line)
            if match and match["chat"] in started:
                durations[match["chat"]] = (
                    _ts(match["ts"]) - started[match["chat"]]
                ).total_seconds()
    tail = order[-count:] if count else order
    return [durations.get(chat) for chat in tail]


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results")
    parser.add_argument("--arm", action="append", required=True, help="NAME=LOGDIR")
    args = parser.parse_args()

    rows = [
        json.loads(line)
        for line in Path(args.results).expanduser().read_text().splitlines()
        if line.strip()
    ]
    logs = {
        spec.split("=", 1)[0]: Path(spec.split("=", 1)[1]).expanduser() / "synth.log"
        for spec in args.arm
    }

    report: dict[str, Any] = {}
    for arm, log_file in logs.items():
        mine = [r for r in rows if r["arm"] == arm and not r.get("error")]
        llm = llm_seconds(log_file, len(mine))
        # The matching only holds if every request reached the model.
        aligned = len(llm) == len(mine)
        overhead = [
            r["latency_s"] - t for r, t in zip(mine, llm) if aligned and t is not None
        ]
        analyses = [(r.get("prompt") or {}).get("analysis") or {} for r in mine]
        kinds: Counter[str] = Counter()
        for a in analyses:
            kinds.update(a.get("memory_kinds") or {})
        n = max(len(mine), 1)
        report[arm] = {
            "requests_ok": len(mine),
            "errors": sum(1 for r in rows if r["arm"] == arm and r.get("error")),
            "latency_median_s": _median([r["latency_s"] for r in mine]),
            "llm_median_s": _median([t for t in llm if t is not None]),
            "overhead_median_s": _median(overhead),
            "prompt_chars_median": (
                int(
                    statistics.median(
                        p["prompt_len"]
                        for p in (r["prompt"] for r in mine)
                        if p.get("prompt_len")
                    )
                )
                if any((r.get("prompt") or {}).get("prompt_len") for r in mine)
                else None
            ),
            "memories_per_prompt": round(
                sum(a.get("n_memories", 0) for a in analyses) / n, 2
            ),
            "repeated_entries_per_prompt": round(
                sum(a.get("repeated_entries", 0) for a in analyses) / n, 2
            ),
            "memory_kinds_per_prompt": {
                k: round(v / n, 2) for k, v in sorted(kinds.items())
            },
        }
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
