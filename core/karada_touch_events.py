"""Karada touch-event manager (WebUI 3D interaction history).

This module records 3D-interaction events coming from the WebUI avatar scene
(taps on the empty environment, on winbox windows, or directly on the Synth
model) and turns them, in a debounced/batched way, into chat-history lines so
Synth is aware of physical interaction with her Karada (body).

Design (see project plan):

- Two logical caches, both persisted on the ``karada_touch_events`` table:
    * *environment* events (``environment_tap`` / ``window_tap``): short-lived
      (~10 min TTL). Flushed as passive history lines attached to the next
      turn; they NEVER trigger a spontaneous LLM turn on their own.
    * *synth* events (``synth_touch``): kept for a sliding 24h window for later
      reflection. A flushed group that contains at least one ``synth_touch``
      triggers a spontaneous LLM turn via the message queue.

- Flushing is debounced by ``TOUCH_EVENT_FLUSH_COOLDOWN_S`` (default 15s):
  incoming events accumulate, and only after the cooldown elapses are they
  grouped into history lines.

- Sensitive body-part names are de-sexualized to neutral, safe terms before
  being written to history (the raw part is preserved separately in the DB for
  internal use only, never surfaced to the prompt).

The toggle ``ENABLE_3D_INTERACTION_HISTORY`` (default ON) gates everything: the
frontend always sends events, and this module drops them when the toggle is
off.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from core.config_manager import config_registry
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.variables_engine import register_exposed_var


# ---------------------------------------------------------------------------
# Exposed variables
# ---------------------------------------------------------------------------

register_exposed_var(
    "ENABLE_3D_INTERACTION_HISTORY",
    label="Record 3D Interaction History",
    default=1,
    value_type=int,
    ui_type="bool",
    description=(
        "When enabled, taps and touches in the WebUI 3D scene (environment, "
        "windows, or the Synth avatar) are recorded into chat history so Synth "
        "is aware of physical interaction."
    ),
    scope="core",
    component="karada",
)

register_exposed_var(
    "TOUCH_EVENT_FLUSH_COOLDOWN_S",
    label="Touch Event Flush Cooldown (s)",
    default=10,
    value_type=int,
    ui_type="number",
    description=(
        "Seconds to batch incoming 3D interaction events before flushing them "
        "into chat history. Prevents flooding history with individual taps."
    ),
    scope="core",
    component="karada",
    advanced=True,
)

register_exposed_var(
    "TOUCH_EVENT_ENV_TTL_S",
    label="Environment Touch TTL (s)",
    default=600,
    value_type=int,
    ui_type="number",
    description=(
        "How long unflushed environment/window tap events are retained before "
        "being discarded (default 10 minutes)."
    ),
    scope="core",
    component="karada",
    advanced=True,
)

register_exposed_var(
    "TOUCH_EVENT_SYNTH_TTL_S",
    label="Synth Touch TTL (s)",
    default=86400,
    value_type=int,
    ui_type="number",
    description=(
        "How long synth_touch events are retained on the sliding window for "
        "later reflection (default 24 hours)."
    ),
    scope="core",
    component="karada",
    advanced=True,
)

register_exposed_var(
    "KARADA_EXPLICIT_TOUCH",
    label="Explicit Touch Terms",
    default=0,
    value_type=int,
    ui_type="bool",
    description=(
        "When enabled, precise anatomical touch points (from the touch-zone "
        "catalog) are injected into the prompt using their technical term "
        "instead of being neutralized. When disabled (default), touch points "
        "are neutralized to safe, generic terms."
    ),
    scope="core",
    component="karada",
    advanced=True,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Event type identifiers.
EVENT_ENVIRONMENT_TAP = "environment_tap"
EVENT_WINDOW_TAP = "window_tap"
EVENT_SYNTH_TOUCH = "synth_touch"

_VALID_EVENT_TYPES = frozenset(
    {EVENT_ENVIRONMENT_TAP, EVENT_WINDOW_TAP, EVENT_SYNTH_TOUCH}
)

# Dedup window: identical events within this many seconds are collapsed.
_DEDUP_WINDOW_S = 2.0

# Mapping of sensitive/anatomical raw part names to neutral, safe terms.
# Matching is done on a normalized (lowercased) key; the raw part is preserved
# in the DB but only the neutral term is ever written to history.
_BODY_PART_DESEXUALIZE: dict[str, str] = {
    "bust": "upper body",
    "breast": "upper body",
    "breasts": "upper body",
    "chest": "chest",
    "boob": "upper body",
    "boobs": "upper body",
    "nipple": "upper body",
    "nipples": "upper body",
    "crotch": "lower body",
    "groin": "lower body",
    "genital": "lower body",
    "genitals": "lower body",
    "pelvis": "lower body",
    "hip": "hip",
    "hips": "hip",
    "butt": "lower back",
    "buttock": "lower back",
    "buttocks": "lower back",
    "thigh": "leg",
    "thighs": "leg",
    "upperleg": "leg",
    "lowerleg": "leg",
    "shin": "leg",
    "calf": "leg",
    "foot": "foot",
    "feet": "foot",
    "toe": "foot",
    "hand": "hand",
    "arm": "arm",
    "forearm": "arm",
    "shoulder": "shoulder",
    "neck": "neck",
    "head": "head",
    "face": "face",
    "hair": "hair",
    "back": "back",
    "belly": "stomach",
    "stomach": "stomach",
    "abdomen": "stomach",
}


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

# In-memory buffer of pending (unflushed) events, keyed by interface_path.
# Each entry is a list of event dicts already persisted to the DB.
_pending_events: dict[str, list[dict[str, Any]]] = {}

# Per-interface debounce timers. RUF006: keep strong references so the tasks
# are not garbage-collected mid-flight.
_flush_tasks: dict[str, asyncio.Task[None]] = {}

# Set of fire-and-forget helper tasks (cleanup, spontaneous-turn enqueue).
_background_tasks: set[asyncio.Task[Any]] = set()

# Guard so the schema is created only once per process.
_table_ready = False

# Handle to the periodic cleanup task.
_cleanup_task: Optional[asyncio.Task[None]] = None


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _is_enabled() -> bool:
    try:
        return bool(int(config_registry.get_value("ENABLE_3D_INTERACTION_HISTORY", 1)))
    except Exception:
        return True


def _flush_cooldown_s() -> float:
    try:
        return max(
            1.0, float(config_registry.get_value("TOUCH_EVENT_FLUSH_COOLDOWN_S", 15))
        )
    except Exception:
        return 15.0


def _env_ttl_s() -> int:
    try:
        return max(1, int(config_registry.get_value("TOUCH_EVENT_ENV_TTL_S", 600)))
    except Exception:
        return 600


def _synth_ttl_s() -> int:
    try:
        return max(1, int(config_registry.get_value("TOUCH_EVENT_SYNTH_TTL_S", 86400)))
    except Exception:
        return 86400


def _explicit_touch_enabled() -> bool:
    """Whether precise anatomical technical terms should reach the prompt."""
    try:
        return bool(int(config_registry.get_value("KARADA_EXPLICIT_TOUCH", 0)))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Touch-zone catalog (shared with the WebUI frontend)
# ---------------------------------------------------------------------------

# Single source of truth for precise touch-zone ids and their term mappings.
# The same file drives the WebUI 3D zoning (vrm-viewer.mjs). Here we only need
# the id -> {technical, neutral} mapping to render the prompt term.
_CATALOG_PATH = (
    Path(__file__).resolve().parent.parent
    / "res"
    / "synth_webui"
    / "data"
    / "karada_touch_zones.json"
)

# id -> {"technical": str, "neutral": str}
_zone_terms: Optional[dict[str, dict[str, str]]] = None


def _load_zone_terms() -> dict[str, dict[str, str]]:
    """Load and cache the id -> term mapping from the touch-zone catalog."""
    global _zone_terms
    if _zone_terms is not None:
        return _zone_terms
    terms: dict[str, dict[str, str]] = {}
    try:
        with open(_CATALOG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        for zone in data.get("zones", []):
            zone_id = zone.get("id")
            if not zone_id:
                continue
            terms[str(zone_id)] = {
                "technical": str(zone.get("technical") or zone_id),
                "neutral": str(zone.get("neutral") or zone_id),
            }
    except Exception as e:
        log_warning(f"[karada_touch] Failed to load touch-zone catalog: {e}")
    _zone_terms = terms
    return terms


def _track_background(task: asyncio.Task[Any]) -> None:
    """RUF006-safe: retain a strong reference to a fire-and-forget task."""
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# ---------------------------------------------------------------------------
# De-sexualization
# ---------------------------------------------------------------------------


def desexualize_part(raw_part: Optional[str]) -> Optional[str]:
    """Map a raw body-part name to the term written into chat history.

    Resolution order:

    1. If ``raw_part`` matches a precise touch-zone id from the catalog, use its
       ``technical`` term when :data:`KARADA_EXPLICIT_TOUCH` is enabled, or its
       ``neutral`` term otherwise.
    2. Otherwise fall back to the legacy :data:`_BODY_PART_DESEXUALIZE` map.
    3. Unknown parts are returned lowercased and stripped as-is (assumed already
       neutral).

    Returns ``None`` when ``raw_part`` is falsy.
    """
    if not raw_part:
        return None

    raw = str(raw_part).strip()

    # 1. Precise catalog zone ids (e.g. "right_breast").
    zone_terms = _load_zone_terms()
    zone = zone_terms.get(raw) or zone_terms.get(raw.lower())
    if zone:
        return zone["technical"] if _explicit_touch_enabled() else zone["neutral"]

    # 2. Legacy de-sexualization map.
    key = raw.lower().replace(" ", "").replace("_", "")
    if key in _BODY_PART_DESEXUALIZE:
        return _BODY_PART_DESEXUALIZE[key]

    # 3. Passthrough.
    return raw.lower() or None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


async def ensure_table() -> None:
    """Create the ``karada_touch_events`` table if it does not exist."""
    global _table_ready
    if _table_ready:
        return
    try:
        from core.db import _get_db_type, get_conn_ctx

        is_postgres = _get_db_type() == "postgres"
        if is_postgres:
            create_sql = """
                CREATE TABLE IF NOT EXISTS karada_touch_events (
                    id SERIAL PRIMARY KEY,
                    interface_path VARCHAR(255) NOT NULL,
                    session_id VARCHAR(255),
                    event_type VARCHAR(50) NOT NULL,
                    body_part VARCHAR(100),
                    raw_part VARCHAR(100),
                    username VARCHAR(255),
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL,
                    flushed BOOLEAN NOT NULL DEFAULT FALSE,
                    attached BOOLEAN NOT NULL DEFAULT FALSE
                )
            """
        else:
            create_sql = """
                CREATE TABLE IF NOT EXISTS karada_touch_events (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    interface_path VARCHAR(255) NOT NULL,
                    session_id VARCHAR(255),
                    event_type VARCHAR(50) NOT NULL,
                    body_part VARCHAR(100),
                    raw_part VARCHAR(100),
                    username VARCHAR(255),
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at DATETIME NOT NULL,
                    flushed BOOLEAN NOT NULL DEFAULT 0,
                    attached BOOLEAN NOT NULL DEFAULT 0,
                    INDEX idx_kte_expires (expires_at),
                    INDEX idx_kte_flushed (flushed, event_type),
                    INDEX idx_kte_iface (interface_path)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(create_sql)
                if is_postgres:
                    await cur.execute(
                        "CREATE INDEX IF NOT EXISTS idx_kte_expires ON karada_touch_events (expires_at)"
                    )
                    await cur.execute(
                        "CREATE INDEX IF NOT EXISTS idx_kte_flushed ON karada_touch_events (flushed, event_type)"
                    )
                    await cur.execute(
                        "CREATE INDEX IF NOT EXISTS idx_kte_iface ON karada_touch_events (interface_path)"
                    )
                try:
                    await conn.commit()
                except Exception:
                    pass
        _table_ready = True
        log_debug("[karada_touch] ensured karada_touch_events table exists")
    except Exception as e:
        log_warning(f"[karada_touch] ensure_table failed: {e}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def record_touch_event(
    session_id: str,
    interface_path: str,
    event_type: str,
    *,
    body_part: Optional[str] = None,
    raw_part: Optional[str] = None,
    username: Optional[str] = None,
) -> None:
    """Record a single 3D-interaction event.

    Args:
        session_id: WebUI session id.
        interface_path: Canonical interface path (e.g. ``synth_webui/<session>``).
        event_type: One of ``environment_tap`` / ``window_tap`` / ``synth_touch``.
        body_part: Pre-neutralized body part (if the caller already mapped it).
        raw_part: Raw body-part name to de-sexualize (takes precedence over
            ``body_part`` when provided).
        username: Display name of the interacting user.
    """
    if not _is_enabled():
        return
    if event_type not in _VALID_EVENT_TYPES:
        log_debug(f"[karada_touch] Ignoring unknown event_type={event_type!r}")
        return

    await ensure_table()

    now = datetime.now(timezone.utc)
    # Neutralize the part. `raw_part` is stored verbatim for internal use only.
    neutral_part = body_part
    if raw_part:
        neutral_part = desexualize_part(raw_part)
    elif body_part:
        neutral_part = desexualize_part(body_part)

    ttl = _synth_ttl_s() if event_type == EVENT_SYNTH_TOUCH else _env_ttl_s()
    expires_at = now + timedelta(seconds=ttl)

    # Dedup: collapse identical events within the dedup window.
    bucket = _pending_events.setdefault(interface_path, [])
    for prev in reversed(bucket):
        if (
            prev.get("event_type") == event_type
            and prev.get("body_part") == neutral_part
        ):
            prev_ts = prev.get("created_at")
            if (
                isinstance(prev_ts, datetime)
                and (now - prev_ts).total_seconds() <= _DEDUP_WINDOW_S
            ):
                log_debug(f"[karada_touch] Deduped {event_type} for {interface_path}")
                return

    event: dict[str, Any] = {
        "session_id": session_id,
        "interface_path": interface_path,
        "event_type": event_type,
        "body_part": neutral_part,
        "raw_part": raw_part,
        "username": username,
        "created_at": now,
        "expires_at": expires_at,
    }

    # Persist to DB (best-effort).
    try:
        from core.db import get_conn_ctx

        # Postgres (asyncpg) rejects string literals for TIMESTAMP columns; pass
        # naive datetime objects (works for both Postgres and MySQL).
        created_val = now.replace(tzinfo=None)
        expires_val = expires_at.replace(tzinfo=None)
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO karada_touch_events
                        (interface_path, session_id, event_type, body_part, raw_part, username, created_at, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        interface_path,
                        session_id,
                        event_type,
                        neutral_part,
                        raw_part,
                        username,
                        created_val,
                        expires_val,
                    ),
                )
                try:
                    await conn.commit()
                except Exception:
                    pass
    except Exception as e:
        log_debug(f"[karada_touch] Failed to persist touch event: {e}")

    bucket.append(event)
    log_debug(
        f"[karada_touch] Recorded {event_type} part={neutral_part} for {interface_path}"
    )

    _schedule_flush(interface_path)


