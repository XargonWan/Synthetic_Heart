"""Per-turn reason trail ("why did I say that").

The health dashboard (Control Deck) has no per-turn record of *which* memory,
diary entry, goal, beat, or emotion drove a reply. This module is a minimal,
fail-open reason trail: it records a compact structural summary of the context
that shaped a turn and exposes it for inspection (WebUI Logs > Reason Trail).

Design rules (per AGENTS.md):

* **Fail-open.** A broken reason trail must never affect the reply. Every DB
  read/write and every config read is best-effort; on any error the write
  degrades to an in-memory fallback list and never raises.
* **Structural.** The recorded fields are the *already-resolved* structural
  context of a turn (memory source/id, diary source ids, dominant emotion, the
  active vessel goal, the beat type, the effective history scope). There is no
  keyword or intent detection anywhere in this module.
* **Self-contained.** Importing this module never raises; it registers two
  config vars and defines its own lazy table init, mirroring
  ``core/llm_failure_log.py``.

The reason summary itself is built by :func:`build_reason_summary` (a pure,
side-effect-free helper) and attached by ``core/prompt_engine`` to the prompt
dict under ``__reason_trail`` (popped before the engine sees it), then persisted
via :func:`record_reason`.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from core.config_manager import config_registry
from core.logging_utils import log_debug, log_warning
from core.variables_engine import register_exposed_var


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

register_exposed_var(
    "REASON_TRAIL_ENABLED",
    label="Reason Trail",
    default=True,
    value_type=bool,
    ui_type="bool",
    description=(
        "Record a per-turn summary of which memories, diary entries, emotion, "
        "goal, and beat drove each reply ('why did I say that')."
    ),
    scope="core",
    component="diagnostics",
)

register_exposed_var(
    "REASON_TRAIL_MAX_ROWS",
    label="Reason Trail Max Rows",
    default=1000,
    value_type=int,
    ui_type="number",
    description=(
        "Maximum number of reason-trail rows returned by the trail listing and "
        "the WebUI (newest first)."
    ),
    scope="core",
    component="diagnostics",
    advanced=True,
)

_REASON_TRAIL_ENABLED = config_registry.get_var(
    "REASON_TRAIL_ENABLED",
    True,
    label="Reason Trail",
    description=(
        "Record a per-turn summary of which memories, diary entries, emotion, "
        "goal, and beat drove each reply."
    ),
    value_type=bool,
    component="diagnostics",
)

_REASON_TRAIL_MAX_ROWS = config_registry.get_var(
    "REASON_TRAIL_MAX_ROWS",
    1000,
    label="Reason Trail Max Rows",
    description=("Maximum number of reason-trail rows returned by the trail listing."),
    value_type=int,
    component="diagnostics",
    advanced=True,
)


def _reason_trail_enabled() -> bool:
    """Read REASON_TRAIL_ENABLED (default True), fail-open on any error."""
    try:
        value = _REASON_TRAIL_ENABLED.value
        if isinstance(value, str):
            return value.strip().lower() not in ("", "0", "false", "no", "off")
        return bool(value)
    except Exception:
        return True


def _reason_trail_max_rows() -> int:
    """Read REASON_TRAIL_MAX_ROWS (default 1000), fail-open on any error."""
    try:
        value = _REASON_TRAIL_MAX_ROWS.value
        if isinstance(value, bool):
            return 1000
        parsed = int(value)
        return parsed if parsed > 0 else 1000
    except Exception:
        return 1000


# ---------------------------------------------------------------------------
# Schema + lazy init
# ---------------------------------------------------------------------------

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS turn_reason_trail (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    interface_path VARCHAR(255),
    reply_preview TEXT,
    memories JSON,
    diary_sources JSON,
    emotion VARCHAR(255),
    goal JSON,
    beat_type VARCHAR(100),
    history_scope VARCHAR(50),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_reason_trail_created_at (created_at),
    INDEX idx_reason_trail_interface_path (interface_path)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

_IN_MEMORY_TTL = timedelta(days=7)
_IN_MEMORY_MAX_ENTRIES = 500
_in_memory_reason_entries: list[dict[str, Any]] = []
_in_memory_reason_id_counter = itertools.count(start=-1, step=-1)
_in_memory_reason_lock: asyncio.Lock | None = None


async def ensure_reason_trail_table() -> None:
    from core.db import get_conn_ctx

    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_TABLE_SQL)
        try:
            await conn.commit()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Pure structural helpers
# ---------------------------------------------------------------------------


def build_reason_summary(
    memories: Any,
    diary_entries: Any,
    emotion: Any,
    beat_type: Any,
    history_scope: Any,
    goal: Any,
) -> dict[str, Any]:
    """Build a compact structural summary of what drove a turn.

    Pure and side-effect-free: reads only the passed values and returns a plain
    JSON-safe dict. None of the inputs are interpreted semantically — this is a
    structural redaction of already-resolved context fields.
    """
    return {
        "memories": _compact_memories(memories),
        "diary_sources": _compact_diary_sources(diary_entries),
        "emotion": _compact_emotion(emotion),
        "goal": _compact_goal(goal),
        "beat_type": str(beat_type).strip() if beat_type else None,
        "history_scope": str(history_scope).strip() if history_scope else None,
    }


def _compact_memories(memories: Any) -> list[dict[str, Any]]:
    if not isinstance(memories, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for mem in memories:
        if not isinstance(mem, dict):
            continue
        item: dict[str, Any] = {}
        for key in ("source", "id"):
            if mem.get(key) is not None:
                item[key] = mem[key]
        snippet = mem.get("snippet") or mem.get("content") or mem.get("text")
        if snippet:
            item["snippet"] = str(snippet)[:200]
        if item:
            result.append(item)
    return result


def _compact_diary_sources(diary_entries: Any) -> list[dict[str, Any]]:
    if not isinstance(diary_entries, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for entry in diary_entries:
        if not isinstance(entry, dict):
            continue
        item: dict[str, Any] = {}
        for key in ("id", "created_at", "interface", "chat_id"):
            if entry.get(key) is not None:
                item[key] = entry[key]
        if item:
            result.append(item)
    return result


def _compact_emotion(emotion: Any) -> str | None:
    if emotion is None:
        return None
    if isinstance(emotion, dict) and emotion:
        parts = [f"{name}:{value}" for name, value in emotion.items()]
        joined = ", ".join(parts).strip()
        return joined[:255] if joined else None
    text = str(emotion).strip()
    return text[:255] if text else None


def _compact_goal(goal: Any) -> dict[str, Any] | None:
    if not isinstance(goal, dict):
        return None
    item: dict[str, Any] = {}
    for key in (
        "id",
        "description",
        "note",
        "target_kind",
        "target_name",
        "status",
        "current_step",
    ):
        if goal.get(key) is not None:
            item[key] = goal[key]
    return item or None


# ---------------------------------------------------------------------------
# Sanitization + normalization
# ---------------------------------------------------------------------------


def _sanitize_for_storage(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _sanitize_for_storage(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_for_storage(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_sanitize_for_storage(item) for item in value), key=str)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, BaseException):
        return str(value)
    if hasattr(value, "__dict__"):
        return _sanitize_for_storage(vars(value))
    return str(value)


def _normalize_created_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _parse_json_column(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, (dict, list)):
                return parsed
        except Exception:
            log_debug("[turn_reason] Failed to parse JSON column")
    return None


def _normalize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": entry.get("id"),
        "interface_path": entry.get("interface_path"),
        "reply_preview": entry.get("reply_preview"),
        "memories": _sanitize_for_storage(entry.get("memories")),
        "diary_sources": _sanitize_for_storage(entry.get("diary_sources")),
        "emotion": entry.get("emotion"),
        "goal": _sanitize_for_storage(entry.get("goal")),
        "beat_type": entry.get("beat_type"),
        "history_scope": entry.get("history_scope"),
        "created_at": _normalize_created_at(entry.get("created_at")),
    }


def _json_column_value(value: Any) -> Any:
    if value is None:
        return None
    return json.dumps(_sanitize_for_storage(value), ensure_ascii=False)


# ---------------------------------------------------------------------------
# In-memory fallback store
# ---------------------------------------------------------------------------


def _get_in_memory_lock() -> asyncio.Lock:
    global _in_memory_reason_lock
    if _in_memory_reason_lock is None:
        _in_memory_reason_lock = asyncio.Lock()
    return _in_memory_reason_lock


def _prune_in_memory_entries_locked() -> None:
    cutoff = datetime.now(timezone.utc) - _IN_MEMORY_TTL
    _in_memory_reason_entries[:] = [
        entry
        for entry in _in_memory_reason_entries
        if _normalize_created_at(entry.get("created_at")) >= cutoff
    ]
    if len(_in_memory_reason_entries) > _IN_MEMORY_MAX_ENTRIES:
        _in_memory_reason_entries[:] = _in_memory_reason_entries[
            -_IN_MEMORY_MAX_ENTRIES:
        ]


async def _store_in_memory_reason(entry: dict[str, Any]) -> int:
    normalized = _normalize_entry(entry)
    normalized["id"] = next(_in_memory_reason_id_counter)

    async with _get_in_memory_lock():
        _prune_in_memory_entries_locked()
        _in_memory_reason_entries.append(normalized)

    return int(normalized["id"])


def _reason_matches_search(entry: dict[str, Any], search: str | None) -> bool:
    if not search:
        return True
    lowered = search.lower()
    searchable_parts = [
        entry.get("interface_path"),
        entry.get("reply_preview"),
        entry.get("emotion"),
        entry.get("beat_type"),
        entry.get("history_scope"),
        entry.get("memories"),
        entry.get("diary_sources"),
        entry.get("goal"),
    ]
    searchable_text = " ".join(
        str(part) for part in searchable_parts if part is not None
    ).lower()
    return lowered in searchable_text


def _reason_sort_key(entry: dict[str, Any]) -> tuple[datetime, int]:
    return (
        _normalize_created_at(entry.get("created_at")),
        int(entry.get("id") or 0),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def record_reason(
    interface_path: str | None,
    reply_preview: str | None = None,
    memories: Any = None,
    diary_sources: Any = None,
    emotion: str | None = None,
    goal: Any = None,
    beat_type: str | None = None,
    history_scope: str | None = None,
) -> None:
    """Persist one reason-trail row, best-effort.

    Never raises. When the trail is disabled this is a no-op; when the DB is
    unavailable the row is held in the in-memory fallback list instead.
    """
    if not _reason_trail_enabled():
        return

    entry = {
        "interface_path": interface_path,
        "reply_preview": reply_preview,
        "memories": memories,
        "diary_sources": diary_sources,
        "emotion": emotion,
        "goal": goal,
        "beat_type": beat_type,
        "history_scope": history_scope,
    }
    normalized = _normalize_entry(entry)

    try:
        await ensure_reason_trail_table()

        from core.db import get_conn_ctx

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO turn_reason_trail (
                        interface_path,
                        reply_preview,
                        memories,
                        diary_sources,
                        emotion,
                        goal,
                        beat_type,
                        history_scope
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        normalized["interface_path"],
                        normalized["reply_preview"],
                        _json_column_value(normalized["memories"]),
                        _json_column_value(normalized["diary_sources"]),
                        normalized["emotion"],
                        _json_column_value(normalized["goal"]),
                        normalized["beat_type"],
                        normalized["history_scope"],
                    ],
                )
            try:
                await conn.commit()
            except Exception:
                pass
    except Exception as exc:
        try:
            await _store_in_memory_reason(normalized)
        except Exception as inner_exc:
            log_warning(f"[turn_reason] In-memory fallback failed: {inner_exc}")
            return
        log_warning(f"[turn_reason] DB record failed, used in-memory fallback: {exc}")


async def _list_db_reasons(search: str | None) -> list[dict[str, Any]]:
    from core.db import get_conn_ctx

    await ensure_reason_trail_table()

    where_sql = ""
    where_params: list[Any] = []
    if search:
        term = f"%{search}%"
        where_sql = (
            "WHERE (interface_path LIKE %s OR reply_preview LIKE %s "
            "OR emotion LIKE %s OR beat_type LIKE %s OR history_scope LIKE %s)"
        )
        where_params = [term] * 5

    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT
                    id,
                    interface_path,
                    reply_preview,
                    memories,
                    diary_sources,
                    emotion,
                    goal,
                    beat_type,
                    history_scope,
                    created_at
                FROM turn_reason_trail
                {where_sql}
                ORDER BY created_at DESC, id DESC
                """,
                where_params,
            )
            rows = await cur.fetchall()

    entries: list[dict[str, Any]] = []
    for row in rows:
        created_at = row[9] if len(row) > 9 else None
        entries.append(
            {
                "id": row[0],
                "interface_path": row[1],
                "reply_preview": row[2],
                "memories": _parse_json_column(row[3]),
                "diary_sources": _parse_json_column(row[4]),
                "emotion": row[5],
                "goal": _parse_json_column(row[6]),
                "beat_type": row[7],
                "history_scope": row[8],
                "created_at": created_at
                if isinstance(created_at, datetime)
                else created_at,
            }
        )
    return entries


