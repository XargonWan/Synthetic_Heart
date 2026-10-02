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


async def _migrate_goals_table() -> None:
    """Rename the legacy ``minecraft_goals`` table to the generic ``goals`` table.

    The goal store was extracted from the Minecraft adapter into a standalone,
    scope-aware ``goals`` plugin. This migration renames the legacy table (when
    it exists and ``goals`` does not yet exist) so the existing Minecraft goals
    survive the extraction, then backfills the new scope columns for those rows
    to ``scope='vessel'`` / ``game='minecraft'`` / ``world='none'``.

    The scope columns themselves are added idempotently by the plugin's
    ``init_goal_table`` (``ADD COLUMN IF NOT EXISTS``); this migration only
    handles the *rename* and the one-time *backfill* of the migrated rows.

    Idempotent (a no-op once ``goals`` exists / rows are backfilled) and
    backend-aware.
    """
    from core.db import _get_db_type, get_conn_ctx

    db_type = _get_db_type()
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            has_legacy = await _table_exists(cur, "minecraft_goals", db_type)
            has_goals = await _table_exists(cur, "goals", db_type)

            # Rename only when the legacy table exists and the new one does not.
            if has_legacy and not has_goals:
                try:
                    if db_type == "postgres":
                        await cur.execute(
                            'ALTER TABLE "minecraft_goals" RENAME TO "goals"'
                        )
                    else:
                        await cur.execute(
                            "ALTER TABLE `minecraft_goals` RENAME TO `goals`"
                        )
                    log_info("[migrations] Renamed `minecraft_goals` -> `goals`")
                    has_goals = True
                except Exception as exc:
                    log_error(
                        f"[migrations] Failed to rename `minecraft_goals`: {exc}",
                        exc,
                    )
                    return

            if not has_goals:
                # Fresh install with no legacy table — nothing to migrate.
                return

            # Ensure the scope columns exist so the backfill can run in a single
            # boot even if this migration executes before the plugin's own
            # ``init_goal_table``. ADD COLUMN IF NOT EXISTS is idempotent on both
            # backends in the versions we target.
            for col_ddl in (
                "scope VARCHAR(64) DEFAULT 'none'",
                "game VARCHAR(64) DEFAULT 'none'",
                "world VARCHAR(64) DEFAULT 'none'",
            ):
                try:
                    await cur.execute(
                        f"ALTER TABLE goals ADD COLUMN IF NOT EXISTS {col_ddl}"
                    )
                except Exception as col_exc:  # pragma: no cover - defensive
                    log_warning(
                        f"[migrations] goals column add skipped ({col_ddl}): {col_exc}"
                    )

            # Backfill scope columns for rows that migrated from the Minecraft
            # adapter. Only touches rows still on the default scope tuple.
            if not await _column_exists(cur, "goals", "scope", db_type):
                return
            try:
                await cur.execute(
                    "UPDATE goals SET scope = %s, game = %s, world = %s "
                    "WHERE scope = %s AND game = %s AND world = %s",
                    (
                        "vessel",
                        "minecraft",
                        "none",
                        "none",
                        "none",
                        "none",
                    ),
                )
            except Exception as exc:
                # Scope columns may not carry the expected defaults on every
                # backend — non-fatal, the plugin still functions.
                log_warning(f"[migrations] goals scope backfill skipped: {exc}")

            try:
                await conn.commit()
            except Exception:
                pass


async def _column_definition(
    cur: Any, table: str, column: str, db_type: str
) -> tuple[int | None, bool, bool]:
    """Measure a column: (declared max length, nullable, has a default).

    ``(None, …)`` for the length means "unbounded or unmeasurable", so a caller
    only ever widens a column it positively measured as bounded.
    """
    if db_type == "postgres":
        query = (
            "SELECT character_maximum_length, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = %s AND column_name = %s"
        )
    else:
        query = (
            "SELECT CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE, COLUMN_DEFAULT "
            "FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s"
        )
    try:
        await cur.execute(query, (table, column))
        row = await cur.fetchone()
    except Exception:
        return (None, True, False)
    if not row:
        return (None, True, False)
    if isinstance(row, dict):
        length_raw = row.get("character_maximum_length")
        nullable_raw = row.get("is_nullable")
        default_raw = row.get("column_default")
    else:
        length_raw, nullable_raw, default_raw = row[0], row[1], row[2]
    try:
        length = int(length_raw) if length_raw is not None else None
    except (TypeError, ValueError):
        length = None
    nullable = str(nullable_raw).upper() != "NO"
    return (length, nullable, default_raw is not None)


# ``memories`` columns the declared schema (``scripts/sql/app_main_postgres.sql``)
# types as TEXT but long-lived stores still carry as varchar(50)/varchar(100).
_MEMORIES_TEXT_COLUMNS: tuple[str, ...] = (
    "emotion",
    "emotion_state",
    "scope",
    "author",
    "source",
)


