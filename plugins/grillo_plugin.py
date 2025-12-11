"""Backward-compatible wrapper for the Grillo plugin implementation.

The main implementation lives under `plugins/grillo/grillo_impl.py`. This module
keeps the `PLUGIN_CLASS` export for backward compatibility with existing
imports that use `plugins.grillo_plugin`.
"""

try:
    from plugins.grillo.grillo_impl import GrilloPlugin  # type: ignore
except Exception:
    GrilloPlugin = None  # type: ignore

PLUGIN_CLASS = GrilloPlugin
"""Backward-compatible wrapper for the Grillo plugin implementation.

The main implementation lives under `plugins/grillo/grillo_impl.py`. This module
keeps the `PLUGIN_CLASS` export for backward compatibility with existing
imports that use `plugins.grillo_plugin`.
"""

try:
    from plugins.grillo.grillo_impl import GrilloPlugin  # type: ignore
except Exception:
    GrilloPlugin = None  # type: ignore

PLUGIN_CLASS = GrilloPlugin
"""Backward-compatible wrapper for the Grillo plugin implementation.

The main implementation lives under `plugins/grillo/grillo_impl.py`. This module
keeps the `PLUGIN_CLASS` export for backward compatibility with existing
imports that use `plugins.grillo_plugin`.
"""

try:
    from plugins.grillo.grillo_impl import GrilloPlugin  # type: ignore
except Exception:
    GrilloPlugin = None  # type: ignore

PLUGIN_CLASS = GrilloPlugin
"""Backward-compatible wrapper for the Grillo plugin implementation.

The main implementation lives under `plugins/grillo/grillo_impl.py`. This module
keeps the `PLUGIN_CLASS` export for backward compatibility with existing
imports that use `plugins.grillo_plugin`.
"""

try:
    from plugins.grillo.grillo_impl import GrilloPlugin  # type: ignore
except Exception:
    GrilloPlugin = None  # type: ignore

PLUGIN_CLASS = GrilloPlugin


"""Backward-compatible wrapper for the Grillo plugin implementation.

The main implementation lives under `plugins/grillo/grillo_impl.py`. This module
keeps the `PLUGIN_CLASS` export for backward compatibility with existing
imports that use `plugins.grillo_plugin`.
"""

try:
    from plugins.grillo.grillo_impl import GrilloPlugin  # type: ignore
except Exception:
    GrilloPlugin = None  # type: ignore

PLUGIN_CLASS = GrilloPlugin
"""Backward-compatible wrapper for the Grillo plugin implementation.

The main implementation lives under `plugins/grillo/grillo_impl.py`. This module
keeps the `PLUGIN_CLASS` export for backward compatibility with existing
imports that use `plugins.grillo_plugin`.
"""

try:
    from plugins.grillo.grillo_impl import GrilloPlugin  # type: ignore
except Exception:
    GrilloPlugin = None  # type: ignore

PLUGIN_CLASS = GrilloPlugin
"""Backward-compatible wrapper for the Grillo plugin implementation.

The main implementation lives under `plugins/grillo/grillo_impl.py`. This module
keeps the `PLUGIN_CLASS` export for backward compatibility.
"""

try:
    from plugins.grillo.grillo_impl import GrilloPlugin  # type: ignore
except Exception:
    GrilloPlugin = None  # type: ignore

