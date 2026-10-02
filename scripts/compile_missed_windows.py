#!/usr/bin/env python3
"""Compile the turns that never became MemCells, straight from the cache.

Trigger: the SOUL plugin compiles a session from an IN-MEMORY buffer
(``SoulPlugin._compile_interface`` reads ``self._buffers``), so a turn only
becomes a memory if it is still buffered when the idle compile fires. A restart,
a crash or a broken s2s channel drops the buffer, and the turn then survives only
in ``chat_history_cache``: the chat holds the words, the store holds no cell, and
recall can never serve it however well the ranking is tuned. Measured on
2026-09-22 in the 2D deployment: the family group's 09:04-12:00 turns (the
daughter telling her mother in her own voice that she wants to marry, the
morning-cup picture, the kitchen exchange) sat in the cache with zero cells
against them, while the same window had compiled normally in the 2B deployment.

``SoulCompiler.post_session_compile`` takes a transcript, so the repair is to
rebuild one from the cache and run the SAME extractor the live path runs, with
the same speaker labelling and the same roleplay filter. Nothing is invented:
every line comes from a stored message, in the order it was sent.

Usage (from the repo root, so the plugin resolves the live store):

    ./.venv/Scripts/python.exe scripts/compile_missed_windows.py \
        --interface telegram_bot/-5293915984 --day 2026-09-22
    ./.venv/Scripts/python.exe scripts/compile_missed_windows.py \
        --interface telegram_bot/-5293915984 --day 2026-09-22 --apply

The preview is the default and writes nothing. Only gaps are filled: a burst that
already has a cell against it is skipped, so a re-run cannot store the same
conversation twice. ``--all-interfaces`` walks every chat with messages in the
window; ``--recompile-thin`` also rebuilds a burst whose message count is out of
proportion with the cells it produced (ratio via ``--thin-ratio``, default 12).

Run it while the instance is idle or stopped. It talks to the live store through
the plugin's own repository, so the cells land exactly where the live compile
would have put them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config_manager import config_registry  # noqa: E402
from core.db import get_conn_ctx  # noqa: E402
from core.soul.roleplay import strip_roleplay_lines  # noqa: E402
from plugins.soul_plugin.soul_plugin import SoulPlugin  # noqa: E402

# A burst ends when the chat goes quiet for this long, or when it grows past the
# per-transcript cap the live buffer compile works within.
BURST_GAP = timedelta(minutes=20)
MAX_BURST_MESSAGES = 80
# A cell within this many minutes of a burst counts as covering it.
CELL_NEIGHBOURHOOD = timedelta(minutes=10)

Row = tuple[Any, ...]


def _house_zone() -> ZoneInfo:
    """The household's zone, so ``--day`` means the family's calendar day."""

    try:
        name = str(config_registry.get_value("TZ", "UTC"))
    except Exception:
        name = "UTC"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


async def _prime_cortex_config() -> str:
    """Load the Cortex keys the extractor resolves; return the DSP-scope engine.

    A declared config var reads as its registered DEFAULT until the boot sweep
    runs, and the sweep does not populate the Cortex scope keys in a standalone
    process (measured: ``BASE_CORTEX`` reads empty here while the store holds
    ``Venice2``). The extractor then falls back to the RULE-BASED path, which
    stores the transcript verbatim instead of distilled knowledge — cells recall
    skips. So set the scope keys explicitly from the store, then resolve the
    engine the same way the extractor does, and let a failure stop the run
    rather than write transcript cells.
    """

    import core.config  # noqa: F401  (declares the scope keys in the registry)
    from core.external_endpoints.registry import get_external_endpoint_registry

    # The engine the store points at is an EXTERNAL endpoint: it exists in the
    # CortexRegistry only after the app's startup has registered it, so a script
    # has to do the same step or the extractor silently falls back to the
    # rule-based path (measured: 'could not load Cortex engine Venice2: Unknown
    # engine').
    await get_external_endpoint_registry().register_all_enabled()

    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT config_key, value FROM config "
                "WHERE config_key LIKE '%%CORTEX%%' AND value IS NOT NULL"
            )
            rows = await cur.fetchall()
    for key, value in rows:
        try:
            await config_registry.set_value(str(key), str(value))
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[compile] could not set {key}: {exc}")

    from core.config import get_active_cortex_engine

    return await get_active_cortex_engine(scope="dsp")


async def _chat_interfaces(start_utc: datetime, end_utc: datetime) -> list[str]:
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT interface_path, count(*) AS msgs
                FROM chat_history_cache
                WHERE created_at >= %s
                  AND created_at < %s
                  AND interface_path IS NOT NULL
                  AND interface_path NOT LIKE 'vessel/%%'
                  AND interface_path NOT LIKE 'grillo/%%'
                GROUP BY 1
                ORDER BY 2 DESC
                """,
                (start_utc, end_utc),
            )
            return [str(r[0]) for r in await cur.fetchall()]


