from __future__ import annotations

import json
from datetime import datetime
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
        normalized_metadata.update(metadata)
    if isinstance(correction_context, dict) and correction_context:
        normalized_metadata.setdefault("correction_context", correction_context)

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


async def record_failure_entry(entry: dict[str, Any]) -> int | None:
    from core.db import get_conn_ctx

    await ensure_failure_log_table()

    metadata_json = json.dumps(entry.get("metadata") or {}, ensure_ascii=False)
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
                    entry.get("failure_code") or "llm_failure",
                    entry.get("stage") or "unknown",
                    entry.get("reason") or "Unknown failure",
                    entry.get("interface_path"),
                    entry.get("chat_id"),
                    entry.get("thread_id"),
                    entry.get("engine"),
                    entry.get("model"),
                    entry.get("message_id"),
                    entry.get("content_preview"),
                    metadata_json,
                ],
            )
            inserted_id = getattr(cur, "lastrowid", None)
        try:
            await conn.commit()
        except Exception:
            pass
    return inserted_id


async def list_failure_entries(
    *,
    page: int = 1,
    per_page: int = 20,
    search: str = "",
    failure_code: str = "",
    stage: str = "",
    sort: str = "desc",
) -> dict[str, Any]:
    from core.db import get_conn_ctx

    await ensure_failure_log_table()

    offset = max(page - 1, 0) * per_page
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
                f"SELECT COUNT(*) FROM llm_failure_log {where_sql}",
                where_params,
            )
            count_row = await cur.fetchone()
            total_count = (
                int(count_row[0]) if count_row and count_row[0] is not None else 0
            )

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
                LIMIT %s OFFSET %s
                """,
                where_params + [per_page, offset],
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

    total_pages = (total_count + per_page - 1) // per_page if total_count > 0 else 1
    return {
        "entries": entries,
        "page": page,
        "per_page": per_page,
        "total_count": total_count,
        "total_pages": total_pages,
    }


async def delete_failure_entry(entry_id: int) -> bool:
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
