"""Delivery circuit breaker + dead-target registry.

SyntH was hammering a single stale Discord channel with 16+ identical
"Unknown channel or user" failures in a two-minute window (and ~76
``delivery_failed`` rows/day). This module is the reusable guard that stops
that loop: after ``DELIVERY_BREAKER_MAX_FAILURES`` consecutive *dead-target*
failures (unknown channel/user) on one target, the breaker trips and every
subsequent delivery to that target is skipped before it ever reaches the
interface. The tripped state is promoted into a persisted, purgeable registry
(``delivery_dead_targets``) surfaced in the WebUI Logs > Dead Targets tab.

Design rules (per AGENTS.md):

* **Fail-open.** A broken guard must never block legitimate delivery. Every
  DB read/write and every config read is best-effort; on error the guard
  simply refuses to skip and reports nothing. The breaker only *skips* a
  target when it has positively and structurally tripped.
* **Structural classification.** The dead-target signal is an exception type
  (``DeadTargetError``) plus well-known third-party error types. A narrow
  message-marker fallback exists only for interfaces that surface permanent
  target loss as a plain string (e.g. Telegram/Matrix) — this is error
  classification, not intent/routing logic.
* **Transient failures never trip.** Timeouts and network errors must not
  mark a target dead; only *permanent* target loss does.
* **Reusable primitive.** The same ``DeliveryGuard`` can later be reused for
  vessel reconnect loops and ``send_file_*`` retries (see the feature review
  §6 "circuit breaker as a first-class core primitive").
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from core.config_manager import config_registry
from core.logging_utils import log_debug, log_info, log_warning
from core.variables_engine import register_exposed_var


class DeadTargetError(Exception):
    """A delivery target no longer exists (unknown channel/user) and will never
    accept a message. Raised by interfaces instead of a bare ``RuntimeError`` so
    the guard can classify the failure structurally."""

    def __init__(self, target: Any = None) -> None:
        self.target = target
        if target is None:
            super().__init__("Unknown channel or user")
        else:
            super().__init__(f"Unknown channel or user: {target}")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

register_exposed_var(
    "DELIVERY_BREAKER_ENABLED",
    label="Delivery Circuit Breaker",
    default=1,
    value_type=int,
    ui_type="bool",
    description=(
        "After repeated 'unknown channel/user' failures on a delivery target, "
        "stop delivering to it and mark it dead instead of retrying forever."
    ),
    scope="core",
    component="delivery_guard",
)

register_exposed_var(
    "DELIVERY_BREAKER_MAX_FAILURES",
    label="Circuit Breaker Trip Threshold",
    default=3,
    value_type=int,
    ui_type="number",
    description=(
        "Number of consecutive dead-target (unknown channel/user) failures "
        "before a target is marked dead and delivery is skipped."
    ),
    scope="core",
    component="delivery_guard",
    advanced=True,
)

_DEFAULT_MAX_FAILURES = 3

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS delivery_dead_targets (
    id INT AUTO_INCREMENT PRIMARY KEY,
    interface VARCHAR(255) NOT NULL,
    chat_id VARCHAR(255) NOT NULL,
    reason TEXT,
    consecutive_failures INT NOT NULL DEFAULT 0,
    last_failure_at DATETIME,
    marked_dead_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_dead_target_interface (interface(191)),
    INDEX idx_dead_target_chat_id (chat_id(191)),
    INDEX idx_dead_target_marked_dead_at (marked_dead_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

# Narrow fallback markers used ONLY when a failure is not already a structural
# exception type. These describe *permanent* target loss; transient conditions
# (timeout, connection, rate-limit) are deliberately absent so they never trip.
_DEAD_TARGET_MARKERS: tuple[str, ...] = (
    "unknown channel",
    "unknown user",
    "unknown member",
    "unknown message",
    "channel not found",
    "user not found",
    "chat not found",
    "room not found",
    "member not found",
    "not found",
)


def classify_delivery_failure(exc: BaseException | None) -> str:
    """Return ``"dead_target"`` for permanent target-loss failures, else
    ``"transient"``. Transient failures must never trip the breaker."""
    if exc is None:
        return "transient"
    if isinstance(exc, DeadTargetError):
        return "dead_target"

    # Discord's own exception hierarchy (structural, no string matching).
    try:
        import discord

        if isinstance(exc, (discord.errors.NotFound, discord.errors.Forbidden)):
            return "dead_target"
    except Exception:  # pragma: no cover - discord optional
        pass

    message = str(exc or "").lower()
    for marker in _DEAD_TARGET_MARKERS:
        if marker in message:
            return "dead_target"
    return "transient"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class DeliveryGuard:
    """Per-target circuit breaker backed by the ``delivery_dead_targets`` table.

    In-memory state is authoritative within the process; the DB row persists the
    tripped (dead) flag and the consecutive-failure counter across restarts.
    """

    def __init__(self) -> None:
        self._failure_counts: dict[str, int] = {}
        self._dead: set[str] = set()
        self._dead_loaded = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------
    def is_enabled(self) -> bool:
        try:
            value = config_registry.get_value("DELIVERY_BREAKER_ENABLED", 1)
            if isinstance(value, str):
                return value.strip().lower() not in ("0", "false", "no", "off")
            return bool(value)
        except Exception:
            return True

    def max_failures(self) -> int:
        try:
            value = config_registry.get_value(
                "DELIVERY_BREAKER_MAX_FAILURES", _DEFAULT_MAX_FAILURES
            )
            parsed = int(value)
            return parsed if parsed >= 1 else _DEFAULT_MAX_FAILURES
        except Exception:
            return _DEFAULT_MAX_FAILURES

    @staticmethod
    def target_key(interface: str, chat_id: Any) -> str:
        return f"{interface or 'unknown'}\x00{chat_id}"

    # ------------------------------------------------------------------
    # In-memory + DB plumbing
    # ------------------------------------------------------------------
    async def _ensure_table(self) -> None:
        from core.db import get_conn_ctx

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_TABLE_SQL)
            try:
                await conn.commit()
            except Exception:
                pass

    async def _load_dead_set(self) -> None:
        if self._dead_loaded:
            return
        self._dead_loaded = True
        threshold = self.max_failures()
        try:
            from core.db import get_conn_ctx

            await self._ensure_table()
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT interface, chat_id FROM delivery_dead_targets "
                        "WHERE consecutive_failures >= %s",
                        [threshold],
                    )
                    rows = await cur.fetchall()
            for row in rows:
                if not row:
                    continue
                interface = row[0] if len(row) > 0 else None
                chat_id = row[1] if len(row) > 1 else None
                if interface is not None and chat_id is not None:
                    self._dead.add(self.target_key(str(interface), str(chat_id)))
            if self._dead:
                log_info(
                    f"[delivery_guard] Loaded {len(self._dead)} dead delivery target(s) "
                    "from registry"
                )
        except Exception as exc:
            log_warning(f"[delivery_guard] Failed to load dead targets: {exc}")

    async def _persist_failure(
        self, interface: str, chat_id: str, reason: str, count: int, dead: bool
    ) -> None:
        try:
            from core.db import get_conn_ctx

            await self._ensure_table()
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT id FROM delivery_dead_targets "
                        "WHERE interface = %s AND chat_id = %s",
                        [interface, chat_id],
                    )
                    row = await cur.fetchone()
                    if row is None:
                        await cur.execute(
                            "INSERT INTO delivery_dead_targets "
                            "(interface, chat_id, reason, consecutive_failures, "
                            " last_failure_at, marked_dead_at) "
                            "VALUES (%s, %s, %s, %s, %s, %s)",
                            [
                                interface,
                                chat_id,
                                reason,
                                count,
                                _now_utc(),
                                _now_utc() if dead else None,
                            ],
                        )
                    else:
                        await cur.execute(
                            "UPDATE delivery_dead_targets "
                            "SET reason = %s, consecutive_failures = %s, "
                            "    last_failure_at = %s, marked_dead_at = %s "
                            "WHERE interface = %s AND chat_id = %s",
                            [
                                reason,
                                count,
                                _now_utc(),
                                _now_utc() if dead else None,
                                interface,
                                chat_id,
                            ],
                        )
                try:
                    await conn.commit()
                except Exception:
                    pass
        except Exception as exc:
            log_warning(f"[delivery_guard] Failed to persist breaker state: {exc}")

    async def _delete_row(self, interface: str, chat_id: str) -> None:
        try:
            from core.db import get_conn_ctx

            await self._ensure_table()
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM delivery_dead_targets "
                        "WHERE interface = %s AND chat_id = %s",
                        [interface, chat_id],
                    )
                try:
                    await conn.commit()
                except Exception:
                    pass
        except Exception as exc:
            log_warning(f"[delivery_guard] Failed to clear breaker row: {exc}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def should_skip(self, interface: str, chat_id: Any) -> bool:
        """Return True when delivery to this target must be skipped (breaker
        open). Fail-open: any error resolves to ``False`` (deliver anyway)."""
        if not self.is_enabled():
            return False
        try:
            await self._load_dead_set()
            key = self.target_key(interface, chat_id)
            async with self._lock:
                return key in self._dead
        except Exception as exc:
            log_debug(f"[delivery_guard] should_skip errored (failing open): {exc}")
            return False

    async def record_failure(
        self, interface: str, chat_id: Any, exc: BaseException | None = None
    ) -> dict[str, Any]:
        """Record a dead-target delivery failure; trip the breaker when the
        consecutive-failure threshold is reached."""
        if not self.is_enabled():
            return {"dead": False, "consecutive_failures": 0}
        interface = str(interface or "unknown")
        chat_id = str(chat_id)
        reason = str(exc) if exc is not None else "Unknown channel or user"
        key = self.target_key(interface, chat_id)

        async with self._lock:
            count = self._failure_counts.get(key, 0) + 1
            self._failure_counts[key] = count
            dead = count >= self.max_failures()
            if dead:
                self._dead.add(key)
                log_warning(
                    f"[delivery_guard] ⚡ Circuit breaker OPEN for {interface}/{chat_id} "
                    f"after {count} consecutive dead-target failures; delivery will be skipped"
                )

        await self._persist_failure(interface, chat_id, reason, count, dead)
        return {"dead": dead, "consecutive_failures": count}

    async def record_success(self, interface: str, chat_id: Any) -> None:
        """Reset the breaker for a target that delivered successfully."""
        if not self.is_enabled():
            return
        interface = str(interface or "unknown")
        chat_id = str(chat_id)
        key = self.target_key(interface, chat_id)

        async with self._lock:
            had_count = self._failure_counts.pop(key, None) is not None
            was_dead = key in self._dead
            self._dead.discard(key)

        # Fast path: the target was never failing, so there is nothing persisted
        # to clear — skip the DB round-trip entirely (this is the common case).
        if not had_count and not was_dead:
            return

        if was_dead:
            log_info(
                f"[delivery_guard] Circuit breaker CLOSED for {interface}/{chat_id} "
                "after a successful delivery"
            )
        await self._delete_row(interface, chat_id)

    async def list_dead_targets(self) -> list[dict[str, Any]]:
        """Return the persisted dead-target registry entries (threshold met)."""
        threshold = self.max_failures()
        entries: list[dict[str, Any]] = []
        try:
            from core.db import get_conn_ctx

            await self._ensure_table()
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT id, interface, chat_id, reason, consecutive_failures, "
                        "       last_failure_at, marked_dead_at "
                        "FROM delivery_dead_targets "
                        "WHERE consecutive_failures >= %s "
                        "ORDER BY marked_dead_at DESC, id DESC",
                        [threshold],
                    )
                    rows = await cur.fetchall()
            for row in rows:
                entries.append(
                    {
                        "id": row[0],
                        "interface": row[1],
                        "chat_id": row[2],
                        "reason": row[3],
                        "consecutive_failures": row[4],
                        "last_failure_at": row[5],
                        "marked_dead_at": row[6],
                    }
                )
        except Exception as exc:
            log_warning(f"[delivery_guard] Failed to list dead targets: {exc}")
        return entries

    async def revive_target(self, target_id: int) -> bool:
        """Remove a dead-target registry entry (by row id) and re-arm delivery.

        Fails open: on any DB error the in-memory set is still cleared so the
        process does not keep skipping a target the user asked to revive.
        """
        interface: str | None = None
        chat_id: str | None = None
        try:
            from core.db import get_conn_ctx

            await self._ensure_table()
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT interface, chat_id FROM delivery_dead_targets WHERE id = %s",
                        [target_id],
                    )
                    row = await cur.fetchone()
                    if row is not None:
                        interface = row[0] if len(row) > 0 else None
                        chat_id = row[1] if len(row) > 1 else None
                    await cur.execute(
                        "DELETE FROM delivery_dead_targets WHERE id = %s", [target_id]
                    )
                try:
                    await conn.commit()
                except Exception:
                    pass
        except Exception as exc:
            log_warning(f"[delivery_guard] Failed to revive target {target_id}: {exc}")

        if interface is not None and chat_id is not None:
            key = self.target_key(str(interface), str(chat_id))
            async with self._lock:
                self._dead.discard(key)
                self._failure_counts.pop(key, None)
            log_info(f"[delivery_guard] Revived delivery target {interface}/{chat_id}")
            return True
        return False


# Module-level singleton — import-safe and side-effect-free (table creation is
# lazy and best-effort), so importing this module never breaks startup.
delivery_guard = DeliveryGuard()
