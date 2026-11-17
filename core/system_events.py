"""Tiny window of recent *system* events for the prompt.

The persona otherwise has no idea that the synth was restarted, how long it was
away, or that a plugin / interface was switched on or off a moment ago. This
module keeps a short, deterministic event log (no LLM, no keyword detection)
and renders the last few events as a small prompt block.

Recorded events (``kind``):

* ``boot``               — the core finished initialising. ``detail`` carries a
                           rough "offline ~N min" estimate when one is known.
* ``shutdown``           — a graceful stop or restart was requested.
* ``component_enabled`` / ``component_disabled`` — a plugin or interface was
                           toggled at runtime by the core (``component`` is the
                           name, ``detail`` is ``plugin`` or ``interface``).
* ``variable_changed``   — the user explicitly changed an exposed variable
                           (``component`` is the key, ``detail`` is
                           ``old -> new``, or ``value hidden`` for a sensitive
                           one). Edits of one key within a minute collapse into
                           a single event, so dragging a slider is one line.

How long the synth was offline
------------------------------
The container can be killed at any moment, so the figure is a *rough* one:

1. A heartbeat row (``kind = 'heartbeat'``, updated in place every
   ``SYSTEM_HEARTBEAT_SECONDS``) records when the synth was last alive.
2. A graceful stop records a ``shutdown`` event; if it is at least as recent as
   the heartbeat the stop was *clean* and the moment is exact.
3. With no heartbeat (first boot after this feature shipped) the last known
   action — the newest ``chat_history_cache`` / ``turn_reason_trail`` /
   ``grillo_activity_log`` row — is used, and the figure is labelled as an
   estimate.
4. With none of those nothing is claimed: the event just says "restarted".

Design rules (per AGENTS.md): fail-open — nothing here may ever raise into the
caller or cost a turn — and DB writes degrade to an in-memory list.

Portability: the database is Postgres or MariaDB depending on the deployment.
Timestamps are therefore never passed as parameters: rows get the database's own
``CURRENT_TIMESTAMP`` and ages are computed by the database (``NOW() - ts``), so
no time-zone or driver conversion can skew the figures.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from core.config_manager import config_registry
from core.logging_utils import log_debug, log_warning
from core.variables_engine import register_exposed_var

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DEFAULTS: dict[str, int] = {
    "SYSTEM_EVENTS_MAX_ROWS": 200,
    "SYSTEM_EVENTS_MAX_SHOWN": 5,
    "SYSTEM_EVENTS_WINDOW_MINUTES": 360,
    "SYSTEM_HEARTBEAT_SECONDS": 60,
}

_LABELS: dict[str, tuple[str, str]] = {
    "SYSTEM_EVENTS_MAX_ROWS": (
        "System Events Max Rows",
        "Rows of the system-event log kept before the oldest are pruned.",
    ),
    "SYSTEM_EVENTS_MAX_SHOWN": (
        "System Events Shown",
        "Most recent system events (restart, plugin on/off) shown in the prompt.",
    ),
    "SYSTEM_EVENTS_WINDOW_MINUTES": (
        "System Events Window (min)",
        "Only system events newer than this many minutes appear in the prompt.",
    ),
    "SYSTEM_HEARTBEAT_SECONDS": (
        "System Heartbeat (s)",
        "How often the 'last seen alive' stamp used to estimate offline time is updated.",
    ),
}

_VARS: dict[str, Any] = {}
for _key, _default in _DEFAULTS.items():
    _label, _description = _LABELS[_key]
    register_exposed_var(
        _key,
        label=_label,
        default=_default,
        value_type=int,
        ui_type="number",
        description=_description,
        scope="core",
        component="diagnostics",
        advanced=True,
    )
    _VARS[_key] = config_registry.get_var(
        _key,
        _default,
        label=_label,
        description=_description,
        value_type=int,
        component="diagnostics",
        advanced=True,
    )


def _cfg(key: str) -> int:
    """Read a positive int config value, falling back to its default."""
    default = _DEFAULTS[key]
    try:
        value = _VARS[key].value
        if isinstance(value, bool):
            return default
        parsed = int(value)
        return parsed if parsed > 0 else default
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Schema + lazy init
# ---------------------------------------------------------------------------

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS system_events (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts DATETIME DEFAULT CURRENT_TIMESTAMP,
    kind VARCHAR(32) NOT NULL,
    component VARCHAR(100),
    detail VARCHAR(500),
    INDEX idx_system_events_ts (ts),
    INDEX idx_system_events_kind (kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

_table_ready = False

# In-memory fallback, used only while the DB is unreachable.
_mem_events: deque[dict[str, Any]] = deque(maxlen=50)
_mem_ids = itertools.count(start=-1, step=-1)
_heartbeat_task: asyncio.Task[None] | None = None
_heartbeat_paused = False


async def ensure_system_events_table() -> None:
    """Create the table once per process."""
    global _table_ready
    if _table_ready:
        return
    from core.db import get_conn_ctx

    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_TABLE_SQL)
        try:
            await conn.commit()
        except Exception:
            pass
    _table_ready = True


def _age_seconds_sql(expr: str) -> str:
    """SQL for "seconds between ``expr`` and now", computed by the database."""
    from core.db import _get_db_type

    if _get_db_type() == "postgres":
        return f"EXTRACT(EPOCH FROM (NOW() - {expr}))"
    return f"TIMESTAMPDIFF(SECOND, {expr}, NOW())"


def _seconds(value: Any) -> int | None:
    """Coerce a DB age (int / float / Decimal / None) to whole seconds >= 0."""
    if value is None:
        return None
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Any) -> datetime | None:
    """Normalise a DB / ISO value to an aware UTC datetime (naive == UTC)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return None


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


