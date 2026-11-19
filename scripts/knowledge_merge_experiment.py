#!/usr/bin/env python3
"""Would merging memory cells by topic keep what matters? A measurement, not a migration.

The idea under test is a "knowledge base" compactor: cells that say the same thing
about one subject (over several days, or from several people) become one cell that
still says who said it and when, with no fact lost. Before building it this script
measures, on a copy of a store, how much there is to merge and whether a model can
merge without losing or inventing anything.

1. Cluster the live ``chat`` cells by cosine similarity of their stored vectors
   (single link, same ``visibility`` only, cluster size capped) and report how many
   cells would fold away at several thresholds.
2. Take a sample of clusters that span more than one day or chat, ask the LLM for one
   merged cell (JSON: summary, facts, provenance), and check it two ways: a
   language-neutral coverage figure (how many of the sources' content words survive)
   and a second LLM call that lists facts lost, changed or invented.
3. Write every result to ``--out``. The aggregate figures contain no conversation
   text; the review file does, so keep ``--out`` outside the repository and delete it
   after reading.

Nothing in the database is written. No word lists are used: tokens are ``\\w{4,}``.

Example:
    . ~/synth-clone/env-b.sh
    uv run python scripts/knowledge_merge_experiment.py --out ~/synth-clone/kb-experiment
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

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import plugins.radio_host.recon_radio_status  # noqa: E402,F401  (registers the radio's sessions)
from core.db import get_conn_ctx  # noqa: E402
from core.soul.service import (  # noqa: E402
    is_housekeeping_session,
    on_demand_session_prefixes,
)

THRESHOLDS = (0.75, 0.80, 0.85, 0.90)
MAX_CLUSTER = 6

MERGE_PROMPT = """You are compacting a long-term memory store. An automatic search thinks the {n} memory cells below are about the same subject. Check that, then summarise them.

1. outliers: list the numbers of cells that are NOT about the same subject as the majority (an unrelated chat, a different event). Be strict.
2. kind, for the cells that remain: "fact" (something true about the world, a person or an event), "preference_or_state", "task_instruction" (a command given to the assistant), "small_talk" (greetings, pleasantries), or "joke_or_figure_of_speech" (not meant literally).
3. summary: ONE summary of what the remaining cells say. Keep every fact, number and name; do not generalise, soften or reinterpret; add nothing that is not in the cells; write in the language of the cells. Do NOT write dates, chat identifiers or who said it: those are added separately by the system.

If fewer than 2 cells remain after removing outliers, answer {{"same_subject": false}} and nothing else.

Answer with JSON only:
{{"same_subject": true, "outliers": [<numbers>], "kind": "<kind>", "summary": "<text>"}}

Cells:
{cells}
"""

JUDGE_PROMPT = """Compare a merged memory against its source cells.

Sources:
{cells}

Merged:
{merged}

