# plugins/event_plugin.py

from __future__ import annotations

import os
from datetime import datetime, timezone

from core.ai_plugin_base import AIPluginBase
from core.db import insert_scheduled_event, get_due_events, mark_event_delivered
from core.logging_utils import log_debug, log_info, log_error, log_warning
from interface.message_send_utils import send_with_thread_fallback
from core.auto_response import request_llm_delivery
import traceback
import asyncio
import json
from core.core_initializer import register_plugin

# Re-exported so consumers/tests can observe dynamic config propagation of the
# corrector retry limit through this module (see tests/test_exposed_variables.py).
from core.action_parser import CORRECTOR_RETRIES  # noqa: F401

# Owners of scheduled events that are self-dispatched by their own plugin.
# The generic scheduler MUST skip these to avoid double delivery: the owning
# plugin fetches and delivers them itself (honouring its configured interface).
_SELF_MANAGED_EVENT_OWNERS: frozenset[str] = frozenset({"weather_plugin"})


def _standard_action_scope() -> list[str] | None:
    """Return the full standard action set from the action registry.

    A ``scheduled_reminder`` beat is delivered on the internal ``grillo``
    interface, but unlike introspective Grillo beats it may legitimately need
    to contact a user. Without an explicit scope the prompt builder falls back
    to ``_derive_default_prompt_action_types`` which filters out every
    interface-bound ``message_*`` action for the ``grillo`` interface, leaving
    the model no way to actually reach out.

    To keep a single source of truth we hand the beat the exact set of actions
    the action registry currently exposes (``available_actions``), so the beat
    receives the standard catalog instead of a distinct, restricted scope.
    Returning ``None`` (registry unavailable) lets the caller fall back to the
    default behaviour rather than crash.
    """
    try:
        from core.core_initializer import core_initializer

        available = core_initializer.actions_block.get("available_actions", {})
        action_names = [str(name) for name in available if name]
        return action_names or None
    except Exception as e:
        log_error(f"[event_plugin] Failed to derive standard action scope: {repr(e)}")
        return None


# Actions a firing reminder must never be allowed to emit: it has to ACT now,
# not re-schedule itself (which loops). Filtered deterministically from the
# catalog — this is not text/keyword matching on the reminder content.
_REMINDER_BLOCKED_ACTIONS: frozenset[str] = frozenset({"schedule_message"})


def _reminder_action_scope() -> list[str] | None:
    """Standard action scope minus the scheduling actions.

    A ``scheduled_reminder`` beat is delivered when an event fires. The model
    must carry out the reminder in that turn (e.g. send the message), never
    create or reschedule another event. Removing the scheduling actions from
    the beat's scope makes that structurally impossible. Returns ``None`` when
    the registry is unavailable so the caller can fall back to default
    behaviour.
    """
    scope = _standard_action_scope()
    if scope is None:
        return None
    return [name for name in scope if name not in _REMINDER_BLOCKED_ACTIONS]


