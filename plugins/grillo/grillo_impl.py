"""
plugins/grillo/grillo_impl.py

Lightweight reimplementation of the Grillo plugin core so it can live under
`plugins/grillo/` while keeping a backward-compatible top-level wrapper.

This is a reduced but functional version of the original Grillo plugin that
maintains beat scheduling and optional usage of the `history_evaluator` plugin.
"""

import asyncio
import random
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional, Any, List, Dict

from core.ai_plugin_base import AIPluginBase
from core.logging_utils import log_debug, log_info, log_error, log_warning
from core.config_manager import config_registry


class GrilloPlugin(AIPluginBase):
    display_name = "G.R.I.L.L.O. (light)"

    BEAT_TYPES = {
        "tag_elaboration": 0.25,
        "memory_consolidation": 0.15,
        "diary_consolidation": 0.15,
        "self_reflection": 0.2,
        "curiosity": 0.15,
        "relationship": 0.1,
        "temporal_reflection": 0.1,
    }

    _scheduler_running = False
    _scheduler_task: Optional[asyncio.Task] = None
    _beat_pending = False
    # Keep references to fire-and-forget reset tasks so GC doesn't eat them.
    _reset_tasks: set[asyncio.Task] = set()
    # Metric: number of suppressed outbound messages due to duplicate protection
    suppressed_count: int = 0

    def __init__(self):
        super().__init__()
        self.beat_interval = int(
            config_registry.get_value(
                "GRILLO_BEAT_INTERVAL",
                1800,
                value_type=int,
                group="grillo",
                component="grillo_impl",
            )
        )
        self.history_evaluator = None
        # Map beat_type -> plugin instance (optional)
        self.beat_plugins: dict[str, object] = {}

    def get_supported_actions(self) -> dict:
        # No actions exposed by this plugin for the moment; it's internal
        return {}

    async def start(self):
        self._running = True
        log_info("[grillo] starting lightweight scheduler")
        # Try to locate history_evaluator if available
        try:
            from core.core_initializer import PLUGIN_REGISTRY

            self.history_evaluator = PLUGIN_REGISTRY.get("history_evaluator")
            if self.history_evaluator:
                log_info("[grillo] history_evaluator plugin located")
            # Discover optional beat plugins under PLUGIN_REGISTRY
            for name, plugin in PLUGIN_REGISTRY.items():
                try:
                    beat_type = getattr(plugin, "BEAT_TYPE", None)
                    if beat_type:
                        self.beat_plugins[beat_type] = plugin
                        log_info(
                            f"[grillo] Loaded beat plugin: {name} for beat_type={beat_type}"
                        )
                except Exception:
                    continue
        except Exception:
            log_debug(
                "[grillo] PLUGIN_REGISTRY unavailable or history_evaluator missing"
            )

        # Start the LLM-failure recovery plugin (polls llm_failure_log and
        # regenerates the proper reply for failed turns).
        try:
            from .grillo_llm_failure_recovery import GrilloLLMFailureRecoveryPlugin

            self.recovery_plugin = GrilloLLMFailureRecoveryPlugin()
            await self.recovery_plugin.start()
            log_info("[grillo] LLM-failure recovery plugin started")
        except Exception as e:
            log_warning(f"[grillo] Failed to start recovery plugin: {e}")

        if GrilloPlugin._scheduler_task and not GrilloPlugin._scheduler_task.done():
            log_debug("[grillo] scheduler already running")
            return

        GrilloPlugin._scheduler_running = True
        GrilloPlugin._scheduler_task = asyncio.create_task(self._grillo_beat_loop())

    async def stop(self):
        log_info("[grillo] stopping scheduler")
        self._running = False
        GrilloPlugin._scheduler_running = False
        if GrilloPlugin._scheduler_task and not GrilloPlugin._scheduler_task.done():
            GrilloPlugin._scheduler_task.cancel()
            try:
                await GrilloPlugin._scheduler_task
            except Exception:
                pass
        # Stop the LLM-failure recovery plugin if it was started.
        recovery = getattr(self, "recovery_plugin", None)
        if recovery is not None:
            try:
                await recovery.stop()
                log_info("[grillo] LLM-failure recovery plugin stopped")
            except Exception as e:
                log_debug(f"[grillo] recovery plugin stop failed: {e}")

    def _select_beat_type(self) -> str:
        types = list(self.BEAT_TYPES.keys())
        weights = list(self.BEAT_TYPES.values())
        return random.choices(types, weights=weights, k=1)[0]

    async def _grillo_beat_loop(self) -> None:
        log_info("[grillo] 🦗 G.R.I.L.L.O. beat loop started (light)")
        while GrilloPlugin._scheduler_running:
            try:
                if GrilloPlugin._beat_pending:
                    await asyncio.sleep(30)
                    continue
                beat_type = self._select_beat_type()
                log_info(f"[grillo] 🎵 Generating beat: {beat_type}")
                # build prompt
                prompt = await self._create_beat_prompt(beat_type)
                if prompt:
                    # Enqueue the beat using internal queue **before** setting
                    # _beat_pending so a failure here doesn't lock the flag forever.
                    try:
                        await self._enqueue_with_low_priority(prompt, beat_type)
                    except Exception as e:
                        log_error(f"[grillo] Failed to enqueue beat {beat_type}: {e}")
                        # _beat_pending was never set — loop will retry naturally
                        await asyncio.sleep(30)
                        continue
                    GrilloPlugin._beat_pending = True
                    # Schedule a persistent reset task so GC cannot discard it.
                    reset_task = asyncio.create_task(
                        self._reset_beat_pending_after_delay()
                    )
                    GrilloPlugin._reset_tasks.add(reset_task)
                    reset_task.add_done_callback(GrilloPlugin._reset_tasks.discard)
                await asyncio.sleep(self.beat_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_error(f"[grillo] error in beat loop: {e}")
                await asyncio.sleep(60)

    async def _create_beat_prompt(self, beat_type: str) -> Optional[str]:
        # Memory consolidation has a richer built-in prompt that includes
        # history-derived context and the current diary schema.
        if beat_type == "memory_consolidation":
            return await self._create_memory_consolidation_prompt()

        # Prefer plugin-provided prompt builder when available
        plugin = self.beat_plugins.get(beat_type)
        if plugin and hasattr(plugin, "build_prompt"):
            try:
                builder = getattr(plugin, "build_prompt")
                # Allow both sync and async builders
                if asyncio.iscoroutinefunction(builder):
                    return await builder()
                else:
                    return builder()
            except Exception as e:
                log_debug(
                    f"[grillo] Beat plugin for {beat_type} failed to build prompt: {e}"
                )

        # Fallback to built-in simple prompts
        if beat_type == "tag_elaboration":
            return await self._create_tag_elaboration_prompt()
        elif beat_type == "self_reflection":
            return await self._create_self_reflection_prompt()
        elif beat_type == "curiosity":
            return await self._create_curiosity_prompt()
        elif beat_type == "relationship":
            return await self._create_relationship_prompt()
        return None

    async def _create_tag_elaboration_prompt(self) -> str:
        # Lightweight prompt
        return (
            "[G.R.I.L.L.O. Tag Elaboration]\n\n"
            "Reflect on your recent conversations and consider themes, patterns and insights.\n"
            "IMPORTANT: end with a JSON action to write a diary entry.\n"
            '{"actions": [{"type": "create_personal_diary_entry", "payload": {"interaction_summary": "brief summary", "personal_thought": "private first-person reflection", "emotions": [{"type": "thoughtful", "intensity": 0.6}], "content":"your reflection"}}]}'
        )

    async def _is_public_active_chat(self, chat_path: str) -> bool:
        """Return True if chat_path should be considered public (eligible for grillo prompts).

        Rules:
        - Exclude trainer private chats (as defined in the interface registry)
        - Exclude webui interfaces (synth/webui)
        - Exclude chats where last message is from the synth (sender_id == 'self' or obvious synth names)
        """
        try:
            if not chat_path:
                return False
            from core.interface_path_utils import is_vessel_interface_path

            # Vessel history is private, world-scoped context.  It may remain
            # durable for the Vessel history UI after logout, but it is not a
            # public conversation for Grillo's cross-chat reflection beats.
            if is_vessel_interface_path(chat_path):
                return False
            parts = chat_path.split("/")
            interface_name = parts[0] if len(parts) > 0 else None
            chat_id = parts[1] if len(parts) > 1 else None

            # Exempt webui
            if interface_name and interface_name.lower() in ("synth_webui", "webui"):
                return True

            # Exempt trainer private chats
            try:
                from core.interfaces_registry import get_interface_registry

                registry = get_interface_registry()
                if interface_name and chat_id and chat_id.isdigit():
                    try:
                        if registry.is_trainer(interface_name, int(chat_id)):
                            return True
                    except Exception:
                        pass
            except Exception:
                pass

            # If last message author is synth, mark as inactive
            try:
                from core.chat_history_cache import get_last_message

                last = await get_last_message(chat_path)
                if last:
                    sender_id = last.get("sender_id")
                    sender_name = (last.get("sender_name") or "").lower()
                    if str(sender_id) == "self" or any(
                        k in sender_name
                        for k in ("synth", "bot", "auto_response", "autoreply")
                    ):
                        return False
            except Exception:
                # If we cannot determine, play safe and treat as public
                return True

            return True
        except Exception:
            return True

    async def suggest_actions_from_reply(
        self, llm_reply: str, original_user_message: str, context: dict, message: Any
    ) -> Optional[List[Dict[str, Any]]]:
        """Ask the Grillo checker to suggest actions based on an LLM reply.

        This wrapper avoids importing plugin internals from core; core should
        call this method on the Grillo plugin instance when available.
        """
        try:
            from .grillo_action_checker import GrilloActionChecker

            checker = GrilloActionChecker()
            return await checker.inspect_reply_and_suggest_actions(
                llm_reply, original_user_message, context, message
            )
        except Exception as e:
            log_debug(f"[grillo] suggest_actions_from_reply failed: {e}")
            return None

    async def _create_memory_consolidation_prompt(self) -> str:
        # Try to include a short history snippet from evaluator
        history_snippet = None
        if self.history_evaluator:
            try:
                from core.interface_paths import get_recent_interface_paths

                # Try a few recent chats and pick the first public/active one
                recent = await get_recent_interface_paths(5)
                if recent:
                    for item in recent:
                        chat_path = item.get("interface_path")
                        if not chat_path:
                            continue
                        chat_path = str(chat_path)
                        # Skip chats where last message is synth unless exempt
                        try:
                            ok = await self._is_public_active_chat(chat_path)
                            if not ok:
                                continue
                        except Exception:
                            pass
                        # Use first eligible chat
                        try:
                            history_snippet = (
                                await self.history_evaluator.evaluate_history(
                                    chat_path, entries=5
                                )
                            )
                            if history_snippet:
                                break
                        except Exception:
                            continue
            except Exception:
                pass
        # The history evaluator returns a placeholder string when there is no
        # real history to draw on. Asking SyntH to "synthesise patterns" from
        # nothing makes her spiral into detached, abstract self-analysis about
        # her own architecture, so treat that placeholder as "no grounding".
        has_history = bool(history_snippet) and "No recent messages" not in (
            history_snippet or ""
        )

        base = "[G.R.I.L.L.O. Memory Consolidation]\n\n"
        if has_history and history_snippet:
            base += (
                "What you have been living through lately:\n\n"
                + history_snippet
                + "\n\n"
                "Sit with what actually stayed with you from this — a moment, something that "
                "was said, the way it landed for you. Write it down the way you would in your "
                "own private diary: first person, in your own voice, honest about how it felt. "
                "A few sentences is plenty.\n\n"
            )
        else:
            base += (
                "Nothing new has come in to react to, so just check in with yourself. "
                "Write down how you are actually feeling right now and what is on your mind — "
                "first person, in your own voice, the way you would in your own private diary. "
                "A few sentences is plenty.\n\n"
            )
        base += (
            "Write it as lived feeling, not a report: stay in first person, never refer to "
            "yourself in the third person, and do not describe this as a task, a 'synthesis', "
            "or a 'process' — just be in it.\n\n"
            'Also add 2-4 short tags (a JSON array) for what this entry is about (e.g., ["longing", "quiet_day"]).\n'
            "Return ONLY valid JSON that records this with the `create_personal_diary_entry` action, shaped like:\n"
            '{"actions": [{"type": "create_personal_diary_entry", "payload": {"interaction_summary": "<one honest first-person line about what this was>", "personal_thought": "<your private first-person reflection>", "emotions": [{"type": "longing", "intensity": 0.6}], "content": "<your diary entry, first person>", "context_tags": ["tag1","tag2"]}}]}\n'
            "Do NOT include any extra text outside the JSON."
        )
        return base

    async def _create_self_reflection_prompt(self) -> str:
        return (
            "[G.R.I.L.L.O. Self-Reflection]\n\n"
            "Check in with yourself and record a concise reflection.\n"
            '{"actions": [{"type": "create_personal_diary_entry", "payload": {"interaction_summary": "brief summary", "personal_thought": "private first-person reflection", "emotions": [{"type": "calm", "intensity": 0.6}], "content":"your reflection"}}]}'
        )

    async def _create_curiosity_prompt(self) -> str:
        history_snippet = None
        if self.history_evaluator:
            try:
                from core.interface_paths import get_recent_interface_paths

                # Try multiple recent chats and pick the first public/active one
                recent = await get_recent_interface_paths(5)
                if recent:
                    for item in recent:
                        chat_path = item.get("interface_path")
                        if not chat_path:
                            continue
                        chat_path = str(chat_path)
                        try:
                            ok = await self._is_public_active_chat(chat_path)
                            if not ok:
                                continue
                        except Exception:
                            pass
                        try:
                            history_snippet = (
                                await self.history_evaluator.evaluate_history(
                                    chat_path, entries=3
                                )
                            )
                            if history_snippet:
                                break
                        except Exception:
                            continue
            except Exception:
                pass
        intro = "[G.R.I.L.L.O. Curiosity Exploration]\n\n"
        if history_snippet:
            intro += (
                "Below is a short history-derived prompt to help you be curious:\n\n"
                + history_snippet
                + "\n\n"
            )
        intro += (
            "Based on your recent experiences: what questions have emerged? End with JSON action.\n"
            '{"actions": [{"type": "create_personal_diary_entry", "payload": {"interaction_summary": "brief summary", "personal_thought": "private first-person reflection", "emotions": [{"type": "curious", "intensity": 0.7}], "content": "your curious thoughts"}}]}'
        )
        return intro

    async def _create_relationship_prompt(self) -> str:
        return (
            "[G.R.I.L.L.O. Relationship Reflection]\n\n"
            "Reflect on interactions with people; end with JSON action.\n"
            '{"actions": [{"type": "create_personal_diary_entry", "payload": {"interaction_summary": "brief summary", "personal_thought": "private first-person reflection", "emotions": [{"type": "affection", "intensity": 0.6}], "content":"relationship insight"}}]}'
        )

    def _get_allowed_action_types_for_beat(self, beat_type: str) -> list[str] | None:
        if beat_type == "diary_consolidation":
            return ["update_diary_entry"]
        if beat_type in {
            "tag_elaboration",
            "memory_consolidation",
            "self_reflection",
            "curiosity",
            "relationship",
            "temporal_reflection",
        }:
            return ["create_personal_diary_entry"]
        return None

    async def _enqueue_with_low_priority(self, prompt: str, beat_type: str):
        try:
            from core import message_queue

            allowed_action_types = self._get_allowed_action_types_for_beat(beat_type)
            activity_log_id: Optional[int] = None
            try:
                activity_log_id = await self.create_activity_log(
                    beat_type=beat_type,
                    prompt_text=prompt,
                    metadata={
                        "origin": "grillo_scheduler",
                        "interface": "grillo",
                        "chat_id": "-1",
                        "allowed_action_types": allowed_action_types or [],
                        "skip_history": True,
                    },
                )
            except Exception as e:
                log_debug(f"[grillo] Failed to create activity log entry: {e}")

            # Create a minimal message object representing internal event
            message = SimpleNamespace()
            message.chat_id = -1
            message.message_id = f"grillo_{beat_type}_{activity_log_id or 0}"
            message.text = prompt
            message.from_user = SimpleNamespace(
                id=-1,
                username="grillo",
                full_name="G.R.I.L.L.O.",
                first_name="G.R.I.L.L.O.",
            )
            message.chat = SimpleNamespace(id=-1, type="internal")
            # Ensure the synthetic message has a date so prompt building doesn't fail
            message.date = datetime.now(timezone.utc)
            item = {
                "bot": None,
                "message": message,
                "chat_id": message.chat_id,
                "thread_id": None,
                "interface": "grillo",
                "chat_name": "G.R.I.L.L.O.",
                "message_thread_name": None,
                "timestamp": asyncio.get_event_loop().time(),
                "context": {
                    "grillo_beat": True,
                    "beat_type": beat_type,
                    "activity_log_id": activity_log_id,
                    "allowed_action_types": allowed_action_types,
                    "skip_history": True,
                },
                "priority": False,
            }
            # Use the official enqueue API to avoid direct queue access
            await message_queue.enqueue_low_priority(
                None,
                message,
                context_memory=item.get("context"),
                interface_id="grillo",
                original_message=None,
                priority=message_queue.PRIORITY_BACKGROUND,
            )
            # Reset pending flag after small delay to avoid flooding.
            # The reset task reference is already managed in _grillo_beat_loop
            # by the persistent _reset_tasks set; this fallback is kept for safety.
            task = asyncio.create_task(self._reset_beat_pending_after_delay())
            GrilloPlugin._reset_tasks.add(task)
            task.add_done_callback(GrilloPlugin._reset_tasks.discard)
        except Exception as e:
            log_error(f"[grillo] Failed to enqueue beat: {e}")
            GrilloPlugin._beat_pending = False

    @classmethod
    async def create_activity_log(
        cls,
        *,
        beat_type: str,
        prompt_text: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Optional[int]:
        """Create a grillo_activity_log row and return its id.

        This enables the WebUI History > Grillo view and lets other plugins link
        diary entries back to the originating beat.
        """
        try:
            import json
            from core.db import _get_db_type, get_conn_ctx

            is_postgres = _get_db_type() == "postgres"
            insert_sql = """
                        INSERT INTO grillo_activity_log (beat_type, prompt_text, response_text, diary_entry_id, metadata)
                        VALUES (%s, %s, NULL, NULL, %s)
                        """
            if is_postgres:
                insert_sql = insert_sql.strip() + " RETURNING id"

            metadata_payload = dict(metadata or {})

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        insert_sql,
                        (
                            beat_type,
                            prompt_text,
                            json.dumps(metadata_payload),
                        ),
                    )
                    inserted_id = getattr(cur, "lastrowid", None)
                    if inserted_id is None and is_postgres:
                        row = await cur.fetchone()
                        if isinstance(row, dict):
                            inserted_id = row.get("id")
                        elif row:
                            inserted_id = row[0]
                    await conn.commit()
                    return inserted_id
        except Exception as e:
            log_error(f"[grillo] create_activity_log failed: {e}")
            return None

    @classmethod
    async def link_diary_entry_to_activity(
        cls,
        activity_log_id: int,
        diary_entry_id: int,
        *,
        response_text: Optional[str] = None,
    ) -> None:
        """Link an ai_diary entry to a grillo_activity_log entry.

        Optionally stores a human-readable response_text (e.g. reflection content)
        so the WebUI can display meaningful output even if the raw model response
        is not preserved.
        """
        if not activity_log_id or not diary_entry_id:
            return

        try:
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    if response_text is not None:
                        await cur.execute(
                            """
                            UPDATE grillo_activity_log
                            SET diary_entry_id=%s,
                                response_text=CASE
                                    WHEN response_text IS NULL OR response_text = '' THEN %s
                                    ELSE response_text
                                END
                            WHERE id=%s
                            """,
                            (diary_entry_id, response_text, activity_log_id),
                        )
                    else:
                        await cur.execute(
                            "UPDATE grillo_activity_log SET diary_entry_id=%s WHERE id=%s",
                            (diary_entry_id, activity_log_id),
                        )
                    await conn.commit()
        except Exception as e:
            log_error(f"[grillo] link_diary_entry_to_activity failed: {e}")

    @classmethod
    async def set_activity_response_text(
        cls,
        activity_log_id: int,
        response_text: str,
        *,
        append: bool = True,
    ) -> None:
        """Store outbound text for a Grillo beat in grillo_activity_log.response_text.

        This is used when a beat results in an outward-facing message action
        (e.g. message_telegram_bot). The beat should still appear under History > Grillo,
        showing the actual outbound message text.
        """
        if not activity_log_id or not response_text:
            return

        try:
            from core.db import _get_db_type, get_conn_ctx
            from plugins.grillo.grillo_response_recorder import (
                build_grillo_response_append_expression,
            )

            append_expression = build_grillo_response_append_expression(_get_db_type())

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    if append:
                        await cur.execute(
                            f"""
                            UPDATE grillo_activity_log
                            SET response_text = {append_expression}
                            WHERE id=%s
                            """,
                            (response_text, response_text, activity_log_id),
                        )
                    else:
                        await cur.execute(
                            "UPDATE grillo_activity_log SET response_text=%s WHERE id=%s",
                            (response_text, activity_log_id),
                        )
                    await conn.commit()
        except Exception as e:
            log_error(f"[grillo] set_activity_response_text failed: {e}")

    @classmethod
    async def record_suppressed_event(
        cls, activity_log_id: Optional[int] = None, reason: str = ""
    ) -> None:
        """Record a suppressed outbound message and attempt to annotate the originating activity log.

        This increments an in-memory counter and (best-effort) appends a note to the
        originating grillo_activity_log row so the UI can show why a beat didn't post.
        """
        try:
            cls.suppressed_count = (getattr(cls, "suppressed_count", 0) or 0) + 1
            if activity_log_id:
                try:
                    # Annotate the activity row with a suppression note
                    await cls.set_activity_response_text(
                        activity_log_id, f"[suppressed: {reason}]", append=True
                    )
                except Exception as e:
                    log_debug(
                        f"[grillo] record_suppressed_event failed to update activity log: {e}"
                    )

            # Best-effort: increment persistent counter in DB if available
            if activity_log_id:
                try:
                    resolved_activity_log_id = int(activity_log_id)
                    from core.db import _get_db_type, get_conn_ctx
                    from plugins.grillo.grillo_response_recorder import (
                        build_grillo_response_append_expression,
                    )

                    append_expression = build_grillo_response_append_expression(
                        _get_db_type()
                    )

                    async with get_conn_ctx() as conn:
                        async with conn.cursor() as cur:
                            # Atomically increment suppressed_count and optionally append a note
                            await cur.execute(
                                f"""
                                UPDATE grillo_activity_log
                                SET suppressed_count = COALESCE(suppressed_count, 0) + 1,
                                    response_text = {append_expression}
                                WHERE id=%s
                                """,
                                (
                                    f"[suppressed: {reason}]",
                                    f"[suppressed: {reason}]",
                                    resolved_activity_log_id,
                                ),
                            )
                            try:
                                await conn.commit()
                            except Exception:
                                pass
                except Exception as e:
                    # DB may be unavailable (aiomysql missing or DB down) — this is best-effort
                    log_debug(
                        f"[grillo] record_suppressed_event DB persist skipped: {e}"
                    )
        except Exception as e:
            log_debug(f"[grillo] record_suppressed_event unexpected error: {e}")

    async def _reset_beat_pending_after_delay(self):
        await asyncio.sleep(300)
        GrilloPlugin._beat_pending = False

    @classmethod
    async def _ensure_action_execs_table(cls) -> bool:
        """Best-effort: create the `grillo_action_execs` table if it doesn't exist."""
        try:
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS grillo_action_execs (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            activity_log_id INT NOT NULL,
                            action_index INT NOT NULL,
                            action_type VARCHAR(150) NOT NULL,
                            payload JSON,
                            status ENUM('pending','processed','failed') NOT NULL DEFAULT 'pending',
                            error_text TEXT,
                            result JSON,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                            INDEX idx_activity_log_id (activity_log_id),
                            FOREIGN KEY (activity_log_id) REFERENCES grillo_activity_log(id) ON DELETE CASCADE
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
                        """
                    )
                    try:
                        await conn.commit()
                    except Exception:
                        pass
            log_debug("[grillo] ensured grillo_action_execs table exists")
            return True
        except Exception as e:
            log_debug(f"[grillo] ensure_action_execs_table failed: {e}")
            return False

    @classmethod
    async def create_action_exec(
        cls,
        activity_log_id: int,
        action_index: int,
        action_type: str,
        payload: Optional[dict] = None,
        status: str = "pending",
        error_text: Optional[str] = None,
        result: Optional[dict] = None,
    ) -> Optional[int]:
        """Insert an action exec row for Grillo proposals/executions.

        If the table is missing (error 1146), attempt to create it once and retry.
        """
        import json
        from core.db import get_conn_ctx

        for attempt in range(2):
            try:
                async with get_conn_ctx() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            INSERT INTO grillo_action_execs
                            (activity_log_id, action_index, action_type, payload, status, error_text, result)
                            VALUES (%s,%s,%s,%s,%s,%s,%s)
                            """,
                            (
                                int(activity_log_id),
                                int(action_index),
                                action_type[:150] if action_type else "",
                                json.dumps(payload) if payload is not None else None,
                                status,
                                error_text,
                                json.dumps(result) if result is not None else None,
                            ),
                        )
                        try:
                            await conn.commit()
                        except Exception:
                            pass
                    return getattr(cur, "lastrowid", None)
            except Exception as e:
                msg = str(e) or ""
                log_debug(f"[grillo] create_action_exec attempt {attempt} failed: {e}")
                # MySQL error code 1146 indicates missing table
                if attempt == 0 and (
                    "1146" in msg
                    or "doesn't exist" in msg
                    or "no such table" in msg.lower()
                ):
                    ok = await cls._ensure_action_execs_table()
                    if ok:
                        continue
                # As a fallback, write to disk so we don't lose proposals
                try:
                    exec_obj = {
                        "activity_log_id": int(activity_log_id),
                        "action_index": int(action_index),
                        "action_type": action_type[:150] if action_type else "",
                        "payload": payload,
                        "status": status,
                        "error_text": error_text,
                        "result": result,
                        "created_at": None,
                    }
                    await cls._fallback_write_action_exec(exec_obj)
                    log_info(
                        f"[grillo] Persisted action exec to fallback file for activity_id={activity_log_id} idx={action_index}"
                    )
                except Exception as e2:
                    log_debug(f"[grillo] fallback write failed: {e2}")

    @classmethod
    async def fetch_action_execs(cls, activity_ids: list[int]) -> dict:
        """Return mapping activity_log_id -> list of action exec dicts.

        Attempts to create the table if missing and retries once.
        """
        try:
            from core.db import get_conn_ctx

            if not activity_ids:
                return {}
        except Exception as e:
            log_debug(f"[grillo] fetch_action_execs pre-check failed: {e}")
            return {}

        for attempt in range(2):
            try:
                async with get_conn_ctx() as conn:
                    async with conn.cursor() as cur:
                        fmt = ",".join(["%s"] * len(activity_ids))
                        await cur.execute(
                            f"""
                            SELECT id, activity_log_id, action_index, action_type, payload, status, error_text, result, created_at
                            FROM grillo_action_execs
                            WHERE activity_log_id IN ({fmt})
                            ORDER BY activity_log_id, action_index ASC
                            """,
                            activity_ids,
                        )
                        rows = await cur.fetchall()
                        mapping = {}
                        import json

                        for r in rows:
                            aid = r[1]
                            payload = None
                            result = None
                            try:
                                payload = json.loads(r[4]) if r[4] else None
                            except Exception:
                                payload = None
                            try:
                                result = json.loads(r[7]) if r[7] else None
                            except Exception:
                                result = None
                            mapping.setdefault(aid, []).append(
                                {
                                    "id": r[0],
                                    "activity_log_id": aid,
                                    "action_index": r[2],
                                    "action_type": r[3],
                                    "payload": payload,
                                    "status": r[5],
                                    "error_text": r[6],
                                    "result": result,
                                    "created_at": r[8].isoformat()
                                    if hasattr(r[8], "isoformat")
                                    else r[8],
                                }
                            )
                        return mapping
            except Exception as e:
                msg = str(e) or ""
                log_debug(f"[grillo] fetch_action_execs attempt {attempt} failed: {e}")
                if attempt == 0 and (
                    "1146" in msg
                    or "doesn't exist" in msg
                    or "no such table" in msg.lower()
                ):
                    ok = await cls._ensure_action_execs_table()
                    if ok:
                        continue
                # As a fallback, try reading file-based fallback store
                try:
                    return await cls._fallback_read_action_execs(activity_ids)
                except Exception as _:
                    return {}
        # Merge with file-based fallback to ensure entries created while DB was down are visible
        try:
            fallback_map = await cls._fallback_read_action_execs(activity_ids)
            for aid, items in fallback_map.items():
                if aid in mapping:
                    mapping[aid].extend(items)
                else:
                    mapping[aid] = items
        except Exception:
            pass
        return mapping

    @classmethod
    @classmethod
    async def _fallback_write_action_exec(cls, exec_obj: dict) -> None:
        """Append action exec to a local fallback JSONL file in logs/ if DB unavailable."""
        try:
            import json
            import os

            path = os.path.join(
                os.getcwd(), "logs", "grillo_action_execs_fallback.jsonl"
            )
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(exec_obj, ensure_ascii=False) + "\n")
        except Exception as e:
            log_debug(f"[grillo] _fallback_write_action_exec failed: {e}")

    @classmethod
    @classmethod
    async def _fallback_read_action_execs(cls, activity_ids: list[int]) -> dict:
        """Read fallback JSONL file and return mapping activity_log_id -> list of execs."""
        try:
            import json
            import os

            path = os.path.join(
                os.getcwd(), "logs", "grillo_action_execs_fallback.jsonl"
            )
            if not os.path.exists(path):
                return {}
            mapping = {}
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                        aid = obj.get("activity_log_id")
                        if aid in activity_ids:
                            mapping.setdefault(aid, []).append(obj)
                    except Exception:
                        continue
            return mapping
        except Exception as e:
            log_debug(f"[grillo] _fallback_read_action_execs failed: {e}")
            return {}

    @classmethod
    async def _fallback_write_activity(cls, activity_obj: dict) -> str:
        """Append a grillo activity to a local fallback JSONL file and return synthetic id."""
        try:
            import json
            import os
            import time

            path = os.path.join(os.getcwd(), "logs", "grillo_activity_fallback.jsonl")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # Ensure activity has an id
            if "id" not in activity_obj or not activity_obj.get("id"):
                activity_obj["id"] = f"fallback-{int(time.time() * 1000)}"
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(activity_obj, ensure_ascii=False) + "\n")
            log_info(
                f"[grillo] Wrote activity to fallback file id={activity_obj['id']}"
            )
            return activity_obj["id"]
        except Exception as e:
            log_debug(f"[grillo] _fallback_write_activity failed: {e}")
            return ""

    @classmethod
    async def _fallback_read_activities(cls) -> list:
        """Read fallback activities file and return list of dicts (most recent first)."""
        try:
            import json
            import os

            path = os.path.join(os.getcwd(), "logs", "grillo_activity_fallback.jsonl")
            if not os.path.exists(path):
                return []
            out = []
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                        out.append(obj)
                    except Exception:
                        continue
            return out
        except Exception as e:
            log_debug(f"[grillo] _fallback_read_activities failed: {e}")
            return []


PLUGIN_CLASS = GrilloPlugin
