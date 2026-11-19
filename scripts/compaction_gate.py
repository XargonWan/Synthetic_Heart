#!/usr/bin/env python3
"""Decision gate for the diary compactor: does a day summary help recall?

The question is not only "summary or raw day". A diary day is one row of about 20,000
characters made of ~46 entries, and SOUL's ``journal`` cell keeps only the first
4,000 of it, so a question about the rest cannot match that cell however good the
ranking is. This script measures five representations of the same days against the
same questions, with the real embedder and SOUL's own recall:

``raw``       one ``journal`` cell per day, truncated at 4,000 (what is filed today)
``summary``   one ``diary_day`` cell per day (what the compactor wrote)
``both``      raw + summary (the live state after the legacy ingest)
``chunks``    the day split into ~1,500-character blocks, one cell each
``chunks+s``  chunks + summary

Questions are written by an LLM from random entries of the day (not from the
summary and not from the 4,000-character head), so none of the representations is
favoured by construction. An LLM also says whether each question can be answered
from the summary alone (retention). Everything the LLM produced is cached so the
retrieval half can be re-run for free.

Needs the application database (``DB_*``), the local zen engine for the questions
(``--llm-url``) and the real embedder (``SYNTH_MODELS_DIR``).

    uv run python scripts/compaction_gate.py --days 24 --out ~/synth-clone/gate
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
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db import get_conn_ctx  # noqa: E402
from core.soul.fastembed_embedder import FastEmbedder  # noqa: E402
from core.soul.legacy_ingest import diary_text  # noqa: E402
from core.soul.repository import InMemorySoulRepository  # noqa: E402
from core.soul.service import MAX_TRACE_CHARS, SoulService  # noqa: E402

SEPARATOR = "\n\n---\n\n"
CHUNK_TARGET = 1500
MIN_FRAGMENT = 250


class CachedEmbedder:
    """Embeds each distinct text once, across the five corpora."""

    def __init__(self, model_id: str | None = None) -> None:
        self._inner = FastEmbedder(model_id) if model_id else FastEmbedder()
        self._cache: dict[str, list[float]] = {}

    async def embed(self, text: str) -> list[float]:
        if text not in self._cache:
            self._cache[text] = await self._inner.embed(text)
        return self._cache[text]


def ask_llm(url: str, prompt: str, timeout: float = 600.0) -> str:
    body = json.dumps(
        {"model": "gemini", "messages": [{"role": "user", "content": prompt}]}
    ).encode()
    request = urllib.request.Request(
        f"{url}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode())
    return str(data["choices"][0]["message"]["content"] or "")


def parse_json(text: str) -> Any:
    for pattern in (r"\[.*\]", r"\{.*\}"):
        match = re.search(pattern, text, re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except ValueError:
                continue
    return None


async def load_days() -> list[dict[str, Any]]:
    """Covered days: the compaction summary and the live raw diary row, by date."""
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT created_at, content FROM memories WHERE source = 'compaction' "
                "ORDER BY created_at"
            )
            # The legacy compactor wrote several summaries per day (one per cluster
            # of entries), so a day has a list of them, not one.
            summaries: dict[str, list[str]] = defaultdict(list)
            for row in await cur.fetchall():
                summaries[row[0].date().isoformat()].append(str(row[1]))
            await cur.execute(
                "SELECT id, created_at, content, personal_thought, interaction_summary "
                "FROM ai_diary ORDER BY created_at"
            )
            rows = await cur.fetchall()
    days = []
    for row in rows:
        day = row[1].date().isoformat()
        if day not in summaries:
            continue
        content = str(row[2] or "")
        days.append(
            {
                "day": day,
                "ts": row[1].isoformat(),
                "summaries": summaries[day],
                "summary": " ".join(summaries[day]),
                "content": content,
                "thought": str(row[3] or ""),
                "interaction": str(row[4] or ""),
                "raw_cell": diary_text(
                    {
                        "content": content,
                        "personal_thought": row[3],
                        "interaction_summary": row[4],
                    }
                )[:MAX_TRACE_CHARS],
            }
        )
    return days


async def load_background(covered: set[str]) -> list[dict[str, str]]:
    """Journal cells of days that are not under test: the haystack."""
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, event_timestamp, episodic_trace FROM mem_cells "
                "WHERE source_kind = 'journal'"
            )
            rows = await cur.fetchall()
    return [
        {"id": str(r[0]), "ts": r[1].isoformat(), "text": str(r[2])}
        for r in rows
        if r[1].date().isoformat() not in covered
    ]


def fragments_of(day: dict[str, Any]) -> list[tuple[int, str]]:
    """``(offset in the day text, fragment)`` for the entries long enough to ask about."""
    out, offset = [], 0
    for part in day["content"].split(SEPARATOR):
        if len(part.strip()) >= MIN_FRAGMENT:
            out.append((offset, part.strip()))
        offset += len(part) + len(SEPARATOR)
    return out


def chunks_of(day: dict[str, Any]) -> list[str]:
    """The day as ~1,500-character blocks (entries kept whole)."""
    head = " ".join(f"{day['interaction']} {day['thought']}".split())
    chunks = [head] if head else []
    current = ""
    for part in day["content"].split(SEPARATOR):
        part = " ".join(part.split())
        if not part:
            continue
        if current and len(current) + len(part) > CHUNK_TARGET:
            chunks.append(current)
            current = ""
        current = f"{current} {part}".strip()
    if current:
        chunks.append(current)
    return chunks


def make_questions(
    url: str, day: dict[str, Any], rng: random.Random
) -> list[dict[str, Any]]:
    frags = fragments_of(day)
    if len(frags) < 2:
        return []
    picked = rng.sample(frags, 2)
    questions: list[dict[str, Any]] = []
    for offset, fragment in picked:
        prompt = (
            "Below is one entry of a personal diary. Write 2 different questions that "
            "someone could ask the diary's author about it later, each answerable ONLY "
            "from this entry, in the language of the entry, without copying its "
            "sentences. Return only a JSON list of 2 strings.\n\nENTRY:\n"
            + fragment[:1500]
        )
        parsed = parse_json(ask_llm(url, prompt))
        if not isinstance(parsed, list):
            continue
        for q in parsed[:2]:
            if isinstance(q, str) and q.strip():
                questions.append(
                    {
                        "q": q.strip(),
                        "offset": offset,
                        "in_head": offset < MAX_TRACE_CHARS,
                    }
                )
    return questions


def judge_summary(
    url: str, summary: str, questions: list[dict[str, Any]]
) -> list[bool]:
    numbered = "\n".join(f"{i + 1}. {q['q']}" for i, q in enumerate(questions))
    prompt = (
        "Here is a short text and some questions. For each question say whether the "
        "text contains the information needed to answer it. Return only a JSON list "
        f"of {len(questions)} booleans, in order.\n\nTEXT:\n{summary}\n\nQUESTIONS:\n{numbered}"
    )
    parsed = parse_json(ask_llm(url, prompt))
    if isinstance(parsed, list) and len(parsed) == len(questions):
        return [bool(x) for x in parsed]
    return [False] * len(questions)


async def build_corpus(
    name: str,
    days: list[dict[str, Any]],
    background: list[dict[str, str]],
    embedder: CachedEmbedder,
) -> SoulService:
    service = SoulService(InMemorySoulRepository(), embedder)
    for item in background:
        await service.ingest_summary(
            kind="journal",
            source_key=f"bg:{item['id']}",
            text=item["text"],
            event_timestamp=datetime.fromisoformat(item["ts"]),
        )
    for day in days:
        ts = datetime.fromisoformat(day["ts"])
        if name in ("raw", "both"):
            await service.ingest_summary(
                kind="journal",
                source_key=f"day:{day['day']}:raw",
                text=day["raw_cell"],
                event_timestamp=ts,
            )
        if name in ("summary", "both", "chunks+s"):
            for i, summary in enumerate(day["summaries"]):
                await service.ingest_summary(
                    kind="diary_day",
                    source_key=f"day:{day['day']}:sum{i}",
                    text=summary,
                    event_timestamp=ts,
                )
        if name in ("chunks", "chunks+s"):
            for i, chunk in enumerate(chunks_of(day)):
                await service.ingest_summary(
                    kind="journal",
                    source_key=f"day:{day['day']}:c{i}",
                    text=chunk,
                    event_timestamp=ts,
                )
    return service


async def rank_of(service: SoulService, question: str, day: str) -> int | None:
    hits = await service.recall(question, limit=10)
    for position, hit in enumerate(hits, start=1):
        if f"day:{day}:" in hit.cell.id:
            return position
    return None


def wilson(hits: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    z = 1.96
    p = hits / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


async def main_async(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    cache = out / "questions.json"
    rng = random.Random(args.seed)

    days = [d for d in await load_days() if len(fragments_of(d)) >= 2]
    rng.shuffle(days)
    days = sorted(days[: args.days], key=lambda d: d["day"])
    print(f"{len(days)} days under test (of the covered ones with enough entries)")

    questions: dict[str, Any] = json.loads(cache.read_text()) if cache.exists() else {}
    for day in days:
        if day["day"] in questions:
            continue
        qs = make_questions(args.llm_url, day, rng)
        retained = judge_summary(args.llm_url, day["summary"], qs) if qs else []
        for q, ok in zip(qs, retained):
            q["answerable_from_summary"] = ok
        questions[day["day"]] = qs
        cache.write_text(json.dumps(questions, ensure_ascii=False))
        print(f"  {day['day']}: {len(qs)} questions", flush=True)

    background = await load_background({d["day"] for d in days})
    embedder = CachedEmbedder(args.embedder)
    results: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for name in ("raw", "summary", "both", "chunks", "chunks+s"):
        service = await build_corpus(name, days, background, embedder)
        for day in days:
            for q in questions.get(day["day"], []):
                rank = await rank_of(service, q["q"], day["day"])
                results[name].append({"rank": rank, "in_head": q["in_head"]})
        print(f"  corpus {name}: done", flush=True)

    all_qs = [q for d in days for q in questions.get(d["day"], [])]
    n = len(all_qs)
    retained = sum(1 for q in all_qs if q.get("answerable_from_summary"))
    report: dict[str, Any] = {
        "embedder": args.embedder or "default (SOUL_EMBEDDER_ID)",
        "days": len(days),
        "questions": n,
        "background_cells": len(background),
        "summaries_per_day_avg": round(
            statistics.mean(len(d["summaries"]) for d in days), 1
        ),
        "summary_chars_per_day_avg": round(
            statistics.mean(len(d["summary"]) for d in days)
        ),
        "day_avg_chars": round(statistics.mean(len(d["content"]) for d in days)),
        "questions_about_text_beyond_the_4000_char_head": sum(
            1 for q in all_qs if not q["in_head"]
        ),
        "summary_could_answer": retained,
        "corpora": {},
    }
    for name, rows in results.items():

        def hit(sub: list[dict[str, Any]], k: int) -> int:
            return sum(1 for r in sub if r["rank"] is not None and r["rank"] <= k)

        head = [r for r in rows if r["in_head"]]
        tail = [r for r in rows if not r["in_head"]]
        mrr = (
            statistics.mean((1 / r["rank"]) if r["rank"] else 0.0 for r in rows)
            if rows
            else 0.0
        )
        report["corpora"][name] = {
            "hit@1": f"{hit(rows, 1)}/{len(rows)}",
            "hit@5": f"{hit(rows, 5)}/{len(rows)}",
            "hit@5_95ci": [round(x, 2) for x in wilson(hit(rows, 5), len(rows))],
            "mrr": round(mrr, 3),
            "hit@5_question_in_first_4000_chars": f"{hit(head, 5)}/{len(head)}",
            "hit@5_question_beyond_4000_chars": f"{hit(tail, 5)}/{len(tail)}",
        }
    (out / f"report{args.tag}.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False)
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--seed", type=int, default=3)
    parser.add_argument("--llm-url", default="http://127.0.0.1:14848")
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--embedder",
        default=None,
        help="fastembed model id to use instead of SOUL's default (questions are cached)",
    )
    parser.add_argument("--tag", default="", help="suffix for the report file name")
    started = time.time()
    code = asyncio.run(main_async(parser.parse_args()))
    print(f"took {time.time() - started:.0f}s")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