def _schedule_flush(interface_path: str) -> None:
    """(Re)arm the debounce timer for the given interface path."""
    existing = _flush_tasks.get(interface_path)
    if existing and not existing.done():
        # Timer already running; the accumulated events will be flushed together.
        return
    try:
        task = asyncio.create_task(_flush_after_cooldown(interface_path))
    except RuntimeError:
        # No running loop (e.g. called from a sync context in tests).
        log_debug("[karada_touch] No running loop; skipping flush scheduling")
        return
    _flush_tasks[interface_path] = task


async def _flush_after_cooldown(interface_path: str) -> None:
    """Wait for the cooldown then flush the accumulated events."""
    try:
        await asyncio.sleep(_flush_cooldown_s())
        await _flush_pending(interface_path)
    except asyncio.CancelledError:  # pragma: no cover - shutdown
        raise
    except Exception as e:
        log_error(f"[karada_touch] flush_after_cooldown failed: {e}")
    finally:
        _flush_tasks.pop(interface_path, None)


def _format_history_line(
    username: str, event_type: str, body_part: Optional[str]
) -> str:
    """Build a single, human-readable history line for one event."""
    who = username or "Someone"
    if event_type == EVENT_ENVIRONMENT_TAP:
        return f"{who} tapped on the screen in the webui"
    if event_type == EVENT_WINDOW_TAP:
        return f"{who} tapped a window in the webui"
    if event_type == EVENT_SYNTH_TOUCH:
        if body_part:
            return f"{who} touched your {body_part}"
        return f"{who} touched you"
    return f"{who} interacted with the webui"