async def _widen_memories_text_columns() -> None:
    """Align the ``memories`` text columns with the declared schema.

    ``emotion`` (and the sibling label columns) drifted narrower than the
    declared ``TEXT`` on long-lived stores. The compactor writes the model's
    free-text ``feeling`` into ``emotion``; anything past the old 50 characters
    failed the insert and left the day uncompacted (observed: 57 failed writes
    and only 4 of 12 eligible days stored in one nightly run).

    Only columns positively measured as bounded are widened, so this is a no-op
    once a store matches the declared schema. Fail-open: a failed ALTER never
    blocks startup.
    """
    from core.db import _get_db_type, get_conn_ctx

    db_type = _get_db_type()
    widened: list[str] = []
    skipped: list[str] = []
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            if not await _table_exists(cur, "memories", db_type):
                return
            for column in _MEMORIES_TEXT_COLUMNS:
                if not await _column_exists(cur, "memories", column, db_type):
                    continue
                length, nullable, has_default = await _column_definition(
                    cur, "memories", column, db_type
                )
                if length is None:
                    continue
                if db_type == "postgres":
                    ddl = f'ALTER TABLE "memories" ALTER COLUMN "{column}" TYPE text'
                elif has_default:
                    # Restating (or dropping) a backend-specific default from a
                    # MODIFY is riskier than leaving the width alone.
                    skipped.append(column)
                    continue
                else:
                    null_clause = "" if nullable else " NOT NULL"
                    ddl = f"ALTER TABLE `memories` MODIFY `{column}` TEXT{null_clause}"
                try:
                    await cur.execute(ddl)
                    widened.append(f"{column}({length})")
                except Exception as exc:
                    log_warning(f"[migrations] memories.{column} widen skipped: {exc}")
            if widened:
                log_info(
                    "[migrations] Widened memories text column(s): "
                    + ", ".join(widened)
                )
            if skipped:
                log_warning(
                    "[migrations] memories text column(s) left as-is (store default): "
                    + ", ".join(skipped)
                )
            try:
                await conn.commit()
            except Exception:
                pass


async def _migrate_llm_failure_log_is_test() -> None:
    """Add the ``is_test`` column to ``llm_failure_log`` for test isolation.

    The failure store now tags test-isolated entries (``interface_path='fake'``
    or ``reason='test reason'``) so runtime failure summaries can exclude them.
    This migration adds the column idempotently to existing installs that
    predate the flag. Backend-aware (``TINYINT`` on MariaDB, ``SMALLINT`` on
    Postgres) and fail-open: a broken ALTER must never block startup.
    """
    from core.db import _get_db_type, get_conn_ctx

    db_type = _get_db_type()
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            if not await _table_exists(cur, "llm_failure_log", db_type):
                return
            if await _column_exists(cur, "llm_failure_log", "is_test", db_type):
                return

            col_type = "SMALLINT" if db_type == "postgres" else "TINYINT(1)"
            try:
                await cur.execute(
                    f"ALTER TABLE llm_failure_log ADD COLUMN is_test {col_type} "
                    "NOT NULL DEFAULT 0"
                )
                log_info("[migrations] Added llm_failure_log.is_test column")
            except Exception as exc:
                log_warning(f"[migrations] llm_failure_log.is_test add skipped: {exc}")
                return

            try:
                await conn.commit()
            except Exception:
                pass


