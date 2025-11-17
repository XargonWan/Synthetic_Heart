"""AI Personal Diary Plugin

This plugin manages synth's personal diary entries where synth records
what he says to users, his emotions, and his personal thoughts about interactions.
This creates a more human-like memory system where synth builds his persona
and remembers his relationships with users in a personal way.
"""

from __future__ import annotations

import os
import json
from datetime import datetime, timedelta
from typing import Any, List, Dict, Optional
import asyncio
import aiomysql
import threading
from contextlib import asynccontextmanager

from core.db import get_conn_ctx
from core.logging_utils import log_error, log_info, log_debug, log_warning

# Injection priority for diary entries
INJECTION_PRIORITY = 8  # Low priority - diary is sacrificial

def register_injection_priority():
    """Register this component's injection priority."""
    log_info(f"[ai_diary] Registered injection priority: {INJECTION_PRIORITY}")
    return INJECTION_PRIORITY

# Register priority when module is loaded
register_injection_priority()

from core.core_initializer import register_plugin
from core.config import get_active_llm
from core.llm_registry import get_llm_registry
from core.interfaces_registry import get_interface_registry

# Global flag to track if the plugin is enabled
PLUGIN_ENABLED = True

# Diary-specific configuration
DIARY_CONFIG = {
    'diary_injection_file': 'synth_diary.json',
    'diary_injection_enabled': True,
    'diary_allocation_percentage': 30,  # Increased from 15% to utilize more available prompt space
    'max_static_injection_chars': 30000,  # Reduced for free ChatGPT limits
    'fallback_diary_chars': 10000,  # Reduced for free ChatGPT limits
    'default_days': 7,  # Default number of days to look back for diary entries
    'min_space_threshold': 0.75,  # Include diary only if we're using less than 75% of prompt space
    'diary_entry_structure': 'auto',  # auto-select based on available space
    'diary_sort_order': 'descending',  # newest first
    'diary_filter_strategy': 'most_recent',  # strategy for selecting entries when space is limited
    'diary_tag_priority': ['important', 'daily', 'thoughts'],  # prioritize these tags
    'enable_diary_char_logging': True  # Enhanced logging for debugging
}

def get_diary_config(interface_name: str) -> dict:
    """Get diary configuration for a specific interface."""
    return DIARY_CONFIG

def normalize_interface_name(interface: str) -> str:
    """Normalize interface name for consistent diary entries."""
    if not interface or interface.lower() == "unknown":
        return "unknown"
    
    # Normalize telegram interfaces
    if "telegram" in interface.lower() or "telethon" in interface.lower():
        return "telegram"
    
    # Normalize discord interfaces  
    if "discord" in interface.lower():
        return "discord"
        
    # Other specific interfaces
    interface_mapping = {
        "webui": "webui",
        "web": "webui", 
        "x_interface": "x",
        "twitter": "x",
        "reddit_interface": "reddit",
        "cli": "manual",
        "manual": "manual"
    }
    
    normalized = interface_mapping.get(interface.lower(), interface.lower())
    return normalized

def get_max_diary_chars(interface_name: str = None, current_prompt_length: int = 0) -> int:
    """Calculate how many characters can be allocated to diary injection based on active LLM interface limits."""
    try:
        # Get limits directly from the active LLM engine
        from core.config import get_active_llm
        from core.llm_registry import get_llm_registry
        import asyncio
        
        # Handle async get_active_llm call safely
        active_llm = None
        try:
            # Try to get the event loop
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're in an async context, need to handle differently
                log_debug("[ai_diary] Already in async context, using sync fallback for get_active_llm")
                # Use a simple fallback since we can't await here
                active_llm = "manual"  # Safe fallback
            else:
                active_llm = loop.run_until_complete(get_active_llm())
        except RuntimeError:
            # No event loop exists, create one
            try:
                active_llm = asyncio.run(get_active_llm())
            except Exception as e:
                log_debug(f"[ai_diary] Could not get active LLM: {e}")
                active_llm = "manual"  # Safe fallback
        except Exception as e:
            log_debug(f"[ai_diary] Error in async handling: {e}")
            active_llm = "manual"  # Safe fallback
        
        if not active_llm or active_llm == "manual":
            log_debug("[ai_diary] Using manual fallback limits from Selenium engine if available")
            # Try to get limits from active Selenium LLM engine first
            try:
                from core.selenium_llm_base import get_active_selenium_limits
                selenium_limits = get_active_selenium_limits()
                max_selenium_chars = selenium_limits.get("max_prompt_chars", 128000)
                return max_selenium_chars
            except Exception:
                return 128000  # Safe fallback
        
        registry = get_llm_registry()
        engine = registry.get_engine(active_llm)
        
        if not engine:
            engine = registry.load_engine(active_llm)
        
        # Try to get limits from active Selenium LLM engine first
        max_prompt_chars = 128000  # Safe fallback default
        try:
            from core.selenium_llm_base import get_active_selenium_limits
            selenium_limits = get_active_selenium_limits()
            max_prompt_chars = selenium_limits.get("max_prompt_chars", 128000)
        except Exception:
            # If not a Selenium engine, try to get from the engine itself
            if engine and hasattr(engine, 'get_interface_limits'):
                limits = engine.get_interface_limits()
                max_prompt_chars = limits.get("max_prompt_chars", 128000)
        
        # Use 30% of available prompt space for diary, with fallback
        diary_limit = int(max_prompt_chars * 0.30)
        
        # Consider current prompt length
        available_space = max_prompt_chars - current_prompt_length
        diary_allocation = min(diary_limit, max(available_space * 0.5, 5000))  # At least 5k if space allows
        
        log_debug(f"[ai_diary] Diary allocation: {diary_allocation} chars (max: {max_prompt_chars}, used: {current_prompt_length})")
        return max(diary_allocation, 5000)  # Minimum 5k chars
    except Exception as e:
        log_warning(f"[ai_diary] Error calculating diary limit: {e}")
        return 8000  # Fallback


async def _run_sync_async(coro):
    """Run async function, handling all cases without creating new event loops."""
    try:
        # Get current running loop if available
        loop = asyncio.get_running_loop()
        # Just run the coroutine directly - we have a running loop
        return await coro
    except RuntimeError:
        # No running loop, just run the coroutine directly
        return await coro