List what is wrong with the merge. Answer with JSON only:
{{"unrelated": [<numbers of sources that are not about the same subject as the others>], "lost": ["<fact present in a source and missing from the merge>"], "changed": ["<fact whose meaning, number, name or date differs>"], "invented": ["<statement in the merge that no source supports>"], "verdict": "ok" | "unrelated" | "lossy" | "invented"}}
Use an empty list when there is nothing. verdict is "invented" if anything is invented, else "unrelated" if any source is unrelated, else "lossy" if anything is lost or changed, else "ok".
"""


def tokens(text: str) -> set[str]:
    """Content words of three or more letters, case-folded; no vocabulary involved."""
    return {t.lower() for t in re.findall(r"\w{4,}", text)}


def ask_llm(url: str, prompt: str, timeout: float = 900.0) -> str:
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
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except ValueError:
        return None


def chat_label(session_id: str) -> str:
    """A chat label that says the interface, not the person or the id."""
    kind = re.sub(r"[_:]-?\d.*$", "", session_id)
    return kind or "chat"


def is_conversation(cell: dict[str, Any], max_trace_chars: int) -> bool:
    """A distilled memory of a conversation: not system text, not a raw transcript.

    The cells above ``max_trace_chars`` hold a whole transcript (a radio programme, a
    vessel's event log) rather than a memory; they embed as their boilerplate and fall
    into clusters that share a format, not a subject.
    """
    session = cell["session"]
    if is_housekeeping_session(session):
        return False
    if session.lower().startswith(on_demand_session_prefixes()):
        return False
    return len(cell["trace"]) <= max_trace_chars


async def load_cells() -> list[dict[str, Any]]:
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT c.id, c.session_id, c.episodic_trace, c.atomic_facts, "
                "c.visibility, c.event_timestamp, v.embedding::text "
                "FROM mem_cells c JOIN mem_cell_vectors v ON v.mem_cell_id = c.id "
                "WHERE c.source_kind = 'chat' ORDER BY c.event_timestamp"
            )
            rows = await cur.fetchall()
    cells = []
    for row in rows:
        facts = row[3]
        if isinstance(facts, str):
            try:
                facts = json.loads(facts)
            except ValueError:
                facts = []
        cells.append(
            {
                "id": str(row[0]),
                "session": str(row[1]),
                "trace": str(row[2] or ""),
                "facts": [str(f) for f in (facts or [])],
                "visibility": str(row[4]),
                "ts": row[5],
                "vec": np.array(json.loads(row[6]), dtype=np.float32),
            }
        )
    return cells


def cluster(
    cells: list[dict[str, Any]], threshold: float, slack: float = 0.05
) -> list[list[int]]:
    """Complete-link clusters of same-visibility cells.

    Two groups join only when every cross pair is at least ``threshold - slack``
    similar, so a cell cannot be pulled in through a chain of neighbours (the single
    link version let an unrelated cell ride in on one similar pair). Groups are capped
    at ``MAX_CLUSTER`` cells.
    """
    n = len(cells)
    matrix = np.stack([c["vec"] for c in cells])
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
    sims = matrix @ matrix.T
    floor = threshold - slack
    group_of = list(range(n))
    members: dict[int, list[int]] = {i: [i] for i in range(n)}
    pairs = np.argwhere(np.triu(sims >= threshold, k=1))
    order = sorted(pairs.tolist(), key=lambda p: -float(sims[p[0], p[1]]))
    for a, b in order:
        if cells[a]["visibility"] != cells[b]["visibility"]:
            continue
        ga, gb = group_of[a], group_of[b]
        if ga == gb or len(members[ga]) + len(members[gb]) > MAX_CLUSTER:
            continue
        if any(
            sims[i, j] < floor or cells[i]["visibility"] != cells[j]["visibility"]
            for i in members[ga]
            for j in members[gb]
        ):
            continue
        for i in members[gb]:
            group_of[i] = ga
        members[ga].extend(members.pop(gb))
    return [g for g in members.values() if len(g) > 1]


def spans_more_than_one(cells: list[dict[str, Any]], group: list[int]) -> bool:
    days = {cells[i]["ts"].date() for i in group}
    chats = {cells[i]["session"] for i in group}
    return len(days) > 1 or len(chats) > 1


def render_cells(cells: list[dict[str, Any]], group: list[int]) -> str:
    parts = []
    for k, i in enumerate(sorted(group, key=lambda i: cells[i]["ts"]), 1):
        c = cells[i]
        facts = "; ".join(c["facts"])
        parts.append(
            f"[{k}] date: {c['ts'].date()} | chat: {chat_label(c['session'])}\n"
            f"{c['trace']}" + (f"\nfacts: {facts}" if facts else "")
        )
    return "\n\n".join(parts)


def coverage(sources: list[str], merged: str) -> float:
    """Share of the sources' content words that the merged text still contains."""
    wanted = set().union(*(tokens(s) for s in sources)) if sources else set()
    return len(wanted & tokens(merged)) / len(wanted) if wanted else 1.0


def compare_thresholds(cells: list[dict[str, Any]]) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for threshold in THRESHOLDS:
        groups = cluster(cells, threshold)
        folded = sum(len(g) - 1 for g in groups)
        multi = [g for g in groups if spans_more_than_one(cells, g)]
        report[str(threshold)] = {
            "clusters": len(groups),
            "cells_in_clusters": sum(len(g) for g in groups),
            "cells_that_would_fold_away": folded,
            "share_of_cells": round(folded / max(len(cells), 1), 3),
            "clusters_spanning_days_or_chats": len(multi),
            "sizes": {
                str(s): sum(1 for g in groups if len(g) == s)
                for s in sorted({len(g) for g in groups})
            },
        }
    return report


async def main_async(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    everything = await load_cells()
    cells = [c for c in everything if is_conversation(c, args.max_trace_chars)]
    print(
        json.dumps(
            {
                "chat_cells": len(everything),
                "kept_as_conversation": len(cells),
                "left_out": len(everything) - len(cells),
            }
        )
    )
    summary: dict[str, Any] = {
        "cells": len(cells),
        "thresholds": compare_thresholds(cells),
    }
    print(json.dumps(summary, indent=2))

    groups = [
        g for g in cluster(cells, args.threshold) if spans_more_than_one(cells, g)
    ]
    random.Random(args.seed).shuffle(groups)
    sample = groups[: args.sample]
    summary["sampled_at_threshold"] = args.threshold
    summary["clusters_available"] = len(groups)
    (out / "summary.json").write_text(json.dumps(summary, indent=2))

    results: list[dict[str, Any]] = []
    review: list[str] = []
    for number, group in enumerate(sample, 1):
        ordered = sorted(group, key=lambda i: cells[i]["ts"])
        rendered = render_cells(cells, ordered)
        started = time.time()
        row: dict[str, Any] = {"n": number, "size": len(group)}
        try:
            merged = parse_json(
                ask_llm(args.llm_url, MERGE_PROMPT.format(n=len(group), cells=rendered))
            )
        except Exception as exc:  # noqa: BLE001 - recorded, the run goes on
            row["error"] = f"merge: {type(exc).__name__}"
            results.append(row)
            continue
        row["merge_seconds"] = round(time.time() - started, 1)
        if (
            not merged
            or not merged.get("same_subject", True)
            or not merged.get("summary")
        ):
            row["same_subject"] = False
            results.append(row)
            review.append(
                f"## {number}. size {len(group)}: the model said these are NOT one subject\n\n{rendered}\n"
            )
            continue
        row["same_subject"] = True
        outliers = {int(x) for x in (merged.get("outliers") or []) if str(x).isdigit()}
        kept = [i for k, i in enumerate(ordered, 1) if k not in outliers]
        row["outliers_flagged"] = len(ordered) - len(kept)
        row["kind"] = str(merged.get("kind", "?"))
        if len(kept) < 2:
            row["same_subject"] = False
            results.append(row)
            review.append(
                f"## {number}. size {len(group)}: only outliers left\n\n{rendered}\n"
            )
            continue
        rendered = render_cells(cells, kept)
        # Who, when and where is computed from the cells, never written by the model.
        provenance = [
            {
                "when": str(cells[i]["ts"].date()),
                "chat": chat_label(cells[i]["session"]),
            }
            for i in kept
        ]
        text = str(merged["summary"])
        sources = [cells[i]["trace"] + " " + " ".join(cells[i]["facts"]) for i in kept]
        row["coverage"] = round(coverage(sources, text), 3)
        row["length_ratio"] = round(len(text) / max(sum(len(s) for s in sources), 1), 2)
        merged["provenance_computed"] = provenance
        verdict: dict[str, Any] = {}
        try:
            verdict = (
                parse_json(
                    ask_llm(
                        args.llm_url,
                        JUDGE_PROMPT.format(cells=rendered, merged=text),
                    )
                )
                or {}
            )
        except Exception as exc:  # noqa: BLE001
            row["judge_error"] = type(exc).__name__
        row["verdict"] = verdict.get("verdict", "unjudged")
        row["lost"] = len(verdict.get("lost") or [])
        row["changed"] = len(verdict.get("changed") or [])
        row["invented"] = len(verdict.get("invented") or [])
        row["unrelated"] = len(verdict.get("unrelated") or [])
        results.append(row)
        review.append(
            f"## {number}. size {len(group)}, coverage {row['coverage']}, "
            f"verdict {row['verdict']}\n\n### sources\n\n{rendered}\n\n### merged\n\n"
            f"{json.dumps(merged, ensure_ascii=False, indent=2)}\n\n### judge\n\n"
            f"{json.dumps(verdict, ensure_ascii=False, indent=2)}\n"
        )
        (out / "results.json").write_text(json.dumps(results, indent=2))
        (out / "review.md").write_text("\n".join(review))
        print(json.dumps(row), flush=True)

    merged_rows = [r for r in results if r.get("same_subject")]
    aggregate = {
        "sampled": len(sample),
        "model_said_not_one_subject": sum(
            1 for r in results if r.get("same_subject") is False
        ),
        "errors": sum(1 for r in results if "error" in r),
        "clusters_where_outliers_were_flagged": sum(
            1 for r in merged_rows if r.get("outliers_flagged")
        ),
        "kinds": {
            k: sum(1 for r in merged_rows if r.get("kind") == k)
            for k in sorted({r.get("kind") for r in merged_rows})
        },
        "verdicts": {
            v: sum(1 for r in merged_rows if r.get("verdict") == v)
            for v in ("ok", "unrelated", "lossy", "invented", "unjudged")
        },
        "median_coverage": round(
            statistics.median([r["coverage"] for r in merged_rows]), 3
        )
        if merged_rows
        else None,
        "median_length_ratio": round(
            statistics.median([r["length_ratio"] for r in merged_rows]), 2
        )
        if merged_rows
        else None,
    }
    summary["merge_sample"] = aggregate
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    (out / "results.json").write_text(json.dumps(results, indent=2))
    (out / "review.md").write_text("\n".join(review))
    print(json.dumps(aggregate, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="directory outside the repository")
    parser.add_argument("--threshold", type=float, default=0.80)
    parser.add_argument("--sample", type=int, default=24)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--max-trace-chars", type=int, default=3000)
    parser.add_argument("--llm-url", default="http://127.0.0.1:14848")
    parser.add_argument(
        "--stats-only", action="store_true", help="only the cluster statistics"
    )
    args = parser.parse_args()
    if args.stats_only:
        args.sample = 0
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
