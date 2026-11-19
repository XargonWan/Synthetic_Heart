"""Bring the legacy diary and ``memories`` table into SOUL.

The persona's diary (``ai_diary`` and ``ai_diary_archive``) and the ``memories``
table were only ever reachable through keyword search; SOUL, which ranks by meaning
and is the store everything is being consolidated into, never saw them. This module
reads those tables and files them through :class:`core.soul.service.SoulService`,
so every row becomes a recallable cell with a stable ``source_key``.

It is a library, not a one-off script, so it can run from a startup migration, a
CLI, or a test, and it is safe to run again: a row already filed is left alone.

What is deliberately NOT carried over:

* ``memories`` rows written by ``grillo_observer`` - raw copies of chat lines that
  ``chat_history_cache`` already holds and SOUL's compiler already reads.
* Archive rows of a day that has a compaction summary or a live diary row - the
  summary (or the consolidated row) stands for the day, and adding its raw
  fragments as well would put the same events in twice.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.logging_utils import log_info, log_warning

from .service import SoulService

#: ``memories.author`` / ``memories.source`` values that are chat copies.
_SKIPPED_SOURCES = frozenset({"grillo_observer"})


@dataclass(slots=True)
class IngestReport:
    """Counts per outcome and per origin, for logging and for the dry run."""

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    by_origin: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def note(self, origin: str, status: str) -> None:
        self.by_origin[origin] = self.by_origin.get(origin, 0) + 1
        if status == "created":
            self.created += 1
        elif status == "updated":
            self.updated += 1
        else:
            self.unchanged += 1


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def diary_text(row: dict[str, Any]) -> str:
    """One readable first-person text out of a diary row's text columns.

    A row carries up to three overlapping texts. They are joined in the order a
    reader wants them (what happened, then what the persona thought, then the
    entry) with duplicates dropped, since the entry often repeats the summary.
    """
    parts: list[str] = []
    for column in ("interaction_summary", "personal_thought", "content"):
        text = " ".join(str(row.get(column) or "").split())
        if text and not any(text in earlier or earlier in text for earlier in parts):
            parts.append(text)
    return " ".join(parts)


def diary_emotion(row: dict[str, Any]) -> tuple[str, float]:
    """The strongest emotion recorded on a diary row, intensity scaled to 0-1."""
    raw = row.get("emotions")
    try:
        items = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except (TypeError, ValueError):
        return "neutral", 0.0
    best_name, best = "neutral", 0.0
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            level = float(item.get("intensity") or 0.0)
        except (TypeError, ValueError):
            continue
        if level > best:
            best_name, best = str(item.get("type") or "neutral"), level
    return best_name, max(0.0, min(1.0, best / 10.0))


async def _fetch(
    cursor: Any, table: str, columns: tuple[str, ...], tail: str
) -> list[dict[str, Any]]:
    """Rows of ``table`` oldest first, keyed by column name.

    The compat cursor has no ``description``, so the caller names the columns.
    """
    await cursor.execute(
        f"SELECT {', '.join(columns)} FROM {table} ORDER BY created_at ASC{tail}"
    )
    rows = await cursor.fetchall()
    return [dict(zip(columns, row)) for row in rows]


async def ingest_legacy(
    service: SoulService,
    cursor: Any,
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> IngestReport:
    """File the diary, its archive and the useful ``memories`` rows into SOUL.

    Args:
        service: The SOUL service to file through.
        cursor: A DB cursor on the application database (the one holding the
            legacy tables).
        dry_run: Count what would be filed without writing anything.
        limit: Stop after this many rows per origin (for trial runs).
    """
    report = IngestReport()
    tail = f" LIMIT {int(limit)}" if limit else ""

    async def _file(origin: str, **kwargs: Any) -> None:
        if dry_run:
            report.note(origin, "created")
            return
        # Long text is filed as blocks, each searchable by its own content.
        results = await service.ingest_chunked(**kwargs)
        if not results:
            report.skip(f"{origin}: empty")
            return
        for result in results:
            report.note(origin, result.status)

    # 1. memories: compaction summaries and voice memories (never observer copies).
    memories = await _fetch(
        cursor,
        "memories",
        ("id", "content", "author", "source", "created_at", "emotion", "intensity"),
        tail,
    )
    covered_days: set[str] = set()
    for row in memories:
        if str(row.get("source") or "") in _SKIPPED_SOURCES or str(
            row.get("author") or ""
        ) in {"observer"}:
            report.skip("memories: observer copy")
            continue
        when = _as_utc(row["created_at"])
        is_summary = str(row.get("source") or "") == "compaction"
        if is_summary:
            covered_days.add(when.date().isoformat())
        try:
            intensity = float(row.get("intensity") or 0.0)
        except (TypeError, ValueError):
            intensity = 0.0
        await _file(
            "memories:compaction" if is_summary else "memories:other",
            kind="diary_day" if is_summary else "legacy_memory",
            source_key=f"memories:{row['id']}",
            text=str(row.get("content") or ""),
            event_timestamp=when,
            emotion=str(row.get("emotion") or "neutral") or "neutral",
            intensity=max(
                0.0, min(1.0, intensity / 10.0 if intensity > 1 else intensity)
            ),
        )

    # 2. the live diary.
    diary_columns = (
        "id",
        "content",
        "personal_thought",
        "emotions",
        "interaction_summary",
        "created_at",
    )
    live_days: set[str] = set()
    for row in await _fetch(cursor, "ai_diary", diary_columns, tail):
        when = _as_utc(row["created_at"])
        live_days.add(when.date().isoformat())
        emotion, intensity = diary_emotion(row)
        await _file(
            "ai_diary",
            kind="journal",
            source_key=f"ai_diary:{row['id']}",
            text=diary_text(row),
            event_timestamp=when,
            emotion=emotion,
            intensity=intensity,
        )

    # 3. the archive. Its rows are the fragments the consolidator folded into a day
    # and the originals of compacted days: one cell per day, not one per fragment,
    # so a recall is not filled with five slices of the same afternoon.
    per_day: dict[str, dict[str, Any]] = {}
    for row in await _fetch(cursor, "ai_diary_archive", diary_columns, tail):
        when = _as_utc(row["created_at"])
        day = when.date().isoformat()
        if day in covered_days:
            report.skip("ai_diary_archive: day has a compaction summary")
            continue
        if day in live_days:
            # The consolidator folds a day's fragments into one live row and
            # archives the originals; filing them again would recall the same
            # events twice.
            report.skip("ai_diary_archive: day is already in the live diary")
            continue
        bucket = per_day.setdefault(
            day, {"when": when, "texts": [], "emotion": "neutral", "intensity": 0.0}
        )
        text = diary_text(row)
        if text and text not in bucket["texts"]:
            bucket["texts"].append(text)
        emotion, intensity = diary_emotion(row)
        if intensity > bucket["intensity"]:
            bucket["emotion"], bucket["intensity"] = emotion, intensity
    for day, bucket in sorted(per_day.items()):
        await _file(
            "ai_diary_archive",
            kind="journal",
            source_key=f"ai_diary_archive:day:{day}",
            text=" --- ".join(bucket["texts"]),
            event_timestamp=bucket["when"],
            emotion=bucket["emotion"],
            intensity=bucket["intensity"],
        )

    log_info(
        "[soul_ingest] legacy ingest "
        f"{'(dry run) ' if dry_run else ''}created={report.created} "
        f"updated={report.updated} unchanged={report.unchanged} "
        f"skipped={report.skipped} origins={report.by_origin}"
    )
    if report.skipped.get("memories: observer copy"):
        log_warning(
            "[soul_ingest] observer copies were not carried over "
            f"({report.skipped['memories: observer copy']}): chat_history_cache and "
            "SOUL's compiler already hold that conversation"
        )
    return report
