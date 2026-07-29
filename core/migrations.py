# core/migrations.py
"""One-shot, idempotent schema migrations run at startup on every deploy.

These migrations are designed to run automatically on *every* user
installation when a new version boots. Each migration must be:

- **Idempotent** — safe to run repeatedly; a no-op once already applied.
- **Backend-aware** — work on both Postgres and MariaDB.
- **Safe** — never drop data without first taking a verified backup.

Migrations are invoked from ``core.db.ensure_plugin_tables`` so they run
during normal startup auto-heal, before any plugin touches the schema.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.logging_utils import log_error, log_info, log_warning


def _backups_dir() -> Path:
    """Resolve the backups directory (shared with ``core.db_backup``)."""
    backups_dir = Path(os.environ.get("SYNTH_BACKUPS_DIR", "backups")).expanduser()
    backups_dir.mkdir(parents=True, exist_ok=True)
    return backups_dir


async def _table_exists(cur: Any, table: str, db_type: str) -> bool:
    """Return True if ``table`` exists in the current database."""
    if db_type == "postgres":
        await cur.execute(
            "SELECT to_regclass(%s) IS NOT NULL",
            (f"public.{table}",),
        )
    else:
        await cur.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = %s",
            (table,),
        )
    row = await cur.fetchone()
    if row is None:
        return False
    # Row may be a dict (DictCursor) or a tuple depending on backend.
    if isinstance(row, dict):
        value = next(iter(row.values()))
    else:
        value = row[0]
    return bool(value)


def _sql_quote(value: Any) -> str:
    """Render a Python value as a portable SQL literal for the backup dump."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (datetime,)):
        return "'" + value.isoformat() + "'"
    # Everything else: coerce to str and single-quote-escape.
    text = str(value).replace("'", "''")
    return "'" + text + "'"


async def _dump_table_to_sql(
    cur: Any,
    table: str,
    columns: list[str],
    out_path: Path,
) -> int:
    """Write a portable INSERT-based dump of ``table`` to ``out_path``.

    Returns the number of data rows written. Does not depend on external
    tools (``pg_dump``/``mysqldump``) so it works on any deploy.
    """
    col_list = ", ".join(columns)
    await cur.execute(f"SELECT {col_list} FROM {table}")  # noqa: S608 - table/cols are internal constants
    rows = await cur.fetchall()

    written = 0
    with out_path.open("w", encoding="utf-8") as fh:
        fh.write(f"-- SyntH legacy backup of `{table}`\n")
        fh.write(f"-- generated {datetime.now(timezone.utc).isoformat()}\n")
        fh.write(f"-- columns: {col_list}\n\n")
        for row in rows:
            if isinstance(row, dict):
                values = [row[c] for c in columns]
            else:
                values = list(row)
            literals = ", ".join(_sql_quote(v) for v in values)
            fh.write(f"INSERT INTO {table} ({col_list}) VALUES ({literals});\n")
            written += 1
    return written


async def _drop_legacy_recent_chats() -> None:
    """Backup + verify + drop the legacy ``recent_chats`` table.

    Superseded by ``interface_paths`` (see ``core.interface_paths``). This
    migration takes a verified backup into the backups directory and only
    drops the table once the row count of the dump matches the live table.
    Idempotent: a no-op if the table is already gone.
    """
    from core.db import _get_db_type, get_conn_ctx

    table = "recent_chats"
    columns = ["chat_id", "last_active", "metadata", "created_at"]
    db_type = _get_db_type()

    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            if not await _table_exists(cur, table, db_type):
                # Already migrated / fresh install — nothing to do.
                return

            # Count live rows before dumping.
            await cur.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
            count_row = await cur.fetchone()
            if isinstance(count_row, dict):
                live_count = int(str(next(iter(count_row.values()))))
            else:
                live_count = int(count_row[0]) if count_row else 0

            # 1) BACKUP -----------------------------------------------------
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            out_path = _backups_dir() / f"{table}_backup_{ts}.sql"
            try:
                written = await _dump_table_to_sql(cur, table, columns, out_path)
            except Exception as dump_err:
                log_error(
                    f"[migrations] Backup of `{table}` failed — NOT dropping: {dump_err}",
                    dump_err,
                )
                return

            # 2) VERIFY -----------------------------------------------------
            if written != live_count:
                log_error(
                    f"[migrations] Backup verification FAILED for `{table}` "
                    f"(dumped {written} rows, table has {live_count}). NOT dropping."
                )
                return
            if not out_path.exists() or out_path.stat().st_size == 0:
                log_error(
                    f"[migrations] Backup file missing/empty for `{table}` "
                    f"({out_path}). NOT dropping."
                )
                return

            log_info(
                f"[migrations] Verified backup of `{table}`: "
                f"{written} rows -> {out_path}"
            )

            # 3) DROP -------------------------------------------------------
            try:
                await cur.execute(f"DROP TABLE IF EXISTS {table}")
                try:
                    await conn.commit()
                except Exception:
                    pass
                log_info(
                    f"[migrations] Dropped legacy table `{table}` (backup retained)"
                )
            except Exception as drop_err:
                log_error(
                    f"[migrations] Failed to drop `{table}` after backup: {drop_err}",
                    drop_err,
                )