async def record_event(
    kind: str,
    component: str | None = None,
    detail: str | None = None,
) -> None:
    """Persist one event (stamped by the database). Never raises.

    Degrades to the in-memory list when the database is unreachable.
    """
    component = component[:100] if component else None
    detail = detail[:500] if detail else None
    try:
        await ensure_system_events_table()
        from core.db import get_conn_ctx

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO system_events (kind, component, detail) "
                    "VALUES (%s, %s, %s)",
                    [kind[:32], component, detail],
                )
                await cur.execute(
                    "DELETE FROM system_events WHERE kind <> 'heartbeat' AND id NOT IN "
                    "(SELECT id FROM (SELECT id FROM system_events "
                    "WHERE kind <> 'heartbeat' ORDER BY id DESC LIMIT %s) AS keep)",
                    [_cfg("SYSTEM_EVENTS_MAX_ROWS")],
                )
            try:
                await conn.commit()
            except Exception:
                pass
    except Exception as exc:
        _mem_events.append(
            {
                "id": next(_mem_ids),
                "ts": _utcnow(),
                "kind": kind,
                "component": component,
                "detail": detail,
            }
        )
        log_warning(f"[system_events] DB record failed, kept in memory: {exc}")


async def record_component_toggle(name: str, kind: str, enabled: bool) -> None:
    """Record that a plugin / interface was switched on or off by the core."""
    await record_event(
        "component_enabled" if enabled else "component_disabled",
        component=name,
        detail=kind,
    )


#: Edits of the same variable closer together than this become one event.
_VAR_COLLAPSE_SECONDS = 60.0
_VAR_VALUE_CHARS = 60
# key -> (monotonic time of the last edit, text of the value before the burst)
_var_edits: dict[str, tuple[float, str]] = {}


def _short_value(value: Any) -> str:
    text = " ".join(str(value).split())
    if len(text) > _VAR_VALUE_CHARS:
        text = text[: _VAR_VALUE_CHARS - 1] + "…"
    return text if text else "''"