async def _messages(
    interface_path: str, start_utc: datetime, end_utc: datetime
) -> list[Row]:
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT sender_name, sender_id, message_text, created_at
                FROM chat_history_cache
                WHERE interface_path = %s
                  AND created_at >= %s
                  AND created_at < %s
                ORDER BY created_at ASC
                """,
                (interface_path, start_utc, end_utc),
            )
            return list(await cur.fetchall())


async def _cells_between(
    session_id: str, start_utc: datetime, end_utc: datetime
) -> list[str]:
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id FROM mem_cells
                WHERE session_id = %s
                  AND event_timestamp >= %s
                  AND event_timestamp < %s
                ORDER BY event_timestamp
                """,
                (session_id, start_utc, end_utc),
            )
            return [str(r[0]) for r in await cur.fetchall()]


def _bursts(rows: list[Row]) -> list[list[Row]]:
    bursts: list[list[Row]] = []
    current: list[Row] = []
    for row in rows:
        stamp = row[3]
        if (
            current
            and stamp
            and current[-1][3]
            and (stamp - current[-1][3]) > BURST_GAP
        ):
            bursts.append(current)
            current = []
        current.append(row)
        if len(current) >= MAX_BURST_MESSAGES:
            bursts.append(current)
            current = []
    if current:
        bursts.append(current)
    return bursts


def _transcript(plugin: SoulPlugin, rows: list[Row]) -> str:
    """The live transcript format: ``[iso] speaker: "text"``, roleplay stripped."""

    parts: list[str] = []
    for row in rows:
        if not row[2]:
            continue
        speaker = plugin._transcript_speaker_label(str(row[0] or row[1] or "user"))
        text = " ".join(str(row[2]).split())
        prefix = f"[{row[3].isoformat()}] " if row[3] else ""
        parts.append(f"{prefix}{speaker}: {json.dumps(text, ensure_ascii=False)}")
    return strip_roleplay_lines("\n".join(parts))


async def _cell_traces(cell_ids: list[str]) -> dict[str, str]:
    if not cell_ids:
        return {}
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, episodic_trace FROM mem_cells WHERE id = ANY(%s)",
                (cell_ids,),
            )
            return {str(r[0]): str(r[1] or "") for r in await cur.fetchall()}


def _looks_like_raw_transcript(trace: str) -> bool:
    """True when the extractor stored a transcript line instead of a memory.

    The rule-based fallback copies the conversation verbatim, so its output
    starts with the transcript's own ``[ISO timestamp]`` prefix. Distilled
    knowledge never does. The LLM strategy falls back silently on any failure
    and still reports ``distils_content``, so the shape of the text is the only
    honest test that the memory is really a memory.
    """

    return bool(re.match(r"^\[20\d\d-\d\d-\d\dT", trace.strip()))


async def _refile(session_id: str, cell_ids: list[str], burst: list[Row]) -> int:
    """Move cells the extractor dated 'now' onto the burst's own clock.

    The extractor dates a cell from the conversation when it can and from the
    compile moment when it cannot, which files a replayed day under today. Each
    cell is re-filed against the burst message its text overlaps most, so the
    memory keeps the day and the hour it belongs to.

    Only ``event_timestamp`` moves. The id embeds the compile time and cannot be
    rewritten in place: ``mem_cell_vectors.mem_cell_id`` is ON DELETE CASCADE
    with NO ACTION on update, so the child cannot follow the parent and the
    parent cannot move under the child (measured: ForeignKeyViolationError). The
    id is a key, not a clock — recall, the curator and the day filters all read
    ``event_timestamp``.
    """

    if not cell_ids:
        return 0
    traces = await _cell_traces(cell_ids)
    moved = 0
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            for cell_id in cell_ids:
                words = set(re.findall(r"[a-z']+", traces.get(cell_id, "").lower()))
                best: datetime | None = None
                best_score = -1.0
                for row in burst:
                    if not row[3]:
                        continue
                    message = set(re.findall(r"[a-z']+", str(row[2] or "").lower()))
                    score = (
                        len(words & message) / max(len(message), 1) if message else 0.0
                    )
                    if score > best_score:
                        best_score, best = score, row[3]
                if best is None:
                    continue
                await cur.execute(
                    "UPDATE mem_cells SET event_timestamp = %s WHERE id = %s",
                    (best, cell_id),
                )
                moved += 1
    return moved