async def list_reasons(
    limit: int | None = None,
    search: str | None = None,
) -> list[dict[str, Any]]:
    """Merge DB + in-memory reason rows, newest first.

    ``limit`` caps the result; when ``None`` the default cap is
    ``REASON_TRAIL_MAX_ROWS``. Degrades to the in-memory view (possibly empty)
    when the DB is unavailable — never raises.
    """
    try:
        cap = _reason_trail_max_rows() if limit is None else max(1, int(limit))
    except Exception:
        cap = _reason_trail_max_rows()
    if not search:
        search = None

    memory_entries: list[dict[str, Any]] = []
    try:
        async with _get_in_memory_lock():
            _prune_in_memory_entries_locked()
            memory_entries = [
                dict(entry)
                for entry in _in_memory_reason_entries
                if _reason_matches_search(entry, search)
            ]
    except Exception:
        memory_entries = []

    db_entries: list[dict[str, Any]] = []
    try:
        db_entries = await _list_db_reasons(search)
    except Exception as exc:
        log_warning(f"[turn_reason] DB list failed, serving degraded view: {exc}")

    merged = db_entries + memory_entries
    merged.sort(key=_reason_sort_key, reverse=True)
    return merged[:cap]


async def delete_reason(entry_id: int) -> bool:
    """Delete one reason-trail row (negative ids target the in-memory store)."""
    if entry_id < 0:
        async with _get_in_memory_lock():
            _prune_in_memory_entries_locked()
            for index, entry in enumerate(_in_memory_reason_entries):
                if int(entry.get("id") or 0) == entry_id:
                    del _in_memory_reason_entries[index]
                    return True
        return False

    from core.db import get_conn_ctx

    await ensure_reason_trail_table()

    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM turn_reason_trail WHERE id = %s", [entry_id])
            deleted = int(getattr(cur, "rowcount", 0) or 0) > 0
        try:
            await conn.commit()
        except Exception:
            pass
    return deleted