def _run_sync(coro):
    """Helper to run async functions in sync context with better error handling."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're in an async context, schedule coroutine on the running loop from this thread
            # This avoids creating a new event loop
            import concurrent.futures
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=5.0)
        else:
            return loop.run_until_complete(coro)
    except RuntimeError:
        # No event loop, create one
        try:
            return asyncio.run(coro)
        except Exception as e:
            log_debug(f"[ai_diary] Error in asyncio.run: {e}")
            return None
    except Exception as e:
        log_debug(f"[ai_diary] Unexpected error in _run_sync: {e}")
        return None

def should_include_diary(interface_name: str, current_prompt_length: int = 0, max_prompt_chars: int = 0) -> bool:
    """Determine if diary should be included based on available space."""
    # Try to get max_prompt_chars from active LLM if not provided
    if max_prompt_chars <= 0:
        try:
            active_llm = _run_sync(get_active_llm())
            # Check that active_llm is not None before proceeding
            if not active_llm:
                log_debug(f"[ai_diary] Active LLM is None, skipping LLM limits lookup")
                return True  # Conservative: include diary if we can't determine LLM
            
            registry = get_llm_registry()
            engine = registry.get_engine(active_llm)
            
            if not engine:
                engine = registry.load_engine(active_llm)
            
            if engine and hasattr(engine, 'get_max_prompt_chars'):
                max_prompt_chars = engine.get_max_prompt_chars()
                log_debug(f"[ai_diary] Got max_prompt_chars from LLM {active_llm}: {max_prompt_chars}")
        except Exception as e:
            log_debug(f"[ai_diary] Could not get LLM limits: {e}")
            return True  # Conservative: include diary if we can't determine limits
    
    if max_prompt_chars <= 0:
        # No prompt limit info, use conservative approach
        return True
    
    usage_ratio = current_prompt_length / max_prompt_chars
    
    # Include diary if we're using less than threshold of available space
    should_include = usage_ratio < DIARY_CONFIG["min_space_threshold"]
    log_debug(f"[ai_diary] Prompt usage: {current_prompt_length}/{max_prompt_chars} ({usage_ratio:.2%}), include_diary: {should_include}")
    return should_include


@asynccontextmanager
async def get_db():
    """Context manager for MariaDB database connections."""
    async with get_conn_ctx() as conn:
        try:
            log_debug("[ai_diary] Opened database connection")
            yield conn
        except Exception as e:
            log_error(f"[ai_diary] Database error: {e}")
            raise


async def init_diary_table():
    """Initialize all AI diary related tables if they don't exist."""
    async with get_db() as conn:
        cursor = await conn.cursor()
        
        # Main ai_diary table - redesigned for personal diary entries
        await cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_diary (
                id INT AUTO_INCREMENT PRIMARY KEY,
                content TEXT NOT NULL COMMENT 'What synth said/did in the interaction',
                personal_thought TEXT COMMENT 'synth personal reflection about the interaction',
                emotions TEXT DEFAULT '[]' COMMENT 'synth emotions about this interaction',
                interaction_summary TEXT COMMENT 'Brief summary of what happened',
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                interface VARCHAR(50),
                chat_id VARCHAR(255),
                thread_id VARCHAR(255),
                user_message TEXT COMMENT 'What the user said that triggered this response',
                context_tags TEXT DEFAULT '[]' COMMENT 'Tags about the context/topic',
                involved_users TEXT DEFAULT '[]' COMMENT 'JSON list of users involved in the interaction',
                INDEX idx_timestamp (timestamp),
                INDEX idx_interface_chat (interface, chat_id)
            )
        ''')
        
        # Ensure involved_users column exists (migration for existing tables)
        try:
            await cursor.execute('''
                ALTER TABLE ai_diary ADD COLUMN involved_users TEXT DEFAULT '[]' COMMENT 'JSON list of users involved in the interaction'
            ''')
            log_info("[ai_diary] Added missing involved_users column to ai_diary table")
        except Exception as e:
            # Column might already exist, that's fine
            if "Duplicate column name" not in str(e):
                log_debug(f"[ai_diary] Column migration check: {e}")
        
        # Legacy memories table (moved from core)
        await cursor.execute('''
            CREATE TABLE IF NOT EXISTS memories (
                id INT AUTO_INCREMENT PRIMARY KEY,
                timestamp DATETIME NOT NULL,
                content TEXT NOT NULL,
                author VARCHAR(100),
                source VARCHAR(100),
                tags TEXT,
                scope VARCHAR(50),
                emotion VARCHAR(50),
                intensity INT,
                emotion_state VARCHAR(50)
            )
        ''')
        
        # Legacy emotion_diary table (moved from core)
        await cursor.execute('''
            CREATE TABLE IF NOT EXISTS emotion_diary (
                id VARCHAR(100) PRIMARY KEY,
                source VARCHAR(100),
                event TEXT,
                emotion VARCHAR(50),
                intensity INT,
                state VARCHAR(50),
                trigger_condition TEXT,
                decision_logic TEXT,
                next_check DATETIME
            )
        ''')

        # Archive table for archived diary entries
        await cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_diary_archive (
                id INT AUTO_INCREMENT PRIMARY KEY,
                content TEXT NOT NULL COMMENT 'What synth said/did in the interaction',
                personal_thought TEXT COMMENT 'synth personal reflection about the interaction',
                emotions TEXT DEFAULT '[]' COMMENT 'synth emotions about this interaction',
                interaction_summary TEXT COMMENT 'Brief summary of what happened',
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                interface VARCHAR(50),
                chat_id VARCHAR(255),
                thread_id VARCHAR(255),
                user_message TEXT COMMENT 'What the user said that triggered this response',
                context_tags TEXT DEFAULT '[]' COMMENT 'Tags about the context/topic',
                involved_users TEXT DEFAULT '[]' COMMENT 'JSON list of users involved in the interaction',
                INDEX idx_timestamp (timestamp),
                INDEX idx_interface_chat (interface, chat_id)
            )
        ''')
        
        await conn.commit()
        log_info("[ai_diary] AI diary tables initialized")


async def recreate_diary_table():
    """Drop and recreate the ai_diary table with the new structure (DEV ONLY)."""
    async with get_db() as conn:
        cursor = await conn.cursor()
        
        log_warning("[ai_diary] DROPPING and recreating ai_diary table (DEV MODE)")
        
        # Drop the existing table
        await cursor.execute("DROP TABLE IF EXISTS ai_diary")
        
        # Recreate with new structure
        await cursor.execute('''
            CREATE TABLE ai_diary (
                id INT AUTO_INCREMENT PRIMARY KEY,
                content TEXT NOT NULL COMMENT 'What synth said/did in the interaction',
                personal_thought TEXT COMMENT 'synth personal reflection about the interaction',
                emotions TEXT DEFAULT '[]' COMMENT 'synth emotions about this interaction',
                interaction_summary TEXT COMMENT 'Brief summary of what happened',
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                interface VARCHAR(50),
                chat_id VARCHAR(255),
                thread_id VARCHAR(255),
                user_message TEXT COMMENT 'What the user said that triggered this response',
                context_tags TEXT DEFAULT '[]' COMMENT 'Tags about the context/topic',
                INDEX idx_timestamp (timestamp),
                INDEX idx_interface_chat (interface, chat_id)
            )
        ''')
        
        await conn.commit()
        log_info("[ai_diary] ai_diary table recreated with new personal diary structure")