def _summarize_group(events: list[dict[str, Any]]) -> str:
    """Turn a group of events into one or more history lines."""
    lines: list[str] = []
    for ev in events:
        ts = ev.get("created_at")
        prefix = ""
        if isinstance(ts, datetime):
            prefix = ts.astimezone().strftime("%H:%M") + " - "
        line = _format_history_line(
            ev.get("username") or "",
            ev.get("event_type") or "",
            ev.get("body_part"),
        )
        lines.append(f"{prefix}{line}")
    return "\n".join(lines)


async def _flush_pending(interface_path: str) -> None:
    """Flush accumulated events for one interface path into chat history."""
    events = _pending_events.pop(interface_path, [])
    if not events:
        return

    has_synth_touch = any(ev.get("event_type") == EVENT_SYNTH_TOUCH for ev in events)
    summary = _summarize_group(events)
    if not summary:
        return

    # Determine a representative user for the batch.
    username = str(
        next((ev.get("username") for ev in events if ev.get("username")), "Someone")
    )
    session_id = next(
        (ev.get("session_id") for ev in events if ev.get("session_id")), None
    )

    # Attach as a passive system/observation line in the chat context.
    try:
        from core.chat_context_manager import add_message_to_context

        await add_message_to_context(
            interface_path=interface_path,
            message_text=summary,
            sender_name="Karada",
            sender_id="karada_touch",
            timestamp=datetime.now(timezone.utc).isoformat(),
            metadata={"karada_touch": True, "synth_touch": has_synth_touch},
        )
        log_debug(
            f"[karada_touch] Attached {len(events)} event(s) to context {interface_path}"
        )
    except Exception as e:
        log_warning(f"[karada_touch] Failed to attach touch history: {e}")

    # Mark the flushed DB rows.
    await _mark_flushed(interface_path, events)

    # A group containing at least one synth_touch triggers a spontaneous turn.
    if has_synth_touch:
        task = asyncio.create_task(
            _trigger_spontaneous_turn(interface_path, session_id, summary, username)
        )
        _track_background(task)


