"""
DB-backed session metadata storage for WebUI chat rect and camera state.

Provides init, get, set functions for session-level metadata keyed by interface_path.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from core.logging_utils import log_debug, log_warning
from core.db import get_conn_ctx


session_meta_table_init_done = False


async def init_session_meta_table() -> None:
    global session_meta_table_init_done
    if session_meta_table_init_done:
        return
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS chat_session_meta (
                        interface_path VARCHAR(512) PRIMARY KEY,
                        meta LONGTEXT NOT NULL,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        INDEX idx_updated_at (updated_at)
                    )
                """)
                log_debug("[session_meta] chat_session_meta initialized")
                session_meta_table_init_done = True
    except Exception as e:  # pragma: no cover - DB dependent
        log_warning(f"[session_meta] Failed to init table: {e}")


async def set_session_meta(interface_path: str, meta: Dict[str, Any]) -> None:
    try:
        await init_session_meta_table()
        # Load existing meta to perform a merge instead of blind overwrite
        existing = await get_session_meta(interface_path) or {}
        merged = _merge_meta(existing, meta)
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO chat_session_meta (interface_path, meta) VALUES (%s, %s) ON DUPLICATE KEY UPDATE meta = VALUES(meta), updated_at = UTC_TIMESTAMP()",
                    (interface_path, json.dumps(merged)),
                )
                log_debug(f"[session_meta] Set meta for {interface_path} (merged)")
    except Exception as e:  # pragma: no cover - DB dependent
        log_warning(f"[session_meta] Failed to set meta for {interface_path}: {e}")


def _merge_meta(
    existing: Optional[Dict[str, Any]], incoming: Dict[str, Any]
) -> Dict[str, Any]:
    """Merge incoming meta into existing meta.

    Special handling: if incoming contains a 'device' key ('mobile'|'desktop'),
    the remaining keys are merged under existing['viewports'][device]. This
    preserves per-viewport metadata while keeping top-level keys for
    backward compatibility.
    """
    if existing is None:
        existing = {}
    # Work on a shallow copy to avoid mutating input
    out = dict(existing)

    device = None
    if isinstance(incoming, dict) and "device" in incoming:
        device = incoming.get("device")

    if device:
        # Ensure viewports namespace exists
        viewports = out.setdefault("viewports", {})
        viewport_meta = dict(viewports.get(device) or {})
        # Merge incoming excluding 'device'
        for k, v in incoming.items():
            if k == "device":
                continue
            if isinstance(v, dict) and isinstance(viewport_meta.get(k), dict):
                # shallow merge for nested dicts
                merged_inner = dict(viewport_meta.get(k))
                merged_inner.update(v)
                viewport_meta[k] = merged_inner
            else:
                viewport_meta[k] = v
        viewports[device] = viewport_meta
        out["viewports"] = viewports
        return out

    # No device provided: perform shallow merge of keys at top-level
    for k, v in incoming.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            merged_inner = dict(out.get(k))
            merged_inner.update(v)
            out[k] = merged_inner
        else:
            out[k] = v

    return out


async def get_session_meta(interface_path: str) -> Optional[Dict[str, Any]]:
    try:
        await init_session_meta_table()
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT meta FROM chat_session_meta WHERE interface_path = %s",
                    (interface_path,),
                )
                row = await cur.fetchone()
                if row and row[0]:
                    try:
                        return json.loads(row[0])
                    except Exception:
                        return None
    except Exception as e:  # pragma: no cover - DB dependent
        log_warning(f"[session_meta] Failed to get meta for {interface_path}: {e}")
    return None
