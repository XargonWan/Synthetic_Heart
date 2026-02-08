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
import os


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


async def create_archive(session_id: str, messages: List[Dict[str, Any]], name: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    archive_id = uuid4().hex
    created_at = datetime.utcnow().isoformat()

    # In-memory testing path ------------------------------------------------
    if os.getenv('SYNTH_TESTING', '0') == '1':
        if not hasattr(create_archive, '_in_memory_store'):
            create_archive._in_memory_store = {}
        create_archive._in_memory_store[archive_id] = {
            "id": archive_id,
            "session_id": session_id,
            "name": name or 'Chat',
            "messages": messages,
            "metadata": metadata,
            "created_at": created_at,
        }
        log_info(f"[chat_archives_db] (testing) Created archive {archive_id} for session {session_id}")
        return {"id": archive_id, "created_at": created_at}

    # Production DB path ----------------------------------------------------
    try:
        await init_chat_archives_table()
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO chat_archives (id, session_id, name, messages, metadata, created_at) VALUES (%s, %s, %s, %s, %s, UTC_TIMESTAMP())",
                    (archive_id, session_id, name or 'Chat', json.dumps(messages), json.dumps(metadata) if metadata else None),
                )
                log_info(f"[chat_archives_db] Created archive {archive_id} for session {session_id}")
                return {"id": archive_id, "created_at": created_at}
    except Exception as e:
        log_warning(f"[chat_archives_db] Failed to create archive: {e}")
        raise


