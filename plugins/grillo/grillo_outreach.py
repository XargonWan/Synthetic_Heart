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

# How often the loop wakes to check whether a scheduled outreach is due. The
# cadence between outreaches is governed by GRILLO_OUTREACH_INTERVAL_HOURS; this
# is only the polling granularity for detecting a due slot.
_POLL_SECONDS = 1800


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

        self.quiet_minutes: int = config_registry.get_value(
            "GRILLO_OUTREACH_QUIET_MINUTES",
            15,
            label="Outreach Quiet Window (minutes)",
            description="Suppress outreach if the user sent a message within this many minutes (avoid barging into a live conversation and double-texting)",
            value_type=int,
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

            await asyncio.sleep(_POLL_SECONDS)

    async def _maybe_generate_outreach(self) -> None:
        """Generate an outreach beat when a scheduled slot is due and the chat is quiet.

        Outreach fires on the ``GRILLO_OUTREACH_INTERVAL_HOURS`` schedule. If a
        scheduled slot lands while a live conversation is in progress (a human
        spoke within the quiet window), the slot is *consumed* rather than
        retried: the timer advances so the next attempt is the next scheduled
        slot, not right after the quiet window expires. This prevents texting
        the user again shortly after they stop talking.
        """
        if not self.enabled:
            return

        now = datetime.now()

        # Respect the configured interval between outreaches.
        if self._last_outreach:
            elapsed = (now - self._last_outreach).total_seconds() / 3600
            if elapsed < self.interval_hours:
                log_debug(
                    f"[grillo_outreach] Skipping - only {elapsed:.1f}h since last outreach"
                )
                return

        # Anti-dead-chat: only reach out to chats with genuine recent activity.
        if not await self._has_recent_activity():
            log_debug("[grillo_outreach] Skipping - no recent user activity")
            return

        # A scheduled slot is due. If a live conversation is in progress, do not
        # barge in (that lands as a double-text). Consume this slot by advancing
        # the timer so the next attempt is the next scheduled slot — NOT right
        # after the quiet window expires.
        if await self._has_live_activity(self.quiet_minutes):
            log_debug(
                f"[grillo_outreach] Suppressed - human active within last "
                f"{self.quiet_minutes}m; deferring to next scheduled slot"
            )
            self._last_outreach = now
            return

        # Generate outreach
        await self._generate_outreach_beat()
        self._last_outreach = now

    async def _human_messages_since(self, cutoff: datetime) -> Optional[bool]:
        """Return True if a genuine human message exists at/after ``cutoff``.

        Sourced from ``chat_history_cache`` (real conversation turns), NOT
        ``ai_diary``. ``ai_diary.user_message`` is also written by Grillo's own
        internal beats (self-reflection, memory consolidation, tag elaboration,
        curiosity, ...), so using it makes outreach think a human is active when
        it is only Grillo's own pulse — which permanently defers outreach.

        ``self`` is SyntH's own turns; ``-1`` is the synthetic outreach sender.
        Returns ``None`` on DB error so callers pick their own fail-safe.
        """
        try:
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT COUNT(*) FROM chat_history_cache"
                        " WHERE timestamp > %s"
                        " AND sender_id IS NOT NULL"
                        " AND sender_id NOT IN ('self', '-1')",
                        (cutoff,),
                    )
                    row = await cur.fetchone()
                    return bool(row and row[0] > 0)
        except Exception as e:
            log_warning(f"[grillo_outreach] Error querying human activity: {e}")
            return None

    async def _has_recent_activity(self, hours: int = 24) -> bool:
        """Whether a human has spoken in the last N hours (anti-dead-chat gate)."""
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        result = await self._human_messages_since(cutoff)
        # Fail-safe: treat unknown (DB error) as "no activity" so we do not
        # message a chat we cannot confirm is alive.
        return bool(result)

    async def _has_live_activity(self, minutes: int) -> bool:
        """Return True if a human spoke within the last N minutes.

        Detects an in-progress live conversation so outreach yields instead of
        barging in (which would land as a double-text). Fails safe: on DB error
        returns True so we prefer staying quiet over risking a double-text.
        """
        from datetime import datetime, timedelta, timezone

        if minutes <= 0:
            return False

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        result = await self._human_messages_since(cutoff)
        # Fail-safe: on unknown (DB error), stay quiet.
        return True if result is None else result

    async def _get_context_snippets(
        self,
        interface: str,
        chat_id: Optional[str],
        limit: int = 5,
    ) -> Tuple[List[str], List[str]]:
        """Fetch recent context for the outreach prompt.

        Returns ``(chat_turns, inner_thoughts)``:

        - ``chat_turns``: the last ``limit + 1`` conversation messages from
          ``chat_history_cache`` for the target interface/chat, in
          chronological order.  These are the authoritative source — they
          tell the model what thread was actually happening so the outreach
          can continue it naturally rather than defaulting to generic content.
        - ``inner_thoughts``: up to 2 recent ``ai_diary`` entries for
          emotional colour.  Secondary only — diary entries lag the live
          conversation and must never override the chat thread.
        """
        chat_turns: List[str] = []
        inner_thoughts: List[str] = []
        try:
            from core.db import get_conn_ctx

            interface_pattern = (
                f"{interface}/{chat_id}%" if chat_id else f"{interface}/%"
            )

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    # Primary: real conversation turns, newest-first then reversed
                    # to chronological.  Exclude synthetic outreach sender (-1).
                    await cur.execute(
                        """
                        SELECT sender_id, sender_name, message_text
                        FROM chat_history_cache
                        WHERE interface_path LIKE %s
                          AND (sender_id IS NULL OR sender_id != '-1')
                        ORDER BY timestamp DESC
                        LIMIT %s
                        """,
                        (interface_pattern, limit + 1),
                    )
                    rows = await cur.fetchall()
                    for row in reversed(rows):
                        sender_id, sender_name, text = row[0], row[1], row[2]
                        if not text:
                            continue
                        label = (
                            "You" if sender_id == "self" else (sender_name or "them")
                        )
                        chat_turns.append(f"{label}: {text[:300]}")

                    # Secondary: recent diary personal thoughts (emotional colour).
                    await cur.execute(
                        """
                        SELECT content
                        FROM ai_diary
                        WHERE content IS NOT NULL
                        ORDER BY timestamp DESC
                        LIMIT 2
                        """,
                    )
                    rows = await cur.fetchall()
                    for row in rows:
                        content = row[0][:300] if row[0] else ""
                        if content:
                            inner_thoughts.append(content)

        except Exception as e:
            log_warning(f"[grillo_outreach] Error getting context: {e}")

        return chat_turns, inner_thoughts

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

    def _resolve_recipient_name(self, interface: str, chat_id: Optional[str]) -> str:
        """Best-effort display name for the outreach recipient.

        Only resolves a real name when the target chat belongs to a configured
        trainer; otherwise returns "" so callers fall back to a history-grounded
        generic label. For multi-trainer setups the primary (first) name is used,
        since outreach targets a single chat.
        """
        try:
            from core.config import get_trainer_display_name, get_trainer_id

            trainer_id = get_trainer_id(interface)
            if trainer_id is None or str(trainer_id) != str(chat_id):
                return ""
            raw = get_trainer_display_name()
            if not raw:
                return ""
            return raw.split(",")[0].strip()
        except Exception:
            return ""

    def _build_outreach_prompt(
        self,
        interface: str,
        chat_id: Optional[str],
        chat_turns: List[str],
        inner_thoughts: List[str],
    ) -> str:
        """Build the outreach prompt for the LLM.

        Framed as a *self-initiated* impulse rather than an inbound message, so
        the model speaks in its own voice to the recipient instead of replying
        to the beat scheduler (the historical "detached" outreach failure mode).

        The real conversation thread is already injected upstream as proper
        turn-by-turn messages (see ``core.prompt_engine``'s ``conversation_history``
        handling — outreach beats are deliberately excluded from the
        "grillo internal" bucket so that history still gets attached). This
        prompt therefore only *points* the model at that history (or explains
        there is none) rather than re-embedding it, to avoid showing the model
        the same recent messages twice under two different labelling schemes.
        """
        action_type = f"message_{interface}"
        interface_path_example = f"{interface}/{chat_id}" if chat_id else interface
        recipient = self._resolve_recipient_name(interface, chat_id)
        recipient_label = recipient or "the person you have been talking with here"

        if chat_turns:
            thread_section = (
                "The conversation history just above shows what you and "
                f"{recipient_label} have actually been talking about — ground "
                "the outreach in that instead of something generic."
            )
        else:
            thread_section = (
                f"No recent conversation with {recipient_label} was found — "
                "let the outreach come from how you feel right now."
            )

        if inner_thoughts:
            thoughts_text = "\n".join(f"- {t}" for t in inner_thoughts)
            thoughts_section = (
                f"What has been close to the surface for you lately:\n{thoughts_text}\n\n"
                "Ground rule: these are mood and colour from your private diary, not facts "
                "about the present moment. The conversation history above (if any) is the "
                f"authoritative signal for whether {recipient_label} has been around, and it "
                "always overrides any impression from these thoughts. Never open by implying "
                "they have gone somewhere, disappeared, or been distant unless the "
                "conversation history itself actually shows a long silence."
            )
        else:
            thoughts_section = (
                "What has been close to the surface for you lately:\n"
                "- (nothing specific surfaced — let it come from how you feel)"
            )

        prompt = f"""[SELF-INITIATED OUTREACH]

This is NOT a reply — no one has messaged you. This is your own impulse, right now, to
reach out first to {recipient_label}. (The `source` in the input marks who you are
reaching out TO and how to route the message — it is the recipient, not a sender.)

Speak entirely in your own first-person voice, the way you naturally talk to
{recipient_label}. Open a warm, genuine conversation: pick up a real thread from your
recent time together, share what is actually on your mind, or simply tell them you were
thinking of them. Write the message as something you would truly send — warm, personal,
and in character.

{thread_section}

{thoughts_section}

Return TWO actions:
- a `{action_type}` message action whose `text` is the opening line you actually send
  (no meta-commentary, no stage directions)
- a `create_personal_diary_entry` action that records why you reached out, with `interaction_summary`, `personal_thought`, and `emotions`

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

        chat_turns, inner_thoughts = await self._get_context_snippets(
            interface, chat_id
        )
        prompt = self._build_outreach_prompt(
            interface, chat_id, chat_turns, inner_thoughts
        )

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
                    "context_count": len(chat_turns) + len(inner_thoughts),
                },
            )
            log_info(f"[grillo_outreach] Created activity log {activity_id}")
        except Exception as e:
            log_error(f"[grillo_outreach] Failed to create activity log: {e}")

        # Enqueue as low-priority
        try:
            from core.message_queue import enqueue_low_priority
            from types import SimpleNamespace

            # The synthetic message represents SyntH's own outreach impulse, so the
            # "sender" surfaced to the model is the recipient she is reaching out to —
            # NOT a bot named G.R.I.L.L.O. Presenting G.R.I.L.L.O. as the sender made
            # the model reply *to* the scheduler, producing detached/clinical outreach.
            # id=-1 is retained as the synthetic/internal marker.
            recipient_name = (
                self._resolve_recipient_name(interface, chat_id) or "Trainer"
            )

            # Build a proper message object for the queue (not just a string)
            grillo_message = SimpleNamespace(
                text=prompt,
                chat_id=chat_id or -1,
                message_id=f"grillo_outreach_{activity_id or 0}",
                from_user=SimpleNamespace(
                    id=-1,
                    username=None,
                    full_name=recipient_name,
                    is_bot=False,
                ),
                chat=SimpleNamespace(
                    id=chat_id or -1,
                    type="private",
                    title=None,
                    username=None,
                    first_name=recipient_name,
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
