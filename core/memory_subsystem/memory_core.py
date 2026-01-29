
"""
Memory Subsystem for Synth
This module handles the storage, retrieval, and analysis of long-term memories and
passive observations (like group chat scraping).
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from core.db import get_conn_ctx, ensure_core_tables, insert_memory
from core.logging_utils import log_debug, log_info, log_warning, log_error

# Constants
OBSERVED_MEMORY_SOURCE = "passive_observer"
OBSERVED_MEMORY_SCOPE = "group_chat"

# --- Database Schema ---
async def ensure_memory_tables():
    """Ensure the memory-related tables exist."""
    await ensure_core_tables() # Basic memories table is in core
    
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
             # Ensure any specific memory indexes are present (placeholder)
             pass

# --- Passive Observation Logic ---

async def record_passive_observation(
    interface_path: str,
    user_id: str,
    username: str, 
    content: str,
    timestamp: str = None
):
    """
    Record a message seen in a group chat without triggering a response.
    This acts as 'sensory input' for future learning.
    """
    if not timestamp:
         timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    tags = json.dumps(["passive", "group_chat", f"user:{user_id}"])
    
    # Store in standard memory table, but marked as passive
    await insert_memory(
        content=f"[{username}]: {content}",
        author=username,
        source=OBSERVED_MEMORY_SOURCE,
        tags=tags,
        scope=f"{OBSERVED_MEMORY_SCOPE}:{interface_path}",
        timestamp=timestamp
    )
    
    # Update participant bio stats (completing the "scrape and learn" foundation)
    await _ensure_participant_tracked(user_id, username)

async def _ensure_participant_tracked(user_id: str, username: str):
    """Ensure user exists in the bio system and update last_accessed."""
    try:
        # Dynamically import to avoid circular dependencies at module level
        from plugins.bio_manager import _ensure_user_exists, _update_last_accessed_async, update_bio_fields_auto
        
        # 1. Ensure they exist in the bio table (Synth "knows" them)
        _ensure_user_exists(user_id)
        
        # 2. Update their last seen time
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        await _update_last_accessed_async(user_id, now)
        
        # 3. If we have a username and the bio doesn't, or it's different, update it
        # This is "learning" their name from the chat stream
        if username and username != "Unknown":
            # We use update_bio_fields_auto to bypass limits for system updates
            # This ensures the bot always has the freshest name reference
            updates = {
                "user_name": username,      # Update primary display name
                "known_as": [username]      # Add to Aliases list (merges automatically)
            }
            update_bio_fields_auto(user_id, updates)

    except ImportError:
        log_warning("[memory_core] bio_manager plugin not found, skipping bio updates")
    except Exception as e:
        log_debug(f"[memory_core] Failed to update bio for {user_id}: {e}")


# --- Analysis / Learning (Async Jobs) ---

async def analyze_recent_chat_segment(interface_path: str, limit: int = 50):
    """
    (Placeholder) 
    This would retrieve the last N passive memories for a chat,
    send them to an LLM to summarize "User X likes Y", and update
    the participant_profiles table.
    """
    pass

