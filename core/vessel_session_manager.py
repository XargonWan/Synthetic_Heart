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

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
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
        # In-memory set of currently-active session ids. Kept in sync by
        # start/end/close_expired so ``has_active_session()`` is a cheap,
        # DB-free check callable on the hot path (e.g. the message queue on
        # every enqueue). It is only an optimisation: the DB remains the source
        # of truth and ``has_active_session()`` falls back to it when the set is
        # empty (e.g. right after a process restart).
        self._active_session_ids: set[str] = set()
        # Optional connection-liveness probe, registered by the I/O layer (the
        # Vessel interface) via :meth:`set_liveness_probe`. It answers a single
        # structural question — "is at least one tracked session backed by a
        # *really connected* connector right now?" — without this module ever
        # importing the interface or the connector registry (keeps ``core`` free
        # of interface deps). It powers the connection-driven 3-state model of
        # :meth:`has_active_session` (see that docstring). ``None`` until the
        # interface starts; the probe must be cheap, synchronous and never raise.
        self._liveness_probe: Callable[[], bool] | None = None
        # Strong references to in-flight background compaction tasks, so the
        # event loop does not garbage-collect them before they finish. Each task
        # removes itself via a done-callback (see :meth:`_launch_compaction`).
        self._compaction_tasks: set[asyncio.Task[None]] = set()

    def set_liveness_probe(self, probe: Callable[[], bool] | None) -> None:
        """Register (or clear) the connector-liveness probe.

        Called once by the Vessel interface on startup. Passing ``None`` clears
        it (e.g. on interface teardown), reverting ``has_active_session`` to the
        bookkeeping-only behaviour.
        """
        self._liveness_probe = probe

    def has_active_session(self) -> bool:
        """Return True only while embodiment is in the **CONNECTED** state.

        The Vessel has a three-state lifecycle driven by the *real* connection
        to the world, not by bookkeeping alone:

        * **CONNECTED** — a session exists *and* its connector is really
          connected. This is the only state that returns ``True`` here, so it is
          the only state in which autonomous perceptions/will-beats are produced
          and the message queue ranks in-world traffic as an active embodiment.
        * **RECONNECTING** — a session exists but its connector has dropped
          (e.g. a container restart or a transient bridge blip). Returns
          ``False`` so beat/perception production is *frozen* and message
          priorities are left untouched while the interface retries the
          connection in the background (within the disconnect-grace window).
        * **ENDED** — the reconnection failed past the grace window; the session
          is closed and its ids removed, so this returns ``False``.

        Implementation: cheap and synchronous (safe on the hot path). With no
        tracked session ids it is trivially ``False``. When a liveness probe has
        been registered by the interface (:meth:`set_liveness_probe`) the probe
        decides CONNECTED vs RECONNECTING from the connector's real
        ``is_connected`` state. Before the probe is registered (very early boot)
        it falls back to the bookkeeping set so behaviour is unchanged. The
        probe must never block or raise; any failure is treated as "not
        connected" (RECONNECTING), which is the safe default — it only pauses
        autonomy, never dispatches into a dead world.
        """
        if not self._active_session_ids:
            return False
        probe = self._liveness_probe
        if probe is None:
            return True
        try:
            return bool(probe())
        except Exception:  # pragma: no cover - defensive
            return False

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
            self._active_session_ids.add(existing)
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
        self._active_session_ids.add(session_id)
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
        """End a session, scheduling a background compaction of its buffer.

        The buffered lived experience is compacted (in chunks, off the hot path)
        into the dedicated ``vessel_diary`` table — it is **no longer** written
        to the real ``ai_diary`` (that polluted every non-vessel Fast-Lane
        prompt). Always returns ``None`` now (the ``diary_entry_id`` link is
        unused). Idempotent: ending an already-ended session is a no-op.
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

        # The lived experience is NO LONGER written to the real ``ai_diary`` —
        # that concatenated into a single shared daily row and polluted every
        # non-vessel Fast-Lane prompt. Instead we compact the buffer (in chunks,
        # off the hot path) into the dedicated ``vessel_diary`` table. The
        # ``diary_entry_id`` link therefore stays NULL. Launched as a background
        # task so end_session returns fast (teardown must not block on the LLM).
        if buffer:
            self._launch_compaction(
                session_id=session_id,
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
                        " ended_at = CURRENT_TIMESTAMP, diary_entry_id = NULL"
                        " WHERE session_id = %s",
                        (session_id,),
                    )
                await conn.commit()
        except Exception as exc:
            log_error(f"[vessel_session] Failed to mark session ended: {exc}")

        if self._current_session_id == session_id:
            self._current_session_id = None
        self._active_session_ids.discard(session_id)

        # The session is over: drop everything still queued for this world scope
        # (autonomous will/action beats, sightings, and any pending player chat).
        # Once the embodiment is gone that traffic is stale — dispatching it into
        # a dead world or coalescing it into the next session is wrong. This is
        # the canonical purge point covering every close path (logout, cooldown,
        # disconnect). Purely structural (the session's ``interface_path`` world
        # scope), never message text; fully guarded.
        if interface_path:
            try:
                from core import message_queue

                message_queue.drop_vessel_queue_for_world(interface_path)
            except Exception as exc:
                log_warning(
                    f"[vessel_session] queue purge failed for {session_id}: {exc}"
                )

        log_info(
            f"[vessel_session] Ended session {session_id} ({reason}); "
            f"scheduled compaction of {len(buffer)} experience items"
        )
        return None

    async def suspend_session(self, session_id: str) -> None:
        """Suspend a session across a process restart without ending it.

        A container/process restart is **not** a logout: the world is still
        there and Synth re-enters it on the next boot. Unlike
        :meth:`end_session`, this keeps the DB row ``active`` (so
        :meth:`~interface.vessel_interface.VesselInterface._reattach_active_sessions`
        finds and re-embodies it) and does **not** flush the experience buffer
        to a diary entry — the lived experience continues, unfragmented, in the
        same session after the restart. It only refreshes ``last_event_at`` so
        the session is still within the inactivity cooldown window when the
        interface comes back up, and drops the in-memory bookkeeping for the
        now-destroyed connector. Best-effort and fully guarded.
        """
        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE vessel_sessions"
                        " SET last_event_at = CURRENT_TIMESTAMP"
                        " WHERE session_id = %s AND status = 'active'",
                        (session_id,),
                    )
                await conn.commit()
        except Exception as exc:
            log_warning(f"[vessel_session] suspend_session failed: {exc}")

        if self._current_session_id == session_id:
            self._current_session_id = None
        self._active_session_ids.discard(session_id)
        log_info(
            f"[vessel_session] Suspended session {session_id} for restart "
            "(kept active for reattach)"
        )

    # ------------------------------------------------------------------
    # Cooldown scheduler
    # ------------------------------------------------------------------
    async def close_expired_sessions(self, cooldown_sec: int) -> int:
        """End every active session idle for longer than ``cooldown_sec``.

        Returns the number of sessions ended. Intended to be called
        periodically by the interface's scheduler tick.
        """
        expired: list[str] = []
        # Compute the cutoff in Python and compare against a plain timestamp
        # parameter. This avoids the ``INTERVAL %s SECOND`` SQL syntax, which is
        # MariaDB-only and is not valid on PostgreSQL (a parameter cannot appear
        # inside an INTERVAL literal), keeping the query backend-agnostic.
        cutoff = datetime.now() - timedelta(seconds=int(cooldown_sec))
        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT session_id FROM vessel_sessions"
                        " WHERE status = 'active'"
                        " AND last_event_at < %s",
                        (cutoff,),
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

    def _launch_compaction(
        self,
        session_id: str,
        environment: str,
        interface_path: str | None,
        buffer: list[dict[str, Any]],
        reason: str,
    ) -> None:
        """Fire-and-forget the chunked compaction so teardown returns fast.

        Compacting a long session is LLM-bound (many chunks, several rounds), so
        it must never sit on the ``end_session`` hot path. We launch it as a
        background task; a strong reference is held until it completes so the
        loop does not garbage-collect it mid-flight.
        """
        try:
            task = asyncio.create_task(
                self._compact_and_store(
                    session_id=session_id,
                    environment=environment,
                    interface_path=interface_path,
                    buffer=buffer,
                    reason=reason,
                )
            )
        except RuntimeError as exc:
            # No running loop (e.g. some sync test paths): run inline as a
            # best-effort fallback rather than dropping the experience.
            log_debug(f"[vessel_session] no loop for compaction task: {exc}")
            return
        self._compaction_tasks.add(task)
        task.add_done_callback(self._compaction_tasks.discard)

    async def _compact_and_store(
        self,
        session_id: str,
        environment: str,
        interface_path: str | None,
        buffer: list[dict[str, Any]],
        reason: str,
    ) -> None:
        """Compact the buffer into ``vessel_diary`` (background). Never raises.

        Deliberately does **not** touch the real ``ai_diary``. Whether to import
        the compacted entry into the real diary is a separate, unimplemented
        decision (see ``core.vessel_diary_compactor.compact_session``).
        """
        if not buffer:
            return
        try:
            from core.vessel_diary_compactor import (
                compact_session,
                save_vessel_diary,
            )

            summary = await compact_session(
                session_id=session_id,
                environment=environment,
                interface_path=interface_path,
                buffer=buffer,
                reason=reason,
            )
            if not summary:
                return
            entry_id = await save_vessel_diary(
                session_id=session_id,
                environment=environment,
                interface_path=interface_path,
                summary=summary,
                moments_count=len(buffer),
                reason=reason,
            )
            log_info(
                f"[vessel_session] Compacted session {session_id} into "
                f"vessel_diary #{entry_id} ({len(buffer)} moments)"
            )
        except Exception as exc:
            log_error(f"[vessel_session] compaction failed for {session_id}: {exc}")


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