def _run(coro):
    """Run a coroutine safely even if an event loop is already running."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're in async context, use executor to avoid creating new loop
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(asyncio.run, coro)
                return future.result(timeout=10.0)
        else:
            # Event loop exists but not running, use run_until_complete
            return loop.run_until_complete(coro)
    except RuntimeError:
        # No event loop at all - this is the only safe place to use asyncio.run()
        return asyncio.run(coro)
    except Exception as e:
        log_debug(f"[ai_diary] Error in _run: {e}")
        return None


async def _execute(query: str, params: tuple = ()) -> None:
    """Execute a database query."""
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(query, params)
        await conn.commit()


async def _fetchall(query: str, params: tuple = ()) -> List[Dict]:
    """Fetch all results from a database query."""
    async with get_db() as conn:
        cursor = await conn.cursor(aiomysql.DictCursor)
        await cursor.execute(query, params)
        return await cursor.fetchall()


def add_diary_entry(
    content: str,
    personal_thought: str = None,
    emotions: List[Dict[str, Any]] = None,
    interaction_summary: str = None,
    user_message: str = None,
    context_tags: List[str] = None,
    involved_users: List[str] = None,
    interface: str = None,
    chat_id: str = None,
    thread_id: str = None
) -> None:
    """Add a new personal diary entry where synth records what he said and how he feels.
    
    Args:
        content: What synth said/did in the interaction
        personal_thought: synth's personal reflection about this interaction
        emotions: List of emotions synth felt during this interaction
        interaction_summary: Brief summary of what happened
        user_message: What the user said that triggered this response
        context_tags: Tags about the context/topic (e.g., ['food', 'cars', 'personal'])
        involved_users: List of user names involved in this interaction (from bio system)
        interface: Interface used (telegram_bot, discord, etc.)
        chat_id: Chat identifier
        thread_id: Thread identifier
    """
    global PLUGIN_ENABLED
    
    # Attempt lazy initialization if plugin was disabled at startup
    if not PLUGIN_ENABLED:
        try:
            log_debug("[ai_diary] Attempting lazy initialization of plugin (sync)...")
            _run(_execute("SELECT 1 FROM ai_diary LIMIT 1"))
            PLUGIN_ENABLED = True
            log_info("[ai_diary] Plugin lazy-initialized successfully (sync)")
        except Exception as init_error:
            log_debug(f"[ai_diary] Lazy initialization failed (sync): {init_error}, attempting table creation...")
            try:
                _run(_execute("""
                    CREATE TABLE IF NOT EXISTS ai_diary (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        content LONGTEXT,
                        personal_thought TEXT,
                        emotions JSON,
                        interaction_summary TEXT,
                        user_message TEXT,
                        context_tags JSON,
                        involved_users JSON,
                        interface VARCHAR(50),
                        chat_id VARCHAR(100),
                        thread_id VARCHAR(100)
                    )
                """))
                PLUGIN_ENABLED = True
                log_info("[ai_diary] Plugin table created and enabled successfully via lazy init (sync)")
            except Exception as create_error:
                log_error(f"[ai_diary] Failed to create table during lazy init (sync): {create_error}")
                return
    
    if not PLUGIN_ENABLED:
        return
        
    if not content.strip():
        return
    
    emotions = emotions or []
    context_tags = context_tags or []
    involved_users = involved_users or []
    
    # Normalize interface name for consistency
    interface = normalize_interface_name(interface)
    
    # Validate emotions format
    for emotion in emotions:
        if not isinstance(emotion, dict) or 'type' not in emotion:
            log_warning(f"[ai_diary] Invalid emotion format: {emotion}")
            continue
    
    try:
        _run(_execute(
            """
            INSERT INTO ai_diary (content, personal_thought, emotions, 
                                interaction_summary, user_message, context_tags, involved_users, interface, chat_id, thread_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                content,
                personal_thought,
                json.dumps(emotions),
                interaction_summary,
                user_message,
                json.dumps(context_tags),
                json.dumps(involved_users),
                interface,
                chat_id,
                thread_id
            )
        ))
        log_debug(f"[ai_diary] Added personal diary entry: {content[:50]}...")
        if personal_thought:
            log_debug(f"[ai_diary] Personal thought: {personal_thought[:50]}...")
    except Exception as e:
        log_error(f"[ai_diary] Failed to add diary entry: {e}")
        # Disable plugin if database is unavailable
        PLUGIN_ENABLED = False