async def _run(args: argparse.Namespace) -> int:
    # A declared config var reads as its registered DEFAULT until the boot sweep
    # runs, and the extractor's model comes from that config: load it first.
    await cast(Any, config_registry).load_all_from_db(force=True)
    try:
        engine = await _prime_cortex_config()
    except Exception as exc:
        print(
            "[compile] the DSP-scope Cortex engine did not resolve "
            f"({exc!r}); refusing to run, because the extractor would fall back "
            "to the rule-based path and store raw transcripts instead of "
            "memories."
        )
        return 2
    print(f"[compile] DSP-scope cortex: {engine}")

    plugin = SoulPlugin()
    compiler = plugin._compiler
    zone = _house_zone()
    day = date.fromisoformat(args.day)
    start_local = datetime.combine(day, time.min, tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    interfaces = (
        [args.interface]
        if args.interface
        else await _chat_interfaces(start_utc, end_utc)
    )

    print(
        f"[compile] {day.isoformat()} in {zone.key} "
        f"({start_utc.isoformat()} .. {end_utc.isoformat()} UTC), "
        f"{'APPLY' if args.apply else 'preview'}"
    )

    planned = 0
    created_total = 0
    for interface_path in interfaces:
        session_id = plugin._normalize_session_id(interface_path)
        rows = await _messages(interface_path, start_utc, end_utc)
        if not rows:
            continue
        print(f"\n[compile] {interface_path} -> {session_id} ({len(rows)} messages)")
        for burst in _bursts(rows):
            first, last = burst[0][3], burst[-1][3]
            covered = await _cells_between(
                session_id,
                first - CELL_NEIGHBOURHOOD,
                last + CELL_NEIGHBOURHOOD,
            )
            if covered and not (
                args.recompile_thin
                and len(burst) / max(len(covered), 1) >= args.thin_ratio
            ):
                print(
                    f"  covered  {first.astimezone(zone):%H:%M}-{last.astimezone(zone):%H:%M} "
                    f"{len(burst):3d} msg -> {len(covered)} cell(s), skipped"
                )
                continue

            transcript = _transcript(plugin, burst)
            label = (
                f"{first.astimezone(zone):%H:%M}-{last.astimezone(zone):%H:%M} "
                f"{len(burst):3d} msg -> {len(covered)} cell(s)"
            )
            if not transcript.strip():
                print(f"  empty    {label}, nothing to compile")
                continue

            planned += 1
            print(f"  MISSING  {label}, transcript {len(transcript)} chars")
            if not args.apply:
                sample = transcript.splitlines()[:2]
                for line in sample:
                    print(f"           | {line[:150]}")
                continue

            before = set(await _cells_between(session_id, start_utc, end_utc))
            created = await compiler.post_session_compile(
                current_date=day,
                transcript=transcript,
                session_id=session_id,
            )
            traces = await _cell_traces(created)
            if any(_looks_like_raw_transcript(t) for t in traces.values()):
                removed = await plugin._repo.delete_memcells(created)
                print(
                    f"           ! the extractor fell back to the rule-based path "
                    f"(a transcript line, not a memory): removed {removed} cell(s) "
                    f"and stopped, so nothing half-written is left behind."
                )
                return 3
            moved = await _refile(session_id, created, burst)
            created_total += len(created)
            for cell_id in created:
                overwrote = cell_id in before
                print(
                    f"           + {cell_id}"
                    f"{' (OVERWROTE an existing cell)' if overwrote else ''}"
                )
            if moved:
                print(f"           re-filed {moved} cell(s) onto the burst's own clock")
            if not created:
                print("           (extractor returned nothing)")

    print(
        f"\n[compile] bursts to compile: {planned}; "
        f"cells created: {created_total}"
        + ("" if args.apply else " (preview only, nothing written)")
    )
    if not args.apply and planned:
        print("[compile] re-run with --apply to write these cells.")
    return 0


def _parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--day",
        required=True,
        help="household-local calendar day, e.g. 2026-09-22",
    )
    parser.add_argument(
        "--interface",
        help="chat path as stored, e.g. telegram_bot/-5293915984 "
        "(default: every chat with messages that day)",
    )
    parser.add_argument(
        "--all-interfaces",
        action="store_true",
        help="explicit form of the default: walk every chat with messages",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the cells (default is a preview that writes nothing)",
    )
    parser.add_argument(
        "--recompile-thin",
        action="store_true",
        help="also rebuild bursts whose messages per cell exceed --thin-ratio",
    )
    parser.add_argument(
        "--thin-ratio",
        type=float,
        default=12.0,
        help="messages-per-cell above which a burst counts as thin (default 12)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run(_parse())))
