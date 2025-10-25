# plugins/event_plugin.py

from __future__ import annotations

import os
from datetime import datetime, timezone

from core.ai_plugin_base import AIPluginBase
from core.db import insert_scheduled_event, get_due_events, mark_event_delivered
from core.logging_utils import log_debug, log_info, log_error, log_warning
from interface.telegram_utils import send_with_thread_fallback
from core.auto_response import request_llm_delivery
import traceback
import asyncio
import json
import time
import aiomysql
from core.core_initializer import core_initializer, register_plugin

CORRECTOR_RETRIES = int(os.getenv("CORRECTOR_RETRIES", "2"))


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
        # Reuse the same variable name used in core.db for consistency
        password = os.getenv("DB_PASS", "")
        db_name = os.getenv("DB_NAME", "synth")

        log_debug(
            f"[event_plugin] Ensuring table scheduled_events in {user}@{host}:{port}/{db_name}"
        )

        conn = await aiomysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            db=db_name,
            autocommit=True,
        )
        try:
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
        finally:
            conn.close()

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
            }
        }

    def validate_payload(self, action_type: str, payload: dict) -> list:
        """Validate payload for event actions."""
        if action_type != "event":
            return []
        
        errors = []
        
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
        if repeat and repeat not in ["none", "daily", "weekly", "monthly", "always"]:
            errors.append("payload.repeat must be one of: none, daily, weekly, monthly, always")
        
        return errors

    def get_prompt_instructions(self, action_name: str) -> dict:
        """Prompt instructions for the supported actions."""
        if action_name != "event":
            return {}
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

    def execute_action(self, action: dict, context: dict, bot, original_message):
        """Execute an event action using the new plugin interface."""
        if action.get("type") == "event":
            log_info(
                "[event_plugin] Executing event action with payload: "
                + str(action.get("payload"))
            )
            try:
                # Use asyncio.create_task to handle async call from sync context
                import asyncio

                asyncio.create_task(
                    self._handle_event_payload(action.get("payload", {}))
                )
            except Exception as e:
                log_error(f"[event_plugin] Error executing event action: {repr(e)}")
        else:
            log_error(f"[event_plugin] Unsupported action type: {action.get('type')}")

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
        else:
            log_error(f"[event_plugin] Unsupported action type: {action_type}")

    async def _handle_event_payload(self, payload: dict):
        """Shared logic for processing an event payload."""
        date_str = payload.get("date")
        description = payload.get("description")
        time_str = payload.get("time") or "00:00"
        repeat = payload.get("repeat", "none")
        created_by = payload.get("created_by", "synth")

        if not date_str or not description:
            log_error(
                "[event_plugin] Invalid event payload: missing 'date' or 'description'"
            )
            return

        await self._save_scheduled_reminder(
            date_str,
            time_str,
            repeat,
            description,
            created_by,
        )
        log_info(
            f"[event_plugin] Reminder scheduled for {date_str} {time_str} ({repeat}): {description}"
        )

        # Confirmation messages are no longer sent directly from the plugin.
        # The LLM will decide if and how to notify the user about scheduled
        # reminders. This keeps event creation interface-agnostic.

    async def _save_scheduled_reminder(
        self,
        date_str: str,
        time_str: str,
        repeat: str,
        description: str,
        created_by: str = "synth",
    ) -> None:
        """Save a scheduled reminder to the database."""
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
            scheduled_time = event.get("scheduled_time", "unknown")

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
            for attempt in range(1, CORRECTOR_RETRIES + 1):
                interface = INTERFACE_REGISTRY.get("telegram_bot")
                if not interface:
                    log_warning(
                        f"[event_plugin] No interface registered for event {event_id} "
                        f"(attempt {attempt}/{CORRECTOR_RETRIES})"
                    )
                else:
                    from types import SimpleNamespace

                    SCHEDULED_EVENTS_CHAT_ID = -999999999
                    synthetic_message = SimpleNamespace(
                        message_id=f"scheduled_event_{event_id}",
                        chat_id=SCHEDULED_EVENTS_CHAT_ID,
                        text=f"[SCHEDULED_EVENT_{event_id}] {event.get('description', '')[:50]}",
                        from_user=SimpleNamespace(
                            id=0, username="scheduler", full_name="Scheduler"
                        ),
                        chat=SimpleNamespace(id=SCHEDULED_EVENTS_CHAT_ID, type="private"),
                    )

                    delivered = await request_llm_delivery(
                        message=synthetic_message,
                        interface=interface,
                        context=event_prompt,
                        reason=f"scheduled_event_{event_id}",
                    )

                    if delivered:
                        log_info(
                            f"[event_plugin] Event {event_id} delivered to LLM"
                        )
                        break

                if attempt < CORRECTOR_RETRIES and not delivered:
                    await asyncio.sleep(attempt)

            if not delivered:
                log_warning(
                    f"[event_plugin] Failed to deliver event {event_id} after {CORRECTOR_RETRIES} attempts"
                )
        finally:
            try:
                if event_id is not None:
                    if await mark_event_delivered(event_id):
                        log_debug(
                            f"[event_plugin] Event {event_id} marked delivered in DB"
                        )
                    else:
                        log_warning(
                            f"[event_plugin] Failed to mark event {event_id} delivered"
                        )
                else:
                    log_warning(
                        "[event_plugin] Cannot mark event with invalid id as delivered"
                    )
            except Exception as e:
                log_warning(
                    f"[event_plugin] Error marking event {event_id} delivered: {e}"
                )

    async def _create_event_prompt(self, event: dict):
        """Create a structured prompt for the event delivery."""

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
                dt_utc = datetime.fromisoformat(str(next_run_val).replace("Z", "+00:00"))
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

            injections = await gather_static_injections()
            if isinstance(injections, dict):
                context.update(injections)
        except Exception as e:
            log_warning(f"[event_plugin] Failed to gather static injections: {e}")

        log_debug(
            f"[event_plugin] Formatting event {event_id} as event_reminder for LLM"
        )

        return {
            "context": context,
            "input": {
                "type": "event_reminder",
                "payload": {
                    "date": date,
                    "time": time,
                    "repeat": event.get("recurrence_type", "none"),
                    "description": description,
                    "created_by": event.get("created_by", "synth"),
                },
                "source": {
                    "event_id": event_id,
                    "origin": "scheduler",
                },
                "timestamp": event.get("next_run")
                or datetime.utcnow().isoformat() + "+00:00",
            },
            "instructions": f"""
SCHEDULED REMINDER #{event_id} {"(LATE)" if is_late else "(ON TIME)"}

Reminder: """
            + str(description)
            + f"""
Scheduled for: {scheduled_time}
Status: {lateness_context}

This is a reminder you set for yourself. Freely decide whether and how to act:

1. If it's a reminder that requires action (e.g. "remember Jay"), decide what to do
2. If it's an internal thought, you might decide to do nothing or something else
3. If it's late, deliver it but communicate that it's late
4. You are NOT obliged to send messages - assess if it's really needed

You can respond with any action (message, etc.) or combination of actions.
If you decide to do nothing, the JSON should not contain any action.

Example of a valid JSON structure for an event:
{{
  "type": "event",
  "payload": {{
    "date": "2025-07-22",
    "time": "15:30",
    "description": "Remember to check if Jay replied to the message",
    "repeat": "none"
  }}
}}

For recurring events, you can use:
- "none": single reminder (default)
- "daily": repeat every day
- "weekly": repeat every week
- "monthly": repeat every month
- "always": keep active indefinitely
            """.strip(),
        }

    def _create_scheduler_message(self, event: dict):
        """Create a scheduler message object for the event."""
        from types import SimpleNamespace

        return SimpleNamespace(
            message_id=f"event_{event['id']}",
            chat_id="SYSTEM_SCHEDULER",
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
        chat_id: int,
        text: str,
        thread_id: int = None,
        event_id: int = None,
    ):
        """Send message directly via transport layer, bypassing interfaces."""
        try:
            # Determine the appropriate transport based on chat_id patterns
            if chat_id < 0:
                # Negative IDs are typically Telegram groups/channels
                await self._send_via_telegram_transport(
                    chat_id, text, thread_id, event_id
                )
            else:
                # Positive IDs could be Telegram private chats or other platforms
                await self._send_via_telegram_transport(
                    chat_id, text, thread_id, event_id
                )

        except Exception as e:
            log_error(
                f"[event_plugin] Error in transport layer for event {event_id}: {repr(e)}"
            )

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
            await self._fallback_send_telegram(
                chat_id, text, thread_id, event_id
            )
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

        class SilentBot:
            async def send_message(self, **kwargs):
                text = kwargs.get("text", "")
                chat_id = kwargs.get("chat_id")
                log_debug(
                    f"[event_plugin] Silent bot action: send_message({chat_id}, '{text[:50]}...')"
                )

        return SilentBot()

    async def _delegate_to_active_llm(
        self, action: dict, event_id: int, event_info: dict = None
    ):
        """Delegate the action execution to the active LLM plugin."""
        try:
            # Track the current event ID for delivery confirmation
            self._current_processing_event_id = event_id

            # Create a unified message for scheduled events to avoid chat flooding
            # This ensures all scheduled events use the same chat context
            unified_message = self._create_unified_scheduled_message(
                action, event_id, event_info
            )

            log_debug(
                f"[event_plugin] Delegating event {event_id} to active LLM via auto-response system"
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
                    dt_utc = datetime.fromisoformat(str(next_run_val).replace("Z", "+00:00"))
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
            "instructions": f"""
You can use {{"type": "event"}} to schedule a reminder in the future.

IMPORTANT RULES for event actions:
- The payload MUST contain:
    • "date": YYYY-MM-DD
    • "description": natural language reminder (not a command or action)
- The payload CAN optionally contain:
    • "time": HH:MM (default "00:00")
    • "repeat": how often to repeat the event
      - "none" (default)
      - "daily"
      - "weekly"
      - "monthly"
      - "always"
- DO NOT include nested "action", "message", or any other structure inside the event.
  The plugin will decide later how to handle the reminder.

Valid examples:

Single reminder (default):
{{
  "type": "event",
  "payload": {{
    "date": "2025-07-22",
    "time": "15:30",
    "description": "Remind Jay to check the system logs for errors"
  }}
}}

Daily recurring reminder:
{{
  "type": "event",
  "payload": {{
    "date": "2025-07-22",
    "time": "09:00",
    "description": "Daily standup meeting reminder",
    "repeat": "daily"
  }}
}}

Weekly recurring reminder:
{{
  "type": "event",
  "payload": {{
    "date": "2025-07-22",
    "time": "14:00",
    "description": "Weekly team sync",
    "repeat": "weekly"
  }}
}}
        """.strip(),
            "interface_instructions": "SCHED: Single JSON reply",
        }

    def _create_mock_bot_for_llm(self):
        """Create a mock bot that delegates LLM responses to action parser."""

        class ScheduledEventBot:
            def __init__(self, event_plugin):
                self.event_plugin = event_plugin

            async def send_message(self, **kwargs):
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
                        from core.action_parser import parse_action

                        # Create a proper message context for the action parser
                        # This ensures the action goes to the right interface
                        action_message = type(
                            "ActionMessage",
                            (),
                            {
                                "chat_id": response_action.get("payload", {}).get(
                                    "target", chat_id
                                ),
                                "thread_id": response_action.get(
                                    "payload", {}
                                ).get("thread_id", thread_id),
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
                if repeat and repeat not in ["none", "daily", "weekly", "monthly", "always"]:
                    errors.append("payload.repeat must be one of: none, daily, weekly, monthly, always")
                
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
                component_name="event"
            )
            
            # Register with validation registry
            registry = get_validation_registry()
            registry.register_component_rules("event", [rule])
            
            log_debug("[event_plugin] Registered custom validation rules with validation registry")
            
        except Exception as e:
            log_warning(f"[event_plugin] Failed to register custom validation: {e}")
    

# Export the plugin class for the loader
PLUGIN_CLASS = EventPlugin