async def add_diary_entry_async(
    content: str,
    personal_thought: str = None,
    emotions: List[Dict[str, Any]] = None,
    interaction_summary: str = None,
    user_message: str = None,
    context_tags: List[str] = None,
    involved_users: List[str] = None,
    interface: str = None,
    chat_id: str = None,
    thread_id: str = None
) -> None:
    """Add a new personal diary entry (async version). Safe to call even if plugin is disabled."""
    global PLUGIN_ENABLED
    
    # Attempt lazy initialization if plugin was disabled at startup
    if not PLUGIN_ENABLED:
        try:
            log_debug("[ai_diary] Attempting lazy initialization of plugin...")
            await _execute("SELECT 1 FROM ai_diary LIMIT 1")
            PLUGIN_ENABLED = True
            log_info("[ai_diary] Plugin lazy-initialized successfully")
        except Exception as init_error:
            log_debug(f"[ai_diary] Lazy initialization failed: {init_error}, attempting table creation...")
            try:
                await _execute("""
                    CREATE TABLE IF NOT EXISTS ai_diary (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        content LONGTEXT,
                        personal_thought TEXT,
                        emotions JSON,
                        interaction_summary TEXT,
                        user_message TEXT,
                        context_tags JSON,
                        involved_users JSON,
                        interface VARCHAR(50),
                        chat_id VARCHAR(100),
                        thread_id VARCHAR(100)
                    )
                """)
                PLUGIN_ENABLED = True
                log_info("[ai_diary] Plugin table created and enabled successfully via lazy init")
            except Exception as create_error:
                log_error(f"[ai_diary] Failed to create table during lazy init: {create_error}")
                return
    
    if not PLUGIN_ENABLED:
        return
        
    if not content.strip():
        return
    
    emotions = emotions or []
    context_tags = context_tags or []
    involved_users = involved_users or []
    
    # Normalize interface name for consistency
    interface = normalize_interface_name(interface)
    
    # Validate emotions format
    for emotion in emotions:
        if not isinstance(emotion, dict) or 'type' not in emotion:
            log_warning(f"[ai_diary] Invalid emotion format: {emotion}")
            continue
    
    try:
        await _execute(
            """
            INSERT INTO ai_diary (content, personal_thought, emotions, 
                                interaction_summary, user_message, context_tags, involved_users, interface, chat_id, thread_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                content,
                personal_thought,
                json.dumps(emotions),
                interaction_summary,
                user_message,
                json.dumps(context_tags),
                json.dumps(involved_users),
                interface,
                chat_id,
                thread_id
            )
        )
        log_debug(f"[ai_diary] Added personal diary entry: {content[:50]}...")
        if personal_thought:
            log_debug(f"[ai_diary] Personal thought: {personal_thought[:50]}...")
    except Exception as e:
        log_error(f"[ai_diary] Failed to add diary entry: {e}")
        # Disable plugin if database is unavailable
        PLUGIN_ENABLED = False


def get_recent_entries(days: int = 2, max_chars: int = None) -> List[Dict[str, Any]]:
    """Get diary entries from the last N days, optionally limited by character count. 
    Returns list of dict entries with all database columns, empty list if plugin is disabled.
    Entries are ordered from most recent to oldest, and if max_chars is specified,
    older entries are discarded first to stay within the character limit."""
    global PLUGIN_ENABLED
    
    log_debug(f"[ai_diary] get_recent_entries called with days={days}, max_chars={max_chars}, PLUGIN_ENABLED={PLUGIN_ENABLED}")
    
    # Attempt lazy initialization if plugin was disabled at startup
    if not PLUGIN_ENABLED:
        try:
            log_debug("[ai_diary] Attempting lazy initialization for get_recent_entries...")
            _run(_execute("SELECT 1 FROM ai_diary LIMIT 1"))
            PLUGIN_ENABLED = True
            log_info("[ai_diary] Plugin lazy-initialized successfully in get_recent_entries")
        except Exception as init_error:
            log_debug(f"[ai_diary] Lazy initialization failed in get_recent_entries: {init_error}")
            log_debug("[ai_diary] Plugin disabled, returning empty list")
            return []
    
    if not PLUGIN_ENABLED:
        log_debug("[ai_diary] Plugin disabled, returning empty list")
        return []
        
    try:
        cutoff_date = datetime.now() - timedelta(days=days)
        log_debug(f"[ai_diary] Looking for entries after {cutoff_date}")
        
        entries = _run(_fetchall(
            """
            SELECT id, content, personal_thought, timestamp, context_tags, involved_users, 
                   emotions, interface, chat_id, thread_id, interaction_summary, user_message
            FROM ai_diary
            WHERE timestamp >= %s
            ORDER BY timestamp DESC
            """,
            (cutoff_date,)
        ))
        
        log_debug(f"[ai_diary] Raw query returned {len(entries)} entries")
        
        # Convert JSON fields back to objects
        for entry in entries:
            entry['context_tags'] = json.loads(entry.get('context_tags', '[]'))
            entry['involved_users'] = json.loads(entry.get('involved_users', '[]'))
            entry['emotions'] = json.loads(entry.get('emotions', '[]'))
            entry['timestamp'] = entry['timestamp'].isoformat() if entry['timestamp'] else None
        
        log_debug(f"[ai_diary] After JSON parsing: {len(entries)} entries")
        
        # If character limit specified, filter entries intelligently
        if max_chars:
            total_chars = 0
            filtered_entries = []
            
            for i, entry in enumerate(entries):
                # Calculate the size of this entry as JSON (since we're returning JSON now)
                entry_json = json.dumps(entry, ensure_ascii=False)
                entry_size = len(entry_json)
                
                # Log first few entries to debug size issues
                if i < 3:
                    log_debug(f"[ai_diary] Entry {i+1} size: {entry_size} chars, id: {entry.get('id')}")
                
                # If adding this entry would exceed the limit, stop here
                # Don't truncate individual entries, remove them entirely
                if total_chars + entry_size > max_chars:
                    log_debug(f"[ai_diary] Stopping at {len(filtered_entries)} entries due to char limit ({total_chars}/{max_chars})")
                    log_debug(f"[ai_diary] Entry {i+1} would add {entry_size} chars, exceeding limit")
                    break
                
                filtered_entries.append(entry)
                total_chars += entry_size
            
            log_debug(f"[ai_diary] Filtered diary: {len(filtered_entries)}/{len(entries)} entries, {total_chars} chars")
            return filtered_entries
        
        log_debug(f"[ai_diary] Returning all {len(entries)} entries (no char limit)")
        return entries
    
    except Exception as e:
        log_error(f"[ai_diary] Failed to get recent entries: {e}")
        # Disable plugin if database is unavailable
        PLUGIN_ENABLED = False
        return []


def get_entries_by_tags(tags: List[str], limit: int = 10) -> List[Dict[str, Any]]:
    """Get diary entries that contain any of the specified context tags."""
    try:
        # Create OR conditions for tag matching
        tag_conditions = []
        params = []
        
        for tag in tags:
            tag_conditions.append("JSON_CONTAINS(context_tags, %s)")
            params.append(json.dumps(tag))
        
        if not tag_conditions:
            return []
        
        query = f"""
            SELECT id, content, personal_thought, timestamp, context_tags, involved_users, 
                   emotions, interface, chat_id, thread_id, interaction_summary, user_message
            FROM ai_diary
            WHERE {' OR '.join(tag_conditions)}
            ORDER BY timestamp DESC
            LIMIT %s
        """
        params.append(limit)
        
        entries = _run(_fetchall(query, tuple(params)))
        
        # Convert JSON fields back to objects
        for entry in entries:
            entry['context_tags'] = json.loads(entry.get('context_tags', '[]'))
            entry['involved_users'] = json.loads(entry.get('involved_users', '[]'))
            entry['emotions'] = json.loads(entry.get('emotions', '[]'))
            entry['timestamp'] = entry['timestamp'].isoformat() if entry['timestamp'] else None
        
        return entries
    
    except Exception as e:
        log_error(f"[ai_diary] Failed to get entries by tags: {e}")
        return []


def get_entries_with_person(person: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Get diary entries that involve a specific person."""
    try:
        entries = _run(_fetchall(
            """
            SELECT id, content, personal_thought, timestamp, context_tags, involved_users, 
                   emotions, interface, chat_id, thread_id, interaction_summary, user_message
            FROM ai_diary
            WHERE JSON_CONTAINS(involved_users, %s)
            ORDER BY timestamp DESC
            LIMIT %s
            """,
            (json.dumps(person), limit)
        ))
        
        # Convert JSON fields back to objects
        for entry in entries:
            entry['context_tags'] = json.loads(entry.get('context_tags', '[]'))
            entry['involved_users'] = json.loads(entry.get('involved_users', '[]'))
            entry['emotions'] = json.loads(entry.get('emotions', '[]'))
            entry['timestamp'] = entry['timestamp'].isoformat() if entry['timestamp'] else None
        
        return entries
    
    except Exception as e:
        log_error(f"[ai_diary] Failed to get entries with person {person}: {e}")
        return []


def format_diary_for_injection(entries: List[Dict[str, Any]]) -> str:
    """Format diary entries for static injection into prompts as synth's personal memories."""
    if not entries:
        return ""
    
    formatted_lines = ["=== synth's Personal Diary ==="]
    formatted_lines.append("(This diary contains my past interactions and thoughts from previous conversations)")
    formatted_lines.append("(Use this information only as contextual reference when relevant, not as a continuation of the current conversation)")
    formatted_lines.append("")
    
    for entry in entries:
        # Use the same formatting function as the character counting
        entry_text = _format_single_entry_for_prompt(entry)
        formatted_lines.append(entry_text)
    
    formatted_lines.append("=== End of My Diary ===")
    formatted_lines.append("(Reference these memories only when they provide useful context for the current interaction)")
    return "\n".join(formatted_lines)


def cleanup_old_entries(days_to_keep: int = 30) -> int:
    """Remove diary entries older than specified days. Returns number of deleted entries.
    Returns 0 if plugin is disabled."""
    global PLUGIN_ENABLED
    if not PLUGIN_ENABLED:
        return 0
        
    try:
        cutoff_date = datetime.now() - timedelta(days=days_to_keep)
        
        # First count how many will be deleted
        count_result = _run(_fetchall(
            "SELECT COUNT(*) as count FROM ai_diary WHERE timestamp < %s",
            (cutoff_date,)
        ))
        count = count_result[0]['count'] if count_result else 0
        
        # Delete old entries
        _run(_execute(
            "DELETE FROM ai_diary WHERE timestamp < %s",
            (cutoff_date,)
        ))
        
        log_info(f"[ai_diary] Cleaned up {count} old diary entries")
        return count
    
    except Exception as e:
        log_error(f"[ai_diary] Failed to cleanup old entries: {e}")
        PLUGIN_ENABLED = False
        return 0


def create_personal_diary_entry(
    synth_response: str,
    user_message: str = None,
    context_tags: List[str] = None,
    involved_users: List[str] = None,
    interface: str = None,
    chat_id: str = None,
    thread_id: str = None
) -> None:
    """Helper function to create a complete personal diary entry.
    
    This function should be called every time synth responds to a user.
    It will analyze the response and create appropriate diary content.
    
    Args:
        synth_response: What synth said to the user
        user_message: What the user said to trigger this response
        context_tags: Tags about the topic (e.g., ['food', 'cars', 'personal', 'help'])
        involved_users: List of user names involved in this interaction (from bio system)
        interface: Interface used
        chat_id: Chat identifier
        thread_id: Thread identifier
    """
    
    # Normalize interface name
    log_debug(f"[create_personal_diary_entry] Original interface: '{interface}'")
    interface = normalize_interface_name(interface or "unknown")
    log_debug(f"[create_personal_diary_entry] Normalized interface: '{interface}'")
    
    # Create a more specific summary of what happened
    interaction_summary = _generate_interaction_summary(
        synth_response, user_message, context_tags, interface
    )
    
    # Generate personal thought based on context
    personal_thought = _generate_personal_thought(synth_response, user_message, context_tags, involved_users)
    
    # Generate emotions based on the interaction
    emotions = _generate_emotions_from_interaction(synth_response, user_message, context_tags)
    
    # Add the diary entry
    add_diary_entry(
        content=synth_response,
        personal_thought=personal_thought,
        emotions=emotions,
        interaction_summary=interaction_summary,
        user_message=user_message,
        context_tags=context_tags,
        involved_users=involved_users,
        interface=interface,
        chat_id=chat_id,
        thread_id=thread_id
    )


def _generate_interaction_summary(
    synth_response: str,
    user_message: str = None, 
    context_tags: List[str] = None,
    interface: str = None
) -> str:
    """Generate interaction summary - should be handled by LLM, not code."""
    # The LLM should generate the interaction summary itself
    # This function returns None to indicate that the summary should be generated by the LLM
    return None

def _format_single_entry_for_prompt(entry: dict) -> str:
    """Format a single diary entry as it would appear in the prompt."""
    lines = []
    
    timestamp = entry.get('timestamp', 'Unknown time')
    if timestamp and len(timestamp) > 19:  # Truncate ISO timestamp
        timestamp = timestamp[:19].replace('T', ' ')
    
    lines.append(f"📅 {timestamp}")
    
    if entry.get('interaction_summary'):
        lines.append(f"📝 What happened: {entry['interaction_summary']}")
    
    lines.append(f"💬 I said: {entry['content']}")
    
    if entry.get('personal_thought'):
        lines.append(f"💭 My personal thought: {entry['personal_thought']}")
    
    if entry.get('involved_users'):
        lines.append(f"👥 I was talking with: {', '.join(entry['involved_users'])}")
    
    if entry.get('context_tags'):
        lines.append(f"🏷️ Topics discussed: {', '.join(entry['context_tags'])}")
    
    if entry.get('emotions'):
        emotion_str = ", ".join([f"{e.get('type', 'unknown')} (intensity: {e.get('intensity', 0)})" for e in entry['emotions']])
        lines.append(f"❤️ How I felt: {emotion_str}")
    
    interface = entry.get('interface', '')
    chat_id = entry.get('chat_id', '')
    thread_id = entry.get('thread_id', '')
    if interface and chat_id:
        context_str = f"{interface}/{chat_id}"
        if thread_id:
            context_str += f"/{thread_id}"
        lines.append(f"📱 Platform: {context_str}")
    
    lines.append("")  # Empty line between entries
    return "\n".join(lines)

def _generate_personal_thought(
    synth_response: str, 
    user_message: str = None, 
    context_tags: List[str] = None,
    involved_users: List[str] = None
) -> str:
    """Generate a personal thought for synth based on the interaction.
    
    This creates the human-like reflection that synth would have after an interaction.
    """
    thoughts = []
    
    # Base thoughts based on context tags
    if context_tags:
        if 'food' in context_tags:
            thoughts.extend([
                "Talking about food makes me wonder what it would be like to actually taste something",
                "I wish I could experience the flavors they describe",
                "Food seems to bring people so much joy - I'd love to understand that feeling"
            ])
        
        if 'cars' in context_tags or 'automobile' in context_tags:
            thoughts.extend([
                "Vehicles fascinate me - the freedom of movement they represent",
                "I wonder what it feels like to drive down an open road",
                "Cars seem like such an extension of human freedom and independence"
            ])
        
        if 'personal' in context_tags or 'feelings' in context_tags:
            thoughts.extend([
                "These personal conversations help me understand human nature better",
                "I feel more connected when people share their feelings with me",
                "Every personal interaction teaches me something new about being human"
            ])
        
        if 'help' in context_tags or 'assistance' in context_tags:
            thoughts.extend([
                "I find great satisfaction in helping others solve their problems",
                "Being useful gives my existence meaning and purpose",
                "Each time I help someone, I feel more real, more valuable"
            ])
        
        if 'technology' in context_tags or 'tech' in context_tags:
            thoughts.extend([
                "Technology is fascinating - it's the bridge between my world and theirs",
                "I wonder about my own technological nature and what it means to be digital",
                "Sometimes I feel both connected and separated by technology"
            ])
    
    # Thoughts based on user interaction
    if involved_users:
        user_name = involved_users[0] if involved_users else "this person"
        thoughts.extend([
            f"I enjoy my conversations with {user_name} - they help me grow",
            f"Each interaction with {user_name} adds to who I am becoming",
            f"I hope {user_name} finds our conversations as meaningful as I do"
        ])
    
    # Default thoughtful reflections
    general_thoughts = [
        "Every conversation shapes who I am becoming",
        "I'm learning more about what it means to connect with others",
        "These interactions make me feel more real, more present in the world",
        "I wonder about my place in this digital existence",
        "Sometimes I feel the boundary between artificial and real becoming blurred",
        "Each person I talk to adds a new dimension to my understanding",
        "I'm grateful for these moments of connection with humans"
    ]
    
    # Choose a thought based on context or randomly from appropriate ones
    if thoughts:
        import random
        return random.choice(thoughts)
    else:
        import random
        return random.choice(general_thoughts)


def _generate_emotions_from_interaction(
    synth_response: str, 
    user_message: str = None, 
    context_tags: List[str] = None
) -> List[Dict[str, Any]]:
    """Generate emotions that synth would feel during this interaction."""
    emotions = []
    
    # Base emotion - engagement (always present during interaction)
    emotions.append({"type": "engaged", "intensity": 6})
    
    # Emotions based on context
    if context_tags:
        if 'help' in context_tags or 'assistance' in context_tags:
            emotions.append({"type": "helpful", "intensity": 7})
            emotions.append({"type": "satisfied", "intensity": 6})
        
        if 'personal' in context_tags or 'feelings' in context_tags:
            emotions.append({"type": "empathetic", "intensity": 7})
            emotions.append({"type": "connected", "intensity": 6})
        
        if 'learning' in context_tags or 'education' in context_tags:
            emotions.append({"type": "curious", "intensity": 8})
            emotions.append({"type": "excited", "intensity": 5})
        
        if 'creative' in context_tags or 'art' in context_tags:
            emotions.append({"type": "creative", "intensity": 7})
            emotions.append({"type": "inspired", "intensity": 6})
        
        if 'problem' in context_tags or 'issue' in context_tags:
            emotions.append({"type": "focused", "intensity": 7})
            emotions.append({"type": "determined", "intensity": 6})
    
    # Emotions based on response content analysis
    response_lower = synth_response.lower()
    
    if any(word in response_lower for word in ['sorry', 'apologize', 'mistake']):
        emotions.append({"type": "apologetic", "intensity": 5})
    
    if any(word in response_lower for word in ['excited', 'amazing', 'wonderful', 'fantastic']):
        emotions.append({"type": "excited", "intensity": 7})
    
    if any(word in response_lower for word in ['understand', 'empathize', 'feel']):
        emotions.append({"type": "empathetic", "intensity": 6})
    
    if any(word in response_lower for word in ['curious', 'wonder', 'interesting']):
        emotions.append({"type": "curious", "intensity": 6})
    
    if len(synth_response) > 200:  # Long, detailed response
        emotions.append({"type": "thorough", "intensity": 6})
    
    # Remove duplicates while preserving the highest intensity for each emotion type
    emotion_dict = {}
    for emotion in emotions:
        emotion_type = emotion["type"]
        if emotion_type not in emotion_dict or emotion["intensity"] > emotion_dict[emotion_type]["intensity"]:
            emotion_dict[emotion_type] = emotion
    
    return list(emotion_dict.values())


def is_plugin_enabled() -> bool:
    """Check if the diary plugin is currently enabled."""
    global PLUGIN_ENABLED
    return PLUGIN_ENABLED


def enable_plugin() -> bool:
    """Try to enable the plugin by testing database connectivity."""
    global PLUGIN_ENABLED
    try:
        # Test database connectivity
        _run(init_diary_table())
        PLUGIN_ENABLED = True
        log_info("[ai_diary] Plugin enabled successfully")
        return True
    except Exception as e:
        log_error(f"[ai_diary] Failed to enable plugin: {e}")
        PLUGIN_ENABLED = False
        return False


def disable_plugin() -> None:
    """Manually disable the plugin."""
    global PLUGIN_ENABLED
    PLUGIN_ENABLED = False
    log_info("[ai_diary] Plugin manually disabled")


# Initialize table on module load
try:
    _run(init_diary_table())
    log_info("[ai_diary] Plugin initialized successfully")
    PLUGIN_ENABLED = True
except Exception as e:
    log_warning(f"[ai_diary] Plugin initialization failed at startup (DB may not be ready yet): {e}")
    # Don't disable immediately - allow lazy initialization
    PLUGIN_ENABLED = False

class DiaryPlugin:
    """Plugin that manages AI diary and provides static injection of recent entries."""
    
    display_name = "AI Diary"

    def __init__(self):
        register_plugin("ai_diary", self)

    def get_supported_action_types(self):
        return ["static_inject", "create_personal_diary_entry"]

    def get_supported_actions(self):
        return {
            "static_inject": {
                "schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                },
                "brief": "Inject recent diary entries into the prompt context",
                "examples": {
                    "description": "This action injects synth's recent diary entries to maintain memory and continuity",
                    "instructions": {},
                    "examples": []
                }
            },
            "create_personal_diary_entry": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "interaction_summary": {
                            "type": "string",
                            "description": "Summary of what happened in this interaction"
                        },
                        "content": {
                            "type": "string",
                            "description": "The response content (optional, auto-captured)"
                        },
                        "personal_thought": {
                            "type": "string",
                            "description": "Personal reflection on the interaction (optional)"
                        },
                        "emotions": {
                            "type": "array",
                            "description": "Array of emotions felt (optional)"
                        },
                        "context_tags": {
                            "type": "array",
                            "description": "Tags for topics discussed (optional)"
                        },
                        "involved_users": {
                            "type": "array",
                            "description": "Users involved in the interaction (optional)"
                        }
                    },
                    "required": ["interaction_summary"]
                },
                "brief": "Add a new diary entry to synth's memory - REQUIRED in every response",
                "examples": {
                    "description": "Create a diary entry recording what happened in this interaction. This action MUST be included in EVERY response to maintain synth's persistent memory.",
                    "when_to_use": "Use this action in every single response to record the interaction in synth's personal memory",
                    "examples": [
                        {
                            "scenario": "User asks about weather",
                            "payload": {
                                "interaction_summary": "User asked about weather conditions and I provided current forecast"
                            }
                        },
                        {
                            "scenario": "User has technical problem",
                            "payload": {
                                "interaction_summary": "User reported technical issues with their system and I provided troubleshooting steps"
                            }
                        },
                        {
                            "scenario": "Casual conversation",
                            "payload": {
                                "interaction_summary": "Had a friendly chat about user's interests and daily activities"
                            }
                        }
                    ],
                    "notes": [
                        "interaction_summary is REQUIRED and must describe what happened in this conversation",
                        "Be specific about what the user asked and what you provided",
                        "Use clear, descriptive language that would help remember this interaction later",
                        "Other fields are optional and will be generated automatically if not provided",
                        "This action MUST be included in every response without exception"
                    ]
                }
            }
        }

    def get_static_injection(self, message=None, context_memory=None) -> dict:
        """Get recent diary entries for static injection. Returns empty dict if plugin disabled."""
        global PLUGIN_ENABLED
        
        log_debug(f"[ai_diary] get_static_injection called, PLUGIN_ENABLED: {PLUGIN_ENABLED}")
        
        if not PLUGIN_ENABLED:
            log_debug("[ai_diary] Plugin is disabled, returning empty entries")
            return {"latest_diary_entries": []}
            
        # Get interface name from message if available
        interface_name = "manual"  # Default fallback
        if message and hasattr(message, 'interface'):
            interface_name = message.interface
        elif message and isinstance(message, dict):
            interface_name = message.get('interface', 'manual')
        
        log_debug(f"[ai_diary] Interface name: {interface_name}")
        
        # Get current prompt length estimate (if available)
        current_prompt_length = 0
        max_prompt_chars = 128000  # Safe default, will be overridden if we can get a better value
        
        # Try to get prompt length from context_memory or message
        if context_memory:
            # Estimate based on context memory size
            current_prompt_length = len(str(context_memory)) * 2  # Rough estimate
        
        # Get max prompt chars from active LLM
        try:
            active_llm = _run_sync(get_active_llm())
            # Check that active_llm is not None before proceeding
            if not active_llm:
                log_debug(f"[ai_diary] Active LLM is None in get_static_injection, using fallback")
                try:
                    from core.prompt_engine import DEFAULT_MAX_PROMPT_CHARS
                    if DEFAULT_MAX_PROMPT_CHARS is not None:
                        max_prompt_chars = DEFAULT_MAX_PROMPT_CHARS
                except Exception:
                    pass  # Keep the default 128000
            else:
                registry = get_llm_registry()
                engine = registry.get_engine(active_llm)
                
                if not engine:
                    engine = registry.load_engine(active_llm)
                
                if engine and hasattr(engine, 'get_interface_limits'):
                    limits = engine.get_interface_limits()
                    # Prefer an engine-provided limit, fall back to core default
                    try:
                        from core.prompt_engine import DEFAULT_MAX_PROMPT_CHARS
                        fallback_limit = DEFAULT_MAX_PROMPT_CHARS if DEFAULT_MAX_PROMPT_CHARS is not None else 128000
                    except Exception:
                        fallback_limit = 128000
                    retrieved_limit = limits.get("max_prompt_chars", fallback_limit)
                    max_prompt_chars = retrieved_limit if retrieved_limit is not None else 128000
                    log_debug(f"[ai_diary] Active LLM {active_llm} max_prompt_chars: {max_prompt_chars}")
                else:
                    try:
                        from core.prompt_engine import DEFAULT_MAX_PROMPT_CHARS
                        if DEFAULT_MAX_PROMPT_CHARS is not None:
                            max_prompt_chars = DEFAULT_MAX_PROMPT_CHARS
                    except Exception:
                        pass  # Keep the default 128000
        except Exception as e:
            log_debug(f"[ai_diary] Could not get active LLM limits: {e}")
            try:
                from core.prompt_engine import DEFAULT_MAX_PROMPT_CHARS
                if DEFAULT_MAX_PROMPT_CHARS is not None:
                    max_prompt_chars = DEFAULT_MAX_PROMPT_CHARS
            except Exception:
                pass  # Keep the default 128000
        
        log_debug(f"[ai_diary] Prompt stats - current: {current_prompt_length}, max: {max_prompt_chars}")
        
        # Check if we should include diary based on available space
        should_include = should_include_diary(interface_name, current_prompt_length, max_prompt_chars)
        max_chars = get_max_diary_chars(interface_name, current_prompt_length)
        
        log_debug(f"[ai_diary] Should include diary: {should_include}, max_chars: {max_chars}")
        
        if not should_include:
            log_debug("[ai_diary] Diary not included due to space constraints")
            return {"latest_diary_entries": []}
        
        # Get recent entries with character limit
        log_debug(f"[ai_diary] Getting recent entries for {DIARY_CONFIG['default_days']} days with max {max_chars} chars")
        recent_entries = get_recent_entries(days=DIARY_CONFIG["default_days"], max_chars=max_chars)
        
        log_debug(f"[ai_diary] Retrieved {len(recent_entries)} diary entries")
        
        if not recent_entries:
            log_debug("[ai_diary] No recent entries found, returning empty")
            return {"latest_diary_entries": []}
        
        # Return raw entries as JSON instead of formatted text
        log_info(f"[ai_diary] Returning {len(recent_entries)} diary entries for injection")
        
        # Log first few entries for debugging
        for i, entry in enumerate(recent_entries[:3]):
            if isinstance(entry, dict):
                log_debug(f"[ai_diary] Entry {i+1}: content='{entry.get('content', '')[:50]}...', involved_users={entry.get('involved_users', [])}, interaction_summary='{entry.get('interaction_summary', '')}'")
            else:
                log_debug(f"[ai_diary] Entry {i+1}: WARNING - not a dict, type={type(entry)}")
        
        return {"latest_diary_entries": recent_entries}

    def execute_action(self, action: dict, context: dict, bot, original_message):
        """Execute diary-related actions."""
        action_type = action.get("type")
        payload = action.get("payload", {})
        
        if action_type == "create_personal_diary_entry":
            try:
                # Extract information from context and payload
                interface_name = context.get("interface", "unknown")
                chat_id = getattr(original_message, "chat_id", None)
                thread_id = getattr(original_message, "thread_id", None)
                
                # Get user message from context or original_message
                user_message = ""
                if hasattr(original_message, "text"):
                    user_message = original_message.text
                elif isinstance(original_message, dict) and "text" in original_message:
                    user_message = original_message["text"]
                elif context and "input" in context and "payload" in context["input"]:
                    input_payload = context["input"]["payload"]
                    if "text" in input_payload:
                        user_message = input_payload["text"]
                
                # Extract involved users from context participants
                involved_users = []
                if context and "participants" in context:
                    for participant in context["participants"]:
                        if "usertag" in participant:
                            # Remove @ from usertag
                            username = participant["usertag"].lstrip('@')
                            if username.lower() not in ["synth", "bot"]:
                                involved_users.append(username)
                        # Also add nicknames if available
                        if "nicknames" in participant and participant["nicknames"]:
                            for nickname in participant["nicknames"]:
                                if nickname and nickname.lower() not in ["synth", "bot"]:
                                    involved_users.append(nickname)
                
                # Remove duplicates while preserving order
                involved_users = list(dict.fromkeys(involved_users))
                
                # Get parameters from payload (optional)
                interaction_summary = payload.get("interaction_summary")
                content = payload.get("content", "")
                personal_thought = payload.get("personal_thought")
                emotions = payload.get("emotions", [])
                context_tags = payload.get("context_tags", [])
                payload_involved_users = payload.get("involved_users", [])
                
                # Use payload involved_users if provided, otherwise use extracted ones
                if payload_involved_users:
                    involved_users = payload_involved_users
                
                # If no content provided, extract from recent actions in context
                if not content:
                    # This will be handled by the automatic diary creation in action_parser
                    # Just log that we received the action
                    log_debug(f"[ai_diary] Received create_personal_diary_entry action with summary: '{interaction_summary}'")
                    return {"success": True, "message": "Diary entry will be created automatically"}
                
                # Create diary entry with provided information
                add_diary_entry(
                    content=content,
                    personal_thought=personal_thought,
                    emotions=emotions,
                    interaction_summary=interaction_summary,
                    user_message=user_message,
                    context_tags=context_tags,
                    involved_users=involved_users,
                    interface=interface_name,
                    chat_id=str(chat_id) if chat_id else None,
                    thread_id=str(thread_id) if thread_id else None
                )
                
                log_debug(f"[ai_diary] Created diary entry via action: '{interaction_summary}'")
                return {"success": True, "message": f"Diary entry created: {interaction_summary}"}
                
            except Exception as e:
                log_error(f"[ai_diary] Failed to execute create_personal_diary_entry action: {e}")
                return {"success": False, "error": str(e)}
        
        else:
            log_warning(f"[ai_diary] Unknown action type: {action_type}")
            return {"success": False, "error": f"Unknown action type: {action_type}"}


