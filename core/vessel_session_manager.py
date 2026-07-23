"""Rift Vessel session manager.

Owns the lifecycle of an *embodiment session* and the persistence of its
buffered lived experience. A session begins when SyntH is embodied in an
external world (connector connected) and ends either on an explicit logout or
after ``VESSEL_SESSION_COOLDOWN_SEC`` of inactivity.

**Hard constraint (lived experience):** no diary / memory entry is written
*during* a session. Perception events and actions accumulate in an in-DB
``experience_buffer`` (``vessel_sessions``); the buffer is flushed to a *single*
diary entry only at end-of-session. Individual events are still logged to
``vessel_activity_log`` for the WebUI Activities tab (audit only — that is not
"lived experience").

This module is deliberately free of any LLM / agentic dependency: it never
routes through the Agent Lane and never spawns Drones.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import aiomysql

from core.db import get_conn_ctx
from core.logging_utils import log_debug, log_error, log_info, log_warning

__all__ = [
    "VesselSessionManager",
    "vessel_session_manager",
    "get_vessel_session_manager",
]


def _now() -> float:
    """Return the current wall-clock time as a float epoch."""
    return time.time()


class VesselSessionManager:
    """Track embodiment sessions and flush lived experience at end-of-session."""

    def __init__(self) -> None:
        """Initialise an empty, un-started session manager."""
        self._current_session_id: str | None = None
        self._last_event_at: float = 0.0

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------
    async def start_session(
        self,
        environment: str,
        interface_path: str | None = None,
    ) -> str:
        """Open a new embodiment session and return its ``session_id``.

        If a session for the same environment is already active it is reused,
        so a reconnect within the cooldown window continues the same lived
        experience rather than fragmenting it.
        """
        existing = await self._find_active_session(environment)
        if existing:
            self._current_session_id = existing
            self._last_event_at = _now()
            log_debug(
                f"[vessel_session] Reusing active session {existing} for '{environment}'"
            )
            return existing

        session_id = uuid.uuid4().hex
        started = _now()
        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO vessel_sessions
                            (session_id, environment, interface_path, status,
                             experience_buffer, started_at, last_event_at)
                        VALUES (%s, %s, %s, 'active', %s,
                                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        """,
                        (session_id, environment, interface_path, json.dumps([])),
                    )
                await conn.commit()
        except Exception as exc:
            log_error(f"[vessel_session] Failed to start session: {exc}")
            raise

        self._current_session_id = session_id
        self._last_event_at = started
        log_info(f"[vessel_session] Started session {session_id} in '{environment}'")
        return session_id

    async def record_experience(
        self,
        session_id: str,
        event_type: str,
        summary: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Append one lived-experience item to the session buffer.

        This does **not** write to the diary — it only accumulates. The buffer
        is flushed to a single diary entry when the session ends.
        """
        item = {
            "event_type": event_type,
            "summary": summary,
            "data": data or {},
            "at": _now(),
        }
        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT experience_buffer FROM vessel_sessions"
                        " WHERE session_id = %s",
                        (session_id,),
                    )
                    row = await cur.fetchone()
                    if not row:
                        log_warning(
                            f"[vessel_session] record_experience: unknown session "
                            f"{session_id}"
                        )
                        return
                    buffer = _load_buffer(row.get("experience_buffer"))
                    buffer.append(item)
                    async with conn.cursor() as cur2:
                        await cur2.execute(
                            "UPDATE vessel_sessions SET experience_buffer = %s,"
                            " last_event_at = CURRENT_TIMESTAMP"
                            " WHERE session_id = %s",
                            (json.dumps(buffer), session_id),
                        )
                await conn.commit()
        except Exception as exc:
            log_error(f"[vessel_session] Failed to record experience: {exc}")
            return
        self._last_event_at = _now()

    async def touch(self, session_id: str) -> None:
        """Refresh a session's ``last_event_at`` without buffering experience."""
        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE vessel_sessions SET last_event_at = CURRENT_TIMESTAMP"
                        " WHERE session_id = %s AND status = 'active'",
                        (session_id,),
                    )
                await conn.commit()
        except Exception as exc:
            log_debug(f"[vessel_session] touch failed: {exc}")
        self._last_event_at = _now()

    async def end_session(
        self,
        session_id: str,
        reason: str = "logout",
    ) -> int | None:
        """End a session, flushing its buffer to a single diary entry.

        Returns the created diary entry id (or ``None`` if nothing was flushed
        or the diary plugin is unavailable). Idempotent: ending an already-ended
        session is a no-op.
        """
        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT environment, interface_path, status,"
                        " experience_buffer FROM vessel_sessions"
                        " WHERE session_id = %s",
                        (session_id,),
                    )
                    row = await cur.fetchone()
        except Exception as exc:
            log_error(f"[vessel_session] end_session lookup failed: {exc}")
            return None

        if not row:
            log_warning(f"[vessel_session] end_session: unknown session {session_id}")
            return None
        if row.get("status") == "ended":
            log_debug(f"[vessel_session] Session {session_id} already ended")
            return None

        environment = row.get("environment") or "unknown"
        interface_path = row.get("interface_path")
        buffer = _load_buffer(row.get("experience_buffer"))

        diary_entry_id = await self._flush_to_diary(
            environment=environment,
            interface_path=interface_path,
            buffer=buffer,
            reason=reason,
        )

        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE vessel_sessions SET status = 'ended',"
                        " ended_at = CURRENT_TIMESTAMP, diary_entry_id = %s"
                        " WHERE session_id = %s",
                        (diary_entry_id, session_id),
                    )
                await conn.commit()
        except Exception as exc:
            log_error(f"[vessel_session] Failed to mark session ended: {exc}")

        if self._current_session_id == session_id:
            self._current_session_id = None

        log_info(
            f"[vessel_session] Ended session {session_id} ({reason}); "
            f"flushed {len(buffer)} experience items"
            + (f" to diary #{diary_entry_id}" if diary_entry_id else "")
        )
        return diary_entry_id

    # ------------------------------------------------------------------
    # Cooldown scheduler
    # ------------------------------------------------------------------
    async def close_expired_sessions(self, cooldown_sec: int) -> int:
        """End every active session idle for longer than ``cooldown_sec``.

        Returns the number of sessions ended. Intended to be called
        periodically by the interface's scheduler tick.
        """
        expired: list[str] = []
        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT session_id FROM vessel_sessions"
                        " WHERE status = 'active'"
                        " AND last_event_at < (NOW() - INTERVAL %s SECOND)",
                        (int(cooldown_sec),),
                    )
                    rows = await cur.fetchall()
                    expired = [r["session_id"] for r in rows or []]
        except Exception as exc:
            log_error(f"[vessel_session] close_expired_sessions query failed: {exc}")
            return 0

        ended = 0
        for session_id in expired:
            await self.end_session(session_id, reason="cooldown")
            ended += 1
        if ended:
            log_info(f"[vessel_session] Cooldown closed {ended} idle session(s)")
        return ended

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _find_active_session(self, environment: str) -> str | None:
        """Return the id of an active session for ``environment`` if any."""
        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT session_id FROM vessel_sessions"
                        " WHERE environment = %s AND status = 'active'"
                        " ORDER BY started_at DESC LIMIT 1",
                        (environment,),
                    )
                    row = await cur.fetchone()
                    return row["session_id"] if row else None
        except Exception as exc:
            log_debug(f"[vessel_session] _find_active_session failed: {exc}")
            return None

    async def _flush_to_diary(
        self,
        environment: str,
        interface_path: str | None,
        buffer: list[dict[str, Any]],
        reason: str,
    ) -> int | None:
        """Write the buffered experience to a single diary entry.

        Returns the diary entry id, or ``None`` if the buffer was empty or the
        diary plugin is unavailable. Failures are swallowed — a missing diary
        plugin must never break session teardown.
        """
        if not buffer:
            return None
        try:
            from plugins.ai_diary import add_diary_entry_async
        except Exception as exc:
            log_debug(f"[vessel_session] diary plugin unavailable: {exc}")
            return None

        lines = [item.get("summary", "") for item in buffer if item.get("summary")]
        content = (
            f"Lived experience in {environment} (session ended: {reason}).\n"
            + "\n".join(f"- {line}" for line in lines)
        )
        interaction_summary = (
            f"Embodied session in {environment}: {len(buffer)} moments"
        )
        try:
            await add_diary_entry_async(
                content=content,
                interaction_summary=interaction_summary,
                context_tags=["vessel", environment],
                interface="vessel",
                chat_id=interface_path or "",
            )
        except Exception as exc:
            log_error(f"[vessel_session] Failed to flush buffer to diary: {exc}")
            return None
        # add_diary_entry_async is an UPSERT of today's row and does not return an
        # id; the diary link is best-effort for the vessel subsystem.
        return None


def _load_buffer(raw: Any) -> list[dict[str, Any]]:
    """Deserialize an ``experience_buffer`` column into a list."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


# Module-level singleton (mirrors the other core managers).
vessel_session_manager = VesselSessionManager()


def get_vessel_session_manager() -> VesselSessionManager:
    """Return the shared :class:`VesselSessionManager` singleton."""
    return vessel_session_manager
