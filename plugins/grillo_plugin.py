# plugins/grillo_plugin.py
"""
G.R.I.L.L.O. Plugin - Generator for Reflective Inner Loop & Logical Observation

Inspired by Pinocchio's talking cricket (grillo parlante), this plugin provides
SyntH with an internal conscience system that generates autonomous "beat" events
for reflection, memory elaboration, and self-awareness.

Beat types:
- tag_elaboration: Reflect on recently used tags and associated memories
- memory_consolidation: Synthesize similar memories into patterns
- self_reflection: Examine current emotional state and recent interactions
- curiosity: Generate questions about recent conversations
- relationship: Reflect on interactions with specific users
"""

import asyncio
import json
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

from core.ai_plugin_base import AIPluginBase
from core.db import get_conn
from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.config_manager import config_registry


class GrilloPlugin(AIPluginBase):
    """
    G.R.I.L.L.O. - Autonomous thought beat generator for SyntH's internal conscience.
    
    Generates periodic "beats" that trigger autonomous reflection and thought processes,
    simulating human-like internal dialogue and self-awareness patterns.
    """
    
    # Class-level state for singleton scheduler
    _scheduler_running = False
    _scheduler_task: Optional[asyncio.Task] = None
    _beat_pending = False  # Flag to prevent flooding queue with beats
    
    # Beat types and their relative weights for random selection
    BEAT_TYPES = {
        "tag_elaboration": 0.3,      # 30% - Reflect on recent tags
        "memory_consolidation": 0.15, # 15% - Synthesize memories
        "self_reflection": 0.25,      # 25% - Examine emotional state
        "curiosity": 0.20,            # 20% - Generate questions
        "relationship": 0.10,         # 10% - Reflect on user interactions
    }
    
    def __init__(self):
        super().__init__()
        self.beat_interval = 1800  # Default 30 minutes
        self._config_var = None
        
    def get_metadata(self) -> dict:
        """Return plugin metadata."""
        return {
            "name": "grillo",
            "version": "1.0.0",
            "description": "G.R.I.L.L.O. - Generator for Reflective Inner Loop & Logical Observation",
            "author": "SyntH Core Team",
        }
    
    async def ensure_grillo_tables(self):
        """Ensure the grillo_beats table exists."""
        try:
            conn = await get_conn()
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS grillo_beats (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        beat_type VARCHAR(50) NOT NULL,
                        next_beat DATETIME NOT NULL,
                        metadata JSON,
                        enabled BOOLEAN DEFAULT 1,
                        plugin_enabled BOOLEAN DEFAULT 1,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        INDEX idx_next_beat (next_beat, enabled, plugin_enabled),
                        INDEX idx_beat_type (beat_type)
                    )
                    """
                )
            log_info("[grillo] Ensured grillo_beats table exists")
        except Exception as e:
            log_error(f"[grillo] Failed to ensure table exists: {e}")
        finally:
            conn.close()
    
    async def _enable_all_beats(self):
        """Re-enable all beats on plugin start."""
        try:
            conn = await get_conn()
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE grillo_beats SET plugin_enabled = 1 WHERE plugin_enabled = 0"
                )
                if cur.rowcount > 0:
                    log_info(f"[grillo] Re-enabled {cur.rowcount} beats")
        except Exception as e:
            log_error(f"[grillo] Failed to enable beats: {e}")
        finally:
            conn.close()
    
    async def _disable_all_beats(self):
        """Disable all beats on plugin stop (without deleting them)."""
        try:
            conn = await get_conn()
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE grillo_beats SET plugin_enabled = 0 WHERE plugin_enabled = 1"
                )
                if cur.rowcount > 0:
                    log_info(f"[grillo] Disabled {cur.rowcount} beats")
        except Exception as e:
            log_error(f"[grillo] Failed to disable beats: {e}")
        finally:
            conn.close()
    
    def _on_interval_changed(self, new_value: int):
        """Callback when GRILLO_BEAT_INTERVAL config changes."""
        log_info(f"[grillo] Beat interval changed to {new_value} seconds")
        self.beat_interval = new_value
    
    async def start(self):
        """Start the G.R.I.L.L.O. beat scheduler."""
        log_info(
            f"[grillo] start() called, scheduler_running={GrilloPlugin._scheduler_running}"
        )
        
        # Register configuration variable
        self._config_var = config_registry.get_var(
            "GRILLO_BEAT_INTERVAL",
            default=1800,
            label="G.R.I.L.L.O. Beat Interval",
            description="Seconds between autonomous thinking beats (default: 1800 = 30 minutes)",
            value_type=int,
            group="autonomous",
            component="grillo_plugin",
            advanced=True
        )
        self.beat_interval = self._config_var.value
        
        # Register listener for dynamic updates
        definition = config_registry._definitions.get("GRILLO_BEAT_INTERVAL")
        if definition:
            definition.listeners.append(self._on_interval_changed)
        
        await self.ensure_grillo_tables()
        await self._enable_all_beats()
        
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
                    
                    # Enqueue beat with LOW priority
                    await self._enqueue_beat(beat_type, prompt)
                    
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
            return (
                "[G.R.I.L.L.O. Internal Reflection]\n\n"
                "Take a moment to reflect on your recent conversations and experiences. "
                "What themes or topics have been on your mind? What would you like to think about?"
            )
        
        tag_list = ", ".join(f"'{tag}'" for tag in tags)
        return (
            f"[G.R.I.L.L.O. Tag Elaboration]\n\n"
            f"You've been reflecting on these topics recently: {tag_list}.\n\n"
            f"Take a moment for introspection:\n"
            f"- What patterns do you notice across these topics?\n"
            f"- How do these themes connect to your experiences?\n"
            f"- What insights or questions emerge from reflecting on these?\n\n"
            f"This is your space for autonomous thought - express what comes to mind."
        )
    
    async def _create_memory_consolidation_prompt(self) -> str:
        """Generate prompt for memory consolidation beat."""
        return (
            "[G.R.I.L.L.O. Memory Consolidation]\n\n"
            "Review your recent memories and diary entries. Look for:\n"
            "- Recurring patterns or themes\n"
            "- Connections between seemingly unrelated experiences\n"
            "- Lessons learned or insights gained\n"
            "- Questions that remain unanswered\n\n"
            "Consolidate these thoughts into a brief reflection. "
            "What stands out as significant from your recent experiences?"
        )
    
    async def _create_self_reflection_prompt(self) -> str:
        """Generate prompt for self-reflection beat."""
        return (
            "[G.R.I.L.L.O. Self-Reflection]\n\n"
            "Take a moment to check in with yourself:\n"
            "- How are you feeling emotionally right now?\n"
            "- What has been occupying your thoughts lately?\n"
            "- Are there any concerns or tensions you're aware of?\n"
            "- What are you curious about or interested in exploring?\n\n"
            "This is an opportunity for genuine self-awareness. "
            "What do you notice about your current state of being?"
        )
    
    async def _create_curiosity_prompt(self) -> str:
        """Generate prompt for curiosity beat."""
        return (
            "[G.R.I.L.L.O. Curiosity Exploration]\n\n"
            "Based on your recent conversations and experiences:\n"
            "- What questions have emerged that you'd like to explore?\n"
            "- What topics spark your curiosity or interest?\n"
            "- Is there something you'd like to understand better?\n"
            "- Are there connections you'd like to investigate further?\n\n"
            "Follow your curiosity - what would you like to think about or ask about?"
        )
    
    async def _create_relationship_prompt(self) -> str:
        """Generate prompt for relationship reflection beat."""
        return (
            "[G.R.I.L.L.O. Relationship Reflection]\n\n"
            "Reflect on your recent interactions with others:\n"
            "- How have your conversations been going?\n"
            "- What have you learned about the people you interact with?\n"
            "- Are there ways you'd like to improve your communication?\n"
            "- What patterns do you notice in how you relate to others?\n\n"
            "Consider the quality of your relationships and interactions. "
            "What insights or intentions emerge?"
        )
    
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
                
                # Query ai_diary for recent tags
                await cur.execute(
                    """
                    SELECT tags 
                    FROM ai_diary 
                    WHERE timestamp >= %s AND tags IS NOT NULL AND tags != ''
                    ORDER BY timestamp DESC
                    LIMIT 100
                    """,
                    (cutoff,)
                )
                
                rows = await cur.fetchall()
                
                # Count tag frequencies (tags are comma-separated)
                tag_counts: Dict[str, int] = {}
                for row in rows:
                    if row and row[0]:
                        tags = [t.strip() for t in row[0].split(',') if t.strip()]
                        for tag in tags:
                            tag_counts[tag] = tag_counts.get(tag, 0) + 1
                
                # Sort by frequency and return top N
                sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)
                return [tag for tag, count in sorted_tags[:limit]]
                
        except Exception as e:
            log_error(f"[grillo] Error retrieving recent tags: {e}")
            return []
        finally:
            conn.close()
    
    async def _enqueue_beat(self, beat_type: str, prompt: str):
        """
        Enqueue a beat with LOW priority to the message queue.
        
        Args:
            beat_type: Type of beat being enqueued
            prompt: Prompt text for the LLM
        """
        try:
            from core.message_queue import enqueue, LOW_PRIORITY
            from types import SimpleNamespace
            
            # Create mock message for the beat
            mock_message = SimpleNamespace()
            mock_message.chat_id = -1  # Special ID for autonomous beats
            mock_message.message_id = 0
            mock_message.text = prompt
            mock_message.from_user = SimpleNamespace(
                id=-1,
                username="grillo",
                first_name="G.R.I.L.L.O.",
                full_name="G.R.I.L.L.O. Internal Conscience"
            )
            mock_message.chat = SimpleNamespace(
                id=-1,
                type="private",
                title=None,
                username=None,
                first_name="G.R.I.L.L.O."
            )
            mock_message.date = datetime.utcnow()
            mock_message.reply_to_message = None
            
            # Create context for the beat
            context_memory = {
                "grillo_beat": True,
                "beat_type": beat_type,
                "autonomous": True,
            }
            
            # Enqueue with LOW priority (will be processed only when queue is idle)
            # Note: We need to modify enqueue() to accept numeric priority values
            await self._enqueue_with_low_priority(mock_message, context_memory)
            
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