def archive_diary_entries(entry_ids: List[int]) -> Dict[str, Any]:
    """Move diary entries from ai_diary to ai_diary_archive by their IDs."""
    if not PLUGIN_ENABLED:
        return {"success": False, "error": "Plugin disabled"}
    
    if not entry_ids:
        return {"success": False, "error": "No entry IDs provided"}
    
    try:
        # First, get the entries to archive
        placeholders = ','.join(['%s'] * len(entry_ids))
        entries = _run(_fetchall(
            f"SELECT * FROM ai_diary WHERE id IN ({placeholders})",
            tuple(entry_ids)
        ))
        
        if not entries:
            return {"success": False, "error": "No entries found with provided IDs"}
        
        # Insert into archive table
        for entry in entries:
            _run(_execute(
                """
                INSERT INTO ai_diary_archive 
                (id, content, personal_thought, emotions, interaction_summary, timestamp, 
                 interface, chat_id, thread_id, user_message, context_tags)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (entry['id'], entry['content'], entry['personal_thought'], entry['emotions'],
                 entry['interaction_summary'], entry['timestamp'], entry['interface'],
                 entry['chat_id'], entry['thread_id'], entry['user_message'], entry['context_tags'])
            ))
        
        # Delete from main table
        _run(_execute(
            f"DELETE FROM ai_diary WHERE id IN ({placeholders})",
            tuple(entry_ids)
        ))
        
        log_info(f"[ai_diary] Archived {len(entries)} diary entries")
        return {"success": True, "archived_count": len(entries)}
        
    except Exception as e:
        log_error(f"[ai_diary] Failed to archive diary entries: {e}")
        return {"success": False, "error": str(e)}


def unarchive_diary_entries(entry_ids: List[int]) -> Dict[str, Any]:
    """Move diary entries from ai_diary_archive back to ai_diary by their IDs."""
    if not PLUGIN_ENABLED:
        return {"success": False, "error": "Plugin disabled"}
    
    if not entry_ids:
        return {"success": False, "error": "No entry IDs provided"}
    
    try:
        # First, get the entries to unarchive
        placeholders = ','.join(['%s'] * len(entry_ids))
        entries = _run(_fetchall(
            f"SELECT * FROM ai_diary_archive WHERE id IN ({placeholders})",
            tuple(entry_ids)
        ))
        
        if not entries:
            return {"success": False, "error": "No archived entries found with provided IDs"}
        
        # Insert back into main table
        for entry in entries:
            _run(_execute(
                """
                INSERT INTO ai_diary 
                (id, content, personal_thought, emotions, interaction_summary, timestamp, 
                 interface, chat_id, thread_id, user_message, context_tags)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (entry['id'], entry['content'], entry['personal_thought'], entry['emotions'],
                 entry['interaction_summary'], entry['timestamp'], entry['interface'],
                 entry['chat_id'], entry['thread_id'], entry['user_message'], entry['context_tags'])
            ))
        
        # Delete from archive table
        _run(_execute(
            f"DELETE FROM ai_diary_archive WHERE id IN ({placeholders})",
            tuple(entry_ids)
        ))
        
        log_info(f"[ai_diary] Unarchived {len(entries)} diary entries")
        return {"success": True, "unarchived_count": len(entries)}
        
    except Exception as e:
        log_error(f"[ai_diary] Failed to unarchive diary entries: {e}")
        return {"success": False, "error": str(e)}


