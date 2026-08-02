from __future__ import annotations

import asyncio
import itertools
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from core.logging_utils import log_debug, log_warning


_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS llm_failure_log (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    failure_code VARCHAR(100) NOT NULL,
    stage VARCHAR(100) NOT NULL,
    reason TEXT NOT NULL,
    interface_path VARCHAR(255),
    chat_id VARCHAR(255),
    thread_id VARCHAR(255),
    engine VARCHAR(255),
    model VARCHAR(255),
    message_id VARCHAR(255),
    content_preview TEXT,
    metadata JSON,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_failure_created_at (created_at),
    INDEX idx_failure_code (failure_code),
    INDEX idx_failure_stage (stage),
    INDEX idx_failure_interface_path (interface_path),
    INDEX idx_failure_engine (engine)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

_IN_MEMORY_TTL = timedelta(days=7)
_IN_MEMORY_MAX_ENTRIES = 500
_in_memory_failure_entries: list[dict[str, Any]] = []
_in_memory_failure_id_counter = itertools.count(start=-1, step=-1)
_in_memory_failure_lock: asyncio.Lock | None = None


def infer_failure_code(
    reason: str,
    *,
    correction_context: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    lowered = str(reason or "").lower()
    errors: list[str] = []

    if isinstance(correction_context, dict):
        raw_errors = correction_context.get("errors")
        if isinstance(raw_errors, list):
            errors.extend(str(item) for item in raw_errors if item)

    if isinstance(metadata, dict):
        raw_errors = metadata.get("errors")
        if isinstance(raw_errors, list):
            errors.extend(str(item) for item in raw_errors if item)

    joined_errors = "\n".join(errors).lower()

    if "unsupported type" in joined_errors or "unsupported type" in lowered:
        return "unsupported_action"
    if "missing 'type'" in joined_errors or "missing 'payload'" in joined_errors:
        return "invalid_action"
    if "interface_path" in joined_errors or "interface_path" in lowered:
        return "invalid_interface_path"
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if "correction loop" in lowered:
        return "correction_loop"
    if "no correction" in lowered or "exhausted" in lowered:
        return "correction_exhausted"
    if "json" in lowered:
        return "malformed_json"
    if "unreachable" in lowered or "connection" in lowered:
        return "provider_unreachable"
    if "delivery" in lowered or "websocket" in lowered or "send" in lowered:
        return "delivery_failed"
    return "llm_failure"


def build_failure_entry(
    *,
    reason: str,
    stage: str,
    interface_path: str | None = None,
    chat_id: str | int | None = None,
    thread_id: str | int | None = None,
    engine: str | None = None,
    model: str | None = None,
    message_id: str | int | None = None,
    content_preview: str | None = None,
    correction_context: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    failure_code: str | None = None,
) -> dict[str, Any]:
    normalized_metadata: dict[str, Any] = {}
    if isinstance(metadata, dict):
        normalized_metadata.update(_sanitize_for_storage(metadata))
    if isinstance(correction_context, dict) and correction_context:
        normalized_metadata.setdefault(
            "correction_context", _sanitize_for_storage(correction_context)
        )

    return {
        "failure_code": failure_code
        or infer_failure_code(
            reason,
            correction_context=correction_context,
            metadata=normalized_metadata,
        ),
        "stage": stage or "unknown",
        "reason": reason,
        "interface_path": interface_path,
        "chat_id": None if chat_id is None else str(chat_id),
        "thread_id": None if thread_id is None else str(thread_id),
        "engine": engine,
        "model": model,
        "message_id": None if message_id is None else str(message_id),
        "content_preview": content_preview,
        "metadata": normalized_metadata,
    }


async def ensure_failure_log_table() -> None:
    from core.db import get_conn_ctx

    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_TABLE_SQL)
        try:
            await conn.commit()
        except Exception:
            pass


def _parse_metadata(raw_metadata: Any) -> dict[str, Any]:
    if isinstance(raw_metadata, dict):
        return raw_metadata
    if isinstance(raw_metadata, str) and raw_metadata.strip():
        try:
            parsed = json.loads(raw_metadata)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            log_debug("[llm_failure_log] Failed to parse metadata JSON")
    return {}


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


def _get_in_memory_failure_lock() -> asyncio.Lock:
    global _in_memory_failure_lock
    if _in_memory_failure_lock is None:
        _in_memory_failure_lock = asyncio.Lock()
    return _in_memory_failure_lock


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


def _normalize_entry_for_storage(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": entry.get("id"),
        "failure_code": str(entry.get("failure_code") or "llm_failure"),
        "stage": str(entry.get("stage") or "unknown"),
        "reason": str(entry.get("reason") or "Unknown failure"),
        "interface_path": entry.get("interface_path"),
        "chat_id": None if entry.get("chat_id") is None else str(entry.get("chat_id")),
        "thread_id": None
        if entry.get("thread_id") is None
        else str(entry.get("thread_id")),
        "engine": entry.get("engine"),
        "model": entry.get("model"),
        "message_id": None
        if entry.get("message_id") is None
        else str(entry.get("message_id")),
        "content_preview": entry.get("content_preview"),
        "metadata": _sanitize_for_storage(entry.get("metadata") or {}),
        "created_at": _normalize_created_at(entry.get("created_at")),
    }


def _prune_in_memory_failure_entries_locked() -> None:
    cutoff = datetime.now(timezone.utc) - _IN_MEMORY_TTL
    _in_memory_failure_entries[:] = [
        entry
        for entry in _in_memory_failure_entries
        if _normalize_created_at(entry.get("created_at")) >= cutoff
    ]
    if len(_in_memory_failure_entries) > _IN_MEMORY_MAX_ENTRIES:
        _in_memory_failure_entries[:] = _in_memory_failure_entries[
            -_IN_MEMORY_MAX_ENTRIES:
        ]


async def _store_in_memory_failure_entry(entry: dict[str, Any]) -> int:
    normalized = _normalize_entry_for_storage(entry)
    normalized["id"] = next(_in_memory_failure_id_counter)

    async with _get_in_memory_failure_lock():
        _prune_in_memory_failure_entries_locked()
        _in_memory_failure_entries.append(normalized)

    return int(normalized["id"])


def _entry_matches_filters(
    entry: dict[str, Any],
    *,
    search: str,
    failure_code: str,
    stage: str,
) -> bool:
    if failure_code and str(entry.get("failure_code") or "") != failure_code:
        return False
    if stage and str(entry.get("stage") or "") != stage:
        return False
    if not search:
        return True

    lowered = search.lower()
    searchable_parts = [
        entry.get("failure_code"),
        entry.get("stage"),
        entry.get("reason"),
        entry.get("interface_path"),
        entry.get("chat_id"),
        entry.get("thread_id"),
        entry.get("engine"),
        entry.get("model"),
        entry.get("message_id"),
        entry.get("content_preview"),
    ]
    searchable_text = " ".join(
        str(part) for part in searchable_parts if isinstance(part, str) and part
    ).lower()
    return lowered in searchable_text


def _entry_sort_key(entry: dict[str, Any]) -> tuple[datetime, int]:
    return (
        _normalize_created_at(entry.get("created_at")),
        int(entry.get("id") or 0),
    )


async def _list_in_memory_failure_entries(
    *,
    search: str,
    failure_code: str,
    stage: str,
) -> list[dict[str, Any]]:
    async with _get_in_memory_failure_lock():
        _prune_in_memory_failure_entries_locked()
        entries = [
            dict(entry)
            for entry in _in_memory_failure_entries
            if _entry_matches_filters(
                entry,
                search=search,
                failure_code=failure_code,
                stage=stage,
            )
        ]
    return entries


async def _delete_in_memory_failure_entry(entry_id: int) -> bool:
    async with _get_in_memory_failure_lock():
        _prune_in_memory_failure_entries_locked()
        for index, entry in enumerate(_in_memory_failure_entries):
            if int(entry.get("id") or 0) == entry_id:
                del _in_memory_failure_entries[index]
                return True
    return False


async def _list_db_failure_entries(
    *,
    search: str,
    failure_code: str,
    stage: str,
    sort: str,
) -> list[dict[str, Any]]:
    from core.db import get_conn_ctx

    await ensure_failure_log_table()

    order = "DESC" if str(sort).lower() != "asc" else "ASC"
    where_clauses: list[str] = []
    where_params: list[Any] = []

    if search:
        search_term = f"%{search}%"
        where_clauses.append(
            "(reason LIKE %s OR interface_path LIKE %s OR engine LIKE %s OR model LIKE %s OR failure_code LIKE %s)"
        )
        where_params.extend([search_term] * 5)

    if failure_code:
        where_clauses.append("failure_code = %s")
        where_params.append(failure_code)

    if stage:
        where_clauses.append("stage = %s")
        where_params.append(stage)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    entries: list[dict[str, Any]] = []
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT
                    id,
                    failure_code,
                    stage,
                    reason,
                    interface_path,
                    chat_id,
                    thread_id,
                    engine,
                    model,
                    message_id,
                    content_preview,
                    metadata,
                    created_at
                FROM llm_failure_log
                {where_sql}
                ORDER BY created_at {order}, id {order}
                """,
                where_params,
            )
            rows = await cur.fetchall()

    for row in rows:
        created_at = row[12] if len(row) > 12 else None
        entries.append(
            {
                "id": row[0],
                "failure_code": row[1],
                "stage": row[2],
                "reason": row[3],
                "interface_path": row[4],
                "chat_id": row[5],
                "thread_id": row[6],
                "engine": row[7],
                "model": row[8],
                "message_id": row[9],
                "content_preview": row[10],
                "metadata": _parse_metadata(row[11]),
                "created_at": created_at
                if isinstance(created_at, datetime)
                else created_at,
            }
        )

    return entries


async def record_failure_entry(entry: dict[str, Any]) -> int | None:
    from core.db import get_conn_ctx

    normalized_entry = _normalize_entry_for_storage(entry)
    metadata_json = json.dumps(normalized_entry["metadata"], ensure_ascii=False)

    try:
        await ensure_failure_log_table()

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO llm_failure_log (
                        failure_code,
                        stage,
                        reason,
                        interface_path,
                        chat_id,
                        thread_id,
                        engine,
                        model,
                        message_id,
                        content_preview,
                        metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        normalized_entry["failure_code"],
                        normalized_entry["stage"],
                        normalized_entry["reason"],
                        normalized_entry["interface_path"],
                        normalized_entry["chat_id"],
                        normalized_entry["thread_id"],
                        normalized_entry["engine"],
                        normalized_entry["model"],
                        normalized_entry["message_id"],
                        normalized_entry["content_preview"],
                        metadata_json,
                    ],
                )
                inserted_id = getattr(cur, "lastrowid", None)
            try:
                await conn.commit()
            except Exception:
                pass
        return inserted_id
    except Exception as exc:
        fallback_id = await _store_in_memory_failure_entry(normalized_entry)
        log_warning(
            f"[llm_failure_log] Falling back to in-memory failure log store: {exc}"
        )
        return fallback_id


async def list_failure_entries(
    *,
    page: int = 1,
    per_page: int = 20,
    search: str = "",
    failure_code: str = "",
    stage: str = "",
    sort: str = "desc",
) -> dict[str, Any]:
    offset = max(page - 1, 0) * per_page
    reverse = str(sort).lower() != "asc"
    memory_entries = await _list_in_memory_failure_entries(
        search=search,
        failure_code=failure_code,
        stage=stage,
    )

    db_entries: list[dict[str, Any]] = []
    try:
        db_entries = await _list_db_failure_entries(
            search=search,
            failure_code=failure_code,
            stage=stage,
            sort=sort,
        )
    except Exception as exc:
        log_warning(f"[llm_failure_log] DB list failed, serving degraded view: {exc}")

    merged_entries = db_entries + memory_entries
    merged_entries.sort(key=_entry_sort_key, reverse=reverse)

    total_count = len(merged_entries)
    paged_entries = merged_entries[offset : offset + per_page]
    total_pages = (total_count + per_page - 1) // per_page if total_count > 0 else 1
    return {
        "entries": paged_entries,
        "page": page,
        "per_page": per_page,
        "total_count": total_count,
        "total_pages": total_pages,
    }


async def delete_failure_entry(entry_id: int) -> bool:
    if entry_id < 0:
        deleted = await _delete_in_memory_failure_entry(entry_id)
        if not deleted:
            log_warning(
                f"[llm_failure_log] In-memory failure entry {entry_id} not found for delete"
            )
        return deleted

    from core.db import get_conn_ctx

    await ensure_failure_log_table()

    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM llm_failure_log WHERE id = %s", [entry_id])
            deleted = int(getattr(cur, "rowcount", 0) or 0) > 0
        try:
            await conn.commit()
        except Exception:
            pass

    if not deleted:
        log_warning(f"[llm_failure_log] Failure entry {entry_id} not found for delete")
    return deleted


async def mark_failure_processed(entry_id: int) -> bool:
    """Mark a failure entry as processed by the recovery plugin.

    Sets ``metadata.processed_by_recovery = True`` so the recovery plugin does
    not revisit it on the next scan. Works for both DB and in-memory entries.
    This is the anti-loop guarantee: once a failure has been handed to recovery
    (whether or not the regeneration succeeded), it must never be picked up
    again, otherwise the plugin would spam new messages forever.
    """
    if entry_id < 0:
        # In-memory entry: patch the dict directly.
        for e in _in_memory_failure_entries:
            if e.get("id") == entry_id:
                meta = e.get("metadata") or {}
                meta["processed_by_recovery"] = True
                e["metadata"] = meta
                return True
        return False

    from core.db import get_conn_ctx

    await ensure_failure_log_table()

    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT metadata FROM llm_failure_log WHERE id = %s", [entry_id]
            )
            row = await cur.fetchone()
            if row is None:
                return False
            raw = row[0] if row else None
            meta = _parse_metadata(raw) or {}
            meta["processed_by_recovery"] = True
            import json

            await cur.execute(
                "UPDATE llm_failure_log SET metadata = %s WHERE id = %s",
                (json.dumps(meta, ensure_ascii=False), entry_id),
            )
        try:
            await conn.commit()
        except Exception:
            pass
    return True