async def _column_exists(cur: Any, table: str, column: str, db_type: str) -> bool:
    """Return True if ``column`` exists on ``table`` in the current database."""
    if db_type == "postgres":
        await cur.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
            (table, column),
        )
    else:
        await cur.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
            (table, column),
        )
    row = await cur.fetchone()
    if row is None:
        return False
    value = next(iter(row.values())) if isinstance(row, dict) else row[0]
    return bool(value)


async def _rename_timestamp_columns() -> None:
    """Rename the reserved-word ``timestamp`` DB column to ``created_at``.

    On a fresh Postgres install the ORM auto-translates a bare ``timestamp``
    column to ``timestamptz``, producing an invalid schema that breaks SyntH.
    This migration renames the legacy ``timestamp`` column to ``created_at``
    on existing MariaDB/Postgres installs so the new DDL matches at runtime.

    ``mem_cells`` is special: its event-time column is renamed to
    ``event_timestamp`` (it already has a distinct ``created_at`` row-creation
    column). Idempotent: a no-op once already applied.
    """
    from core.db import _get_db_type, get_conn_ctx

    # (table, old_column, new_column, column_type_for_mariadb)
    renames: list[tuple[str, str, str, str]] = [
        ("chat_history_cache", "timestamp", "created_at", "DATETIME"),
        ("ai_diary", "timestamp", "created_at", "DATETIME"),
        ("ai_diary_archive", "timestamp", "created_at", "DATETIME"),
        ("memories", "timestamp", "created_at", "DATETIME"),
        ("emotion_state", "timestamp", "created_at", "DATETIME"),
        ("emotion_diary", "timestamp", "created_at", "DATETIME"),
        ("message_map", "timestamp", "created_at", "REAL"),
        ("radio_activity_log", "timestamp", "created_at", "DATETIME"),
        ("mem_cells", "timestamp", "event_timestamp", "TIMESTAMPTZ"),
    ]
    # Index renames keyed by table (old index name -> new index name).
    index_renames: dict[str, tuple[str, str]] = {
        "chat_history_cache": ("idx_timestamp", "idx_created_at"),
        "ai_diary": ("idx_timestamp", "idx_created_at"),
        "ai_diary_archive": ("idx_timestamp", "idx_created_at"),
        "memories": ("idx_timestamp", "idx_created_at"),
        "emotion_state": ("idx_timestamp", "idx_created_at"),
        "emotion_diary": ("idx_timestamp", "idx_created_at"),
        "radio_activity_log": ("idx_radio_timestamp", "idx_radio_created_at"),
        "mem_cells": ("idx_mem_cells_timestamp", "idx_mem_cells_event_timestamp"),
    }

    db_type = _get_db_type()
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            for table, old_col, new_col, col_type in renames:
                if not await _table_exists(cur, table, db_type):
                    continue
                if not await _column_exists(cur, table, old_col, db_type):
                    continue
                if await _column_exists(cur, table, new_col, db_type):
                    # Both columns present (partial migration) — leave as-is to
                    # avoid data loss; the new code path uses ``new_col``.
                    log_warning(
                        f"[migrations] `{table}` has both `{old_col}` and "
                        f"`{new_col}`; skipping rename to avoid data loss."
                    )
                    continue
                try:
                    if db_type == "postgres":
                        await cur.execute(
                            f'ALTER TABLE "{table}" '
                            f'RENAME COLUMN "{old_col}" TO "{new_col}"'
                        )
                    else:
                        await cur.execute(
                            f"ALTER TABLE `{table}` "
                            f"CHANGE `{old_col}` `{new_col}` {col_type}"
                        )
                    log_info(
                        f"[migrations] Renamed `{table}.{old_col}` -> "
                        f"`{table}.{new_col}`"
                    )
                except Exception as exc:
                    log_error(
                        f"[migrations] Failed to rename `{table}.{old_col}`: {exc}",
                        exc,
                    )

            # Rename stale indexes that still reference the old column name.
            for table, (old_idx, new_idx) in index_renames.items():
                if not await _table_exists(cur, table, db_type):
                    continue
                try:
                    if db_type == "postgres":
                        await cur.execute(
                            f'ALTER INDEX IF EXISTS "{old_idx}" RENAME TO "{new_idx}"'
                        )
                    else:
                        await cur.execute(
                            f"ALTER TABLE `{table}` "
                            f"RENAME INDEX `{old_idx}` TO `{new_idx}`"
                        )
                except Exception as exc:
                    # Index may not exist (e.g. never created) — non-fatal.
                    log_warning(
                        f"[migrations] Index rename `{old_idx}` -> "
                        f"`{new_idx}` skipped: {exc}"
                    )

            try:
                await conn.commit()
            except Exception:
                pass