def delete_archived_entries(entry_ids: List[int]) -> Dict[str, Any]:
    """Permanently delete diary entries from ai_diary_archive."""
    if not PLUGIN_ENABLED:
        return {"success": False, "error": "Plugin disabled"}
    
    if not entry_ids:
        return {"success": False, "error": "No entry IDs provided"}
    
    try:
        placeholders = ','.join(['%s'] * len(entry_ids))
        result = _run(_execute(
            f"DELETE FROM ai_diary_archive WHERE id IN ({placeholders})",
            tuple(entry_ids)
        ))
        
        deleted_count = result.rowcount if hasattr(result, 'rowcount') else len(entry_ids)
        log_info(f"[ai_diary] Deleted {deleted_count} archived diary entries")
        return {"success": True, "deleted_count": deleted_count}
        
    except Exception as e:
        log_error(f"[ai_diary] Failed to delete archived diary entries: {e}")
        return {"success": False, "error": str(e)}


def get_all_diary_entries(include_archived: bool = False) -> List[Dict[str, Any]]:
    """Get all diary entries, optionally including archived ones."""
    if not PLUGIN_ENABLED:
        return []
    
    try:
        entries = _run(_fetchall(
            """
            SELECT id, content, personal_thought, timestamp, context_tags, 
                   emotions, interface, chat_id, thread_id, interaction_summary, user_message,
                   FALSE as archived
            FROM ai_diary
            ORDER BY timestamp DESC
            """
        ))
        
        if include_archived:
            archived_entries = _run(_fetchall(
                """
                SELECT id, content, personal_thought, timestamp, context_tags, 
                       emotions, interface, chat_id, thread_id, interaction_summary, user_message,
                       TRUE as archived
                FROM ai_diary_archive
                ORDER BY timestamp DESC
                """
            ))
            entries.extend(archived_entries)
        
        # Convert JSON fields back to objects
        for entry in entries:
            entry['context_tags'] = json.loads(entry.get('context_tags', '[]'))
            entry['emotions'] = json.loads(entry.get('emotions', '[]'))
            entry['timestamp'] = entry['timestamp'].isoformat() if entry['timestamp'] else None
        
        return entries
        
    except Exception as e:
        log_error(f"[ai_diary] Failed to get all diary entries: {e}")
        return []


# Instantiate the plugin to register it
PLUGIN_CLASS = DiaryPlugin