async def _update_last_variable_event(key: str, detail: str) -> bool:
    """Fold a new edit into the previous event of the same key. True if done."""
    for event in reversed(_mem_events):
        if event.get("kind") == "variable_changed" and event.get("component") == key:
            event["detail"] = detail
            event["ts"] = _utcnow()
            return True
    try:
        await ensure_system_events_table()
        from core.db import get_conn_ctx

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE system_events SET detail = %s, ts = CURRENT_TIMESTAMP "
                    "WHERE id = (SELECT m FROM (SELECT MAX(id) AS m FROM system_events "
                    "WHERE kind = 'variable_changed' AND component = %s) AS last)",
                    [detail[:500], key],
                )
                updated = int(getattr(cur, "rowcount", 0) or 0) > 0
            try:
                await conn.commit()
            except Exception:
                pass
        return updated
    except Exception as exc:
        log_debug(f"[system_events] variable event not folded: {exc}")
        return False


async def _drop_last_variable_event(key: str) -> None:
    """Remove the newest event of ``key`` (an edit burst that ended where it began)."""
    for event in reversed(_mem_events):
        if event.get("kind") == "variable_changed" and event.get("component") == key:
            _mem_events.remove(event)
            return
    try:
        await ensure_system_events_table()
        from core.db import get_conn_ctx

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM system_events WHERE id = (SELECT m FROM "
                    "(SELECT MAX(id) AS m FROM system_events "
                    "WHERE kind = 'variable_changed' AND component = %s) AS last)",
                    [key],
                )
            try:
                await conn.commit()
            except Exception:
                pass
    except Exception as exc:
        log_debug(f"[system_events] variable event not dropped: {exc}")


async def record_variable_change(
    key: str, old: Any, new: Any, *, sensitive: bool = False
) -> None:
    """Note that the user explicitly changed an exposed variable. Never raises.

    ``sensitive`` variables (tokens, keys) log the fact only, never the values.
    """
    try:
        if not sensitive and str(old) == str(new):
            return
        now = time.monotonic()
        before = _short_value(old)
        previous = _var_edits.get(key)
        if previous and now - previous[0] < _VAR_COLLAPSE_SECONDS:
            before = previous[1]
        detail = "value hidden" if sensitive else f"{before} -> {_short_value(new)}"
        in_burst = bool(previous and now - previous[0] < _VAR_COLLAPSE_SECONDS)
        if in_burst and not sensitive and before == _short_value(new):
            # The burst ended where it began: nothing changed in the end.
            await _drop_last_variable_event(key)
            _var_edits.pop(key, None)
            return
        if in_burst and await _update_last_variable_event(key, detail):
            _var_edits[key] = (now, before)
            return
        _var_edits[key] = (now, _short_value(old))
        await record_event("variable_changed", component=key, detail=detail)
    except Exception as exc:  # pragma: no cover - defensive, fail-open
        log_debug(f"[system_events] variable change not recorded: {exc}")


async def touch_heartbeat() -> None:
    """Stamp "alive now" on the single heartbeat row. Never raises."""
    if _heartbeat_paused:
        return
    try:
        await ensure_system_events_table()
        from core.db import get_conn_ctx

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE system_events SET ts = CURRENT_TIMESTAMP "
                    "WHERE kind = 'heartbeat'"
                )
                updated = int(getattr(cur, "rowcount", 0) or 0)
                if updated == 0:
                    # MariaDB reports 0 when the stamp did not change within the
                    # same second, so check before inserting a first row.
                    await cur.execute(
                        "SELECT COUNT(*) FROM system_events WHERE kind = 'heartbeat'"
                    )
                    row = await cur.fetchone()
                    if not row or int(row[0] or 0) == 0:
                        await cur.execute(
                            "INSERT INTO system_events (kind) VALUES ('heartbeat')"
                        )
            try:
                await conn.commit()
            except Exception:
                pass
    except Exception as exc:
        log_debug(f"[system_events] heartbeat not stored: {exc}")


async def _heartbeat_loop() -> None:
    while True:
        await asyncio.sleep(_cfg("SYSTEM_HEARTBEAT_SECONDS"))
        await touch_heartbeat()


def start_heartbeat() -> None:
    """(Re)start the background heartbeat. Call from inside a running loop."""
    global _heartbeat_task, _heartbeat_paused
    _heartbeat_paused = False
    if _heartbeat_task is not None and not _heartbeat_task.done():
        return
    try:
        _heartbeat_task = asyncio.get_running_loop().create_task(_heartbeat_loop())
        _heartbeat_task.set_name("system_events_heartbeat")
    except Exception as exc:
        log_debug(f"[system_events] heartbeat not started: {exc}")