async def _mark_flushed(interface_path: str, events: list[dict[str, Any]]) -> None:
    """Mark the persisted rows for these events as flushed.

    Environment/window rows are deleted (they are transient); synth rows are
    kept for the sliding window but flagged flushed + attached.
    """
    try:
        from core.db import _get_db_type, get_conn_ctx

        is_postgres = _get_db_type() == "postgres"
        true_val = "TRUE" if is_postgres else "1"
        oldest = min(
            (
                ev["created_at"]
                for ev in events
                if isinstance(ev.get("created_at"), datetime)
            ),
            default=None,
        )
        if oldest is None:
            return
        oldest_val = oldest.replace(tzinfo=None)

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                # Delete transient environment/window events already flushed.
                await cur.execute(
                    f"""
                    DELETE FROM karada_touch_events
                    WHERE interface_path = %s
                      AND event_type IN ('{EVENT_ENVIRONMENT_TAP}', '{EVENT_WINDOW_TAP}')
                      AND created_at >= %s
                    """,
                    (interface_path, oldest_val),
                )
                # Flag synth touches as flushed + attached (retained for reflection).
                await cur.execute(
                    f"""
                    UPDATE karada_touch_events
                    SET flushed = {true_val}, attached = {true_val}
                    WHERE interface_path = %s
                      AND event_type = '{EVENT_SYNTH_TOUCH}'
                      AND created_at >= %s
                    """,
                    (interface_path, oldest_val),
                )
                try:
                    await conn.commit()
                except Exception:
                    pass
    except Exception as e:
        log_debug(f"[karada_touch] Failed to mark flushed: {e}")


