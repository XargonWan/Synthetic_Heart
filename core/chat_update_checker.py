"""Chat update checker service.

Provides a reusable async checker to determine whether there are new messages
since the last check. It is intended to be scheduled periodically (default
60s) and callable by other components (e.g., Grillo observer).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from core.logging_utils import log_info, log_debug, log_warning, log_error
from core.config_manager import config_registry
from core.db import execute_query
from core import recent_chats

# Register configuration variables
CHAT_UPDATE_CHECK_INTERVAL = int(
    config_registry.get_value(
        "CHAT_UPDATE_CHECK_INTERVAL",
        60,
        label="Chat update check interval",
        description="Default interval in seconds for the chat update checker",
        value_type=int,
        group="scheduling",
        component="core",
    )
)
CHAT_UPDATE_CHECKER_ENABLED = bool(
    config_registry.get_value(
        "CHAT_UPDATE_CHECKER_ENABLED",
        True,
        label="Chat update checker enabled",
        description="Enable periodic checking for new chat messages",
        value_type=bool,
        group="scheduling",
        component="core",
    )
)


class ChatUpdateChecker:
    """Service that checks the `recent_chats` last_active values to detect
    whether new messages arrived since the last check.

    The checker keeps a last-seen timestamp (max last_active) and optionally
    returns the list of chats that updated since that timestamp.
    """

    def __init__(self, interval: Optional[int] = None, enabled: Optional[bool] = None) -> None:
        self.interval = int(interval) if interval is not None else int(CHAT_UPDATE_CHECK_INTERVAL)
        self.enabled = bool(enabled) if enabled is not None else bool(CHAT_UPDATE_CHECKER_ENABLED)
        self._task: Optional[asyncio.Task] = None
        self._last_known_ts: float = 0.0
        self._last_checked: float = 0.0
        self._last_count: int = 0

    async def check_for_updates(self) -> Dict[str, Any]:
        """Public method that returns a dict:
        {
            "updated": bool,
            "new_messages": [ { "chat_id": ..., "last_active": ... }, ... ],
            "last_checked": iso_ts
        }
        """
        return await self._check_once()

    async def _check_once(self) -> Dict[str, Any]:
        now = time.time()
        new_messages: List[Dict[str, Any]] = []
        updated = False

        try:
            # Query DB for most recent activity timestamp
            rows = await execute_query("SELECT MAX(last_active) as max_ts FROM recent_chats")
            max_ts = None
            if rows and len(rows) > 0:
                # rows may be list of tuples or dicts depending on driver; support both
                r = rows[0]
                if isinstance(r, dict):
                    max_ts = r.get("max_ts")
                elif isinstance(r, (list, tuple)):
                    max_ts = r[0]

            if max_ts is None:
                log_debug("[chat_update_checker] No recent_chats rows found (max_ts is None)")
                # No activity recorded yet
                # Fall back to counting active chats in memory
                active = await recent_chats.get_last_active_chats()
                if len(active) != self._last_count:
                    updated = True
                    self._last_count = len(active)
            else:
                max_ts_float = float(max_ts)
                log_debug(f"[chat_update_checker] max_ts={max_ts_float}, last_known={self._last_known_ts}")
                if self._last_known_ts == 0.0:
                    # First run: initialize last_known but do not report updates
                    self._last_known_ts = max_ts_float
                    log_debug("[chat_update_checker] Initialized last_known_ts")
                elif max_ts_float > self._last_known_ts:
                    updated = True
                    # Get chats that updated since last known ts
                    rows2 = await execute_query(
                        "SELECT chat_id, last_active FROM recent_chats WHERE last_active > %s ORDER BY last_active ASC",
                        (self._last_known_ts,),
                    )
                    for r in rows2:
                        if isinstance(r, dict):
                            chat_id = r.get("chat_id")
                            last_active = r.get("last_active")
                        else:
                            chat_id = r[0]
                            last_active = r[1]
                        new_messages.append({"chat_id": chat_id, "last_active": float(last_active)})
                    # Update last_known to the newest timestamp
                    self._last_known_ts = max_ts_float

        except Exception as e:
            # DB error - fallback to in-memory recent_chats (best-effort)
            log_warning(f"[chat_update_checker] DB query failed, falling back to in-memory check: {e}")
            try:
                active = await recent_chats.get_last_active_chats()
                if len(active) != self._last_count:
                    updated = True
                    self._last_count = len(active)
            except Exception as e2:
                log_error(f"[chat_update_checker] Fallback recent_chats check failed: {e2}")

        self._last_checked = now
        iso_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        result = {"updated": updated, "new_messages": new_messages, "last_checked": iso_ts}
        log_debug(f"[chat_update_checker] check result: {result}")
        return result

    async def _run_loop(self) -> None:
        log_info(f"[chat_update_checker] Background loop started (interval={self.interval}s)")
        while True:
            try:
                if not self.enabled:
                    log_debug("[chat_update_checker] Checker disabled, sleeping")
                    await asyncio.sleep(self.interval)
                    continue
                await self._check_once()
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                log_debug("[chat_update_checker] Background loop cancelled")
                break
            except Exception as e:
                log_error(f"[chat_update_checker] Background loop error: {e}")
                await asyncio.sleep(self.interval)

    def start(self) -> Optional[asyncio.Task]:
        """Start background loop, return the task or None if cannot start."""
        if self._task is not None and not self._task.done():
            log_debug("[chat_update_checker] Background task already running")
            return self._task
        try:
            self._task = asyncio.create_task(self._run_loop())
            return self._task
        except RuntimeError:
            log_debug("[chat_update_checker] Could not start background task (no running event loop)")
            return None

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
            log_info("[chat_update_checker] Background task stopped")


# Module-level singleton
_checker = ChatUpdateChecker()

def get_chat_update_checker() -> ChatUpdateChecker:
    return _checker

async def check_for_updates_once() -> Dict[str, Any]:
    return await _checker.check_for_updates()

async def start_chat_update_checker() -> Optional[asyncio.Task]:
    if not _checker.enabled:
        log_info("[chat_update_checker] Checker is disabled by config")
        return None
    try:
        task = _checker.start()
        if task:
            log_info("[chat_update_checker] Checker started")
        return task
    except Exception as e:  # pragma: no cover - best effort
        log_warning(f"[chat_update_checker] Failed to start checker: {e}")
        return None


__all__ = [
    "ChatUpdateChecker",
    "get_chat_update_checker",
    "check_for_updates_once",
    "start_chat_update_checker",
]