class EventPlugin(AIPluginBase):
    """Plugin that stores future events without using an LLM."""

    # Class-level variables to prevent multiple schedulers
    _scheduler_running = False
    _scheduler_task = None
    # TZ-change listener state (recomputes inherited-timezone events at runtime)
    _tz_listener_registered = False
    _tz_recompute_tasks: set[asyncio.Task] = set()
    # External-calendar polling state: monotonic timestamp of the last poll.
    _external_last_poll: float = 0.0
    # Human-readable name required by core initializer
    display_name = "Event Plugin"

    def __init__(self, notify_fn=None, bot=None):
        self.reply_map: dict[int, tuple[int, int]] = {}
        self.notify_fn = notify_fn
        self.bot = bot
        # Track events currently being processed to mark them as delivered after successful send
        self._pending_events: dict[str, dict] = {}  # message_id -> event_info
        log_info("[event_plugin] EventPlugin instance created")
        register_plugin("event", self)

        # Register custom validation with the new validation system
        self._register_custom_validation()

        log_info("[event_plugin] Registered EventPlugin")

    def set_bot(self, bot):
        """Update the bot reference used for sending messages."""
        self.bot = bot
        log_debug("[event_plugin] Telegram bot reference updated")

    async def ensure_table_exists(self) -> None:
        """Ensure the scheduled_events table is present."""
        host = os.getenv("DB_HOST", "localhost")
        port = int(os.getenv("DB_PORT", "3306"))
        user = os.getenv("DB_USER", "root")
        # DB password read/used by core.db; not needed locally here
        db_name = os.getenv("DB_NAME", "synth")

        log_debug(
            f"[event_plugin] Ensuring table scheduled_events in {user}@{host}:{port}/{db_name}"
        )

        try:
            from core.db import get_conn_ctx, _get_db_type

            is_postgres = _get_db_type() == "postgres"

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    if is_postgres:
                        await cur.execute(
                            """
                            CREATE TABLE IF NOT EXISTS scheduled_events (
                                id BIGSERIAL PRIMARY KEY,
                                date DATE NOT NULL,
                                time TIME DEFAULT '00:00',
                                recurrence_type TEXT DEFAULT 'none',
                                next_run TIMESTAMPTZ NOT NULL,
                                description TEXT NOT NULL,
                                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                                delivered BOOLEAN DEFAULT FALSE,
                                created_by TEXT DEFAULT 'synth',
                                uid TEXT,
                                rrule TEXT,
                                tzid TEXT,
                                source TEXT DEFAULT 'synth'
                            )
                            """
                        )
                    else:
                        await cur.execute(
                            """
                            CREATE TABLE IF NOT EXISTS scheduled_events (
                                id INT AUTO_INCREMENT PRIMARY KEY,
                                `date` DATE NOT NULL,
                                `time` TIME DEFAULT '00:00',
                                recurrence_type VARCHAR(20) DEFAULT 'none',
                                next_run DATETIME NOT NULL,
                                description TEXT NOT NULL,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                delivered BOOLEAN DEFAULT 0,
                                created_by VARCHAR(100) DEFAULT 'synth',
                                uid VARCHAR(255),
                                rrule VARCHAR(255),
                                tzid VARCHAR(100),
                                source VARCHAR(100) DEFAULT 'synth'
                            )
                            """
                        )
            log_info("[event_plugin] ensured scheduled_events table exists")
            await self._migrate_calendar_columns()
        except Exception as e:
            log_error(f"[event_plugin] Failed to ensure table exists: {repr(e)}")

        try:
            from core.external_calendars import ensure_external_calendars_table

            await ensure_external_calendars_table()
        except Exception as e:
            log_error(
                f"[event_plugin] Failed to ensure external_calendars table: {repr(e)}"
            )

    async def _migrate_calendar_columns(self) -> None:
        """Add iCalendar columns to legacy ``scheduled_events`` tables and backfill.

        Idempotent: safe to run on every startup. Adds ``uid``/``rrule``/
        ``tzid``/``source`` when missing, then backfills rows where ``uid`` is
        NULL with a stable UID, an RRULE derived from ``recurrence_type``,
        ``tzid=NULL`` (inherited system TZ) and a ``source`` derived from
        ``created_by``.
        """
        from core.db import get_conn_ctx, _get_db_type
        from core.calendar_utils import build_event_uid, recurrence_to_rrule

        is_postgres = _get_db_type() == "postgres"

        # Column definitions (name -> backend-specific type)
        columns = {
            "uid": "TEXT" if is_postgres else "VARCHAR(255)",
            "rrule": "TEXT" if is_postgres else "VARCHAR(255)",
            "tzid": "TEXT" if is_postgres else "VARCHAR(100)",
            "source": "TEXT DEFAULT 'synth'"
            if is_postgres
            else "VARCHAR(100) DEFAULT 'synth'",
        }

        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    for col_name, col_type in columns.items():
                        if is_postgres:
                            await cur.execute(
                                f"ALTER TABLE scheduled_events "
                                f"ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
                            )
                        else:
                            # MySQL/MariaDB: check information_schema before adding.
                            await cur.execute(
                                "SELECT COUNT(*) FROM information_schema.columns "
                                "WHERE table_name = 'scheduled_events' "
                                "AND column_name = %s",
                                (col_name,),
                            )
                            row = await cur.fetchone()
                            exists = bool(
                                row
                                and (
                                    row[0]
                                    if not isinstance(row, dict)
                                    else list(row.values())[0]
                                )
                            )
                            if not exists:
                                await cur.execute(
                                    f"ALTER TABLE scheduled_events "
                                    f"ADD COLUMN `{col_name}` {col_type}"
                                )
            log_debug("[event_plugin] calendar columns ensured")
        except Exception as e:
            log_error(f"[event_plugin] Failed to add calendar columns: {repr(e)}")
            return

        # Legacy-event backfill is gated behind a flag so it can stay disabled
        # during the calendar test phase. Only the schema (columns above) is
        # applied unconditionally; the data migration below is opt-in.
        if os.getenv("EVENT_MIGRATION_BACKFILL_ENABLED", "0").lower() not in (
            "1",
            "true",
            "yes",
        ):
            log_info(
                "[event_plugin] legacy event backfill disabled "
                "(set EVENT_MIGRATION_BACKFILL_ENABLED=1 to enable)"
            )
            return

        # Backfill rows missing a UID.
        try:
            from core.db import aiomysql

            async with get_conn_ctx() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT id, recurrence_type, created_by "
                        "FROM scheduled_events WHERE uid IS NULL"
                    )
                    legacy_rows = await cur.fetchall()

                if not legacy_rows:
                    log_debug("[event_plugin] no legacy events to backfill")
                    return

                async with conn.cursor() as cur:
                    for r in legacy_rows:
                        event_id = r.get("id")
                        if event_id is None:
                            continue
                        uid = build_event_uid(event_id)
                        rrule = recurrence_to_rrule(r.get("recurrence_type"))
                        created_by = (r.get("created_by") or "synth").lower()
                        source = (
                            "synth"
                            if created_by in ("synth", "weather_plugin")
                            else "user"
                        )
                        await cur.execute(
                            "UPDATE scheduled_events "
                            "SET uid = %s, rrule = %s, tzid = NULL, source = %s "
                            "WHERE id = %s",
                            (uid, rrule, source, event_id),
                        )
                    log_info(
                        f"[event_plugin] backfilled {len(legacy_rows)} legacy event(s) "
                        "with iCalendar metadata"
                    )
        except Exception as e:
            log_error(f"[event_plugin] Failed to backfill calendar metadata: {repr(e)}")

    async def start(self):
        """Start the event scheduler."""
        log_info(
            f"[event_plugin] start() called, scheduler_running={EventPlugin._scheduler_running}"
        )

        await self.ensure_table_exists()
        task = EventPlugin._scheduler_task

        if task and not task.done():
            log_warning(
                "[event_plugin] Scheduler already running globally, ignoring start() call"
            )
            return

        if task and task.done():
            log_warning(
                "[event_plugin] Previous scheduler task was not running; restarting"
            )

        EventPlugin._scheduler_running = True
        EventPlugin._scheduler_task = asyncio.create_task(self._event_scheduler())
        log_info("[event_plugin] Event scheduler started (singleton)")

        self._register_tz_listener()

    def _register_tz_listener(self) -> None:
        """Recompute inherited-timezone events when the TZ config changes.

        Events with ``tzid IS NULL`` follow the system timezone, so their UTC
        ``next_run`` must be rebuilt whenever ``TZ`` is updated at runtime.
        """
        if EventPlugin._tz_listener_registered:
            return

        from core.config_manager import config_registry

        def _on_tz_change(_new_value: object) -> None:
            from core.db import recompute_all_next_runs

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No running loop (e.g. called from a worker thread); run inline.
                asyncio.run(recompute_all_next_runs())
                return
            task = loop.create_task(recompute_all_next_runs())
            EventPlugin._tz_recompute_tasks.add(task)
            task.add_done_callback(EventPlugin._tz_recompute_tasks.discard)

        try:
            config_registry.add_listener("TZ", _on_tz_change)
            EventPlugin._tz_listener_registered = True
            log_info("[event_plugin] Registered TZ-change listener for event recompute")
        except KeyError:
            log_warning(
                "[event_plugin] TZ config key not registered; skipping listener"
            )

    async def stop(self):
        """Stop the event scheduler."""
        EventPlugin._scheduler_running = False

        task = EventPlugin._scheduler_task
        if not task:
            log_info("[event_plugin] Event scheduler not running")
            return

        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        EventPlugin._scheduler_task = None
        log_info("[event_plugin] Event scheduler stopped")

    def get_supported_action_types(self):
        """Return the action types this plugin supports."""
        return ["event", "static_inject"]

    @staticmethod
    def get_interface_id() -> str:
        """Return the unique identifier for this internal interface."""
        return "event"

    def get_supported_actions(self) -> dict:
        """Return schema information for supported actions."""
        return {
            "event": {
                "required_fields": ["date", "description"],
                "optional_fields": ["time", "repeat", "created_by"],
                "description": "Create or schedule a future event",
            },
            "schedule_message": {
                "required_fields": ["text"],
                "optional_fields": ["send_in", "send_at"],
                "description": "Schedule a message to be sent after a delay (send_in) or at a specific time (send_at)",
            },
            "static_inject": {
                "required_fields": [],
                "optional_fields": [],
                "description": "Inject upcoming scheduled events into the prompt context (informational only)",
            },
        }

    async def _fetch_upcoming_event_rows(self) -> list[dict]:
        """Fetch all ``scheduled_events`` rows needed to expand upcoming occurrences."""
        from core.db import get_conn_ctx

        columns = [
            "id",
            "date",
            "time",
            "recurrence_type",
            "next_run",
            "description",
            "created_at",
            "created_by",
            "uid",
            "rrule",
            "tzid",
            "source",
        ]
        col_list = ", ".join(columns)
        rows: list[dict] = []
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {col_list} FROM scheduled_events ORDER BY next_run ASC"
                )
                fetched = await cur.fetchall()
                for row in fetched:
                    rows.append(dict(zip(columns, row)))
        return rows

    async def get_static_injection(self, message=None, context_memory=None) -> dict:
        """Inject the next few upcoming events into the prompt context.

        Purely informational: SyntH sees its own upcoming commitments so it can
        reason about them naturally. It expands recurring events (via RRULE)
        starting from *now* over a short look-ahead window and returns a small,
        preformatted text block (never more than ``max_events`` lines).
        """
        try:
            from core.config_manager import config_registry

            lookahead_days = int(
                config_registry.get_value(
                    "UPCOMING_EVENTS_LOOKAHEAD_DAYS",
                    3,
                    value_type=int,
                    label="Upcoming events look-ahead (days)",
                    description=(
                        "How many days ahead SyntH's upcoming-events context "
                        "block looks. Informational only."
                    ),
                    group="calendar",
                    component="event_plugin",
                )
            )
            max_events = int(
                config_registry.get_value(
                    "UPCOMING_EVENTS_MAX",
                    5,
                    value_type=int,
                    label="Upcoming events shown in context",
                    description=(
                        "Maximum number of upcoming events injected into the "
                        "prompt context. Informational only."
                    ),
                    group="calendar",
                    component="event_plugin",
                )
            )
        except Exception as e:
            log_warning(f"[event_plugin] upcoming events config read failed: {e}")
            lookahead_days = 3
            max_events = 5

        lookahead_days = max(1, min(31, lookahead_days))
        max_events = max(1, min(20, max_events))

        try:
            from datetime import datetime as _dt
            from datetime import timedelta
            import recurring_ical_events

            from core.calendar_utils import build_calendar
            from core.time_zone_utils import get_local_timezone

            system_tz = get_local_timezone()
            window_start = _dt.now(tz=system_tz)
            window_end = window_start + timedelta(days=lookahead_days)

            rows = await self._fetch_upcoming_event_rows()
            if not rows:
                return {}

            calendar = build_calendar(rows, system_tz=system_tz)

            try:
                occurrences = recurring_ical_events.of(calendar).between(
                    window_start, window_end
                )
            except Exception as exc:
                log_warning(f"[event_plugin] upcoming expansion failed: {exc}")
                return {}

            collected: list[tuple[float, str]] = []
            for occ in occurrences:
                try:
                    dtstart = occ.get("dtstart")
                    if dtstart is None:
                        continue
                    start_dt = dtstart.dt
                    if isinstance(start_dt, datetime):
                        if start_dt.tzinfo is None:
                            start_dt = start_dt.replace(tzinfo=system_tz)
                        local_dt = start_dt.astimezone(system_tz)
                        all_day = False
                    else:
                        # All-day (date only) -> anchor at local midnight.
                        local_dt = _dt(
                            start_dt.year,
                            start_dt.month,
                            start_dt.day,
                            tzinfo=system_tz,
                        )
                        all_day = True

                    if local_dt < window_start:
                        continue

                    if all_day:
                        when = f"{local_dt.strftime('%b %-d')} (all day)"
                    else:
                        tz_abbr = local_dt.strftime("%Z") or "local"
                        when = (
                            f"{local_dt.strftime('%b %-d')}, "
                            f"{local_dt.hour}:{local_dt.strftime('%M')} ({tz_abbr})"
                        )

                    description = str(occ.get("summary", "")).strip()
                    if not description:
                        continue
                    collected.append((local_dt.timestamp(), f"{when} - {description}"))
                except Exception as exc:
                    log_debug(f"[event_plugin] skipping upcoming occurrence: {exc}")
                    continue

            if not collected:
                return {}

            collected.sort(key=lambda item: item[0])
            lines = [line for _, line in collected[:max_events]]

            block = (
                f"upcoming events (next {lookahead_days} days) "
                "(informational only, do not act unless relevant):\n"
                + "\n".join(f"- {line}" for line in lines)
            )
            return {"upcoming_events": block}

        except Exception as exc:
            log_error(f"[event_plugin] get_static_injection failed: {exc}")
            return {}

    def validate_payload(self, action_type: str, payload: dict) -> list:
        """Validate payload for event actions."""
        errors = []

        if action_type == "event":
            # Required fields validation
            date_str = payload.get("date")
            if not date_str:
                errors.append("payload.date is required for event action")
            else:
                try:
                    from datetime import datetime

                    datetime.strptime(date_str, "%Y-%m-%d")
                except Exception:
                    errors.append("payload.date must be in format YYYY-MM-DD")

            if not payload.get("description"):
                errors.append("payload.description is required for event action")

            # Optional fields validation
            time_str = payload.get("time")
            if time_str:
                try:
                    from datetime import datetime

                    datetime.strptime(time_str, "%H:%M")
                except Exception:
                    errors.append("payload.time must be in format HH:MM")

            repeat = payload.get("repeat")
            if repeat and repeat not in [
                "none",
                "daily",
                "weekly",
                "monthly",
                "always",
            ]:
                errors.append(
                    "payload.repeat must be one of: none, daily, weekly, monthly, always"
                )

        elif action_type == "schedule_message":
            # Required fields validation
            if not payload.get("text"):
                errors.append("payload.text is required for schedule_message action")

            # Either send_in OR send_at must be provided (at least one)
            send_in = payload.get("send_in")
            send_at = payload.get("send_at")
            if not send_in and not send_at:
                errors.append(
                    "Either payload.send_in (delay) or payload.send_at (specific time) is required for schedule_message action"
                )

            # Validate send_at format if provided
            if send_at:
                try:
                    from datetime import datetime

                    # Try Unix timestamp
                    try:
                        ts = int(send_at)
                        if ts > 0:
                            datetime.fromtimestamp(ts)
                        else:
                            raise ValueError("Invalid timestamp")
                    except ValueError:
                        # Try ISO format: 2025-11-20T09:00:00
                        try:
                            datetime.fromisoformat(send_at.replace("Z", "+00:00"))
                        except ValueError:
                            # Try YYYY-MM-DD HH:MM format
                            try:
                                datetime.strptime(send_at, "%Y-%m-%d %H:%M")
                            except ValueError:
                                # Try HH:MM format (today)
                                datetime.strptime(send_at, "%H:%M")
                except Exception:
                    errors.append(
                        "payload.send_at has invalid format. Accepted: HH:MM (today), YYYY-MM-DD HH:MM, ISO 8601 (2025-11-20T09:00:00), or Unix timestamp (1735142400)"
                    )

            # IMPORTANT: Reject invalid fields that should not be in schedule_message
            # interface_path is auto-extracted from original_message, not from payload
            if "chat_id" in payload:
                errors.append(
                    "payload.chat_id is not a valid field for schedule_message - use interface_path instead (auto-extracted from context)"
                )

            if "thread_id" in payload:
                errors.append(
                    "payload.thread_id is not a valid field for schedule_message - use interface_path instead (auto-extracted from context)"
                )

            if "interface_path" in payload:
                errors.append(
                    "payload.interface_path is not a valid field for schedule_message - it's auto-extracted from original_message context"
                )

        return errors

    def get_prompt_instructions(self, action_name: str) -> dict:
        """Prompt instructions for the supported actions."""
        if action_name == "event":
            return {
                "description": "Schedule a future reminder or event",
                "payload": {
                    "date": "2025-07-30",
                    "time": "13:00",
                    "repeat": "weekly",
                    "description": "Remind me to water the plants",
                    "created_by": "synth",
                    "interface": self.get_interface_id(),  # interface auto-corrected
                },
            }
        elif action_name == "schedule_message":
            return {
                "description": "Schedule a message to be sent after a delay (send_in) or at a specific time (send_at)",
                "payload": {
                    "text": "Reminder: do something important",
                    "send_in": "1 minute",  # Can be "5 minutes", "1 hour", "2 days", etc. OR use send_at instead
                    "send_at": "09:30",  # Alternatively: specific time as HH:MM, YYYY-MM-DD HH:MM, ISO 8601, or Unix timestamp
                },
            }
        return {}

    def execute_action(self, action: dict, context: dict, bot, original_message):
        """Execute an event action using the new plugin interface - SIMPLIFIED."""
        import asyncio

        action_type = action.get("type")
        payload = action.get("payload", {})

        log_info(
            f"[event_plugin] 🎬 execute_action: type={action_type}, payload={str(payload)[:80]}"
        )

        try:
            if action_type == "event":
                # Schedule on main loop instead of creating new one
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.create_task(
                            self._handle_event_payload(
                                payload, original_message=original_message
                            )
                        )
                    else:
                        loop.run_until_complete(
                            self._handle_event_payload(
                                payload, original_message=original_message
                            )
                        )
                except RuntimeError as e:
                    # No event loop available - log and return
                    log_error(
                        f"[event_plugin] Could not get event loop for event payload: {e}"
                    )
                log_info("[event_plugin] ✅ Event saved")

            elif action_type == "schedule_message":
                self._execute_schedule_message_sync(
                    payload, original_message=original_message
                )

            else:
                log_error(f"[event_plugin] ❌ Unsupported action: {action_type}")
        except Exception as e:
            log_error(f"[event_plugin] ❌ execute_action failed: {repr(e)}")
            import traceback

            log_error(traceback.format_exc())

    async def handle_custom_action(self, action_type: str, payload: dict):
        """Handle custom event actions (legacy method - kept for compatibility)."""
        if action_type == "event":
            log_info(
                "[event_plugin] Handling event action with payload: " + str(payload)
            )
            try:
                await self._handle_event_payload(payload)
            except Exception as e:
                log_error(f"[event_plugin] Error handling event action: {repr(e)}")
        elif action_type == "schedule_message":
            log_info(
                "[event_plugin] Handling schedule_message action with payload: "
                + str(payload)
            )
            try:
                await self._handle_schedule_message_payload(
                    payload, original_message=None
                )
            except Exception as e:
                log_error(
                    f"[event_plugin] Error handling schedule_message action: {repr(e)}"
                )
        else:
            log_error(f"[event_plugin] Unsupported action type: {action_type}")

    async def _handle_event_payload(self, payload: dict, original_message=None):
        """Shared logic for processing an event payload."""
        date_str = payload.get("date")
        description = payload.get("description")
        time_str = payload.get("time") or "00:00"
        repeat = payload.get("repeat", "none")
        created_by = payload.get("created_by", "synth")

        # Clean time_str to remove timezone suffixes like "UTC"
        import re

        time_str = re.sub(r"\s+UTC$", "", time_str).strip()

        if not date_str or not description:
            log_error(
                "[event_plugin] Invalid event payload: missing 'date' or 'description'"
            )
            return

        # Extract interface_path from original_message if available
        interface_path = None
        log_debug(
            f"[event_plugin] _handle_event_payload: original_message={original_message}"
        )
        log_debug(
            f"[event_plugin] _handle_event_payload: original_message attrs={dir(original_message) if original_message else 'None'}"
        )

        if original_message:
            if hasattr(original_message, "interface_path"):
                interface_path = original_message.interface_path
                log_debug(
                    f"[event_plugin] ✅ Extracted interface_path from original_message: {interface_path}"
                )
            elif hasattr(original_message, "chat_id") or hasattr(
                original_message, "chat"
            ):
                # Construct interface_path from Telegram message attributes
                chat_id = getattr(original_message, "chat_id", None) or (
                    original_message.chat.id
                    if hasattr(original_message, "chat")
                    else None
                )
                thread_id = getattr(original_message, "message_thread_id", None)

                if chat_id:
                    interface_path = f"telegram_bot/{chat_id}"
                    if thread_id:
                        interface_path += f"/{thread_id}"
                    log_info(
                        f"[event_plugin] ✅ Constructed interface_path from Telegram message: {interface_path}"
                    )
                else:
                    log_warning(
                        "[event_plugin] ⚠️ Could not extract chat_id from original_message"
                    )
            else:
                log_warning(
                    "[event_plugin] ⚠️ No interface_path or chat info found in original_message"
                )

        # Add interface_path to description for later retrieval
        if interface_path:
            description += f" [interface_path: {interface_path}]"
            log_info("[event_plugin] ✅ Appended interface_path to description")

        # Extract original context from the conversation
        original_context = self._extract_original_context(original_message)

        await self._save_scheduled_reminder(
            date_str,
            time_str,
            repeat,
            description,
            created_by,
            original_context=original_context,
        )
        log_info(
            f"[event_plugin] Reminder scheduled for {date_str} {time_str} ({repeat}): {description}"
        )

        # Confirmation messages are no longer sent directly from the plugin.
        # The LLM will decide if and how to notify the user about scheduled
        # reminders. This keeps event creation interface-agnostic.

    def _execute_schedule_message_sync(self, payload: dict, original_message=None):
        """Schedule message by creating async task (don't use asyncio.run - we're already in event loop)."""
        import asyncio

        try:
            # Create task instead of asyncio.run() - we're already inside an event loop!
            task = asyncio.create_task(
                self._handle_schedule_message_payload(
                    payload, original_message=original_message
                )
            )
            log_info(
                f"[event_plugin] 🎯 Schedule message task created: {task.get_name()}"
            )
        except Exception as e:
            log_error(
                f"[event_plugin] ❌ _execute_schedule_message_sync failed: {repr(e)}"
            )
            import traceback

            log_error(traceback.format_exc())

    def _extract_original_context(self, original_message) -> str:
        """Extract conversation context from original_message object."""
        if not original_message:
            return None

        try:
            # Try to build a summary of what triggered this action
            parts = []

            # Add user message if available
            if hasattr(original_message, "text") and original_message.text:
                user_text = original_message.text[:200]  # Limit length
                parts.append(f"User: {user_text}")

            # Add message ID and interface_path info if available (for context linking)
            if hasattr(original_message, "message_id"):
                parts.append(f"[Message ID: {original_message.message_id}]")

            if hasattr(original_message, "interface_path"):
                parts.append(f"[Interface: {original_message.interface_path}]")

            context_str = " / ".join(parts)
            return context_str if context_str else None
        except Exception as e:
            log_debug(
                f"[event_plugin] Could not extract context from original_message: {e}"
            )
            return None

    async def _handle_schedule_message_payload(
        self, payload: dict, original_message=None
    ):
        """Handle schedule_message action by converting delay (send_in) or absolute time (send_at) to date/time."""
        log_info(
            f"[event_plugin] ⏰ _handle_schedule_message_payload CALLED with payload: {payload}"
        )
        text = payload.get("text")
        send_in = payload.get("send_in")
        send_at = payload.get("send_at")

        if not text:
            log_error("[event_plugin] Invalid schedule_message payload: missing 'text'")
            return

        from datetime import datetime, timedelta, timezone
        import re

        try:
            future_time = None

            # Priority: send_at (absolute time) over send_in (delay)
            if send_at:
                log_info(f"[event_plugin] Processing send_at: {send_at}")
                from core.time_zone_utils import get_local_timezone

                # Try Unix timestamp first
                try:
                    ts = int(send_at)
                    future_time = datetime.fromtimestamp(ts, tz=timezone.utc)
                    log_info(f"[event_plugin] Parsed as Unix timestamp: {future_time}")
                except (ValueError, TypeError):
                    # Try ISO format: 2025-11-20T09:00:00 or 2025-11-20T09:00:00Z
                    try:
                        future_time = datetime.fromisoformat(
                            send_at.replace("Z", "+00:00")
                        )
                        if future_time.tzinfo is None:
                            future_time = future_time.replace(tzinfo=timezone.utc)
                        log_info(f"[event_plugin] Parsed as ISO format: {future_time}")
                    except ValueError:
                        # Try YYYY-MM-DD HH:MM format
                        try:
                            dt_local = datetime.strptime(send_at, "%Y-%m-%d %H:%M")
                            local_tz = get_local_timezone()
                            dt_local = dt_local.replace(tzinfo=local_tz)
                            future_time = dt_local.astimezone(timezone.utc)
                            log_info(
                                f"[event_plugin] Parsed as YYYY-MM-DD HH:MM (local): {send_at} → UTC {future_time}"
                            )
                        except ValueError:
                            # Try HH:MM format (today in local timezone)
                            try:
                                now_local = datetime.now(tz=get_local_timezone())
                                time_part = datetime.strptime(send_at, "%H:%M").time()
                                dt_local = datetime.combine(
                                    now_local.date(),
                                    time_part,
                                    tzinfo=get_local_timezone(),
                                )
                                future_time = dt_local.astimezone(timezone.utc)
                                log_info(
                                    f"[event_plugin] Parsed as HH:MM (today, local): {send_at} → UTC {future_time}"
                                )
                            except ValueError:
                                error_msg = (
                                    f"❌ Formato send_at non riconosciuto: `{send_at}`\n\n"
                                    f"**Formati accettati:**\n"
                                    f"  • **HH:MM**: `09:30` (oggi a quell'ora)\n"
                                    f"  • **YYYY-MM-DD HH:MM**: `2025-11-20 09:30`\n"
                                    f"  • **ISO 8601**: `2025-11-20T09:30:00` o `2025-11-20T09:30:00Z`\n"
                                    f"  • **Unix timestamp**: `1735142400`\n\n"
                                    f"**Esempi:**\n"
                                    f"  ✅ `schedule_message` con `send_at='09:30'` (oggi)\n"
                                    f"  ✅ `schedule_message` con `send_at='2025-11-20 09:30'`\n"
                                    f"  ✅ `schedule_message` con `send_at='2025-11-20T09:30:00Z'`"
                                )
                                await self._send_corrective_message(
                                    error_msg, original_message
                                )
                                return

            elif send_in:
                log_info(f"[event_plugin] Processing send_in (delay): {send_in}")
                # Parse delay in multiple formats:
                # - Full format: "5 minutes", "2 hours", "1 day"
                # - Abbreviated: "6m", "2h", "1d", "1w" (with or without space)
                # - Time format: "1:23" (1 minute 23 seconds), "5:30" (5 minutes 30 seconds)

                quantity = None
                unit = None

                # Try MM:SS format first (e.g., "1:23" means 1m 23s)
                match = re.match(r"^(\d+):(\d+)$", send_in.strip())
                if match:
                    minutes = int(match.group(1))
                    seconds = int(match.group(2))
                    # Convert to total seconds
                    total_seconds = minutes * 60 + seconds
                    quantity = total_seconds
                    unit = "second"
                else:
                    # Try full format (e.g., "5 minutes", "2 hours")
                    match = re.match(
                        r"(\d+)\s+(second|minute|hour|day|week)s?", send_in.lower()
                    )
                    if match:
                        quantity = int(match.group(1))
                        unit = match.group(2)
                        # Normalize unit to singular form
                        if unit.endswith("s"):
                            unit = unit[:-1]
                    else:
                        # Try abbreviation format (e.g., "6m", "2h", "1d", "1w")
                        match = re.match(r"^(\d+)\s*([smhdw])$", send_in.lower())
                        if match:
                            quantity = int(match.group(1))
                            abbrev = match.group(2)
                            # Convert abbreviation to full name
                            abbrev_map = {
                                "s": "second",
                                "m": "minute",
                                "h": "hour",
                                "d": "day",
                                "w": "week",
                            }
                            unit = abbrev_map.get(abbrev)

                if not quantity or not unit:
                    log_error(f"[event_plugin] Invalid send_in format: {send_in}")
                    # Send corrective feedback
                    error_msg = (
                        f"❌ Formato send_in non riconosciuto: `{send_in}`\n\n"
                        f"**Formati accettati:**\n"
                        f"  • **Abbreviati**: `6m`, `2h`, `1d`, `1w`, `30s`\n"
                        f"  • **Completi**: `5 minutes`, `2 hours`, `1 day`\n"
                        f"  • **MM:SS**: `1:30` (1 minuto 30 secondi), `5:45`\n\n"
                        f"**Esempi corretti:**\n"
                        f"  ✅ `schedule_message` con `send_in='5 minutes'`\n"
                        f"  ✅ `schedule_message` con `send_in='6m'`\n"
                        f"  ✅ `schedule_message` con `send_in='1:30'`"
                    )
                    await self._send_corrective_message(error_msg, original_message)
                    return

                log_info(f"[event_plugin] ⏰ Parsed delay: {quantity} {unit}(s)")

                # Calculate future time
                now_utc = datetime.now(timezone.utc)
                if unit == "second":
                    future_time = now_utc + timedelta(seconds=quantity)
                elif unit == "minute":
                    future_time = now_utc + timedelta(minutes=quantity)
                elif unit == "hour":
                    future_time = now_utc + timedelta(hours=quantity)
                elif unit == "day":
                    future_time = now_utc + timedelta(days=quantity)
                elif unit == "week":
                    future_time = now_utc + timedelta(weeks=quantity)
                else:
                    log_error(f"[event_plugin] Unknown time unit: {unit}")
                    return

            else:
                # Neither send_in nor send_at provided (should have been caught by validation)
                log_error("[event_plugin] Neither send_in nor send_at provided")
                return

            # Convert UTC future time to local timezone for display/storage
            from core.time_zone_utils import utc_to_local

            future_time_local = utc_to_local(future_time)

            # Extract date and time strings in LOCAL timezone
            date_str = future_time_local.strftime("%Y-%m-%d")
            time_str = future_time_local.strftime("%H:%M")
            log_info(
                f"[event_plugin] ⏰ Scheduled for {date_str} {time_str} local time (UTC: {future_time.strftime('%Y-%m-%d %H:%M')})"
            )

            # Extract interface_path from original_message for proper delivery
            interface_path = None
            if original_message and hasattr(original_message, "interface_path"):
                interface_path = original_message.interface_path
                log_debug(
                    f"[event_plugin] 📍 Extracted interface_path from original_message: {interface_path}"
                )
            else:
                log_debug(
                    f"[event_plugin] 📍 No interface_path in original_message (None={original_message is None})"
                )

            # Build description with interface context for proper delivery
            # Format: MESSAGE: {text} [interface_path: {interface_path}]
            description = f"MESSAGE: {text}"
            if interface_path:
                description += f" [interface_path: {interface_path}]"
                log_debug(
                    f"[event_plugin] 📝 Added interface_path to description: {interface_path}"
                )
            else:
                log_debug(
                    "[event_plugin] ⚠️ No interface_path to add to description - will use default during delivery"
                )

            # Extract original context from the conversation
            original_context = self._extract_original_context(original_message)
            log_debug(
                f"[event_plugin] 📋 Extracted original_context: {original_context}"
            )

            # Save as a one-time reminder (will be converted to UTC by _save_scheduled_reminder)
            await self._save_scheduled_reminder(
                date_str=date_str,
                time_str=time_str,
                repeat="none",
                description=description,
                created_by="synth",
                original_context=original_context,
            )
            log_info(
                f"[event_plugin] Scheduled message for {date_str} {time_str}: {text[:50]}..."
            )

        except Exception as e:
            log_error(f"[event_plugin] Error processing schedule_message: {repr(e)}")

    async def _save_scheduled_reminder(
        self,
        date_str: str,
        time_str: str,
        repeat: str,
        description: str,
        created_by: str = "synth",
        original_context: str = None,
        conversation_user_message: str = None,
        conversation_llm_response: str = None,
    ) -> None:
        """Save a scheduled reminder to the database.

        Args:
            date_str: Event date (YYYY-MM-DD)
            time_str: Event time (HH:MM)
            repeat: Recurrence pattern (none, daily, weekly, monthly, always)
            description: Event description
            created_by: Who created this event (default: "synth")
            original_context: Original context from conversation (for user-initiated events)
            conversation_user_message: Original user message that triggered event creation
            conversation_llm_response: Original LLM response that created the event
        """
        try:
            valid_recurrence_types = {"none", "daily", "weekly", "monthly", "always"}
            if repeat not in valid_recurrence_types:
                log_warning(
                    f"[event_plugin] Invalid repeat '{repeat}', defaulting to 'none'"
                )
                repeat = "none"

            reminder_description = "REMINDER: " + str(description)

            await insert_scheduled_event(
                date_str,
                time_str,
                repeat,
                reminder_description,
                created_by,
                original_context=original_context,
                conversation_user_message=conversation_user_message,
                conversation_llm_response=conversation_llm_response,
            )
            from core.time_zone_utils import parse_local_to_utc, format_dual_time

            try:
                utc_dt = parse_local_to_utc(date_str, time_str or "00:00")
                dual = format_dual_time(utc_dt)
            except Exception:
                dual = f"{date_str} {time_str}"
            log_debug(
                f"[event_plugin] Saved scheduled reminder for {dual} (repeat: {repeat}): {description}"
            )
        except Exception as e:
            log_error(f"[event_plugin] Failed to save scheduled reminder: {repr(e)}")

    async def _event_scheduler(self):
        """Background task that checks and executes due events."""
        log_info("[event_plugin] Event scheduler loop started (singleton)")

        # On startup, log pending events that survived a reboot
        try:
            from core.db import get_due_events

            pending = await get_due_events()
            if pending:
                log_warning(
                    f"[event_plugin] ⏰ STARTUP: Found {len(pending)} pending events from previous session (some may be late)"
                )
                for evt in pending:
                    is_late_marker = "⏱️ LATE" if evt.get("is_late") else "✓ On-time"
                    desc = evt.get("description", "no description")[:50]
                    log_info(
                        f"[event_plugin]   {is_late_marker}: Event #{evt.get('id')} - {desc}"
                    )
            else:
                log_debug("[event_plugin] No pending events found at startup")
        except Exception as e:
            log_warning(
                f"[event_plugin] Could not check pending events at startup: {e}"
            )

        while EventPlugin._scheduler_running:
            try:
                log_debug("[event_plugin] Event scheduler checking for due events...")
                await self._check_and_execute_events()
                await self._poll_external_calendars()
                await asyncio.sleep(30)  # Check every 30 seconds
            except asyncio.CancelledError:
                log_info("[event_plugin] Event scheduler cancelled")
                break
            except Exception as e:
                log_error(
                    f"[event_plugin] Error in event scheduler: {repr(e)}\n{traceback.format_exc()}"
                )
                await asyncio.sleep(60)  # Wait longer on error
        log_info("[event_plugin] Event scheduler loop ended")

    async def _check_and_execute_events(self):
        """Check for due events and execute them."""
        try:
            log_debug("[EventPlugin] Starting due events check...")
            due_events = await get_due_events()

            # Skip events owned by self-managed plugins (e.g. weather_plugin),
            # which dispatch their own events to the correct interface. Without
            # this filter both the generic scheduler and the owning plugin would
            # deliver the same event, causing duplicate messages and a skipped
            # reschedule day.
            if due_events:
                skipped = [
                    e
                    for e in due_events
                    if e.get("created_by") in _SELF_MANAGED_EVENT_OWNERS
                ]
                if skipped:
                    log_debug(
                        f"[event_plugin] Skipping {len(skipped)} self-managed "
                        f"event(s) owned by {_SELF_MANAGED_EVENT_OWNERS}"
                    )
                due_events = [
                    e
                    for e in due_events
                    if e.get("created_by") not in _SELF_MANAGED_EVENT_OWNERS
                ]

            if due_events:
                log_info(
                    f"[event_plugin] Found {len(due_events)} due events to execute"
                )
                log_debug(f"[EventPlugin] Found events: {len(due_events)}")
                # Separate on-time and late events for logging
                on_time_events = [e for e in due_events if not e.get("is_late", False)]
                late_events = [e for e in due_events if e.get("is_late", False)]

                if on_time_events:
                    log_info(
                        f"[event_plugin] {len(on_time_events)} events executing on time"
                    )
                if late_events:
                    log_warning(
                        f"[event_plugin] {len(late_events)} events executing late!"
                    )
                    for event in late_events:
                        minutes_late = event.get("minutes_late", 0)
                        scheduled_time = event.get("scheduled_time", "unknown")
                        log_warning(
                            f"[event_plugin] Event {event['id']} is {minutes_late} minutes late (scheduled: {scheduled_time})"
                        )

                for event in due_events:
                    log_debug(f"[EventPlugin] Checking event: {event}")
                    await self._execute_scheduled_event(event)
            else:
                log_debug("[event_plugin] No due events to execute")
        except Exception as e:
            log_error(f"[event_plugin] Error checking due events: {repr(e)}")

    async def _poll_external_calendars(self) -> None:
        """Poll enabled external calendars and act on upcoming occurrences.

        Behaviour is gated by two config vars:

        * ``EXTERNAL_CAL_POLL_INTERVAL`` (seconds, default 900) — how often to
          fetch external calendars. The scheduler ticks every 30s, so this
          method self-rate-limits with a monotonic timestamp.
        * ``EXTERNAL_CAL_TRIGGER_BEATS`` (bool, default False) — when True,
          imminent occurrences fire ``scheduled_reminder`` Grillo beats exactly
          like internal events; when False the occurrences only enrich prompt
          context (handled elsewhere) and no beat is emitted here.
        """
        try:
            from core.config_manager import config_registry

            poll_interval = int(
                config_registry.get_value(
                    "EXTERNAL_CAL_POLL_INTERVAL",
                    900,
                    value_type=int,
                    label="External calendar poll interval (s)",
                    description=(
                        "How often SyntH fetches subscribed external calendars, "
                        "in seconds."
                    ),
                    group="calendar",
                    component="event_plugin",
                    advanced=True,
                )
            )
            trigger_beats = bool(
                config_registry.get_value(
                    "EXTERNAL_CAL_TRIGGER_BEATS",
                    False,
                    value_type=bool,
                    label="External calendars alert SyntH",
                    description=(
                        "When enabled, upcoming events from subscribed external "
                        "calendars proactively alert SyntH (like internal "
                        "reminders). When disabled, they only enrich context."
                    ),
                    group="calendar",
                    component="event_plugin",
                )
            )
        except Exception as e:
            log_warning(f"[event_plugin] external calendar config read failed: {e}")
            return

        loop = asyncio.get_running_loop()
        now_monotonic = loop.time()
        if (now_monotonic - EventPlugin._external_last_poll) < poll_interval:
            return
        EventPlugin._external_last_poll = now_monotonic

        if not trigger_beats:
            # Context-only mode: nothing to alert on. Occurrences are surfaced to
            # the prompt via the context enrichment path, not here.
            log_debug(
                "[event_plugin] external calendars in context-only mode; "
                "skipping beat trigger"
            )
            return

        try:
            from datetime import timedelta

            from core.external_calendars import gather_all_external_occurrences

            window_start = datetime.now(timezone.utc)
            window_end = window_start + timedelta(seconds=max(poll_interval, 900))
            occurrences = await gather_all_external_occurrences(
                window_start=window_start, window_end=window_end
            )
        except Exception as e:
            log_error(f"[event_plugin] external calendar poll failed: {repr(e)}")
            return

        if not occurrences:
            log_debug("[event_plugin] no upcoming external calendar occurrences")
            return

        log_info(
            f"[event_plugin] external calendars: {len(occurrences)} upcoming "
            "occurrence(s), triggering reminder beats"
        )
        for occ in occurrences:
            try:
                await self._enqueue_external_reminder_beat(occ)
            except Exception as e:
                log_warning(
                    f"[event_plugin] failed to enqueue external reminder beat: {repr(e)}"
                )

    async def _enqueue_external_reminder_beat(self, occurrence: dict) -> None:
        """Enqueue a ``scheduled_reminder`` beat for an external occurrence.

        Mirrors ``_enqueue_reminder_beat`` for internal events but sources the
        prompt from an expanded external occurrence dict.
        """
        summary = occurrence.get("summary") or "(untitled event)"
        start_dt = occurrence.get("start")
        calendar_name = occurrence.get("calendar_name") or "external calendar"
        when_text = ""
        if isinstance(start_dt, datetime):
            try:
                from core.time_zone_utils import format_dual_time

                when_text = format_dual_time(start_dt)
            except Exception:
                when_text = start_dt.isoformat()

        prompt = (
            "An event from a subscribed external calendar is coming up.\n"
            f"Calendar: {calendar_name}\n"
            f"Event: {summary}\n"
            f"When: {when_text}\n\n"
            "Decide whether, how, and who to notify about this. You are not "
            "obligated to contact anyone; use your judgement."
        )

        from types import SimpleNamespace

        try:
            from core import message_queue

            source = occurrence.get("source") or "external"
            beat_id = f"{source}_{summary}"

            message = SimpleNamespace()
            message.chat_id = -1
            message.message_id = f"scheduled_reminder_{beat_id}"
            message.text = prompt
            message.from_user = SimpleNamespace(
                id=-1,
                username="scheduler",
                full_name="Scheduler",
                first_name="Scheduler",
            )
            message.chat = SimpleNamespace(id=-1, type="internal")
            message.date = datetime.now(timezone.utc)

            context_memory = {
                "grillo_beat": True,
                "beat_type": "scheduled_reminder",
                "external_calendar": calendar_name,
                "external_source": source,
                "skip_history": True,
            }
            reminder_scope = _reminder_action_scope()
            if reminder_scope is not None:
                context_memory["allowed_action_types"] = reminder_scope
            await message_queue.enqueue_low_priority(
                None,
                message,
                context_memory=context_memory,
                interface_id="grillo",
                original_message=None,
            )
            log_debug(f"[event_plugin] enqueued external reminder beat for '{summary}'")
        except Exception as e:
            log_error(
                f"[event_plugin] enqueue external reminder beat failed: {repr(e)}"
            )

    async def _execute_scheduled_event(self, event: dict):
        """Execute a scheduled event and deliver it to the LLM for processing."""
        try:
            description = event.get("description", "")
            event_id = event.get("id", "unknown")

            # Extract lateness info
            is_late = event.get("is_late", False)
            minutes_late = event.get("minutes_late", 0)

            # Log execution with lateness info
            if is_late:
                log_info(
                    f"[event_plugin] Delivering LATE event {event_id} ({minutes_late} min late): {description[:50]}..."
                )
            else:
                log_info(
                    f"[event_plugin] Delivering scheduled event {event_id}: {description[:50]}..."
                )

            log_debug(f"[EventPlugin] Executing event: {event}")
            # Create a structured prompt for the LLM representing this scheduled event
            # The LLM will decide what to do with it
            await self._deliver_event_to_llm(event)

            log_debug(f"[EventPlugin] Event {event['id']} executed successfully")

        except Exception as e:
            log_error(
                f"[event_plugin] Error delivering event {event.get('id', 'unknown')}: {repr(e)}"
            )

    async def _deliver_event_to_llm(self, event: dict):
        """Deliver a due event to Synth as an internal ``scheduled_reminder`` beat.

        Events are NOT tied to any single interface. Instead of hardcoding a
        delivery target (the old behaviour, which forced ``telegram_bot`` and
        regex-scraped an ``interface_path`` from the description — silently
        dropping the reminder when none was found), the reminder is enqueued as
        an internal Grillo beat. Synth receives it as a thought and decides
        whether, how and whom to contact, choosing from the routable targets
        presented in the prompt.

        The event is marked delivered ONLY after a successful enqueue, so a
        transient failure leaves it pending for the next scheduler cycle.
        """
        raw_id = event.get("id")
        try:
            event_id = int(raw_id) if raw_id is not None else None
        except (TypeError, ValueError):
            event_id = None

        try:
            reminder_prompt = await self._build_reminder_beat_prompt(event)

            enqueued = await self._enqueue_reminder_beat(event_id, reminder_prompt)

            if not enqueued:
                log_warning(
                    f"[event_plugin] Failed to enqueue reminder beat for event "
                    f"{event_id}; will retry next cycle"
                )
                return

            log_info(
                f"[event_plugin] Event {event_id} enqueued as scheduled_reminder beat"
            )

            # Mark delivered ONLY after a successful enqueue.
            try:
                if event_id is not None:
                    if await mark_event_delivered(event_id):
                        log_info(
                            f"[event_plugin] Event {event_id} marked as delivered in DB"
                        )
                    else:
                        log_warning(
                            f"[event_plugin] Failed to mark event {event_id} as "
                            f"delivered in DB (will retry next cycle)"
                        )
                else:
                    log_warning(
                        "[event_plugin] Cannot mark event with invalid id as delivered"
                    )
            except Exception as e:
                log_warning(
                    f"[event_plugin] Error marking event {event_id} delivered: {e}"
                )
        except Exception as outer_e:
            log_error(
                f"[event_plugin] Error in _deliver_event_to_llm for event {event_id}: {repr(outer_e)}"
            )

    async def _build_reminder_beat_prompt(self, event: dict) -> str:
        """Frame a due event as an internal-thought prompt for Synth.

        Includes the event details and the currently routable interface_path
        targets so Synth can pick a real path if it decides to reach out.
        """
        from core.beat_utils import (
            collect_routable_targets,
            render_routable_targets_block,
        )
        from core.time_zone_utils import format_dual_time

        event_id = event.get("id", "unknown")
        description = str(event.get("description", "")).strip()
        repeat = event.get("recurrence_type", "none")

        next_run_val = event.get("next_run")
        scheduled_time = "unknown"
        try:
            if isinstance(next_run_val, datetime):
                dt_utc = next_run_val
            else:
                dt_utc = datetime.fromisoformat(
                    str(next_run_val).replace("Z", "+00:00")
                )
            if dt_utc.tzinfo is None:
                dt_utc = dt_utc.replace(tzinfo=timezone.utc)
            scheduled_time = format_dual_time(dt_utc)
        except Exception:
            scheduled_time = "unknown"

        is_late = bool(event.get("is_late", False))
        minutes_late = event.get("minutes_late", 0)
        lateness_note = ""
        if is_late and minutes_late:
            lateness_note = f" (this reminder is {minutes_late} minutes late)"

        targets = await collect_routable_targets()
        targets_block = render_routable_targets_block(targets)

        header = (
            "[SCHEDULED REMINDER] One of your own scheduled events is now due. "
            "This is an internal thought firing right now, not a message from "
            "anyone. This reminder already exists, so there is no need to schedule "
            "or reschedule it again — the moment to act on it is this turn. If the "
            "reminder tells you what to do (for example it asks you to send a "
            "message on a specific interface path), the natural thing is to follow "
            "through now and send that message on the path it names. Otherwise "
            "it's up to you: reach out to someone, do something, or simply take "
            "note of it. When you do choose to contact someone, pick one of the "
            "routable interface_path values below rather than making one up."
        )
        body = (
            f"\n\nEvent #{event_id} scheduled for {scheduled_time}{lateness_note}.\n"
            f"Recurrence: {repeat}.\n"
            f"Reminder: {description or '(no description)'}\n"
        )
        return header + body + targets_block

    async def _enqueue_reminder_beat(self, event_id: object, prompt: str) -> bool:
        """Enqueue the reminder as a low-priority ``scheduled_reminder`` beat."""
        from types import SimpleNamespace

        try:
            from core import message_queue

            message = SimpleNamespace()
            message.chat_id = -1
            message.message_id = f"scheduled_reminder_{event_id}"
            message.text = prompt
            message.from_user = SimpleNamespace(
                id=-1,
                username="scheduler",
                full_name="Scheduler",
                first_name="Scheduler",
            )
            message.chat = SimpleNamespace(id=-1, type="internal")
            message.date = datetime.now(timezone.utc)

            context_memory = {
                "grillo_beat": True,
                "beat_type": "scheduled_reminder",
                "event_id": event_id,
                "skip_history": True,
            }
            reminder_scope = _reminder_action_scope()
            if reminder_scope is not None:
                context_memory["allowed_action_types"] = reminder_scope

            await message_queue.enqueue_low_priority(
                None,
                message,
                context_memory=context_memory,
                interface_id="grillo",
                original_message=None,
            )
            return True
        except Exception as e:
            log_error(
                f"[event_plugin] Failed to enqueue scheduled_reminder beat for "
                f"event {event_id}: {repr(e)}"
            )
            return False

    def _create_scheduler_message(self, event: dict):
        """Create a scheduler message object for the event."""
        from types import SimpleNamespace

        return SimpleNamespace(
            message_id=f"event_{event['id']}",
            interface_path="system/scheduler",
            chat_id=-1,
            text="Reminder: " + str(event.get("description", "")),
            from_user=SimpleNamespace(
                id=-1,  # System user ID
                full_name="synth Scheduler",
                username="synth_scheduler",
            ),
            date=datetime.utcnow(),
            reply_to_message=None,
            chat=SimpleNamespace(
                id="SYSTEM_SCHEDULER", type="private", title="System Scheduler"
            ),
            thread_id=None,
        )

    async def _execute_action_silently(self, action: dict, event_id: int):
        """Execute an action silently without involving any interfaces."""
        try:
            action_type = action.get("type")
            payload = action.get("payload", {})

            log_debug(
                f"[event_plugin] Executing silent action {action_type} for event {event_id}"
            )

            if action_type == "message":
                # For message actions, send directly through the appropriate transport
                await self._send_scheduled_message(payload, event_id)
            else:
                # For other action types, delegate to action plugins
                await self._execute_other_action_silently(action, event_id)

        except Exception as e:
            log_error(
                f"[event_plugin] Error executing silent action for event {event_id}: {repr(e)}"
            )

    async def _send_scheduled_message(self, payload: dict, event_id: int):
        """Send a scheduled message directly without interface involvement."""
        try:
            text = payload.get("text", "")
            target_chat_id = payload.get("target")
            thread_id = payload.get("thread_id")

            if not text or not target_chat_id:
                log_error(
                    f"[event_plugin] Invalid message payload for event {event_id}"
                )
                return

            log_info(
                f"[event_plugin] Sending scheduled message to {target_chat_id}: {text}"
            )

            # Get the appropriate transport layer directly
            await self._send_via_transport_layer(
                target_chat_id, text, thread_id, event_id
            )

        except Exception as e:
            log_error(
                f"[event_plugin] Error sending scheduled message for event {event_id}: {repr(e)}"
            )

    async def _send_via_transport_layer(
        self,
        interface_path_or_chat_id,
        text: str,
        thread_id: int = None,
        event_id: int = None,
    ):
        """Send message via transport layer using interface_path (preferred) or legacy chat_id."""
        try:
            # Check if this is an interface_path (string starting with interface name)
            # or a chat_id (integer or numeric string)
            if (
                isinstance(interface_path_or_chat_id, str)
                and not interface_path_or_chat_id.replace("-", "")
                .replace("+", "")
                .isdigit()
            ):
                # This is an interface_path
                interface_path = interface_path_or_chat_id
                await self._send_via_interface_path(interface_path, text, event_id)
            else:
                # This is a chat_id (legacy path)
                chat_id = (
                    int(interface_path_or_chat_id)
                    if isinstance(interface_path_or_chat_id, str)
                    else interface_path_or_chat_id
                )
                await self._send_via_telegram_transport(
                    chat_id, text, thread_id, event_id
                )

        except Exception as e:
            log_error(
                f"[event_plugin] Error in transport layer for event {event_id}: {repr(e)}"
            )

    async def _send_via_interface_path(
        self, interface_path: str, text: str, event_id: int = None
    ):
        """Send message using interface_path through the message plugin."""
        try:
            from core.action_parser import run_action
            from types import SimpleNamespace

            # Create a message-like object for the action
            payload = {
                "text": text,
                "interface_path": interface_path,
            }

            message = SimpleNamespace(
                interface_path=interface_path,
                from_cortex=False,  # This is from the system, not the LLM
            )

            # Route to the interface named in the path (telegram_bot/...,
            # synth_webui/..., ...) instead of assuming Telegram.
            interface_name = interface_path.split("/")[0]
            action = {
                "type": f"message_{interface_name}",
                "payload": payload,
            }

            context = {"interface_path": interface_path}
            await run_action(action, context, None, message)
            log_info(
                f"[event_plugin] ✅ Message sent via interface_path {interface_path} (event {event_id})"
            )

        except Exception as e:
            log_error(f"[event_plugin] Error sending via interface_path: {repr(e)}")

    async def _send_corrective_message(self, error_msg: str, original_message=None):
        """Send corrective feedback to user about invalid format."""
        try:
            if not original_message:
                log_warning(
                    "[event_plugin] No original_message to send corrective feedback"
                )
                return

            interface_path = getattr(original_message, "interface_path", None)

            if not interface_path:
                log_warning(
                    "[event_plugin] Cannot determine interface_path for corrective message"
                )
                return

            log_info(
                f"[event_plugin] 💬 Sending corrective message via {interface_path}"
            )
            await self._send_via_transport_layer(interface_path, error_msg)

        except Exception as e:
            log_error(f"[event_plugin] Error sending corrective message: {repr(e)}")

    async def _send_via_telegram_transport(
        self,
        chat_id: int,
        text: str,
        thread_id: int = None,
        event_id: int = None,
    ):
        """Send message directly via Telegram transport layer."""
        try:
            from core.core_initializer import INTERFACE_REGISTRY

            bot = None
            telegram_iface = INTERFACE_REGISTRY.get("telegram_bot")
            if telegram_iface and getattr(telegram_iface, "bot", None):
                bot = telegram_iface.bot
                self.bot = bot
            elif self.bot:
                bot = self.bot
            if not bot:
                raise ImportError

            await send_with_thread_fallback(
                bot,
                chat_id,
                text,
                thread_id=thread_id,  # fixed: correct param is thread_id
                parse_mode="Markdown",
            )

            log_info(
                f"[event_plugin] ✅ Scheduled message sent to {chat_id} (event {event_id})"
            )

        except ImportError:
            log_error(
                f"[event_plugin] Telegram transport layer not available for event {event_id}"
            )
            # Fallback: use the bot instance directly if available
            await self._fallback_send_telegram(chat_id, text, thread_id, event_id)
        except Exception as e:
            log_error(
                f"[event_plugin] Error in Telegram transport for event {event_id}: {repr(e)}"
            )

    async def _fallback_send_telegram(
        self,
        chat_id: int,
        text: str,
        thread_id: int = None,
        event_id: int = None,
    ):
        """Fallback method to send via Telegram bot directly."""
        try:
            from core.core_initializer import INTERFACE_REGISTRY

            bot = None
            telegram_iface = INTERFACE_REGISTRY.get("telegram_bot")
            if telegram_iface and getattr(telegram_iface, "bot", None):
                bot = telegram_iface.bot
                self.bot = bot
            elif self.bot:
                bot = self.bot

            if bot:
                await send_with_thread_fallback(
                    bot,
                    chat_id,
                    text,
                    thread_id=thread_id,  # fixed: correct param is thread_id
                    parse_mode="Markdown",
                )
                log_info(
                    f"[event_plugin] ✅ Fallback Telegram send successful for event {event_id}"
                )
            else:
                log_error(
                    f"[event_plugin] No Telegram bot available for fallback send (event {event_id})"
                )

        except Exception as e:
            log_error(
                f"[event_plugin] Fallback Telegram send failed for event {event_id}: {repr(e)}"
            )

    async def _execute_other_action_silently(self, action: dict, event_id: int):
        """Execute non-message actions silently."""
        try:
            # For non-message actions, use the action parser directly
            from core.action_parser import parse_action

            # Create a silent bot that doesn't interact with interfaces
            silent_bot = self._create_silent_bot()

            # Create a minimal message context
            silent_message = type(
                "SilentMessage",
                (),
                {
                    "chat_id": -999999999,  # Special ID for silent execution
                    "thread_id": None,
                },
            )()

            await parse_action(action, silent_bot, silent_message)

            log_debug(f"[event_plugin] Silent action executed for event {event_id}")

        except Exception as e:
            log_error(
                f"[event_plugin] Error executing silent action for event {event_id}: {repr(e)}"
            )

    def _create_silent_bot(self):
        """Create a bot that silently logs actions instead of sending them."""

        class SilentBot:  # Internal logging class - methods NOT exposed as actions
            async def _log_message(self, **kwargs):
                text = kwargs.get("text", "")
                chat_id = kwargs.get("chat_id")
                log_debug(
                    f"[event_plugin] Silent bot action: send_message({chat_id}, '{text[:50]}...')"
                )

        return SilentBot()

    async def _delegate_to_active_cortex(
        self, action: dict, event_id: int, event_info: dict = None
    ):
        """Delegate the action execution to the active Cortex plugin."""
        try:
            # Track the current event ID for delivery confirmation
            self._current_processing_event_id = event_id

            # Create a unified message for scheduled events to avoid chat flooding
            # This ensures all scheduled events use the same chat context
            unified_message = self._create_unified_scheduled_message(
                action, event_id, event_info
            )

            log_debug(
                f"[event_plugin] Delegating event {event_id} to active Cortex via auto-response system"
            )

            # Create a JSON prompt for the scheduled action with lateness info
            scheduled_prompt = await self._create_scheduled_action_prompt(
                action, event_id, event_info
            )

            # Use auto-response system for autonomous scheduled event execution
            await request_llm_delivery(
                message=unified_message,
                interface=None,  # Let auto-response determine interface
                context=scheduled_prompt,
                reason=f"scheduled_action_{event_id}",
            )

        except Exception as e:
            log_error(
                f"[event_plugin] Error processing scheduled event {event_id}: {repr(e)}"
            )
            if hasattr(self, "_current_processing_event_id"):
                delattr(self, "_current_processing_event_id")

    def _create_unified_scheduled_message(
        self, action: dict, event_id: int, event_info: dict = None
    ):
        """Create a unified message object for scheduled events."""
        # Use a special chat_id for all scheduled events to avoid chat flooding
        # This uses a special negative ID that the chat management system can handle
        SCHEDULED_EVENTS_CHAT_ID = -999999999  # Special ID for scheduled events

        # Extract target info from the action for later routing
        target_chat_id = action.get("payload", {}).get(
            "target", SCHEDULED_EVENTS_CHAT_ID
        )
        thread_id = action.get("payload", {}).get("thread_id")

        # Extract lateness info
        is_late = event_info.get("is_late", False) if event_info else False
        minutes_late = event_info.get("minutes_late", 0) if event_info else 0

        # Create message text with lateness indication
        base_text = f"[SCHEDULED_EVENT_{event_id}] Execute planned action"
        if is_late:
            base_text += f" (⚠️ {minutes_late} minutes late)"

        # Create a message-like object that works with the existing chat management
        from types import SimpleNamespace

        message = SimpleNamespace(
            message_id=f"scheduled_event_{event_id}",
            # Use the special scheduled events chat ID - this will be managed by the chat system
            chat_id=SCHEDULED_EVENTS_CHAT_ID,
            text=base_text,
            from_user=SimpleNamespace(
                id=0,  # System user ID
                full_name="synth Scheduler",
                username="synth_scheduler",
            ),
            date=datetime.utcnow(),
            reply_to_message=None,
            chat=SimpleNamespace(
                id=SCHEDULED_EVENTS_CHAT_ID,
                type="private",  # Treat as private chat for management purposes
                title="synth Scheduled Events",  # Give it a recognizable title
            ),
            # Store the real target info for final message routing
            _scheduled_target_chat_id=target_chat_id,
            _scheduled_thread_id=thread_id,
            # Store lateness info
            _is_late=is_late,
            _minutes_late=minutes_late,
            # Add thread_id if present (for topic support)
            thread_id=None,  # Scheduled events don't use threads in their own chat
        )

        return message

    async def _create_scheduled_action_prompt(
        self, action: dict, event_id: int, event_info: dict = None
    ):
        """Create a JSON prompt for the scheduled action with lateness information."""

        # Extract lateness info if available
        is_late = event_info.get("is_late", False) if event_info else False
        minutes_late = event_info.get("minutes_late", 0) if event_info else 0
        from core.time_zone_utils import format_dual_time

        scheduled_time = "unknown"
        if event_info:
            next_run_val = event_info.get("next_run")
            try:
                if isinstance(next_run_val, datetime):
                    dt_utc = next_run_val
                else:
                    dt_utc = datetime.fromisoformat(
                        str(next_run_val).replace("Z", "+00:00")
                    )
                if dt_utc.tzinfo is None:
                    dt_utc = dt_utc.replace(tzinfo=timezone.utc)
                scheduled_time = format_dual_time(dt_utc)
            except Exception:
                scheduled_time = event_info.get("scheduled_time", "unknown")

        # Create lateness context for the LLM
        lateness_context = ""
        if is_late:
            if minutes_late < 60:
                lateness_context = f"⚠️ THIS MESSAGE IS {minutes_late} MINUTES LATE! It was scheduled for {scheduled_time}."
            else:
                hours_late = minutes_late // 60
                remaining_minutes = minutes_late % 60
                if remaining_minutes > 0:
                    lateness_context = f"⚠️ THIS MESSAGE IS LATE BY {hours_late}h {remaining_minutes}m! It was scheduled for {scheduled_time}."
                else:
                    lateness_context = f"⚠️ THIS MESSAGE IS LATE BY {hours_late} {'hour' if hours_late == 1 else 'hours'}! It was scheduled for {scheduled_time}."
        else:
            lateness_context = f"✅ Message on time (scheduled for {scheduled_time})"

        context = {
            "messages": [],
            "memories": [],
            "location": "",
            "date": datetime.utcnow().strftime("%Y-%m-%d"),
            "time": format_dual_time(datetime.utcnow().replace(tzinfo=timezone.utc)),
            "event_status": {
                "is_late": is_late,
                "minutes_late": minutes_late,
                "scheduled_time": scheduled_time,
                "lateness_context": lateness_context,
            },
        }

        try:
            from core.action_parser import gather_static_injections

            injections = await gather_static_injections()
            if isinstance(injections, dict):
                context.update(injections)
        except Exception as e:
            log_warning(f"[event_plugin] Failed to gather static injections: {e}")

        try:
            from core.prompt_engine import load_json_instructions

            json_rules = load_json_instructions()
        except Exception:
            json_rules = "Respond with strict JSON actions only."

        return {
            "context": context,
            "input": {
                "type": "scheduled_event",
                "event_id": event_id,
                "scheduled_action": action,
                "is_late": is_late,
                "minutes_late": minutes_late,
                "payload": {
                    "text": f"Execute scheduled event {event_id}{' (LATE)' if is_late else ''}",
                    "source": {
                        "chat_id": -999999999,  # Scheduled events chat
                        "message_id": f"scheduled_event_{event_id}",
                        "username": "synth Scheduler",
                        "usertag": "@synth_scheduler",
                    },
                    "timestamp": datetime.utcnow().isoformat() + "+00:00",
                    "privacy": "private",
                    "scope": "local",
                },
            },
            "instructions": (
                json_rules
                + "\n\nEvent reminder mode: interpret input.type='scheduled_event' and decide actions accordingly."
            ),
            "interface_instructions": "SCHED: Single JSON reply",
        }

    def _create_mock_bot_for_llm(self):
        """Create a mock bot that delegates LLM responses to action parser."""

        class ScheduledEventBot:
            def __init__(self, event_plugin):
                self.event_plugin = event_plugin

            async def _log_message(self, **kwargs):
                """Handle LLM responses and delegate to action parser."""
                text = kwargs.get("text", "")
                chat_id = kwargs.get("chat_id")
                thread_id = kwargs.get("thread_id")

                log_debug(f"[event_plugin] LLM responded with: {text}")

                # Parse the JSON response from the LLM
                if text.strip().startswith("{") and text.strip().endswith("}"):
                    try:
                        # Parse the action generated by LLM
                        response_action = json.loads(text.strip())
                        log_info(
                            f"[event_plugin] LLM generated action: {response_action}"
                        )

                        # Send this action through the normal action parser flow

                        # Create a proper message context for the action parser
                        # This ensures the action goes to the right interface
                        _action_message = type(
                            "ActionMessage",
                            (),
                            {
                                "chat_id": response_action.get("payload", {}).get(
                                    "target", chat_id
                                ),
                                "thread_id": response_action.get("payload", {}).get(
                                    "thread_id", thread_id
                                ),
                            },
                        )()

                        # Get the real bot instance from the active interface
                        real_bot = await self._get_active_bot()

                        if real_bot:
                            # Placeholder for the missing logic
                            pass
                    except Exception as e:
                        log_error(
                            f"[event_plugin] Error parsing LLM response action: {repr(e)}"
                        )
                else:
                    log_warning(f"[event_plugin] Ignored non-JSON LLM response: {text}")

        return ScheduledEventBot(self)

    def _register_custom_validation(self):
        """Register custom validation rules with the new validation system."""
        try:
            from core.validation_registry import ValidationRule, get_validation_registry

            def validate_event_payload(payload):
                """Enhanced validation for event actions."""
                errors = []

                # Validate date format and logic
                date_str = payload.get("date")
                if date_str:
                    try:
                        from datetime import datetime

                        event_date = datetime.strptime(date_str, "%Y-%m-%d")
                        # Check if date is not in the past
                        today = datetime.now().date()
                        if event_date.date() < today:
                            errors.append("Event date cannot be in the past")
                    except Exception:
                        errors.append("payload.date must be in format YYYY-MM-DD")

                # Validate time format if provided
                time_str = payload.get("time")
                if time_str:
                    try:
                        from datetime import datetime

                        datetime.strptime(time_str, "%H:%M")
                    except Exception:
                        errors.append("payload.time must be in format HH:MM")

                # Validate repeat options
                repeat = payload.get("repeat")
                if repeat and repeat not in [
                    "none",
                    "daily",
                    "weekly",
                    "monthly",
                    "always",
                ]:
                    errors.append(
                        "payload.repeat must be one of: none, daily, weekly, monthly, always"
                    )

                # Validate description length
                description = payload.get("description", "")
                if len(description) > 500:
                    errors.append("Event description cannot exceed 500 characters")

                return errors

            # Create custom validation rule
            rule = ValidationRule(
                action_type="event",
                required_fields=["date", "description"],
                custom_validator=validate_event_payload,
                component_name="event",
            )

            # Register with validation registry
            registry = get_validation_registry()
            registry.register_component_rules("event", [rule])

            log_debug(
                "[event_plugin] Registered custom validation rules with validation registry"
            )

        except Exception as e:
            log_warning(f"[event_plugin] Failed to register custom validation: {e}")


# Export the plugin class for the loader
PLUGIN_CLASS = EventPlugin
