"""
DB-backed chat archives (new module `chat_archives_db`).

This avoids overwriting the existing (messy) `chat_archives.py` while we
migrate to a DB-only approach. Use this module in `webui.py` and other code
that needs DB-backed archives.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from core.logging_utils import log_debug, log_info, log_warning
from core.db import get_conn_ctx


async def init_chat_archives_table() -> None:
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS chat_archives (
                        id VARCHAR(64) PRIMARY KEY,
                        session_id VARCHAR(255) DEFAULT NULL,
                        name VARCHAR(255) DEFAULT NULL,
                        messages LONGTEXT NOT NULL,
                        metadata LONGTEXT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_session (session_id),
                        INDEX idx_created_at (created_at)
                    )
                """)
                log_debug("[chat_archives_db] Initialized table chat_archives")
    except Exception as e:
        log_warning(f"[chat_archives_db] Failed to init table: {e}")


async def create_archive(
    session_id: str,
    messages: List[Dict[str, Any]],
    name: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    archive_id = uuid4().hex
    created_at = datetime.utcnow().isoformat()
    try:
        await init_chat_archives_table()
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO chat_archives (id, session_id, name, messages, metadata, created_at) VALUES (%s, %s, %s, %s, %s, UTC_TIMESTAMP())",
                    (
                        archive_id,
                        session_id,
                        name or "Chat",
                        json.dumps(messages),
                        json.dumps(metadata) if metadata else None,
                    ),
                )
                log_info(
                    f"[chat_archives_db] Created archive {archive_id} for session {session_id}"
                )
                return {"id": archive_id, "created_at": created_at}
    except Exception as e:
        log_warning(f"[chat_archives_db] Failed to create archive: {e}")
        raise


async def list_archives(session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    try:
        log_debug(
            f"[chat_archives_db] list_archives called with session_id={session_id}"
        )
        await init_chat_archives_table()
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                # Use JSON_LENGTH to count number of messages when possible; fall back to CHAR_LENGTH if not supported
                if session_id:
                    await cur.execute(
                        "SELECT id, session_id, name, created_at, COALESCE(JSON_LENGTH(messages), CHAR_LENGTH(messages)) as message_count FROM chat_archives WHERE session_id=%s ORDER BY created_at DESC",
                        (session_id,),
                    )
                else:
                    await cur.execute(
                        "SELECT id, session_id, name, created_at, COALESCE(JSON_LENGTH(messages), CHAR_LENGTH(messages)) as message_count FROM chat_archives ORDER BY created_at DESC",
                    )
                rows = await cur.fetchall()
                out: List[Dict[str, Any]] = []
                for r in rows:
                    out.append(
                        {
                            "id": r[0],
                            "session_id": r[1],
                            "name": r[2] or "Chat",
                            "created_at": r[3].isoformat()
                            if hasattr(r[3], "isoformat")
                            else str(r[3]),
                            "message_count": int(r[4]) if r[4] else 0,
                        }
                    )
                return out
    except Exception as e:
        log_warning(f"[chat_archives_db] Failed to list archives: {e}")
        raise


async def load_archive(archive_id: str) -> Dict[str, Any]:
    try:
        await init_chat_archives_table()
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, session_id, name, messages, metadata, created_at FROM chat_archives WHERE id = %s",
                    (archive_id,),
                )
                row = await cur.fetchone()
                if not row:
                    log_debug(
                        f"[chat_archives_db] load_archive: archive {archive_id} not found in DB"
                    )
                    raise FileNotFoundError(archive_id)
                return {
                    "id": row[0],
                    "session_id": row[1],
                    "name": row[2],
                    "messages": json.loads(row[3]) if row[3] else [],
                    "metadata": json.loads(row[4]) if row[4] else None,
                    "created_at": row[5].isoformat()
                    if hasattr(row[5], "isoformat")
                    else str(row[5]),
                }
    except FileNotFoundError:
        raise
    except Exception as e:
        log_warning(f"[chat_archives_db] Failed to load archive {archive_id}: {e}")
        raise


async def delete_archive(archive_id: str) -> None:
    try:
        await init_chat_archives_table()
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM chat_archives WHERE id = %s", (archive_id,)
                )
                log_info(f"[chat_archives_db] Deleted archive {archive_id}")
    except Exception as e:
        log_warning(f"[chat_archives_db] Failed to delete archive {archive_id}: {e}")
        raise


async def rename_archive(archive_id: str, new_name: str) -> Dict[str, Any]:
    try:
        await init_chat_archives_table()
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE chat_archives SET name=%s WHERE id=%s",
                    (new_name, archive_id),
                )
                await cur.execute(
                    "SELECT id, session_id, name, messages, metadata, created_at FROM chat_archives WHERE id = %s",
                    (archive_id,),
                )
                row = await cur.fetchone()
                if not row:
                    raise FileNotFoundError(archive_id)
                return {
                    "id": row[0],
                    "session_id": row[1],
                    "name": row[2],
                    "messages": json.loads(row[3]) if row[3] else [],
                    "metadata": json.loads(row[4]) if row[4] else None,
                    "created_at": row[5].isoformat()
                    if hasattr(row[5], "isoformat")
                    else str(row[5]),
                }
    except FileNotFoundError:
        raise
    except Exception as e:
        log_warning(f"[chat_archives_db] Failed to rename archive {archive_id}: {e}")
        raise