def _dedup_text_segments(text: str | None, separator: str) -> str | None:
    """Drop duplicate ``separator``-joined segments from ``text`` (normalised compare).

    Returns the de-duplicated string, or the original value when nothing changed
    / there is nothing to dedup. Normalisation is lowercase + collapsed
    whitespace — structural only, no keyword or phrase matching.
    """
    if not text or separator not in text:
        return text
    seen: set[str] = set()
    kept: list[str] = []
    for seg in text.split(separator):
        norm = " ".join(seg.split()).lower()
        if not norm:
            # Preserve genuinely empty segments verbatim (rare) to avoid altering
            # spacing when there is nothing to dedup.
            kept.append(seg)
            continue
        if norm in seen:
            continue
        seen.add(norm)
        kept.append(seg)
    return separator.join(kept)


async def _dedup_diary_segments() -> None:
    """Retroactively de-duplicate repeated segments in existing ``ai_diary`` rows.

    The daily upsert concatenates every entry into one row per day. Before the
    insert-time dedup was added, an LLM re-emitting the same content/summary/
    thought/user_message made rows accumulate identical fragments. This one-shot
    migration rewrites each row with duplicate segments removed.

    Idempotent (a second run finds nothing to change) and best-effort: any row
    that fails is skipped without aborting the batch. ``content`` and
    ``personal_thought`` are split on the ``\\n\\n---\\n\\n`` separator;
    ``interaction_summary`` and ``user_message`` on ``\\n---\\n``.
    """
    from core.db import _get_db_type, get_conn_ctx

    _SEP_BLOCK = "\n\n---\n\n"
    _SEP_LINE = "\n---\n"
    # (column, separator)
    fields: list[tuple[str, str]] = [
        ("content", _SEP_BLOCK),
        ("personal_thought", _SEP_BLOCK),
        ("interaction_summary", _SEP_LINE),
        ("user_message", _SEP_LINE),
    ]

    db_type = _get_db_type()
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            if not await _table_exists(cur, "ai_diary", db_type):
                return
            await cur.execute(
                "SELECT id, content, personal_thought, interaction_summary, "
                "user_message FROM ai_diary"
            )
            rows = await cur.fetchall()
            changed = 0
            for row in rows or []:
                if isinstance(row, dict):
                    row_id = row.get("id")
                    values = {col: row.get(col) for col, _sep in fields}
                else:
                    row_id = row[0]
                    values = {
                        "content": row[1],
                        "personal_thought": row[2],
                        "interaction_summary": row[3],
                        "user_message": row[4],
                    }
                updates: dict[str, str | None] = {}
                for col, sep in fields:
                    original = values.get(col)
                    deduped = _dedup_text_segments(original, sep)
                    if deduped != original:
                        updates[col] = deduped
                if not updates:
                    continue
                set_clause = ", ".join(f"{col}=%s" for col in updates)
                params = list(updates.values()) + [row_id]
                try:
                    await cur.execute(
                        f"UPDATE ai_diary SET {set_clause} WHERE id=%s",  # noqa: S608
                        tuple(params),
                    )
                    changed += 1
                except Exception as exc:
                    log_warning(
                        f"[migrations] diary dedup skipped row id={row_id}: {exc}"
                    )
            try:
                await conn.commit()
            except Exception:
                pass
            if changed:
                log_info(
                    f"[migrations] De-duplicated segments in {changed} ai_diary row(s)"
                )


# Registry of startup migrations, applied in order. Each entry is
# (name, coroutine-callable). Add new one-shot migrations here.
_STARTUP_MIGRATIONS: list[tuple[str, Any]] = [
    ("drop_legacy_recent_chats", _drop_legacy_recent_chats),
    ("rename_timestamp_columns", _rename_timestamp_columns),
    ("dedup_diary_segments", _dedup_diary_segments),
]


async def run_startup_migrations() -> None:
    """Run all registered startup migrations (idempotent, best-effort)."""
    for name, fn in _STARTUP_MIGRATIONS:
        try:
            await fn()
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"[migrations] Startup migration '{name}' failed: {exc}")


__all__ = ["run_startup_migrations"]