async def record_shutdown(reason: str = "") -> None:
    """Record a graceful stop and freeze the heartbeat so it cannot outrun it."""
    global _heartbeat_task, _heartbeat_paused
    _heartbeat_paused = True
    task, _heartbeat_task = _heartbeat_task, None
    if task is not None:
        task.cancel()
    await record_event("shutdown", detail=reason or None)


# ---------------------------------------------------------------------------
# Offline estimate
# ---------------------------------------------------------------------------


async def _fetch_one(sql: str) -> Any:
    from core.db import get_conn_ctx

    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql)
            row = await cur.fetchone()
    return row[0] if row else None


async def estimate_offline() -> dict[str, Any] | None:
    """Rough "how long was the synth away" for the boot that is happening now.

    Returns ``{"seconds", "basis", "clean", "last_seen"}`` or ``None`` when no
    evidence exists. ``basis`` is ``heartbeat``, ``shutdown`` or
    ``last_action``; ``last_seen`` is an aware UTC datetime or ``None``.
    """
    try:
        await ensure_system_events_table()
        heartbeat_age = _seconds(
            await _fetch_one(
                f"SELECT {_age_seconds_sql('MAX(ts)')} FROM system_events "
                "WHERE kind = 'heartbeat'"
            )
        )
        shutdown_age = _seconds(
            await _fetch_one(
                f"SELECT {_age_seconds_sql('MAX(ts)')} FROM system_events "
                "WHERE kind = 'shutdown'"
            )
        )
    except Exception as exc:
        log_debug(f"[system_events] offline estimate unavailable: {exc}")
        return None

    ages = [age for age in (heartbeat_age, shutdown_age) if age is not None]
    if ages:
        seconds = min(ages)  # the smaller the age, the more recent the stamp
        # A graceful stop is at least as recent as the last heartbeat (the
        # heartbeat is frozen first); allow a second of slack.
        clean = shutdown_age is not None and (
            heartbeat_age is None or shutdown_age <= heartbeat_age + 1
        )
        return {
            "seconds": seconds,
            "basis": "shutdown" if clean else "heartbeat",
            "clean": clean,
            "last_seen": _utcnow() - timedelta(seconds=seconds),
        }

    # No heartbeat yet: fall back on the newest action the synth recorded.
    action_ages: list[int] = []
    for table, column in (
        ("chat_history_cache", "created_at"),
        ("turn_reason_trail", "created_at"),
        ("grillo_activity_log", "executed_at"),
    ):
        try:
            age = _seconds(
                await _fetch_one(
                    f"SELECT {_age_seconds_sql(f'MAX({column})')} FROM {table}"
                )
            )
        except Exception:
            continue
        if age is not None:
            action_ages.append(age)
    if action_ages:
        return {
            "seconds": min(action_ages),
            "basis": "last_action",
            "clean": False,
            "last_seen": None,
        }
    return None


def _duration(seconds: int) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 90:
        return f"{minutes} min"
    hours = round(seconds / 3600)
    if hours < 48:
        return f"{hours} h"
    return f"{round(seconds / 86400)} days"


#: Below this the gap is just the restart itself and is not worth a mention.
_MIN_OFFLINE_SECONDS = 120


def describe_offline(estimate: dict[str, Any] | None) -> str:
    """Human-readable offline summary for the ``boot`` event ('' when unknown)."""
    if not estimate:
        return ""
    seconds = int(estimate.get("seconds") or 0)
    if seconds < _MIN_OFFLINE_SECONDS:
        return ""
    text = f"offline ~{_duration(seconds)}"
    basis = estimate.get("basis")
    if basis == "last_action":
        return f"{text}, estimated from the last recorded activity"
    text += ", clean shutdown" if estimate.get("clean") else ", unexpected stop"
    last_seen = estimate.get("last_seen")
    if isinstance(last_seen, datetime):
        text += f" (last seen {_local_clock(last_seen)})"
    return text