async def _migrate_selenium_config_keys() -> None:
    """Rename legacy ``SELENIUM_*`` config keys to ``ZEN_*``.

    The Zen LLM Engine was formerly named "Selenium". Existing installations
    have ``SELENIUM_*`` rows in the ``config`` table. This migration renames
    them to ``ZEN_*`` so the code (which now reads ``ZEN_*`` keys) finds the
    existing values. Idempotent and best-effort: existing ``ZEN_*`` rows are
    preserved, and any failure is logged without blocking startup.
    """
    from core.db import get_conn_ctx

    renames: list[tuple[str, str]] = [
        ("SELENIUM_DOUBLE_PROMPT", "ZEN_DOUBLE_PROMPT"),
        ("SELENIUM_DOUBLE_PROMPT_ENABLED", "ZEN_DOUBLE_PROMPT_ENABLED"),
        ("SELENIUM_DOUBLE_PROMPT_RETRIES", "ZEN_DOUBLE_PROMPT_RETRIES"),
        ("SELENIUM_DOUBLE_PROMPT_SAVE_PART1", "ZEN_DOUBLE_PROMPT_SAVE_PART1"),
        ("SELENIUM_DOUBLE_PROMPT_TIMEOUT_SECONDS", "ZEN_DOUBLE_PROMPT_TIMEOUT_SECONDS"),
        ("SELENIUM_MAX_RETRIES", "ZEN_MAX_RETRIES"),
        ("SELENIUM_PART1_PROCESSING_TIMEOUT", "ZEN_PART1_PROCESSING_TIMEOUT"),
        ("SELENIUM_PART1_RESPONSE_STABLE_GRACE", "ZEN_PART1_RESPONSE_STABLE_GRACE"),
        ("SELENIUM_POST_SEND_CONFIRM_TIMEOUT", "ZEN_POST_SEND_CONFIRM_TIMEOUT"),
        ("SELENIUM_RESPONSE_POLL_INTERVAL", "ZEN_RESPONSE_POLL_INTERVAL"),
        ("SELENIUM_RESPONSE_STABLE_GRACE", "ZEN_RESPONSE_STABLE_GRACE"),
        ("SELENIUM_SPLIT_PROMPT_PARTS", "ZEN_SPLIT_PROMPT_PARTS"),
    ]

    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute("SELECT config_key FROM config")
                existing = {row["config_key"] if isinstance(row, dict) else row[0] for row in await cur.fetchall()}
            except Exception as exc:
                log_warning(f"[migrations] Cannot read config table for SELENIUM→ZEN rename: {exc}")
                return

            migrated = 0
            for old_key, new_key in renames:
                if new_key in existing:
                    continue
                if old_key not in existing:
                    continue
                try:
                    await cur.execute(
                        "UPDATE config SET config_key = %s WHERE config_key = %s",
                        (new_key, old_key),
                    )
                    migrated += 1
                except Exception as exc:
                    log_warning(f"[migrations] Failed to rename config key {old_key} → {new_key}: {exc}")

            if migrated:
                try:
                    await conn.commit()
                except Exception:
                    pass
                log_info(f"[migrations] Renamed {migrated} SELENIUM_* config key(s) → ZEN_*")


# Interface paths that only ever come from the test suite: the ``fake``
# component and the fake chat ids a test uses to stand in for a Telegram or
# WebUI conversation. None of these can be a live chat - Telegram user ids are
# large, channel ids are negative, and the WebUI's single session is
# ``webui_default``, never a bare number. Used once, to flag rows written before
# the failure store started flagging test writes itself.
_HISTORIC_TEST_FAILURE_PATHS: tuple[str, ...] = (
    "telegram_bot/1",
    "telegram_bot/123",
    "telegram_bot/5551234567",
    "synth_webui/42",
    "synth_webui/sid",
)


async def _flag_historic_test_failure_rows() -> None:
    """Flag the failure rows the test suite wrote before the store did it.

    ``is_test`` was added to ``llm_failure_log`` after the fact and never
    backfilled, so test writes that predate it still read as runtime failures:
    the recovery loop finds them on every scan, spends a full turn on a chat that
    does not exist, and the failure views count them. Rows are flagged, never
    deleted. Idempotent (only ``is_test = 0`` rows are touched) and fail-open.
    """
    from core.db import _get_db_type, get_conn_ctx

    db_type = _get_db_type()
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            if not await _table_exists(cur, "llm_failure_log", db_type):
                return
            if not await _column_exists(cur, "llm_failure_log", "is_test", db_type):
                return

            placeholders = ", ".join(["%s"] * len(_HISTORIC_TEST_FAILURE_PATHS))
            sql = (
                "UPDATE llm_failure_log SET is_test = 1 "
                "WHERE is_test = 0 AND "
                f"(interface_path LIKE 'fake%' OR interface_path IN ({placeholders}))"
            )
            try:
                await cur.execute(sql, _HISTORIC_TEST_FAILURE_PATHS)
                flagged = getattr(cur, "rowcount", None)
                log_info(
                    "[migrations] Flagged historic test failure row(s) as is_test"
                    + (f": {flagged}" if isinstance(flagged, int) else "")
                )
            except Exception as exc:
                log_warning(f"[migrations] test failure row flag skipped: {exc}")
                return

            try:
                await conn.commit()
            except Exception:
                pass


# Registry of startup migrations, applied in order. Each entry is
# (name, coroutine-callable). Add new one-shot migrations here.
_STARTUP_MIGRATIONS: list[tuple[str, Any]] = [
    ("drop_legacy_recent_chats", _drop_legacy_recent_chats),
    ("rename_timestamp_columns", _rename_timestamp_columns),
    ("dedup_diary_segments", _dedup_diary_segments),
    ("migrate_goals_table", _migrate_goals_table),
    ("migrate_llm_failure_log_is_test", _migrate_llm_failure_log_is_test),
    ("migrate_selenium_config_keys", _migrate_selenium_config_keys),


    ("widen_memories_text_columns", _widen_memories_text_columns),
    ("flag_historic_test_failure_rows", _flag_historic_test_failure_rows),
]


async def run_startup_migrations() -> None:
    """Run all registered startup migrations (idempotent, best-effort)."""
    for name, fn in _STARTUP_MIGRATIONS:
        try:
            await fn()
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"[migrations] Startup migration '{name}' failed: {exc}")


__all__ = ["run_startup_migrations"]
