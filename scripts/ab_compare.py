#!/usr/bin/env python3
"""Send the same prompts to two running SyntH instances and compare what they build.

Built for the memory refactor's A/B: arm A is the previous engine, arm B the
SOUL-first one, both on copies of the same database. For every prompt it records,
per arm, the end-to-end latency, the reply, and - read back from the arm's log - the
size and make-up of the prompt that was assembled (how many recalled memories, of
which kind, how many entries repeat another one).

Prompts come from ``--prompts-file`` (one per line) or are sampled from the human
messages in ``chat_history_cache`` with ``--sample`` (the DB environment is the
application's). Sampled text is never printed; it goes only to the result file.

Arms are queried one after the other, alternating which goes first, because both
share one LLM front end and a concurrent run would measure queueing.

Example:
    uv run python scripts/ab_compare.py --sample 12 --seed 7 \
        --arm A=http://127.0.0.1:11435,~/synth-clone/logs-a \
        --arm B=http://127.0.0.1:11437,~/synth-clone/logs-b \
        --out ~/synth-clone/ab-results
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import statistics
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_PROMPT_LINE_RE = re.compile(
    r"\[flow\] -> LLM plugin: handing off .*prompt_len=(?P<len>\d+)"
    r"(?: pre_reduction_size=(?P<pre>\d+))?"
)
_PROMPT_JSON_MARK = "[flow] prompt content: "
_WS_RE = re.compile(r"\s+")


def _norm(text: Any) -> str:
    return _WS_RE.sub(" ", str(text or "")).strip().casefold()


def _strip_header(entry: Any) -> str:
    """The body of a context entry, without its ``[label | date | ...]`` header."""
    text = _norm(entry)
    return re.sub(r"^\[[^\]]*\]\s*", "", text)


_RECALL_HEADER_RE = re.compile(
    r"^Recalled memory from [\d-]+(?: \((?P<qual>[^)]*)\))?", re.IGNORECASE
)


def memory_kind(text: str) -> str:
    """Which store a rendered memory came from, read off its header qualifiers.

    The prompt engine renders every recalled memory as
    ``Recalled memory from <date> (<qualifiers>): <body>``; the qualifiers say where
    it came from (``my diary``, ``long-term memory``, ``chat history``,
    ``other chat: ...``). Only the header is read, never the body.
    """
    match = _RECALL_HEADER_RE.match(text.strip())
    qual = (match.group("qual") or "").lower() if match else ""
    if "my diary" in qual:
        return "soul_diary"
    if "older memory" in qual:
        return "soul_older_memory"
    if "chat history" in qual:
        return "chat_history_tier"
    if "long-term memory" in qual:
        return "memories_table"
    if "same chat" in qual:
        return "soul_chat_same"
    if "other chat" in qual:
        return "soul_chat_other"
    if re.search(r"\bdiary\b", qual):
        return "diary_table"
    return "other"


def analyse_prompt(payload: dict[str, Any]) -> dict[str, Any]:
    """Counts describing one assembled prompt (no text is kept)."""
    context = payload.get("context") if isinstance(payload, dict) else None
    context = context if isinstance(context, dict) else {}
    memories = [m for m in context.get("memories", []) if m]
    labels: dict[str, int] = {}
    for entry in memories:
        text = str(entry if not isinstance(entry, dict) else entry.get("snippet", ""))
        kind = memory_kind(text)
        labels[kind] = labels.get(kind, 0) + 1

    pools = {
        "memories": [_strip_header(m) for m in memories],
        "history_recent": [_strip_header(m) for m in context.get("history_recent", [])],
        "thoughts": [_strip_header(m) for m in context.get("thoughts", [])],
    }
    bodies = [b[:120] for pool in pools.values() for b in pool if len(b) > 30]
    repeated = len(bodies) - len(set(bodies))
    return {
        "context_keys": sorted(context.keys()),
        "n_memories": len(memories),
        "memory_kinds": labels,
        "n_history_recent": len(pools["history_recent"]),
        "n_thoughts": len(pools["thoughts"]),
        "repeated_entries": repeated,
        "context_chars": len(json.dumps(context, ensure_ascii=False)),
    }


def read_new_log(log_file: Path, offset: int) -> str:
    with log_file.open("rb") as handle:
        handle.seek(offset)
        return handle.read().decode("utf-8", errors="replace")


def parse_prompt_from_log(segment: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for line in segment.splitlines():
        match = _PROMPT_LINE_RE.search(line)
        if match:
            result["prompt_len"] = int(match.group("len"))
            if match.group("pre"):
                result["pre_reduction_size"] = int(match.group("pre"))
        if _PROMPT_JSON_MARK in line and "context" not in result:
            raw = line.split(_PROMPT_JSON_MARK, 1)[1]
            try:
                result["analysis"] = analyse_prompt(json.loads(raw))
            except ValueError:
                result["analysis_error"] = "prompt line is not valid JSON"
                result["analysis"] = {}
            result["context"] = True
    result.pop("context", None)
    return result


def ask(base_url: str, prompt: str, timeout: float) -> tuple[str, float]:
    body = json.dumps(
        {
            "model": "Rei",
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    elapsed = time.monotonic() - started
    reply = ""
    for chunk in raw.splitlines():
        try:
            doc = json.loads(chunk)
        except ValueError:
            continue
        reply = (
            (doc.get("message") or {}).get("content") or doc.get("response") or reply
        )
    return str(reply), elapsed


async def sample_prompts(count: int, seed: int, trainer_only: bool = True) -> list[str]:
    """Human messages from ``chat_history_cache`` (the trainer's, by default).

    Bots, the persona itself and automatic notices make poor prompts: the first
    sample drew a bot's channel notice, which tells nothing about recall. The
    trainer ids come from the ``TRAINER_IDS`` setting (``interface:id,...``).
    """
    from core.db import get_conn_ctx

    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            trainer_ids: list[str] = []
            if trainer_only:
                await cur.execute(
                    "SELECT value FROM config WHERE config_key = %s", ("TRAINER_IDS",)
                )
                row = await cur.fetchone()
                for part in str((row or [""])[0] or "").split(","):
                    ident = part.split(":", 1)[-1].strip()
                    if ident:
                        trainer_ids.append(ident)
            who = ""
            params: tuple[str, ...] = ()
            if trainer_ids:
                who = f" AND sender_id IN ({', '.join(['%s'] * len(trainer_ids))})"
                params = tuple(trainer_ids)
            await cur.execute(
                """
                SELECT message_text FROM chat_history_cache
                WHERE sender_name IS NOT NULL AND sender_name <> 'self'
                  AND LEFT(COALESCE(interface_path, ''), 7) NOT IN ('vessel/', 'grillo/')
                  AND created_at > NOW() - INTERVAL '120 days'
                  AND LENGTH(message_text) BETWEEN 25 AND 220
                  AND POSITION('http' IN message_text) = 0
                  AND LEFT(message_text, 1) <> '['
                """
                + who,
                params,
            )
            rows = [str(r[0]) for r in await cur.fetchall()]
    random.Random(seed).shuffle(rows)
    return rows[:count]


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_arm: dict[str, list[dict[str, Any]]] = {}
    for row in results:
        by_arm.setdefault(row["arm"], []).append(row)
    out: dict[str, Any] = {}
    for arm, rows in sorted(by_arm.items()):
        ok = [r for r in rows if not r.get("error")]
        lat = [r["latency_s"] for r in ok]
        lens = [
            r["prompt"].get("prompt_len") for r in ok if r["prompt"].get("prompt_len")
        ]
        ana = [r["prompt"].get("analysis") or {} for r in ok]
        out[arm] = {
            "requests": len(rows),
            "errors": len(rows) - len(ok),
            "latency_median_s": round(statistics.median(lat), 1) if lat else None,
            "latency_p90_s": (
                round(sorted(lat)[max(0, int(len(lat) * 0.9) - 1)], 1) if lat else None
            ),
            "prompt_len_median": int(statistics.median(lens)) if lens else None,
            "memories_mean": (
                round(statistics.mean(a.get("n_memories", 0) for a in ana), 2)
                if ana
                else None
            ),
            "repeated_entries_mean": (
                round(statistics.mean(a.get("repeated_entries", 0) for a in ana), 2)
                if ana
                else None
            ),
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", action="append", required=True, help="NAME=URL,LOGDIR")
    parser.add_argument("--prompts-file")
    parser.add_argument("--sample", type=int, default=0)
    parser.add_argument(
        "--any-sender",
        action="store_true",
        help="sample every human sender, not only the trainer",
    )
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--out", required=True)
    parser.add_argument("--deadline", help="HH:MM local time after which to stop")
    args = parser.parse_args()

    arms: list[tuple[str, str, Path]] = []
    for spec in args.arm:
        name, rest = spec.split("=", 1)
        url, logdir = rest.split(",", 1)
        arms.append((name, url, Path(logdir).expanduser() / "synth.log"))

    if args.prompts_file:
        prompts = [
            line.strip()
            for line in Path(args.prompts_file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    else:
        prompts = asyncio.run(
            sample_prompts(args.sample, args.seed, not args.any_sender)
        )
    if not prompts:
        print("no prompts", file=sys.stderr)
        return 2

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"ab-{int(time.time())}.jsonl"
    results: list[dict[str, Any]] = []
    print(f"{len(prompts)} prompts, arms={[a[0] for a in arms]}, out={out_file}")

    for index, prompt in enumerate(prompts):
        if args.deadline:
            now = time.strftime("%H:%M")
            if now >= args.deadline and now < "12:00":
                print(f"deadline {args.deadline} reached, stopping")
                break
        order = arms if index % 2 == 0 else list(reversed(arms))
        for name, url, log_file in order:
            offset = log_file.stat().st_size if log_file.exists() else 0
            row: dict[str, Any] = {"i": index, "arm": name, "prompt_text": prompt}
            try:
                reply, elapsed = ask(url, prompt, args.timeout)
                row.update(reply=reply, latency_s=round(elapsed, 2))
                time.sleep(2)  # let the log flush the prompt line
                row["prompt"] = parse_prompt_from_log(read_new_log(log_file, offset))
            except Exception as exc:  # noqa: BLE001 - recorded, run continues
                row.update(error=str(exc), prompt={})
            results.append(row)
            with out_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(
                f"[{index + 1}/{len(prompts)}] {name}: "
                f"{row.get('latency_s', 'ERR')}s prompt_len={row['prompt'].get('prompt_len')}"
                f" memories={(row['prompt'].get('analysis') or {}).get('n_memories')}"
            )

    print(json.dumps(summarise(results), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