async def record_boot() -> None:
    """Record that the core is up, with the offline estimate; start the heartbeat."""
    try:
        estimate = await estimate_offline()
        await record_event("boot", detail=describe_offline(estimate) or None)
        await touch_heartbeat()
        start_heartbeat()
    except Exception as exc:  # pragma: no cover - defensive, fail-open
        log_warning(f"[system_events] boot not recorded: {exc}")


# ---------------------------------------------------------------------------
# Reading + rendering
# ---------------------------------------------------------------------------


def _local(dt: datetime) -> datetime:
    from core.time_zone_utils import utc_to_local

    return utc_to_local(dt)


def _local_clock(dt: datetime) -> str:
    return _local(dt).strftime("%H:%M")


async def recent_events(
    limit: int | None = None, window_minutes: int | None = None
) -> list[dict[str, Any]]:
    """Newest ``limit`` events inside the window, oldest first. Never raises."""
    cap = limit if limit and limit > 0 else _cfg("SYSTEM_EVENTS_MAX_SHOWN")
    window = (
        window_minutes
        if window_minutes and window_minutes > 0
        else _cfg("SYSTEM_EVENTS_WINDOW_MINUTES")
    )
    now = _utcnow()
    cutoff = now - timedelta(minutes=window)
    events: list[dict[str, Any]] = [
        dict(e) for e in _mem_events if (_as_utc(e.get("ts")) or cutoff) >= cutoff
    ]
    try:
        await ensure_system_events_table()
        from core.db import get_conn_ctx

        # The age is computed by the database, so its time zone never matters;
        # the wall-clock stamp is rebuilt from it here.
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT id, {_age_seconds_sql('ts')}, kind, component, detail "
                    "FROM system_events WHERE kind <> 'heartbeat' "
                    "ORDER BY id DESC LIMIT %s",
                    [cap],
                )
                rows = await cur.fetchall()
        for row in rows:
            age = _seconds(row[1])
            if age is None or age > window * 60:
                continue
            events.append(
                {
                    "id": row[0],
                    "ts": now - timedelta(seconds=age),
                    "kind": row[2],
                    "component": row[3],
                    "detail": row[4],
                }
            )
    except Exception as exc:
        log_debug(f"[system_events] read failed, using in-memory view: {exc}")
    events.sort(key=lambda e: (e["ts"], e.get("id") or 0))
    return events[-cap:]


def _event_text(event: dict[str, Any]) -> str:
    kind = event.get("kind")
    component = event.get("component") or ""
    detail = event.get("detail") or ""
    if kind == "boot":
        return f"synth restarted ({detail})" if detail else "synth restarted"
    if kind == "shutdown":
        return f"synth shutting down ({detail})" if detail else "synth shutting down"
    if kind == "bot_ward":
        return f"conversation with bot {component} paused {detail}".strip()
    if kind == "variable_changed":
        return f"user changed setting {component}: {detail}".rstrip(": ")
    if kind in ("component_enabled", "component_disabled"):
        state = "enabled" if kind == "component_enabled" else "disabled"
        label = f"{detail} {component}".strip() if detail else component
        return f"{label} {state}".strip()
    return " ".join(part for part in (str(kind or ""), component, detail) if part)


def format_events_block(events: list[dict[str, Any]]) -> str:
    """Render events as ``- HH:MM text`` lines ('' when there are none)."""
    if not events:
        return ""
    today = _local(_utcnow()).date()
    lines: list[str] = []
    for event in events:
        stamp = _as_utc(event.get("ts"))
        if stamp is None:
            continue
        local = _local(stamp)
        prefix = local.strftime("%H:%M")
        if local.date() != today:
            prefix = f"{local.strftime('%m-%d')} {prefix}"
        text = _event_text(event)
        if text:
            lines.append(f"- {prefix} {text}")
    return "\n".join(lines)


async def get_system_events_block() -> str:
    """The prompt block: the last few events inside the window ('' if none)."""
    try:
        return format_events_block(await recent_events())
    except Exception as exc:  # pragma: no cover - defensive, fail-open
        log_debug(f"[system_events] block skipped: {exc}")
        return ""