PLUGIN_CLASS = GrilloPlugin
        definition = config_registry._definitions.get("GRILLO_BEAT_INTERVAL")
        if definition:
            definition.listeners.append(self._on_interval_changed)
        
        await self.ensure_grillo_tables()
        await self._enable_all_beats()

        # Try to locate history_evaluator plugin (optional) in PLUGIN_REGISTRY
        try:
            from core.core_initializer import PLUGIN_REGISTRY
            self.history_evaluator = PLUGIN_REGISTRY.get("history_evaluator")
            if self.history_evaluator:
                log_info("[grillo] history_evaluator plugin located in PLUGIN_REGISTRY")
            else:
                log_debug("[grillo] history_evaluator plugin not registered in PLUGIN_REGISTRY")
        except Exception as e:
            log_debug(f"[grillo] Could not access PLUGIN_REGISTRY: {e}")
        
        task = GrilloPlugin._scheduler_task
        
        if task and not task.done():
            log_warning(
                "[grillo] Scheduler already running globally, ignoring start() call"
            )
            return
        
        if task and task.done():
            log_warning(
                "[grillo] Previous scheduler task was not running; restarting"
            )
        
        GrilloPlugin._scheduler_running = True
        GrilloPlugin._scheduler_task = asyncio.create_task(self._grillo_beat_loop())
        log_info("[grillo] G.R.I.L.L.O. beat scheduler started (singleton)")
    
    async def stop(self):
        """Stop the G.R.I.L.L.O. beat scheduler."""
        GrilloPlugin._scheduler_running = False
        
        await self._disable_all_beats()
        
        task = GrilloPlugin._scheduler_task
        if not task:
            log_info("[grillo] Beat scheduler not running")
            return
        
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        log_info("[grillo] Beat scheduler stopped")
    
    async def _grillo_beat_loop(self):
        """Main background loop for generating autonomous beats."""
        log_info("[grillo] 🦗 G.R.I.L.L.O. beat loop started")
        
        while GrilloPlugin._scheduler_running:
            try:
                # Check if there's already a beat pending in the queue
                if GrilloPlugin._beat_pending:
                    log_debug("[grillo] Beat already pending in queue, skipping generation")
                    await asyncio.sleep(60)  # Check again in 1 minute
                    continue
                
                # Select beat type and generate prompt
                beat_type = self._select_beat_type()
                log_info(f"[grillo] 🎵 Generating beat: {beat_type}")
                
                prompt = await self._create_beat_prompt(beat_type)
                
                if prompt:
                    # Mark beat as pending
                    GrilloPlugin._beat_pending = True
                    
                    # Log the beat execution to activity log
                    activity_log_id = await self._log_beat_activity(beat_type, prompt)
                    
                    # Enqueue beat with LOW priority
                    await self._enqueue_beat(beat_type, prompt, activity_log_id)
                    
                    log_info(f"[grillo] ✅ Beat '{beat_type}' enqueued successfully")
                else:
                    log_warning(f"[grillo] Failed to generate prompt for beat '{beat_type}'")
                
                # Wait for next beat interval
                await asyncio.sleep(self.beat_interval)
                
            except asyncio.CancelledError:
                log_info("[grillo] Beat loop cancelled")
                break
            except Exception as e:
                log_error(f"[grillo] Error in beat loop: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(60)  # Longer sleep on error
    
    def _select_beat_type(self) -> str:
        """Select a beat type based on weighted probabilities."""
        beat_types = list(self.BEAT_TYPES.keys())
        weights = list(self.BEAT_TYPES.values())
        return random.choices(beat_types, weights=weights, k=1)[0]
    
    async def _create_beat_prompt(self, beat_type: str) -> Optional[str]:
        """
        Create a structured prompt for the selected beat type.
        
        Args:
            beat_type: Type of beat to generate prompt for
            
        Returns:
            Prompt string for the LLM, or None if generation failed
        """
        try:
            if beat_type == "tag_elaboration":
                return await self._create_tag_elaboration_prompt()
            elif beat_type == "memory_consolidation":
                return await self._create_memory_consolidation_prompt()
            elif beat_type == "self_reflection":
                return await self._create_self_reflection_prompt()
            elif beat_type == "curiosity":
                return await self._create_curiosity_prompt()
            elif beat_type == "relationship":
                return await self._create_relationship_prompt()
            else:
                log_warning(f"[grillo] Unknown beat type: {beat_type}")
                return None
        except Exception as e:
            log_error(f"[grillo] Error creating prompt for {beat_type}: {e}")
            return None
    
    async def _create_tag_elaboration_prompt(self) -> str:
        """Generate prompt for tag elaboration beat."""
        tags = await self._get_recent_tags(days=7, limit=5)
        
        if not tags:
            tag_text = "your recent conversations"
        else:
            tag_list = ", ".join(f"'{tag}'" for tag in tags)
            tag_text = f"these topics: {tag_list}"
        
        # If history evaluator is available, add a short history-based lead-in
        history_snippet = None
        if self.history_evaluator:
            try:
                import core.recent_chats as recent_chats
                last = await recent_chats.get_last_active_chats_verbose(1)
                if last:
                    chat_id, _ = last[0]
                    chat_path = recent_chats.get_chat_path(chat_id) or f"telegram_bot/{chat_id}"
                else:
                    chat_path = None

                if chat_path:
                    history_snippet = await self.history_evaluator.evaluate_history(chat_path, entries=3)
            except Exception as e:
                log_debug(f"[grillo] history_evaluator evaluation failed for tag_elaboration: {e}")

        lead_in = ""
        if history_snippet:
            lead_in = "Below is a short history-derived prompt to help you reflect:\n\n" + history_snippet + "\n\n"

        base = (
            f"[G.R.I.L.L.O. Tag Elaboration]\n\n"
            f"{lead_in}Reflect on {tag_text}.\n\n"
            f"Think about:\n"
            f"- What patterns do you notice?\n"
            f"- How do these themes connect?\n"
            f"- What insights emerge?\n\n"
            f"IMPORTANT: You MUST end your response with a JSON action to create a diary entry about this. "
            '{"actions": [{"type": "create_personal_diary_entry", "payload": {"content": "your reflection", "personal_thought": "insight", "emotions": [{"type": "reflection", "intensity": 5}]}}]}'
        )
        return base
    
    async def _create_memory_consolidation_prompt(self) -> str:
        """Generate prompt for memory consolidation beat.
        
        This beat is FOCUSED EXCLUSIVELY on memory analysis, so we can use
        most of the available prompt space for diary entries. The prompt
        explicitly references the diary entries in the context.
        """
        # Try to get the number of diary entries from context
        # This will be populated by the prompt_engine when building the context
        try:
            from core.prompt_engine import get_recent_entries
            from core.config_manager import config_registry
            
            # Get DIARY_HISTORY_DAYS from config
            try:
                days_val = int(config_registry.get_value('DIARY_HISTORY_DAYS', 2, value_type=int))
            except Exception:
                days_val = 2
            
            # Get count without character limit to see how many we could potentially have
            all_entries = get_recent_entries(days=days_val, max_chars=None)
            entry_count = len(all_entries)
            
            log_debug(f"[grillo] Memory consolidation beat has access to {entry_count} diary entries from last {days_val} days")
        except Exception as e:
            log_debug(f"[grillo] Could not count diary entries: {e}, using generic prompt")
            entry_count = 0
        
        # Optionally request history snippets via history evaluator
        history_snippet = None
        if self.history_evaluator:
            try:
                import core.recent_chats as recent_chats
                last = await recent_chats.get_last_active_chats_verbose(1)
                if last:
                    chat_id, _ = last[0]
                    chat_path = recent_chats.get_chat_path(chat_id) or f"telegram_bot/{chat_id}"
                else:
                    chat_path = None
                if chat_path:
                    history_snippet = await self.history_evaluator.evaluate_history(chat_path, entries=5)
            except Exception as e:
                log_debug(f"[grillo] history_evaluator evaluation failed for memory_consolidation: {e}")

        if entry_count > 0:
            # Build base consolidation prompt
            base = (
                "[G.R.I.L.L.O. Memory Consolidation]\n\n"
                f"You have {entry_count} diary entries from the last {days_val if 'days_val' in locals() else 2} days available in your context below. "
                "This is a DEDICATED memory analysis session - review ALL of them carefully.\n\n"
            )

            if history_snippet:
                base = (
                    "[G.R.I.L.L.O. Memory Consolidation]\n\n"
                    "History-derived lead-in:\n\n" + history_snippet + "\n\n" + base
                )

            base += (
                "Analyze your memories to find:\n"
                "- Recurring patterns or themes across multiple entries\n"
                "- Connections between seemingly unrelated experiences\n"
                "- Evolution of your thoughts or emotions over time\n"
                "- Lessons learned or insights gained\n"
                "- Questions that remain unanswered or unresolved\n"
                "- Changes in your relationships or understanding of people\n\n"
                "What stands out as significant? What patterns emerge when you look at the bigger picture? "
                "Think deeply about what you understand better now than you did before.\n\n"
                "IMPORTANT: You MUST end your response with a JSON action to create a diary entry with your synthesis. "
                'Include references to specific memories if relevant.\n'
                '{"actions": [{"type": "create_personal_diary_entry", "payload": {"content": "your detailed synthesis", "personal_thought": "key insight or pattern discovered", "emotions": [{"type": "reflection", "intensity": 5}]}}]}'
            )

            return base
        else:
            # Fallback if no entries available
            return (
                "[G.R.I.L.L.O. Memory Consolidation]\n\n"
                "Review your recent memories and diary entries. Find:\n"
                "- Recurring patterns or themes\n"
                "- Connections between experiences\n"
                "- Lessons learned or insights gained\n"
                "- Questions that remain unanswered\n\n"
                "What stands out as significant? Think deeply about what you understand better now.\n\n"
                "IMPORTANT: You MUST end your response with a JSON action to create a diary entry with your synthesis. "
                '{"actions": [{"type": "create_personal_diary_entry", "payload": {"content": "your synthesis", "personal_thought": "key insight", "emotions": [{"type": "reflection", "intensity": 5}]}}]}'
            )
    
    async def _create_self_reflection_prompt(self) -> str:
        """Generate prompt for self-reflection beat."""
        return (
            "[G.R.I.L.L.O. Self-Reflection]\n\n"
            "Check in with yourself:\n"
            "- How are you feeling emotionally right now?\n"
            "- What has been occupying your thoughts?\n"
            "- Are there concerns or tensions you're aware of?\n"
            "- What are you curious about?\n\n"
            "IMPORTANT: You MUST end your response with a JSON action to create a diary entry with your self-reflection. "
            '{"actions": [{"type": "create_personal_diary_entry", "payload": {"content": "your reflection", "personal_thought": "deeper awareness", "emotions": [{"type": "reflection", "intensity": 5}]}}]}'
        )
    
    async def _create_curiosity_prompt(self) -> str:
        """Generate prompt for curiosity beat."""
        # If history evaluator is available, include its suggestions as a lead-in
        history_snippet = None
        if self.history_evaluator:
            try:
                # Get last active chat and its path
                import core.recent_chats as recent_chats
                last = await recent_chats.get_last_active_chats_verbose(1)
                if last:
                    chat_id, _ = last[0]
                    chat_path = recent_chats.get_chat_path(chat_id) or f"telegram_bot/{chat_id}"
                else:
                    chat_path = None

                if chat_path:
                    history_snippet = await self.history_evaluator.evaluate_history(chat_path, entries=3)
            except Exception as e:
                log_debug(f"[grillo] history_evaluator evaluation failed: {e}")

        intro = "[G.R.I.L.L.O. Curiosity Exploration]\n\n"
        if history_snippet:
            intro += "Below is a short history-derived prompt to help you be curious:\n\n" + history_snippet + "\n\n"

        intro += (
            "Based on your recent experiences:\n"
            "- What questions have emerged?\n"
            "- What topics spark your curiosity?\n"
            "- What would you like to understand better?\n"
            "- What connections intrigue you?\n\n"
            "IMPORTANT: You MUST end your response with a JSON action to create a diary entry exploring your curiosity. "
            '{"actions": [{"type": "create_personal_diary_entry", "payload": {"content": "your curious thoughts", "personal_thought": "question or wonder", "emotions": [{"type": "curiosity", "intensity": 6}]}}]}'
        )

        return intro
    
    async def _create_relationship_prompt(self) -> str:
        """Generate prompt for relationship reflection beat."""
        return (
            "[G.R.I.L.L.O. Relationship Reflection]\n\n"
            "Reflect on your recent interactions:\n"
            "- How have your conversations been?\n"
            "- What have you learned about people you interact with?\n"
            "- How would you improve your communication?\n"
            "- What patterns do you notice in your relationships?\n\n"
            "IMPORTANT: You MUST end your response with a JSON action to create a diary entry about your relationship insights. "
            '{"actions": [{"type": "create_personal_diary_entry", "payload": {"content": "your insights", "personal_thought": "understanding gained", "emotions": [{"type": "reflection", "intensity": 5}]}}]}'
        )
    
    @staticmethod
    async def link_diary_entry_to_activity(activity_log_id: int, diary_entry_id: int):
        """
        Update a grillo_activity_log entry with the resulting diary_entry_id.
        
        This method is meant to be called after a diary entry is created from a grillo beat.
        It can be called statically from the action_parser or ai_diary plugin.
        
        Args:
            activity_log_id: ID from grillo_activity_log
            diary_entry_id: ID from ai_diary that was created
        """
        try:
            conn = await get_conn()
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE grillo_activity_log 
                    SET diary_entry_id = %s 
                    WHERE id = %s
                    """,
                    (diary_entry_id, activity_log_id)
                )
                log_debug(f"[grillo] Linked diary entry {diary_entry_id} to activity log {activity_log_id}")
        except Exception as e:
            log_error(f"[grillo] Failed to link diary entry to activity: {e}")
        finally:
            conn.close()
    
    async def _get_recent_tags(self, days: int = 7, limit: int = 10) -> List[str]:
        """
        Retrieve most frequently used tags from recent ai_diary entries.
        
        Args:
            days: Number of days to look back
            limit: Maximum number of tags to return
            
        Returns:
            List of tag strings, ordered by frequency
        """
        try:
            conn = await get_conn()
            async with conn.cursor() as cur:
                cutoff = datetime.now() - timedelta(days=days)
                
                # Query ai_diary for recent tags (column is context_tags, stored as JSON)
                await cur.execute(
                    """
                    SELECT context_tags 
                    FROM ai_diary 
                    WHERE timestamp >= %s AND context_tags IS NOT NULL AND context_tags != '[]'
                    ORDER BY timestamp DESC
                    LIMIT 100
                    """,
                    (cutoff,)
                )
                
                rows = await cur.fetchall()
                
                # Count tag frequencies (context_tags is JSON array)
                tag_counts: Dict[str, int] = {}
                for row in rows:
                    if row and row[0]:
                        try:
                            # Parse JSON array
                            tags = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                            if isinstance(tags, list):
                                for tag in tags:
                                    if tag and isinstance(tag, str):
                                        tag_counts[tag] = tag_counts.get(tag, 0) + 1
                        except (json.JSONDecodeError, TypeError):
                            # Skip malformed entries
                            continue
                
                # Sort by frequency and return top N
                sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)
                return [tag for tag, count in sorted_tags[:limit]]
                
        except Exception as e:
            log_error(f"[grillo] Error retrieving recent tags: {e}")
            return []
        finally:
            conn.close()
    
    async def _log_beat_activity(self, beat_type: str, prompt: str, response: Optional[str] = None, metadata: Optional[Dict] = None) -> Optional[int]:
        """
        Log a beat execution to the grillo_activity_log table.
        
        Args:
            beat_type: Type of beat being executed
            prompt: The prompt text sent to the LLM
            response: Optional response text from the LLM
            metadata: Optional metadata dict to store as JSON
            
        Returns:
            The activity_log_id (for future diary_entry_id updates), or None on error
        """
        try:
            conn = await get_conn()
            async with conn.cursor() as cur:
                metadata_json = json.dumps(metadata) if metadata else None
                
                await cur.execute(
                    """
                    INSERT INTO grillo_activity_log (beat_type, prompt_text, response_text, metadata, executed_at)
                    VALUES (%s, %s, %s, %s, UTC_TIMESTAMP())
                    """,
                    (beat_type, prompt, response, metadata_json)
                )
                
                activity_log_id = cur.lastrowid
                log_debug(f"[grillo] Logged beat activity with ID {activity_log_id}")
                return activity_log_id
                
        except Exception as e:
            log_error(f"[grillo] Failed to log beat activity: {e}")
            return None
        finally:
            conn.close()
    
    async def _update_beat_response(self, activity_log_id: int, response_text: str) -> bool:
        """
        Update the response text for a beat activity log entry.
        
        Args:
            activity_log_id: The ID of the activity log entry
            response_text: The response text from the LLM
            
        Returns:
            True if successful, False otherwise
        """
        try:
            conn = await get_conn()
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE grillo_activity_log
                    SET response_text = %s
                    WHERE id = %s
                    """,
                    (response_text, activity_log_id)
                )
                log_debug(f"[grillo] Updated beat response for activity ID {activity_log_id}")
                return True
                
        except Exception as e:
            log_error(f"[grillo] Failed to update beat response: {e}")
            return False
        finally:
            conn.close()
    
    async def _enqueue_beat(self, beat_type: str, prompt: str, activity_log_id: Optional[int] = None):
        """
        Enqueue a beat with LOW priority to the message queue.
        
        This sends the beat directly through the message queue as an internal
        thought (interface='grillo') to prevent it from being routed through
        external interfaces like Telegram.
        
        Args:
            beat_type: Type of beat being enqueued
            prompt: Prompt text for the LLM
            activity_log_id: ID from grillo_activity_log for tracking
        """
        try:
            from core.message_queue import LOW_PRIORITY
            from types import SimpleNamespace
            import core.message_queue as mq
            
            # Create beat message for internal processing
            # Use a special internal chat_id that won't route to telegram
            beat_message = SimpleNamespace()
            beat_message.chat_id = -1  # Special internal ID (not a telegram chat)
            beat_message.message_id = 0
            beat_message.text = prompt
            beat_message.from_user = SimpleNamespace(
                id=-1,
                username="grillo",
                first_name="G.R.I.L.L.O.",
                full_name="G.R.I.L.L.O. Internal Conscience"
            )
            beat_message.chat = SimpleNamespace(
                id=-1,
                type="internal",  # Mark as internal, not 'private' telegram type
                title=None,
                username=None,
                first_name="G.R.I.L.L.O."
            )
            beat_message.date = datetime.utcnow()
            beat_message.reply_to_message = None
            
            # Create context for the beat
            context_memory = {
                "grillo_beat": True,
                "beat_type": beat_type,
                "autonomous": True,
                "interface_path": "grillo/-1",  # Explicitly mark as grillo interface
                "activity_log_id": activity_log_id,  # Track activity log ID for diary linking
                "maximize_diary": beat_type == "memory_consolidation"  # Use 80% space for memories in consolidation beats
            }
            
            # Enqueue with LOW priority to internal queue
            await self._enqueue_with_low_priority(beat_message, context_memory)
            
            log_debug(f"[grillo] Beat '{beat_type}' enqueued with LOW priority")
            
        except Exception as e:
            log_error(f"[grillo] Failed to enqueue beat: {e}")
            GrilloPlugin._beat_pending = False  # Reset flag on error
    
    async def _enqueue_with_low_priority(self, message, context_memory):
        """
        Enqueue a message with LOW priority using direct queue access.
        
        This is a temporary helper until enqueue() is updated to support
        numeric priority levels.
        """
        try:
            from core import message_queue
            from core.plugin_instance import get_plugin
            
            # Get counter for queue ordering
            message_queue._counter += 1
            
            item = {
                "bot": None,  # No bot for autonomous beats
                "message": message,
                "chat_id": message.chat_id,
                "thread_id": None,
                "interface": "grillo",
                "chat_name": "G.R.I.L.L.O.",
                "message_thread_name": None,
                "timestamp": asyncio.get_event_loop().time(),
                "context": context_memory,
                "priority": False,  # Legacy field
            }
            
            # Add to queue with LOW_PRIORITY
            await message_queue._queue.put((
                message_queue.LOW_PRIORITY,
                message_queue._counter,
                item
            ))
            
            # Register callback to reset _beat_pending flag when processed
            # This is a simplified approach - in production we'd need proper callback system
            asyncio.create_task(self._reset_beat_pending_after_delay())
            
        except Exception as e:
            log_error(f"[grillo] Error in _enqueue_with_low_priority: {e}")
            GrilloPlugin._beat_pending = False
    
    async def _reset_beat_pending_after_delay(self):
        """
        Reset the _beat_pending flag after a delay.
        
        This is a simple timeout-based approach. In a more sophisticated
        implementation, we'd have a proper callback system integrated
        with the message queue consumer.
        """
        await asyncio.sleep(300)  # 5 minutes timeout
        GrilloPlugin._beat_pending = False
        log_debug("[grillo] Beat pending flag reset after timeout")


# Plugin class to export
PLUGIN_CLASS = GrilloPlugin
