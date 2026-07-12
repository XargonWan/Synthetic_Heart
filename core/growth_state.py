"""Shared helpers for SyntH's self-growth state.

The ``growth_states`` table stores a rolling history (max 10 rows) of SyntH's
evolving self-growth reflection. Exactly one row is flagged ``is_current`` at a
time. This module is intentionally free of plugin dependencies so it can be used
by both the writer (``plugins/grillo/grillo_growth.py``) and readers such as
``core/persona_manager.py`` without creating import cycles.
"""

from __future__ import annotations

import json
from typing import Any

from core.db import DictCursor, _get_db_type, get_conn_ctx
from core.logging_utils import log_debug, log_error

# Rolling history cap: only the newest N rows are retained.
MAX_GROWTH_HISTORY = 10


def _is_postgres() -> bool:
    return _get_db_type() == "postgres"


def _decode_str_list(value: Any) -> list[str]:
    """Decode a JSON-encoded list of strings stored in a TEXT column."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    try:
        parsed = json.loads(value)
    except Exception:
        return []
    if isinstance(parsed, list):
        return [str(x) for x in parsed]
    return []


async def ensure_growth_table() -> None:
    """Create the ``growth_states`` table if it does not already exist.

    Both ``init-db.sql`` and the Postgres schema seed this table, but running
    instances that predate the feature need lazy creation.
    """
    try:
        if _is_postgres():
            ddl = (
                "CREATE TABLE IF NOT EXISTS growth_states ("
                "id BIGSERIAL PRIMARY KEY, "
                "content TEXT NOT NULL, "
                "created_by TEXT NOT NULL DEFAULT 'grillo_growth', "
                "source TEXT NOT NULL DEFAULT 'weekly', "
                "is_current BOOLEAN NOT NULL DEFAULT FALSE, "
                "likes TEXT NOT NULL DEFAULT '[]', "
                "dislikes TEXT NOT NULL DEFAULT '[]', "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
        else:
            ddl = (
                "CREATE TABLE IF NOT EXISTS growth_states ("
                "id INT AUTO_INCREMENT PRIMARY KEY, "
                "content LONGTEXT NOT NULL, "
                "created_by VARCHAR(64) NOT NULL DEFAULT 'grillo_growth', "
                "source VARCHAR(64) NOT NULL DEFAULT 'weekly', "
                "is_current BOOLEAN NOT NULL DEFAULT 0, "
                "likes LONGTEXT NULL, "
                "dislikes LONGTEXT NULL, "
                "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "INDEX idx_growth_current (is_current), "
                "INDEX idx_growth_created_at (created_at DESC)"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
            )
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(ddl)
                # Lazy migration: instances predating the likes/dislikes columns
                # need them added. ADD COLUMN IF NOT EXISTS is supported by both
                # Postgres and MariaDB/MySQL 8+.
                for col in ("likes", "dislikes"):
                    try:
                        await cur.execute(
                            f"ALTER TABLE growth_states ADD COLUMN IF NOT EXISTS {col} "
                            + ("TEXT" if _is_postgres() else "LONGTEXT NULL")
                        )
                    except Exception as mig_err:  # pragma: no cover - defensive
                        log_debug(f"[growth_state] add column {col} skipped: {mig_err}")
            commit = getattr(conn, "commit", None)
            if commit is not None:
                await commit()
    except Exception as e:
        log_error(f"[growth_state] ensure_growth_table failed: {e}")


async def get_current_growth() -> str | None:
    """Return the content of the current self-growth state, or ``None``."""
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor(DictCursor) as cur:
                if _is_postgres():
                    await cur.execute(
                        "SELECT content FROM growth_states WHERE is_current = TRUE "
                        "ORDER BY created_at DESC LIMIT 1"
                    )
                else:
                    await cur.execute(
                        "SELECT content FROM growth_states WHERE is_current = 1 "
                        "ORDER BY created_at DESC LIMIT 1"
                    )
                row = await cur.fetchone()
        if not row:
            return None
        content = row.get("content") if isinstance(row, dict) else row[0]
        text = str(content or "").strip()
        return text or None
    except Exception as e:
        log_debug(f"[growth_state] get_current_growth failed: {e}")
        return None


async def get_growth_history(limit: int = MAX_GROWTH_HISTORY) -> list[dict[str, Any]]:
    """Return up to ``limit`` growth states, newest first.

    Each entry: ``{id, content, created_by, source, is_current, created_at,
    likes, dislikes}`` where ``likes``/``dislikes`` are lists of strings (empty
    for rows created before the columns existed).
    """
    try:
        await ensure_growth_table()
        async with get_conn_ctx() as conn:
            async with conn.cursor(DictCursor) as cur:
                await cur.execute(
                    "SELECT id, content, created_by, source, is_current, "
                    "likes, dislikes, created_at "
                    "FROM growth_states ORDER BY created_at DESC, id DESC LIMIT %s",
                    (int(limit),),
                )
                rows = await cur.fetchall()
        results: list[dict[str, Any]] = []
        for row in rows or []:
            if isinstance(row, dict):
                d = dict(row)
            else:
                d = {
                    "id": row[0],
                    "content": row[1],
                    "created_by": row[2],
                    "source": row[3],
                    "is_current": row[4],
                    "likes": row[5],
                    "dislikes": row[6],
                    "created_at": row[7],
                }
            created_at = d.get("created_at")
            if created_at is not None and not isinstance(created_at, str):
                try:
                    d["created_at"] = created_at.isoformat()
                except Exception:
                    d["created_at"] = str(created_at)
            d["is_current"] = bool(d.get("is_current"))
            d["likes"] = _decode_str_list(d.get("likes"))
            d["dislikes"] = _decode_str_list(d.get("dislikes"))
            results.append(d)
        return results
    except Exception as e:
        log_error(f"[growth_state] get_growth_history failed: {e}")
        return []


async def _prune_history(cur: Any) -> None:
    """Delete rows beyond the newest ``MAX_GROWTH_HISTORY`` (uses an open cursor)."""
    if _is_postgres():
        await cur.execute(
            "DELETE FROM growth_states WHERE id NOT IN ("
            "SELECT id FROM growth_states ORDER BY created_at DESC, id DESC LIMIT %s)",
            (MAX_GROWTH_HISTORY,),
        )
    else:
        # MySQL cannot delete from a table referenced in a subquery without an
        # extra derived-table wrapper.
        await cur.execute(
            "DELETE FROM growth_states WHERE id NOT IN ("
            "SELECT id FROM (SELECT id FROM growth_states "
            "ORDER BY created_at DESC, id DESC LIMIT %s) AS keep)",
            (MAX_GROWTH_HISTORY,),
        )


async def save_growth_state(
    content: str,
    *,
    created_by: str = "grillo_growth",
    source: str = "weekly",
    allow_empty: bool = False,
    likes: list[str] | None = None,
    dislikes: list[str] | None = None,
) -> int | None:
    """Insert a new growth state, mark it current, and prune to the newest 10.

    Returns the new row id, or ``None`` on failure. When ``allow_empty`` is
    ``False`` (the default, used by automatic weekly rounds) an empty/blank
    ``content`` is skipped; manual user saves pass ``allow_empty=True`` to
    intentionally clear the current self-growth reflection.

    ``likes``/``dislikes`` capture the likes/dislikes applied alongside this
    growth state so the history can show what was proposed at that iteration.
    """
    text = str(content or "").strip()
    if not text and not allow_empty:
        log_debug(
            "[growth_state] save_growth_state called with empty content; skipping"
        )
        return None
    likes_json = json.dumps([str(x) for x in (likes or [])])
    dislikes_json = json.dumps([str(x) for x in (dislikes or [])])
    await ensure_growth_table()
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                # Demote the previous current state.
                if _is_postgres():
                    await cur.execute(
                        "UPDATE growth_states SET is_current = FALSE WHERE is_current = TRUE"
                    )
                    await cur.execute(
                        "INSERT INTO growth_states "
                        "(content, created_by, source, is_current, likes, dislikes) "
                        "VALUES (%s, %s, %s, TRUE, %s, %s) RETURNING id",
                        (text, created_by, source, likes_json, dislikes_json),
                    )
                    row = await cur.fetchone()
                    new_id = int(row[0]) if row else None
                else:
                    await cur.execute(
                        "UPDATE growth_states SET is_current = 0 WHERE is_current = 1"
                    )
                    await cur.execute(
                        "INSERT INTO growth_states "
                        "(content, created_by, source, is_current, likes, dislikes) "
                        "VALUES (%s, %s, %s, 1, %s, %s)",
                        (text, created_by, source, likes_json, dislikes_json),
                    )
                    new_id = getattr(cur, "lastrowid", None)
                await _prune_history(cur)
            commit = getattr(conn, "commit", None)
            if commit is not None:
                await commit()
        return new_id
    except Exception as e:
        log_error(f"[growth_state] save_growth_state failed: {e}")
        return None


async def revert_to_state(state_id: int) -> int | None:
    """Revert to a historical growth state by copying it into a new current row.

    The table is append-only; reverting inserts a fresh row (source
    ``'revert'``) with the historical content and marks it current. Returns the
    new row id, or ``None`` on failure/not-found.
    """
    await ensure_growth_table()
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor(DictCursor) as cur:
                await cur.execute(
                    "SELECT content, likes, dislikes FROM growth_states WHERE id = %s",
                    (int(state_id),),
                )
                row = await cur.fetchone()
            if not row:
                log_debug(f"[growth_state] revert_to_state: id {state_id} not found")
                return None
            if isinstance(row, dict):
                content = row.get("content")
                likes = _decode_str_list(row.get("likes"))
                dislikes = _decode_str_list(row.get("dislikes"))
            else:
                content = row[0]
                likes = _decode_str_list(row[1])
                dislikes = _decode_str_list(row[2])
    except Exception as e:
        log_error(f"[growth_state] revert_to_state lookup failed: {e}")
        return None

    return await save_growth_state(
        str(content or ""),
        created_by="user",
        source="revert",
        likes=likes,
        dislikes=dislikes,
    )