async def _trigger_spontaneous_turn(
    interface_path: str,
    session_id: Optional[str],
    summary: str,
    username: str,
) -> None:
    """Enqueue a spontaneous LLM turn in reaction to being touched."""
    try:
        from types import SimpleNamespace
        from core import message_queue

        prompt_text = (
            f"[Karada] {summary}\n"
            "(You just felt this physical interaction with your body. "
            "React naturally, in character.)"
        )

        message = SimpleNamespace(
            chat_id=session_id or interface_path,
            interface_path=interface_path,
            message_id=int(datetime.now(timezone.utc).timestamp() * 1000) % 1_000_000,
            text=prompt_text,
            attachments=[],
            # A cortex turn triggered by a physical tap/touch is treated as a
            # forced-audio interaction: Synth answers with a voice message. Set
            # is_voice_input so message_chain auto-injects tts_speak (see
            # core/message_chain.py TTS auto-inject block).
            is_voice_input=True,
            date=datetime.now(timezone.utc),
            from_user=SimpleNamespace(
                id="karada_touch",
                username=username or "Someone",
                first_name=username or "Someone",
                last_name="",
                full_name=username or "Someone",
            ),
            chat=SimpleNamespace(
                id=session_id or interface_path,
                type="web",
                title="Karada Touch",
                full_name="Karada Touch",
            ),
            reply_to_message=None,
        )

        from core.webui import INTERFACE_NAME

        await message_queue.enqueue(
            bot=None,
            message=message,
            context_memory=None,
            priority=False,
            interface_id=INTERFACE_NAME,
            skip_mention_check=True,
            original_message=message,
        )
        log_info(
            f"[karada_touch] Enqueued spontaneous turn for touch on {interface_path}"
        )
    except Exception as e:
        log_error(f"[karada_touch] Failed to enqueue spontaneous turn: {e}")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