async def list_archives(session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    # Testing path: list from in-memory store
    if os.getenv('SYNTH_TESTING', '0') == '1':
        store = getattr(create_archive, '_in_memory_store', {})
        out = []
        for a in store.values():
            if session_id and a.get('session_id') != session_id:
                continue
            out.append({
                "id": a['id'],
                "session_id": a['session_id'],
                "name": a.get('name', 'Chat'),
                "created_at": a.get('created_at'),
                "message_count": len(a.get('messages') or []),
            })
        # Order by created_at descending
        out.sort(key=lambda x: x.get('created_at') or '', reverse=True)
        return out

    try:
        log_debug(f"[chat_archives_db] list_archives called with session_id={session_id}")
        await init_chat_archives_table()
        async with get_conn_ctx() as conn:
            # Try to use a dict cursor but pass it as a positional arg to be compatible
            # with different aiomysql / connection wrappers that may not accept
            # keyword-only API for cursor creation.
            # Use default cursor (tuple rows). We handle dict rows when present
            # to maximize compatibility across different DB adapters / wrappers.
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
                    # Support both dict-row and tuple-row results for maximum compatibility
                    try:
                        if isinstance(r, dict):
                            aid = r.get('id')
                            sid = r.get('session_id')
                            name = r.get('name') or 'Chat'
                            created = r.get('created_at')
                            msgcount = r.get('message_count')
                        else:
                            # tuple-style fallback
                            aid = r[0]
                            sid = r[1]
                            name = r[2] or 'Chat'
                            created = r[3]
                            msgcount = r[4] if len(r) > 4 else 0

                        out.append({
                            "id": aid,
                            "session_id": sid,
                            "name": name,
                            "created_at": created.isoformat() if hasattr(created, 'isoformat') else str(created),
                            "message_count": int(msgcount) if msgcount else 0,
                        })
                    except Exception as ex:
                        log_warning(f"[chat_archives_db] Skipping malformed row while listing archives: {ex}")
                return out
    except Exception as e:
        log_warning(f"[chat_archives_db] Failed to list archives: {e}")
        raise


async def load_archive(archive_id: str) -> Dict[str, Any]:
    # Testing path ---------------------------------------------------------
    if os.getenv('SYNTH_TESTING', '0') == '1':
        store = getattr(create_archive, '_in_memory_store', {})
        arch = store.get(archive_id)
        if not arch:
            raise FileNotFoundError(archive_id)
        return arch

    try:
        await init_chat_archives_table()
        async with get_conn_ctx() as conn:
            # Use default cursor (tuple rows) for max compatibility
            async with conn.cursor() as cur:
                await cur.execute("SELECT id, session_id, name, messages, metadata, created_at FROM chat_archives WHERE id = %s", (archive_id,))
                row = await cur.fetchone()
                if not row:
                    log_debug(f"[chat_archives_db] load_archive: archive {archive_id} not found in DB")
                    raise FileNotFoundError(archive_id)

                # Support both dict-style and tuple-style rows
                if isinstance(row, dict):
                    messages = json.loads(row.get('messages')) if row.get('messages') else []
                    metadata = json.loads(row.get('metadata')) if row.get('metadata') else None
                    created = row.get('created_at')
                    return {
                        "id": row.get('id'),
                        "session_id": row.get('session_id'),
                        "name": row.get('name'),
                        "messages": messages,
                        "metadata": metadata,
                        "created_at": created.isoformat() if hasattr(created, 'isoformat') else str(created)
                    }

                return {
                    "id": row[0],
                    "session_id": row[1],
                    "name": row[2],
                    "messages": json.loads(row[3]) if row[3] else [],
                    "metadata": json.loads(row[4]) if row[4] else None,
                    "created_at": row[5].isoformat() if hasattr(row[5], 'isoformat') else str(row[5])
                }
    except FileNotFoundError:
        raise
    except Exception as e:
        log_warning(f"[chat_archives_db] Failed to load archive {archive_id}: {e}")
        raise


async def delete_archive(archive_id: str) -> None:
    # Testing path ---------------------------------------------------------
    if os.getenv('SYNTH_TESTING', '0') == '1':
        store = getattr(create_archive, '_in_memory_store', {})
        if archive_id in store:
            del store[archive_id]
            log_info(f"[chat_archives_db] (testing) Deleted archive {archive_id}")
            return
        raise FileNotFoundError(archive_id)

    try:
        await init_chat_archives_table()
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM chat_archives WHERE id = %s", (archive_id,))
                log_info(f"[chat_archives_db] Deleted archive {archive_id}")
    except Exception as e:
        log_warning(f"[chat_archives_db] Failed to delete archive {archive_id}: {e}")
        raise


async def rename_archive(archive_id: str, new_name: str) -> Dict[str, Any]:
    try:
        await init_chat_archives_table()
        async with get_conn_ctx() as conn:
            # Use default cursor (tuple rows) for max compatibility
            async with conn.cursor() as cur:
                await cur.execute("UPDATE chat_archives SET name=%s WHERE id=%s", (new_name, archive_id))
                await cur.execute("SELECT id, session_id, name, messages, metadata, created_at FROM chat_archives WHERE id = %s", (archive_id,))
                row = await cur.fetchone()
                if not row:
                    raise FileNotFoundError(archive_id)

                if isinstance(row, dict):
                    messages = json.loads(row.get('messages')) if row.get('messages') else []
                    metadata = json.loads(row.get('metadata')) if row.get('metadata') else None
                    created = row.get('created_at')
                    return {
                        "id": row.get('id'),
                        "session_id": row.get('session_id'),
                        "name": row.get('name'),
                        "messages": messages,
                        "metadata": metadata,
                        "created_at": created.isoformat() if hasattr(created, 'isoformat') else str(created)
                    }

                return {
                    "id": row[0],
                    "session_id": row[1],
                    "name": row[2],
                    "messages": json.loads(row[3]) if row[3] else [],
                    "metadata": json.loads(row[4]) if row[4] else None,
                    "created_at": row[5].isoformat() if hasattr(row[5], 'isoformat') else str(row[5])
                }
    except FileNotFoundError:
        raise
    except Exception as e:
        log_warning(f"[chat_archives_db] Failed to rename archive {archive_id}: {e}")
        raise
