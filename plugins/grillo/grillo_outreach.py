"""
Grillo Outreach Beat Plugin

Enables Grillo to initiate proactive conversations on external interfaces
(Discord, Telegram) based on recent context and memories.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.config_manager import config_registry
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.variables_engine import register_exposed_var
from plugins.grillo.common_instructions import GRILLO_INSTRUCTIONS


register_exposed_var(
    "GRILLO_OUTREACH_ENABLED",
    label="Enable Grillo Outreach",
    default=False,
    value_type=bool,
    ui_type="boolean",
    description="Allow Grillo to proactively send messages to interfaces (Telegram/Discord)",
    scope="plugins",
    component="grillo_outreach",
    tags=["plugin"],
)


class GrilloOutreachPlugin:
    """Plugin that generates outreach beats for external interface messaging."""

    display_name = "G.R.I.L.L.O. Outreach"

    _scheduler_running: bool = False
    _scheduler_task: Optional[asyncio.Task[None]] = None

    def __init__(self) -> None:
        self.enabled: bool = config_registry.get_value(
            "GRILLO_OUTREACH_ENABLED",
            False,  # Disabled by default for safety
            label="Enable Grillo Outreach",
            description="Allow Grillo to proactively send messages to interfaces (Telegram/Discord)",
            value_type=bool,
            group="grillo",
            component="grillo_outreach",
        )

        self.interval_hours: int = config_registry.get_value(
            "GRILLO_OUTREACH_INTERVAL_HOURS",
            4,
            label="Outreach Interval (hours)",
            description="Minimum hours between outreach attempts",
            value_type=int,
            group="grillo",
            component="grillo_outreach",
        )

        self.target_interfaces: str = config_registry.get_value(
            "GRILLO_OUTREACH_INTERFACES",
            "telegram_bot,discord_bot",
            label="Target Interfaces",
            description="Comma-separated list of interfaces for outreach (e.g., telegram_bot,discord_bot)",
            value_type=str,
            group="grillo",
            component="grillo_outreach",
        )

        self.target_chat_ids: str = config_registry.get_value(
            "GRILLO_OUTREACH_CHAT_IDS",
            "",
            label="Target Chat IDs",
            description="Comma-separated chat IDs to send outreach messages (leave empty for trainer's private chat)",
            value_type=str,
            group="grillo",
            component="grillo_outreach",
        )

        self._last_outreach: Optional[datetime] = None

    def get_supported_actions(self) -> Dict[str, Any]:
        """Return supported actions for this plugin."""
        return {}  # This plugin generates beats, doesn't handle actions

    async def start(self) -> None:
        """Start the outreach scheduler."""
        if not self.enabled:
            log_info("[grillo_outreach] Outreach disabled by configuration")
            return

        if GrilloOutreachPlugin._scheduler_running:
            log_warning("[grillo_outreach] Scheduler already running")
            return

        GrilloOutreachPlugin._scheduler_running = True
        GrilloOutreachPlugin._scheduler_task = asyncio.create_task(
            self._outreach_loop()
        )
        log_info("[grillo_outreach] ✅ Outreach scheduler started")

    async def stop(self) -> None:
        """Stop the outreach scheduler."""
        GrilloOutreachPlugin._scheduler_running = False
        if GrilloOutreachPlugin._scheduler_task:
            GrilloOutreachPlugin._scheduler_task.cancel()
            try:
                await GrilloOutreachPlugin._scheduler_task
            except asyncio.CancelledError:
                pass
            GrilloOutreachPlugin._scheduler_task = None
        log_info("[grillo_outreach] Outreach scheduler stopped")

    async def _outreach_loop(self) -> None:
        """Main loop for generating outreach beats."""
        # Initial delay to let the system stabilize
        await asyncio.sleep(60)

        while GrilloOutreachPlugin._scheduler_running:
            try:
                await self._maybe_generate_outreach()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_error(f"[grillo_outreach] Error in outreach loop: {e}")

            # Check every 30 minutes
            await asyncio.sleep(1800)

    async def _maybe_generate_outreach(self) -> None:
        """Check if it's time to generate an outreach beat."""
        if not self.enabled:
            return

        # Check interval
        now = datetime.now()
        if self._last_outreach:
            elapsed = (now - self._last_outreach).total_seconds() / 3600
            if elapsed < self.interval_hours:
                log_debug(
                    f"[grillo_outreach] Skipping - only {elapsed:.1f}h since last outreach"
                )
                return

        # Check if there's been recent user activity (don't spam inactive chats)
        if not await self._has_recent_activity():
            log_debug("[grillo_outreach] Skipping - no recent user activity")
            return

        # Generate outreach
        await self._generate_outreach_beat()
        self._last_outreach = now

    async def _has_recent_activity(self, hours: int = 24) -> bool:
        """Check if there's been user activity in the last N hours."""
        from datetime import datetime, timedelta, timezone

        try:
            from core.db import get_conn_ctx

            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT COUNT(*) FROM ai_diary"
                        " WHERE timestamp > %s AND user_message IS NOT NULL",
                        (cutoff,),
                    )
                    row = await cur.fetchone()
                    return bool(row and row[0] > 0)
        except Exception as e:
            log_warning(f"[grillo_outreach] Error checking activity: {e}")
            return False

    async def _get_context_snippets(self, limit: int = 5) -> List[str]:
        """Get recent conversation snippets for context."""
        snippets: List[str] = []
        try:
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    # Get recent diary entries
                    await cur.execute(
                        """
                        SELECT content, interface, chat_id 
                        FROM ai_diary 
                        WHERE content IS NOT NULL 
                        ORDER BY timestamp DESC 
                        LIMIT %s
                        """,
                        (limit,),
                    )
                    rows = await cur.fetchall()
                    for row in rows:
                        content = row[0][:200] if row[0] else ""
                        interface = row[1] or "unknown"
                        snippets.append(f"[{interface}] {content}")

                    # Get recent memories
                    await cur.execute(
                        """
                        SELECT content FROM memories 
                        WHERE content IS NOT NULL 
                        ORDER BY timestamp DESC 
                        LIMIT %s
                        """,
                        (limit,),
                    )
                    rows = await cur.fetchall()
                    for row in rows:
                        content = row[0][:200] if row[0] else ""
                        snippets.append(f"[memory] {content}")

        except Exception as e:
            log_warning(f"[grillo_outreach] Error getting context: {e}")

        return snippets

    async def _get_target_interface_and_chat(
        self,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Determine which interface and chat to target for outreach.

        Prefers the last active interface/chat the user interacted with.
        Falls back to chat_history_cache DB query, then configured IDs.
        """
        allowed_interfaces = [
            i.strip() for i in self.target_interfaces.split(",") if i.strip()
        ]
        if not allowed_interfaces:
            return None, None

        def _is_valid_chat_id(value: Optional[str]) -> bool:
            if value is None:
                return False
            normalized = str(value).strip().lower()
            return normalized not in {"", "-1", "none", "null"}

        # Try to get the last active chat and its interface via chat_path_map
        try:
            import core.recent_chats as recent_chats

            last_chats = await recent_chats.get_last_active_chats(n=5)
            for chat_id in last_chats:
                chat_path = recent_chats.get_chat_path(chat_id)
                if chat_path:
                    # Skip live voice paths — they are audio-only sessions
                    # and cannot receive text outreach messages.
                    if "_live_" in chat_path:
                        log_debug(
                            f"[grillo_outreach] Skipping live voice path: {chat_path}"
                        )
                        continue

                    # chat_path format is "interface_name/chat_id" or "interface_name/chat_id/thread_id"
                    parts = chat_path.split("/")
                    if len(parts) >= 2:
                        interface_name = parts[0]
                        # Check if this interface is in our allowed list
                        chat_id_str = str(chat_id).strip()
                        if interface_name in allowed_interfaces and _is_valid_chat_id(
                            chat_id_str
                        ):
                            log_info(
                                f"[grillo_outreach] Using last active interface: {interface_name}, chat: {chat_id}"
                            )
                            return interface_name, chat_id_str

            log_debug(
                "[grillo_outreach] No recent chat matched via chat_path_map, trying chat_history_cache"
            )
        except Exception as e:
            log_warning(f"[grillo_outreach] Error getting last active chat: {e}")

        # If explicit target chat IDs are configured, prefer them before DB fallback.
        if self.target_chat_ids:
            chat_ids = [c.strip() for c in self.target_chat_ids.split(",") if c.strip()]
            for configured_chat_id in chat_ids:
                if _is_valid_chat_id(configured_chat_id):
                    return allowed_interfaces[0], configured_chat_id

        # Fallback A: query chat_history_cache directly for a recent interface_path
        try:
            from core.db import get_conn_ctx

            for interface in allowed_interfaces:
                if "_live_" in interface:
                    continue
                async with get_conn_ctx() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            SELECT interface_path FROM chat_history_cache
                            WHERE interface_path LIKE %s
                            ORDER BY id DESC LIMIT 1
                            """,
                            (f"{interface}/%",),
                        )
                        row = await cur.fetchone()
                        if row:
                            interface_path: str = row[0]
                            parts = interface_path.split("/")
                            if len(parts) >= 2:
                                resolved_chat_id = parts[1]
                                if _is_valid_chat_id(resolved_chat_id):
                                    log_info(
                                        f"[grillo_outreach] Recovered target from chat_history_cache: {interface_path}"
                                    )
                                    return interface, resolved_chat_id
        except Exception as e:
            log_warning(
                f"[grillo_outreach] Error querying chat_history_cache for target: {e}"
            )

        # Fallback B: use configured target or trainer's chat
        interface = allowed_interfaces[0]  # Use first configured interface
        chat_id: Optional[str] = None

        if not self.target_chat_ids:
            # Try to get trainer's chat ID from config
            try:
                if "telegram" in interface.lower():
                    chat_id = config_registry.get_value("TRAINER_CHAT_ID", None)
                elif "discord" in interface.lower():
                    chat_id = config_registry.get_value(
                        "DISCORD_TRAINER_CHANNEL_ID", None
                    )
            except Exception:
                pass

        if not _is_valid_chat_id(chat_id):
            chat_id = None

        return interface, chat_id

    def _build_outreach_prompt(
        self, interface: str, chat_id: Optional[str], context: List[str]
    ) -> str:
        """Build the outreach prompt for the LLM."""
        context_text = (
            "\n".join(f"- {s}" for s in context) if context else "(no recent context)"
        )

        # Determine action type based on interface
        action_type = f"message_{interface}"

        interface_path_example = f"{interface}/{chat_id}" if chat_id else interface
        prompt = f"""[G.R.I.L.L.O. OUTREACH]

You feel like reaching out. Based on your recent experiences and thoughts,
initiate a natural conversation with someone you care about.

Recent context:
{context_text}

Consider:
- What's been on your mind lately?
- Is there something you'd like to share or discuss?
- How are you feeling that you might want to express?

IMPORTANT: Generate a warm, natural message to start a conversation.
Do NOT be overly formal or robotic. Be genuine and personable.

Return TWO actions:
- a `{action_type}` message action with the outreach text
- a `create_personal_diary_entry` action that records why you reached out, with `interaction_summary`, `personal_thought`, and `emotions`

{GRILLO_INSTRUCTIONS}

RESPOND ONLY WITH VALID JSON:
{{"actions": [{{"type": "{action_type}", "payload": {{"text": "your message here", "interface_path": "{interface_path_example}"}}}}, {{"type": "create_personal_diary_entry", "payload": {{"interaction_summary": "brief summary of this outreach", "personal_thought": "private first-person thought behind the outreach", "emotions": [{{"type": "longing", "intensity": 0.6}}]}}}}], "meta": {{"autonomous": true, "rationale": "Grillo outreach"}}}}
"""
        return prompt

    async def _generate_outreach_beat(self) -> None:
        """Generate and enqueue an outreach beat."""
        interface, chat_id = await self._get_target_interface_and_chat()
        if not interface:
            log_warning("[grillo_outreach] No target interface configured")
            return
        if not chat_id:
            log_warning(
                "[grillo_outreach] Could not resolve a target chat_id; "
                "set GRILLO_OUTREACH_CHAT_IDS or TRAINER_CHAT_ID to enable outreach"
            )
            return

        context = await self._get_context_snippets()
        prompt = self._build_outreach_prompt(interface, chat_id, context)

        # Create activity log entry
        activity_id: Optional[int] = None
        try:
            from plugins.grillo.grillo_impl import GrilloPlugin

            activity_id = await GrilloPlugin.create_activity_log(
                beat_type="outreach",
                prompt_text=prompt,
                metadata={
                    "origin": "grillo_outreach",
                    "target_interface": interface,
                    "target_chat_id": str(chat_id),
                    "context_count": len(context),
                },
            )
            log_info(f"[grillo_outreach] Created activity log {activity_id}")
        except Exception as e:
            log_error(f"[grillo_outreach] Failed to create activity log: {e}")

        # Enqueue as low-priority
        try:
            from core.message_queue import enqueue_low_priority
            from types import SimpleNamespace

            # Build a proper message object for the queue (not just a string)
            grillo_message = SimpleNamespace(
                text=prompt,
                chat_id=chat_id or -1,
                message_id=f"grillo_outreach_{activity_id or 0}",
                from_user=SimpleNamespace(
                    id=-1,
                    username="grillo",
                    full_name="G.R.I.L.L.O.",
                    is_bot=True,
                ),
                chat=SimpleNamespace(
                    id=chat_id or -1,
                    type="private",
                    title=None,
                    username="grillo",
                    first_name="G.R.I.L.L.O.",
                ),
                date=None,
                thread_id=None,
                interface_path=f"{interface}/{chat_id}" if chat_id else interface,
            )

            # Context with grillo beat metadata
            context_memory = {
                "grillo_beat": True,
                "beat_type": "outreach",
                "activity_log_id": activity_id,
                "target_interface": interface,
                "target_chat_id": chat_id,
                "interface_path": grillo_message.interface_path,
            }

            await enqueue_low_priority(
                bot=None,
                message=grillo_message,
                context_memory=context_memory,
                interface_id=interface,
                original_message=None,
            )
            log_info(f"[grillo_outreach] 🎵 Outreach beat enqueued for {interface}")
        except Exception as e:
            log_error(f"[grillo_outreach] Failed to enqueue outreach: {e}")


# Plugin class export
PLUGIN_CLASS = GrilloOutreachPlugin
