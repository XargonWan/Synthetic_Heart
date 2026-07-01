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
from core.action_parser import CORRECTOR_RETRIES


class EventPlugin(AIPluginBase):
    """Plugin that stores future events without using an LLM."""

    # Class-level variables to prevent multiple schedulers
    _scheduler_running = False
    _scheduler_task = None
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
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
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
                            created_by VARCHAR(100) DEFAULT 'synth'
                        )
                        """
                    )
            log_info("[event_plugin] ensured scheduled_events table exists")
        except Exception as e:
            log_error(f"[event_plugin] Failed to ensure table exists: {repr(e)}")

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
        return ["event"]

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
        }

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
        """Deliver the event to the LLM as a structured input and wait for the response."""
        raw_id = event.get("id")
        try:
            event_id = int(raw_id) if raw_id is not None else None
        except (TypeError, ValueError):
            event_id = None

        try:
            event_prompt = await self._create_event_prompt(event)

            from core.core_initializer import INTERFACE_REGISTRY

            delivered = False
            for attempt in range(1, int(CORRECTOR_RETRIES) + 1):
                interface = INTERFACE_REGISTRY.get("telegram_bot")
                if not interface:
                    log_warning(
                        f"[event_plugin] No interface registered for event {event_id} "
                        f"(attempt {attempt}/{int(CORRECTOR_RETRIES)})"
                    )
                else:
                    from types import SimpleNamespace
                    import re

                    # Extract interface_path from description (format: "MESSAGE: ... [interface_path: {interface_path}]")
                    description = event.get("description", "")
                    interface_path = None
                    match = re.search(r"\[interface_path:\s*([^\]]+)\]", description)
                    if match:
                        interface_path = match.group(1).strip()
                        log_debug(
                            f"[event_plugin] ✅ Extracted interface_path from description: {interface_path}"
                        )

                    # Fallback: extract from original_context if not in description
                    if not interface_path:
                        original_context = event.get("original_context", "")
                        if original_context:
                            # Extract from format: [Interface: telegram_bot/chat_id/thread_id]
                            interface_match = re.search(
                                r"\[Interface:\s*([^\]]+)\]", original_context
                            )
                            if interface_match:
                                interface_path = interface_match.group(1).strip()
                                log_info(
                                    f"[event_plugin] ✅ Extracted interface_path from original_context: {interface_path}"
                                )

                    # If no interface_path, do NOT use a dummy one
                    # Leave it as None - the telegram_bot.send_message will check and silently skip
                    if not interface_path:
                        log_warning(
                            f"[event_plugin] ⚠️ Could not extract interface_path from event {event_id}, synthetic message will NOT be routed to any interface"
                        )

                    # Extract chat_id from interface_path for compatibility
                    # Format: telegram_bot/chat_id or telegram_bot/chat_id/thread_id
                    chat_id = None
                    if interface_path:
                        parts = interface_path.split("/")
                        if len(parts) >= 2:
                            chat_id = parts[1]  # Extract chat_id

                    synthetic_message = SimpleNamespace(
                        message_id=f"scheduled_event_{event_id}",
                        interface_path=interface_path,  # Will be None if not extracted
                        chat_id=int(chat_id)
                        if chat_id and chat_id.lstrip("-").isdigit()
                        else None,
                        text=f"[SCHEDULED_EVENT_{event_id}] {description[:50]}",
                        from_user=SimpleNamespace(
                            id=0, username="scheduler", full_name="Scheduler"
                        ),
                        chat=SimpleNamespace(
                            id=int(chat_id)
                            if chat_id and chat_id.lstrip("-").isdigit()
                            else None,
                            type="supergroup",
                        ),
                    )

                    delivered = await request_llm_delivery(
                        message=synthetic_message,
                        interface=interface,
                        context=event_prompt,
                        reason=f"scheduled_event_{event_id}",
                    )

                    if delivered:
                        log_info(f"[event_plugin] Event {event_id} delivered to LLM")
                        break

                if attempt < int(CORRECTOR_RETRIES) and not delivered:
                    await asyncio.sleep(attempt)

            if not delivered:
                log_warning(
                    f"[event_plugin] Failed to deliver event {event_id} after {int(CORRECTOR_RETRIES)} attempts"
                )
            else:
                # Mark as delivered ONLY if it was successfully sent to LLM
                try:
                    if event_id is not None:
                        if await mark_event_delivered(event_id):
                            log_info(
                                f"[event_plugin] ✅ Event {event_id} successfully marked as delivered in DB"
                            )
                        else:
                            log_warning(
                                f"[event_plugin] ⚠️ Failed to mark event {event_id} as delivered in DB (will retry next cycle)"
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

    async def _create_event_prompt(self, event: dict):
        """Create a structured prompt for the event delivery."""

        def make_json_serializable(obj):
            """Recursively convert datetime objects and dataclasses to JSON-serializable types."""
            from datetime import datetime, date, timedelta
            from dataclasses import is_dataclass, asdict

            if isinstance(obj, (datetime, date)):
                return obj.isoformat() if hasattr(obj, "isoformat") else str(obj)
            elif isinstance(obj, timedelta):
                return str(obj)
            elif isinstance(obj, dict):
                return {k: make_json_serializable(v) for k, v in obj.items()}
            elif isinstance(obj, (list, tuple)):
                return [make_json_serializable(item) for item in obj]
            elif is_dataclass(obj):
                return make_json_serializable(asdict(obj))
            return obj

        # Extract event details
        event_id = event.get("id", "unknown")
        from core.time_zone_utils import format_dual_time

        date = event.get("date", "")
        time = event.get("time", "")
        description = event.get("description", "")
        is_late = event.get("is_late", False)
        minutes_late = event.get("minutes_late", 0)

        next_run_val = event.get("next_run")
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

        # Create lateness context
        lateness_context = ""
        if is_late:
            if minutes_late < 60:
                lateness_context = f"⚠️ THIS EVENT IS {minutes_late} MINUTES LATE! It was scheduled for {scheduled_time}."
            else:
                hours_late = minutes_late // 60
                remaining_minutes = minutes_late % 60
                if remaining_minutes > 0:
                    lateness_context = f"⚠️ THIS EVENT IS LATE BY {hours_late}h {remaining_minutes}m! It was scheduled for {scheduled_time}."
                else:
                    lateness_context = f"⚠️ THIS EVENT IS LATE BY {hours_late} {'hour' if hours_late == 1 else 'hours'}! It was scheduled for {scheduled_time}."
        else:
            lateness_context = f"✅ Event on time (scheduled for {scheduled_time})"

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

            # FOR EVENT REMINDERS: Only get minimal injections to avoid bloating context with full persona/diary
            # Event reminders don't need complete memory context - just essential system state
            log_info(
                "[event_plugin] 📦 EVENT_REMINDER context reduction: gathering minimal injections only"
            )
            injections = await gather_static_injections()
            if isinstance(injections, dict):
                # Keep only essential keys for event reminders, skip heavy diary/persona data
                allowed_keys = {
                    "persona",
                    "persona_preferences",
                    "weather",
                    "current_time",
                    "instructions",
                }
                reduced_injections = {
                    k: v for k, v in injections.items() if k in allowed_keys
                }

                # For persona, if it exists, limit diary entries to last 5 only
                if "persona" in reduced_injections and isinstance(
                    reduced_injections["persona"], dict
                ):
                    persona = reduced_injections["persona"]
                    if "latest_diary_entries" in persona and isinstance(
                        persona["latest_diary_entries"], list
                    ):
                        # Keep only 5 most recent diary entries
                        persona["latest_diary_entries"] = persona[
                            "latest_diary_entries"
                        ][:5]
                        log_debug(
                            "[event_plugin] Persona diary reduced to 5 entries for event reminder"
                        )
                    if "memories" in persona and isinstance(persona["memories"], list):
                        # Keep only 3 most recent memories
                        persona["memories"] = persona["memories"][:3]
                        log_debug(
                            "[event_plugin] Persona memories reduced to 3 entries for event reminder"
                        )

                context.update(reduced_injections)
                log_info(
                    f"[event_plugin] 📦 Context size after reduction: {len(str(context))} chars (was potentially 200KB+)"
                )
        except Exception as e:
            log_warning(f"[event_plugin] Failed to gather static injections: {e}")

        log_debug(
            f"[event_plugin] Formatting event {event_id} as event_reminder for LLM"
        )

        # Convert date/time to serializable strings
        date_str = str(date) if date else ""
        time_str = str(time) if time else ""

        # Extract metadata from description for LLM
        import re

        interface_match = re.search(r"\[interface_path:\s*([^\]]+)\]", description)
        interface_path = interface_match.group(1).strip() if interface_match else None
        interface_name = (
            interface_path.split("/", 1)[0]
            if isinstance(interface_path, str) and "/" in interface_path
            else interface_path
        )

        result = {
            "context": context,
            "input": {
                "type": "event_reminder",
                "payload": {
                    "date": date_str,
                    "time": time_str,
                    "repeat": event.get("recurrence_type", "none"),
                    "description": description,
                    "created_by": event.get("created_by", "synth"),
                    "interface_path": interface_path,
                },
                "source": {
                    "event_id": event_id,
                    "origin": "scheduler",
                },
                "timestamp": (
                    event.get("next_run").isoformat()
                    if isinstance(event.get("next_run"), datetime)
                    else str(event.get("next_run"))
                    if event.get("next_run")
                    else datetime.utcnow().isoformat() + "+00:00"
                ),
            },
        }

        # Build PromptRequest delivery path for migrated engines while keeping
        # the legacy context/input/instructions keys for compatibility.
        try:
            from core.prompt_engine import build_delivery_request

            action_outputs = [
                {
                    "event_id": event_id,
                    "description": description,
                    "date": date_str,
                    "time": time_str,
                    "scheduled_time": scheduled_time,
                    "is_late": is_late,
                    "minutes_late": minutes_late,
                    "lateness_context": lateness_context,
                    "interface_path": interface_path,
                }
            ]
            result["__prompt_request"] = await build_delivery_request(
                action_type="event_reminder",
                action_outputs=action_outputs,
                interface_name=interface_name,
                interface_path=interface_path,
            )
        except Exception as e:
            log_debug(f"[event_plugin] build_delivery_request skipped: {e}")

        # Make all datetime objects JSON-serializable before returning
        log_debug(
            f"[event_plugin] Applying make_json_serializable to event {event_id} payload"
        )
        serializable_result = make_json_serializable(result)
        log_debug(f"[event_plugin] Serialization complete for event {event_id}")

        # Test JSON serialization immediately
        try:
            import json

            _ = json.dumps(serializable_result, ensure_ascii=False)
            log_debug(
                f"[event_plugin] ✅ Event {event_id} payload is JSON-serializable"
            )
        except Exception as e:
            log_error(
                f"[event_plugin] ❌ Event {event_id} payload still NOT JSON-serializable: {e}"
            )

        return serializable_result

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
