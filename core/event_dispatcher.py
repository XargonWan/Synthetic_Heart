from __future__ import annotations

from datetime import datetime, timezone

from core.time_zone_utils import get_local_timezone, format_dual_time

from core.db import get_due_events
from core import message_queue
from core.logging_utils import log_debug, log_warning
import time
from core.core_initializer import PLUGIN_REGISTRY

# Prefer plugin registry lookup to avoid importing plugin internals into core


# Track events currently dispatched to prevent duplicate processing
_processing_events: dict[int, float] = {}
_PROCESSING_TTL = 300  # seconds


def event_completed(event_id: int) -> None:
    """Remove an event from the processing cache."""
    if event_id in _processing_events:
        _processing_events.pop(event_id, None)
        log_debug(f"[event_dispatcher] Event {event_id} removed from processing cache")


async def dispatch_pending_events(bot):
    """Dispatch events that are due and handle repeats."""
    tz = get_local_timezone()
    now_local = datetime.now(tz)
    now_utc = now_local.astimezone(timezone.utc)

    # Purge stale processing markers
    now_ts = time.time()
    for e_id, ts in list(_processing_events.items()):
        if now_ts - ts > _PROCESSING_TTL:
            _processing_events.pop(e_id, None)

    events = await get_due_events(now_utc)
    if not events:
        return 0

    log_debug(f"[event_dispatcher] Retrieved {len(events)} events from the database")
    dispatched = 0
    # Obtain event plugin from registry if available
    event_plugin = None
    if isinstance(PLUGIN_REGISTRY, dict):
        event_plugin = PLUGIN_REGISTRY.get("event_plugin")
    if event_plugin is None:
        # Fallback to dynamic import if registry doesn't provide it
        try:
            from plugins.event_plugin import EventPlugin as _EventPlugin

            event_plugin = _EventPlugin()
        except Exception:
            log_warning(
                "[event_dispatcher] Event plugin not available; skipping event prompt creation"
            )
            event_plugin = None

    for ev in events:
        ev_id = ev.get("id")
        # Skip events already being processed recently
        ts = _processing_events.get(ev_id)
        if ts and time.time() - ts < _PROCESSING_TTL:
            log_debug(f"[event_dispatcher] Event {ev_id} already processing, skipping")
            continue

        log_debug(f"[event_dispatcher] Processing event: {ev}")

        try:
            next_run_val = ev.get("next_run")
            if isinstance(next_run_val, datetime):
                scheduled_dt = next_run_val
            else:
                scheduled_dt = datetime.fromisoformat(
                    str(next_run_val).replace("Z", "+00:00")
                )
            if scheduled_dt.tzinfo is None:
                scheduled_dt = scheduled_dt.replace(tzinfo=timezone.utc)
        except Exception:
            scheduled_dt = datetime.now(timezone.utc)

        prompt = event_plugin._create_event_prompt(ev)
        old_instructions = prompt.get("instructions", "")
        prompt["instructions"] = "🕒 Event Dispatcher\n\n" + old_instructions

        summary = format_dual_time(scheduled_dt) + " → " + str(ev["description"])

        try:
            await message_queue.enqueue_event(bot, prompt, event_id=ev_id)
            _processing_events[ev_id] = time.time()
            log_debug(f"[DISPATCH] Event queued with priority: {summary}")
            dispatched += 1
        except Exception as exc:
            log_warning(
                f"[event_dispatcher] Error while processing event {ev['id']}: {exc}"
            )
            continue

    log_debug(f"[event_dispatcher] Dispatched {dispatched} event(s)")
    return dispatched