async def cleanup_expired() -> int:
    """Delete expired rows from ``karada_touch_events``. Returns rows removed."""
    try:
        from core.db import get_conn_ctx

        await ensure_table()
        now_val = datetime.now(timezone.utc).replace(tzinfo=None)
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM karada_touch_events WHERE expires_at < %s",
                    (now_val,),
                )
                removed = getattr(cur, "rowcount", 0) or 0
                try:
                    await conn.commit()
                except Exception:
                    pass
        if removed:
            log_debug(f"[karada_touch] cleanup_expired removed {removed} row(s)")
        return removed
    except Exception as e:
        log_debug(f"[karada_touch] cleanup_expired failed: {e}")
        return 0


async def _cleanup_loop(interval_s: float = 300.0) -> None:
    """Periodically purge expired touch-event rows."""
    while True:
        try:
            await asyncio.sleep(interval_s)
            await cleanup_expired()
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception as e:
            log_debug(f"[karada_touch] cleanup loop error: {e}")


def start_cleanup_task(interval_s: float = 300.0) -> None:
    """Start the periodic cleanup task (idempotent)."""
    global _cleanup_task
    if _cleanup_task and not _cleanup_task.done():
        return
    try:
        _cleanup_task = asyncio.create_task(_cleanup_loop(interval_s))
    except RuntimeError:
        log_debug("[karada_touch] No running loop; cleanup task not started")
