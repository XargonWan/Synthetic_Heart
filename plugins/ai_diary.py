"""AI Personal Diary Plugin

This plugin manages synth's personal diary entries where synth records
what he says to users, his emotions, and his personal thoughts about interactions.
This creates a more human-like memory system where synth builds his persona
and remembers his relationships with users in a personal way.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, List, Dict
import asyncio
import aiomysql
from contextlib import asynccontextmanager

from core.config import get_active_cortex_engine
from core.core_initializer import register_plugin
from core.cortex_registry import get_cortex_registry
from core.db import get_conn_ctx, _get_db_type
from core.logging_utils import log_error, log_info, log_debug, log_warning

try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "DIARY_CONTEXT_MAX_CHARS",
        label="Diary Context Max Chars",
        default=8000,
        value_type=int,
        ui_type="number",
        description="Maximum number of diary characters allowed in prompt context after diary entries are trimmed.",
        scope="core",
        component="diary",
        advanced=True,
    )
except Exception:
    pass

# Injection priority for diary entries
INJECTION_PRIORITY = 8  # Low priority - diary is sacrificial


def register_injection_priority():
    """Register this component's injection priority."""
    log_info(f"[ai_diary] Registered injection priority: {INJECTION_PRIORITY}")
    return INJECTION_PRIORITY


# Register priority when module is loaded
register_injection_priority()

# Global flag to track if the plugin is enabled
PLUGIN_ENABLED = True

# Diary-specific configuration
DIARY_CONFIG = {
    "diary_injection_file": "synth_diary.json",
    "diary_injection_enabled": True,
    "diary_allocation_percentage": 30,  # Increased from 15% to utilize more available prompt space
    "max_static_injection_chars": 30000,  # Reduced for free ChatGPT limits
    "fallback_diary_chars": 10000,  # Reduced for free ChatGPT limits
    "default_days": 7,  # Default number of days to look back for diary entries
    "min_space_threshold": 0.75,  # Include diary only if we're using less than 75% of prompt space
    "diary_entry_structure": "auto",  # auto-select based on available space
    "diary_sort_order": "descending",  # newest first
    "diary_filter_strategy": "most_recent",  # strategy for selecting entries when space is limited
    "diary_tag_priority": ["important", "daily", "thoughts"],  # prioritize these tags
    "enable_diary_char_logging": True,  # Enhanced logging for debugging
}


def _build_json_array_membership_clause(
    column: str, values: List[str]
) -> tuple[list[str], list[Any]]:
    if _get_db_type() == "postgres":
        return (
            [f"COALESCE(NULLIF(BTRIM({column}), ''), '[]')::jsonb ? %s"] * len(values),
            list(values),
        )

    return (
        [f"JSON_CONTAINS({column}, %s)"] * len(values),
        [json.dumps(value) for value in values],
    )


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
        "manual": "manual",
    }

    normalized = interface_mapping.get(interface.lower(), interface.lower())
    return normalized


def get_max_diary_chars(
    interface_name: str = None,
    current_prompt_length: int = 0,
    context_memory: dict = None,
) -> int:
    """Calculate how many characters can be allocated to diary injection based on active LLM interface limits.

    Args:
        interface_name: Name of the interface
        current_prompt_length: Current length of the prompt
        context_memory: Context dictionary that may contain maximize_diary flag for memory-focused operations
    """
    try:
        # Get limits directly from the active LLM engine
        from core.config import get_active_cortex_engine
        from core.cortex_registry import get_cortex_registry
        import asyncio

        # Handle async get_active_cortex_engine call safely
        active_cortex = None
        try:
            # Try to get the event loop
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're in an async context, need to handle differently
                log_debug(
                    "[ai_diary] Already in async context, using sync fallback for get_active_cortex_engine"
                )
                # Use a simple fallback since we can't await here
                active_cortex = "manual"  # Safe fallback
            else:
                active_cortex = loop.run_until_complete(get_active_cortex_engine())
        except RuntimeError:
            # No event loop exists, create one
            try:
                active_cortex = asyncio.run(get_active_cortex_engine())
            except Exception as e:
                log_debug(f"[ai_diary] Could not get active Cortex engine: {e}")
                active_cortex = "manual"  # Safe fallback
        except Exception as e:
            log_debug(f"[ai_diary] Error in async handling: {e}")
            active_cortex = "manual"  # Safe fallback

        if not active_cortex or active_cortex == "manual":
            log_debug("[ai_diary] Using manual fallback limits")
            return 128001  # Safe fallback

        registry = get_cortex_registry()
        engine = registry.get_engine(active_cortex)

        if not engine:
            engine = registry.load_engine(active_cortex)

        # Get limits from the active engine
        max_prompt_chars = 128001  # Safe fallback default
        if engine and hasattr(engine, "get_interface_limits"):
            try:
                limits = engine.get_interface_limits()
                max_prompt_chars = limits.get("max_prompt_chars", 128001)
            except Exception:
                pass

        # Check if this is a memory-focused operation (e.g., Grillo memory consolidation beat)
        maximize_diary = False
        if context_memory and isinstance(context_memory, dict):
            maximize_diary = context_memory.get("maximize_diary", False)

        # Use 80% for memory consolidation, 30% for normal operations
        diary_percentage = 0.80 if maximize_diary else 0.30
        diary_limit = int(max_prompt_chars * diary_percentage)

        # Consider current prompt length
        available_space = max_prompt_chars - current_prompt_length
        diary_allocation = min(
            diary_limit, max(available_space * (0.9 if maximize_diary else 0.5), 5000)
        )  # Higher % when maximizing

        mode = "MAXIMIZED (80%)" if maximize_diary else "standard (30%)"
        log_info(
            f"[ai_diary] Diary allocation {mode}: {diary_allocation} chars (max: {max_prompt_chars}, used: {current_prompt_length})"
        )
        return max(diary_allocation, 5000)  # Minimum 5k chars
    except Exception as e:
        log_warning(f"[ai_diary] Error calculating diary limit: {e}")
        return 8001  # Fallback


async def _run_sync_async(coro):
    """Run async function, handling all cases without creating new event loops."""
    try:
        # Get current running loop if available
        asyncio.get_running_loop()
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


def should_include_diary(
    interface_name: str, current_prompt_length: int = 0, max_prompt_chars: int = 0
) -> bool:
    """Determine if diary should be included based on available space."""
    # Try to get max_prompt_chars from active LLM if not provided
    if max_prompt_chars <= 0:
        try:
            active_cortex = _run_sync(get_active_cortex_engine())
            # Check that active_cortex is not None before proceeding
            if not active_cortex:
                log_debug(
                    "[ai_diary] Active Cortex engine is None, skipping limits lookup"
                )
                return True  # Conservative: include diary if we can't determine LLM

            registry = get_cortex_registry()
            engine = registry.get_engine(active_cortex)

            if not engine:
                engine = registry.load_engine(active_cortex)

            if engine and hasattr(engine, "get_max_prompt_chars"):
                max_prompt_chars = engine.get_max_prompt_chars()
                log_debug(
                    f"[ai_diary] Got max_prompt_chars from Cortex {active_cortex}: {max_prompt_chars}"
                )
        except Exception as e:
            log_debug(f"[ai_diary] Could not get Cortex limits: {e}")
            return True  # Conservative: include diary if we can't determine limits

    if max_prompt_chars <= 0:
        # No prompt limit info, use conservative approach
        return True

    usage_ratio = current_prompt_length / max_prompt_chars

    # Include diary if we're using less than threshold of available space
    should_include = usage_ratio < DIARY_CONFIG["min_space_threshold"]
    log_debug(
        f"[ai_diary] Prompt usage: {current_prompt_length}/{max_prompt_chars} ({usage_ratio:.2%}), include_diary: {should_include}"
    )
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
        await cursor.execute("""
            CREATE TABLE IF NOT EXISTS ai_diary (
                id INT AUTO_INCREMENT PRIMARY KEY,
                content TEXT NOT NULL COMMENT 'What synth said/did in the interaction',
                personal_thought TEXT COMMENT 'synth personal reflection about the interaction',
                emotions TEXT DEFAULT '[]' COMMENT 'synth emotions about this interaction',
                interaction_summary TEXT COMMENT 'Brief summary of what happened',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                interface VARCHAR(50),
                chat_id VARCHAR(255),
                thread_id VARCHAR(255),
                user_message TEXT COMMENT 'What the user said that triggered this response',
                context_tags TEXT DEFAULT '[]' COMMENT 'Tags about the context/topic',
                involved_users TEXT DEFAULT '[]' COMMENT 'JSON list of users involved in the interaction',
                INDEX idx_created_at (created_at),
                INDEX idx_interface_chat (interface, chat_id)
            )
        """)

        # Ensure involved_users column exists (migration for existing tables)
        try:
            await cursor.execute("""
                ALTER TABLE ai_diary ADD COLUMN involved_users TEXT DEFAULT '[]' COMMENT 'JSON list of users involved in the interaction'
            """)
            log_info("[ai_diary] Added missing involved_users column to ai_diary table")
        except Exception as e:
            # Column might already exist, that's fine
            if "Duplicate column name" not in str(e):
                log_debug(f"[ai_diary] Column migration check: {e}")

        # Legacy memories table (moved from core)
        await cursor.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INT AUTO_INCREMENT PRIMARY KEY,
                created_at DATETIME NOT NULL,
                content TEXT NOT NULL,
                author VARCHAR(100),
                source VARCHAR(100),
                tags TEXT,
                scope VARCHAR(50),
                emotion VARCHAR(50),
                intensity INT,
                emotion_state VARCHAR(50)
            )
        """)

        # emotion_diary is owned by plugins/emotion_manager.py. This bootstrap
        # DDL must stay identical to EmotionManager._ensure_table_exists so
        # whichever plugin initializes first creates the same schema — the old
        # variant here (id VARCHAR, intensity INT, no timestamp) truncated
        # float intensities to zero (see AGENTS.md §12).
        await cursor.execute("""
            CREATE TABLE IF NOT EXISTS emotion_diary (
                id INT AUTO_INCREMENT PRIMARY KEY,
                source VARCHAR(100),
                event VARCHAR(100),
                emotion VARCHAR(100),
                intensity FLOAT,
                state VARCHAR(100),
                trigger_condition VARCHAR(255),
                decision_logic TEXT,
                next_check DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_created_at (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # Archive table for archived diary entries
        await cursor.execute("""
            CREATE TABLE IF NOT EXISTS ai_diary_archive (
                id INT AUTO_INCREMENT PRIMARY KEY,
                content TEXT NOT NULL COMMENT 'What synth said/did in the interaction',
                personal_thought TEXT COMMENT 'synth personal reflection about the interaction',
                emotions TEXT DEFAULT '[]' COMMENT 'synth emotions about this interaction',
                interaction_summary TEXT COMMENT 'Brief summary of what happened',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                interface VARCHAR(50),
                chat_id VARCHAR(255),
                thread_id VARCHAR(255),
                user_message TEXT COMMENT 'What the user said that triggered this response',
                context_tags TEXT DEFAULT '[]' COMMENT 'Tags about the context/topic',
                involved_users TEXT DEFAULT '[]' COMMENT 'JSON list of users involved in the interaction',
                INDEX idx_created_at (created_at),
                INDEX idx_interface_chat (interface, chat_id)
            )
        """)

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
        await cursor.execute("""
            CREATE TABLE ai_diary (
                id INT AUTO_INCREMENT PRIMARY KEY,
                content TEXT NOT NULL COMMENT 'What synth said/did in the interaction',
                personal_thought TEXT COMMENT 'synth personal reflection about the interaction',
                emotions TEXT DEFAULT '[]' COMMENT 'synth emotions about this interaction',
                interaction_summary TEXT COMMENT 'Brief summary of what happened',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                interface VARCHAR(50),
                chat_id VARCHAR(255),
                thread_id VARCHAR(255),
                user_message TEXT COMMENT 'What the user said that triggered this response',
                context_tags TEXT DEFAULT '[]' COMMENT 'Tags about the context/topic',
                INDEX idx_created_at (created_at),
                INDEX idx_interface_chat (interface, chat_id)
            )
        """)

        await conn.commit()
        log_info(
            "[ai_diary] ai_diary table recreated with new personal diary structure"
        )


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


async def _execute(query: str, params: tuple = ()):
    """Execute a database query and return the cursor.

    Returning the cursor allows callers to read `lastrowid` and `rowcount`.
    """
    async with get_db() as conn:
        cursor = await conn.cursor()
        await cursor.execute(query, params)
        await conn.commit()
        return cursor


async def _fetchall(query: str, params: tuple = ()) -> List[Dict]:
    """Fetch all results from a database query as plain mutable dicts.

    The Postgres compat cursor yields immutable asyncpg ``Record`` rows even
    when a DictCursor is requested; callers mutate JSON fields in place, so
    every row is copied into a real dict here.
    """
    async with get_db() as conn:
        cursor = await conn.cursor(aiomysql.DictCursor)
        await cursor.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


def _parse_json_list(value: Any) -> list:
    """Parse a JSON-list column value defensively (handles None, str, list)."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _normalize_emotions(emotions: Any) -> list[dict[str, Any]]:
    """Coerce assorted emotion shapes into ``[{"type": str, "intensity": num}]``.

    Small local models emit emotions inconsistently — as a ``{name: intensity}``
    map (the ``update_emotion_state`` shape), a list of names, or the canonical
    list of dicts. Normalise them all so diary entries actually capture emotions
    instead of dropping them with an "Invalid emotion format" warning.
    """
    if not emotions:
        return []

    def _intensity(value: Any) -> Any:
        return (
            value
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else None
        )

    normalized: list[dict[str, Any]] = []

    if isinstance(emotions, dict):
        for name, intensity in emotions.items():
            if not name:
                continue
            entry: dict[str, Any] = {"type": str(name)}
            if _intensity(intensity) is not None:
                entry["intensity"] = intensity
            normalized.append(entry)
        return normalized

    if isinstance(emotions, (list, tuple)):
        for item in emotions:
            if isinstance(item, dict):
                etype = item.get("type") or item.get("emotion") or item.get("name")
                if not etype:
                    continue
                entry = {"type": str(etype)}
                if _intensity(item.get("intensity")) is not None:
                    entry["intensity"] = item["intensity"]
                normalized.append(entry)
            elif isinstance(item, str) and item.strip():
                normalized.append({"type": item.strip()})
        return normalized

    return []


def _isoformat_timestamp(value: Any) -> Any:
    """Convert a datetime column value to ISO text, passing through others."""
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else value


def _merge_json_list(existing_json: str | None, new_items: list) -> list:
    """Merge a JSON-encoded list with new_items, preserving order and deduplicating strings."""
    try:
        existing: list = json.loads(existing_json) if existing_json else []
    except Exception:
        existing = []
    seen: set = {str(item) for item in existing}
    combined = list(existing)
    for item in new_items or []:
        if str(item) not in seen:
            combined.append(item)
            seen.add(str(item))
    return combined


async def _get_user_message_column_limit(cursor: Any) -> int:
    """Discover ai_diary.user_message max length from INFORMATION_SCHEMA.

    Falls back to a conservative legacy-safe limit if schema metadata is unavailable.
    """
    try:
        await cursor.execute(
            """
            SELECT DATA_TYPE, CHARACTER_MAXIMUM_LENGTH
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'ai_diary'
              AND COLUMN_NAME = 'user_message'
            LIMIT 1
            """
        )
        row = await cursor.fetchone()
        if not row:
            return 255

        data_type: str | None = None
        if isinstance(row, dict):
            data_type = str(row.get("DATA_TYPE") or "").lower()
            raw_len = row.get("CHARACTER_MAXIMUM_LENGTH")
        else:
            data_type = str(row[0] or "").lower()
            raw_len = row[1]

        if data_type in {"text", "mediumtext", "longtext"}:
            return 65535

        if raw_len is not None:
            return max(64, int(raw_len))
    except Exception:
        pass

    return 255


def _clip_for_column(text: str | None, max_len: int) -> str | None:
    """Trim text to fit a VARCHAR/TEXT column while preserving context."""
    if text is None:
        return None
    if len(text) <= max_len:
        return text
    if max_len <= 16:
        return text[:max_len]
    suffix = "\n...[truncated]"
    keep = max_len - len(suffix)
    if keep <= 0:
        return text[:max_len]
    return f"{text[:keep]}{suffix}"


def _normalize_diary_origin_value(value: Any) -> str | None:
    """Normalize optional diary origin metadata into a comparable string."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "null"}:
        return None
    return text


def _merge_diary_interface(existing: Any, incoming: Any) -> str | None:
    """Prefer meaningful external interfaces over placeholders/internal ones."""
    internal_interfaces = {"unknown", "grillo", "diary_merge"}
    existing_text = _normalize_diary_origin_value(existing)
    incoming_text = _normalize_diary_origin_value(incoming)
    if incoming_text and incoming_text not in internal_interfaces:
        return incoming_text
    if existing_text and existing_text not in internal_interfaces:
        return existing_text
    return incoming_text or existing_text


def _merge_diary_chat_id(existing: Any, incoming: Any) -> str | None:
    """Prefer real chat ids over internal sentinel ids when merging a day row."""
    existing_text = _normalize_diary_origin_value(existing)
    incoming_text = _normalize_diary_origin_value(incoming)
    if incoming_text and incoming_text != "-1":
        return incoming_text
    if existing_text and existing_text != "-1":
        return existing_text
    return incoming_text or existing_text


def _merge_diary_thread_id(existing: Any, incoming: Any) -> str | None:
    """Fill thread ids when available without blanking an existing value."""
    return _normalize_diary_origin_value(incoming) or _normalize_diary_origin_value(
        existing
    )


def _is_user_message_overflow_error(exc: Exception) -> bool:
    """Check whether DB error corresponds to ai_diary.user_message overflow."""
    msg = str(exc).lower()
    return "data too long for column 'user_message'" in msg


async def _upsert_diary_impl(
    content: str,
    personal_thought: str | None,
    emotions: list,
    interaction_summary: str | None,
    user_message: str | None,
    context_tags: list,
    involved_users: list,
    interface: str | None,
    chat_id: str | None,
    thread_id: str | None,
) -> int | None:
    """Core upsert: one diary row per calendar day, updated on every new entry.

    If a row already exists for today (by server date) the new content is appended
    to the existing row using a '---' separator.  JSON list fields (emotions,
    context_tags, involved_users) are merged and deduplicated.  The row's timestamp
    is updated to NOW() so it reflects the last-modified time.

    Returns the diary entry id (existing or newly inserted), or None on error.
    """
    _SEP = "\n\n---\n\n"
    try:
        async with get_db() as conn:
            cursor = await conn.cursor()
            user_message_limit = await _get_user_message_column_limit(cursor)
            # Look for today's entry
            await cursor.execute(
                "SELECT id, content, personal_thought, interaction_summary, "
                "user_message, emotions, context_tags, involved_users, "
                "interface, chat_id, thread_id "
                "FROM ai_diary WHERE DATE(created_at) = CURDATE() "
                "ORDER BY created_at DESC LIMIT 1"
            )
            existing = await cursor.fetchone()
            if existing:
                (
                    ex_id,
                    ex_content,
                    ex_thought,
                    ex_summary,
                    ex_user_msg,
                    ex_emotions,
                    ex_tags,
                    ex_involved,
                    ex_interface,
                    ex_chat_id,
                    ex_thread_id,
                ) = existing
                merged_content = (
                    f"{ex_content}{_SEP}{content}" if ex_content else content
                )
                merged_thought = (
                    f"{ex_thought}{_SEP}{personal_thought}"
                    if ex_thought and personal_thought
                    else (personal_thought or ex_thought)
                )
                merged_summary = (
                    f"{ex_summary}\n---\n{interaction_summary}"
                    if ex_summary and interaction_summary
                    else (interaction_summary or ex_summary)
                )
                merged_user_msg = (
                    f"{ex_user_msg}\n---\n{user_message}"
                    if ex_user_msg and user_message
                    else (user_message or ex_user_msg)
                )
                merged_user_msg = _clip_for_column(merged_user_msg, user_message_limit)
                merged_interface = _merge_diary_interface(ex_interface, interface)
                merged_chat_id = _merge_diary_chat_id(ex_chat_id, chat_id)
                merged_thread_id = _merge_diary_thread_id(ex_thread_id, thread_id)
                update_sql = """
                    UPDATE ai_diary
                    SET content=%s, personal_thought=%s, interaction_summary=%s,
                        user_message=%s, emotions=%s, context_tags=%s,
                        involved_users=%s, interface=%s, chat_id=%s,
                        thread_id=%s, created_at=NOW()
                    WHERE id=%s
                    """
                update_params = (
                    merged_content,
                    merged_thought,
                    merged_summary,
                    merged_user_msg,
                    json.dumps(_merge_json_list(ex_emotions, emotions)),
                    json.dumps(_merge_json_list(ex_tags, context_tags)),
                    json.dumps(_merge_json_list(ex_involved, involved_users)),
                    merged_interface,
                    merged_chat_id,
                    merged_thread_id,
                    ex_id,
                )
                try:
                    await cursor.execute(update_sql, update_params)
                except Exception as e:
                    if not _is_user_message_overflow_error(e):
                        raise
                    # Legacy schemas can keep user_message as short VARCHAR.
                    hardened_params = list(update_params)
                    hardened_params[3] = _clip_for_column(merged_user_msg, 255)
                    await cursor.execute(update_sql, tuple(hardened_params))
                await conn.commit()
                log_debug(
                    f"[ai_diary] Updated today's diary entry id={ex_id}: {content[:50]}..."
                )
                return ex_id
            else:
                insert_sql = """
                    INSERT INTO ai_diary
                        (content, personal_thought, emotions, interaction_summary,
                         user_message, context_tags, involved_users,
                         interface, chat_id, thread_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """
                insert_params = (
                    content,
                    personal_thought,
                    json.dumps(emotions),
                    interaction_summary,
                    _clip_for_column(user_message, user_message_limit),
                    json.dumps(context_tags),
                    json.dumps(involved_users),
                    interface,
                    chat_id,
                    thread_id,
                )
                try:
                    await cursor.execute(insert_sql, insert_params)
                except Exception as e:
                    if not _is_user_message_overflow_error(e):
                        raise
                    hardened_params = list(insert_params)
                    hardened_params[4] = _clip_for_column(user_message, 255)
                    await cursor.execute(insert_sql, tuple(hardened_params))
                await conn.commit()
                diary_entry_id = cursor.lastrowid
                log_debug(
                    f"[ai_diary] Created new diary entry for today: {content[:50]}..."
                )
                return diary_entry_id
    except Exception as e:
        log_error(f"[ai_diary] _upsert_diary_impl failed: {e}")
        return None


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
    thread_id: str = None,
    grillo_activity_log_id: int = None,
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
            log_debug(
                f"[ai_diary] Lazy initialization failed (sync): {init_error}, attempting table creation..."
            )
            try:
                _run(
                    _execute("""
                    CREATE TABLE IF NOT EXISTS ai_diary (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
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
                )
                PLUGIN_ENABLED = True
                log_info(
                    "[ai_diary] Plugin table created and enabled successfully via lazy init (sync)"
                )
            except Exception as create_error:
                log_error(
                    f"[ai_diary] Failed to create table during lazy init (sync): {create_error}"
                )
                return

    if not PLUGIN_ENABLED:
        return

    if not content.strip():
        return

    emotions = _normalize_emotions(emotions)
    context_tags = context_tags or []
    involved_users = involved_users or []

    # Normalize interface name for consistency
    interface = normalize_interface_name(interface)

    try:
        diary_entry_id = _run(
            _upsert_diary_impl(
                content,
                personal_thought,
                emotions,
                interaction_summary,
                user_message,
                context_tags,
                involved_users,
                interface,
                chat_id,
                thread_id,
            )
        )

        if diary_entry_id is not None:
            log_debug(
                f"[ai_diary] Upserted today's diary entry id={diary_entry_id}: {content[:50]}..."
            )
        if personal_thought:
            log_debug(f"[ai_diary] Personal thought: {personal_thought[:50]}...")

        # Link to grillo activity log if this entry was created from a grillo beat
        if grillo_activity_log_id and diary_entry_id:
            try:
                import asyncio

                try:
                    from plugins.grillo.grillo_impl import GrilloPlugin
                except ImportError:
                    # Fallback if direct import fails (e.g. structure change)
                    from plugins.grillo_plugin import GrilloPlugin

                if GrilloPlugin:
                    asyncio.create_task(
                        GrilloPlugin.link_diary_entry_to_activity(
                            grillo_activity_log_id,
                            diary_entry_id,
                            response_text=content,
                        )
                    )
                    log_debug(
                        f"[ai_diary] Scheduled grillo activity link: activity_log={grillo_activity_log_id}, diary={diary_entry_id}"
                    )
                else:
                    log_warning("[ai_diary] GrilloPlugin not available for linking")
            except Exception as link_error:
                log_warning(f"[ai_diary] Failed to link grillo activity: {link_error}")
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
    thread_id: str = None,
    grillo_activity_log_id: int = None,
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
            log_debug(
                f"[ai_diary] Lazy initialization failed: {init_error}, attempting table creation..."
            )
            try:
                await _execute("""
                    CREATE TABLE IF NOT EXISTS ai_diary (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
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
                log_info(
                    "[ai_diary] Plugin table created and enabled successfully via lazy init"
                )
            except Exception as create_error:
                log_error(
                    f"[ai_diary] Failed to create table during lazy init: {create_error}"
                )
                return

    if not PLUGIN_ENABLED:
        return

    if not content.strip():
        return

    emotions = _normalize_emotions(emotions)
    context_tags = context_tags or []
    involved_users = involved_users or []

    # Normalize interface name for consistency
    interface = normalize_interface_name(interface)

    try:
        diary_entry_id = await _upsert_diary_impl(
            content,
            personal_thought,
            emotions,
            interaction_summary,
            user_message,
            context_tags,
            involved_users,
            interface,
            chat_id,
            thread_id,
        )

        if diary_entry_id is not None:
            log_debug(
                f"[ai_diary] Upserted today's diary entry id={diary_entry_id}: {content[:50]}..."
            )
        if personal_thought:
            log_debug(f"[ai_diary] Personal thought: {personal_thought[:50]}...")

        # Link to grillo activity log if this entry was created from a grillo beat
        if grillo_activity_log_id and diary_entry_id:
            try:
                from plugins.grillo_plugin import GrilloPlugin

                await GrilloPlugin.link_diary_entry_to_activity(
                    grillo_activity_log_id,
                    diary_entry_id,
                    response_text=content,
                )
                log_debug(
                    f"[ai_diary] Linked grillo activity: activity_log={grillo_activity_log_id}, diary={diary_entry_id}"
                )
            except Exception as link_error:
                log_warning(f"[ai_diary] Failed to link grillo activity: {link_error}")
    except Exception as e:
        log_error(f"[ai_diary] Failed to add diary entry: {e}")
        # Disable plugin if database is unavailable
        PLUGIN_ENABLED = False


async def get_recent_entries_async(
    days: int = 2, max_chars: int | None = None
) -> List[Dict[str, Any]]:
    """Get diary entries from the last N days, optionally limited by character count (async version).
    Returns list of dict entries with all database columns, empty list if plugin is disabled.
    Entries are ordered from most recent to oldest, and if max_chars is specified,
    older entries are discarded first to stay within the character limit."""
    global PLUGIN_ENABLED

    log_debug(
        f"[ai_diary] get_recent_entries_async called with days={days}, max_chars={max_chars}, PLUGIN_ENABLED={PLUGIN_ENABLED}"
    )

    # Attempt lazy initialization if plugin was disabled at startup
    if not PLUGIN_ENABLED:
        try:
            log_debug(
                "[ai_diary] Attempting lazy initialization for get_recent_entries_async..."
            )
            await _execute("SELECT 1 FROM ai_diary LIMIT 1")
            PLUGIN_ENABLED = True
            log_info(
                "[ai_diary] Plugin lazy-initialized successfully in get_recent_entries_async"
            )
        except Exception as init_error:
            log_debug(
                f"[ai_diary] Lazy initialization failed in get_recent_entries_async: {init_error}"
            )
            log_debug("[ai_diary] Plugin disabled, returning empty list")
            return []

    if not PLUGIN_ENABLED:
        log_debug("[ai_diary] Plugin disabled, returning empty list")
        return []

    try:
        cutoff_date = datetime.now() - timedelta(days=days)
        log_debug(f"[ai_diary] Looking for entries after {cutoff_date}")

        entries = await _fetchall(
            """
            SELECT id, content, personal_thought, created_at, context_tags, involved_users, 
                   emotions, interface, chat_id, thread_id, interaction_summary, user_message
            FROM ai_diary
            WHERE created_at >= %s
            ORDER BY created_at DESC
            """,
            (cutoff_date,),
        )

        log_debug(f"[ai_diary] Raw query returned {len(entries)} entries")

        # Convert JSON fields back to objects (defensively: columns may hold
        # NULL, malformed text, or already-decoded lists on JSON-typed schemas)
        for entry in entries:
            entry["context_tags"] = _parse_json_list(entry.get("context_tags"))
            entry["involved_users"] = _parse_json_list(entry.get("involved_users"))
            entry["emotions"] = _parse_json_list(entry.get("emotions"))
            entry["created_at"] = _isoformat_timestamp(entry.get("created_at"))

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
                    log_debug(
                        f"[ai_diary] Entry {i + 1} size: {entry_size} chars, id: {entry.get('id')}"
                    )

                # If adding this entry would exceed the limit, stop here
                # Don't truncate individual entries, remove them entirely
                if total_chars + entry_size > max_chars:
                    log_debug(
                        f"[ai_diary] Stopping at {len(filtered_entries)} entries due to char limit ({total_chars}/{max_chars})"
                    )
                    log_debug(
                        f"[ai_diary] Entry {i + 1} would add {entry_size} chars, exceeding limit"
                    )
                    break

                filtered_entries.append(entry)
                total_chars += entry_size

            log_debug(
                f"[ai_diary] Filtered diary: {len(filtered_entries)}/{len(entries)} entries, {total_chars} chars"
            )
            return filtered_entries

        log_debug(f"[ai_diary] Returning all {len(entries)} entries (no char limit)")
        return entries

    except Exception as e:
        log_error(f"[ai_diary] Failed to get recent entries async: {e}")
        # Disable plugin if database is unavailable
        PLUGIN_ENABLED = False
        return []


def get_recent_entries(
    days: int = 2, max_chars: int | None = None
) -> List[Dict[str, Any]]:
    """Get diary entries from the last N days, optionally limited by character count (sync wrapper)."""
    return _run(get_recent_entries_async(days=days, max_chars=max_chars))


def get_entries_by_tags(tags: List[str], limit: int = 10) -> List[Dict[str, Any]]:
    """Get diary entries that contain any of the specified context tags."""
    try:
        # Create OR conditions for tag matching
        tag_conditions, params = _build_json_array_membership_clause(
            "context_tags", tags
        )

        if not tag_conditions:
            return []

        query = f"""
            SELECT id, content, personal_thought, created_at, context_tags, involved_users, 
                   emotions, interface, chat_id, thread_id, interaction_summary, user_message
            FROM ai_diary
            WHERE {" OR ".join(tag_conditions)}
            ORDER BY created_at DESC
            LIMIT %s
        """
        params.append(limit)

        entries = _run(_fetchall(query, tuple(params)))

        # Convert JSON fields back to objects (defensively: columns may hold
        # NULL, malformed text, or already-decoded lists on JSON-typed schemas)
        for entry in entries:
            entry["context_tags"] = _parse_json_list(entry.get("context_tags"))
            entry["involved_users"] = _parse_json_list(entry.get("involved_users"))
            entry["emotions"] = _parse_json_list(entry.get("emotions"))
            entry["created_at"] = _isoformat_timestamp(entry.get("created_at"))

        return entries

    except Exception as e:
        log_error(f"[ai_diary] Failed to get entries by tags: {e}")
        return []


def get_entries_with_person(person: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Get diary entries that involve a specific person."""
    try:
        person_conditions, person_params = _build_json_array_membership_clause(
            "involved_users", [person]
        )
        entries = _run(
            _fetchall(
                """
            SELECT id, content, personal_thought, created_at, context_tags, involved_users, 
                   emotions, interface, chat_id, thread_id, interaction_summary, user_message
            FROM ai_diary
            WHERE """
                + " OR ".join(person_conditions)
                + """
            ORDER BY created_at DESC
            LIMIT %s
            """,
                tuple(person_params + [limit]),
            )
        )

        # Convert JSON fields back to objects (defensively: columns may hold
        # NULL, malformed text, or already-decoded lists on JSON-typed schemas)
        for entry in entries:
            entry["context_tags"] = _parse_json_list(entry.get("context_tags"))
            entry["involved_users"] = _parse_json_list(entry.get("involved_users"))
            entry["emotions"] = _parse_json_list(entry.get("emotions"))
            entry["created_at"] = _isoformat_timestamp(entry.get("created_at"))

        return entries

    except Exception as e:
        log_error(f"[ai_diary] Failed to get entries with person {person}: {e}")
        return []


def format_diary_for_injection(entries: List[Dict[str, Any]]) -> str:
    """Format diary entries for static injection into prompts as synth's personal memories."""
    if not entries:
        return ""

    formatted_lines = ["=== synth's Personal Diary ==="]
    formatted_lines.append(
        "(This diary contains my past interactions and thoughts from previous conversations)"
    )
    formatted_lines.append(
        "(Use this information only as contextual reference when relevant, not as a continuation of the current conversation)"
    )
    formatted_lines.append("")

    for entry in entries:
        # Use the same formatting function as the character counting
        entry_text = _format_single_entry_for_prompt(entry)
        formatted_lines.append(entry_text)

    formatted_lines.append("=== End of My Diary ===")
    formatted_lines.append(
        "(Reference these memories only when they provide useful context for the current interaction)"
    )
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
        count_result = _run(
            _fetchall(
                "SELECT COUNT(*) as count FROM ai_diary WHERE created_at < %s",
                (cutoff_date,),
            )
        )
        count = count_result[0]["count"] if count_result else 0

        # Delete old entries
        _run(_execute("DELETE FROM ai_diary WHERE created_at < %s", (cutoff_date,)))

        log_info(f"[ai_diary] Cleaned up {count} old diary entries")
        return count

    except Exception as e:
        log_error(f"[ai_diary] Failed to cleanup old entries: {e}")
        PLUGIN_ENABLED = False
        return 0


def create_personal_diary_entry(
    synth_response: str,
    user_message: str | None = None,
    context_tags: List[str] | None = None,
    involved_users: List[str] | None = None,
    interface: str | None = None,
    chat_id: str | None = None,
    thread_id: str | None = None,
    grillo_activity_log_id: int | None = None,
    interaction_summary: str | None = None,
    personal_thought: str | None = None,
    emotions: List[Dict[str, Any]] | None = None,
) -> None:
    """Helper function to create a complete personal diary entry.

    This function should be called every time synth responds to a user.
    Thought/emotions/summary are expected from LLM action payload.

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

    # No hardcoded generation: persist only what the LLM explicitly provides.
    if emotions is None:
        emotions = []

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
        thread_id=thread_id,
        grillo_activity_log_id=grillo_activity_log_id,
    )


def _format_single_entry_for_prompt(entry: dict) -> str:
    """Format a single diary entry as it would appear in the prompt."""
    lines = []

    timestamp = entry.get("created_at", "Unknown time")
    if timestamp and len(timestamp) > 19:  # Truncate ISO timestamp
        timestamp = timestamp[:19].replace("T", " ")

    lines.append(f"📅 {timestamp}")

    if entry.get("interaction_summary"):
        lines.append(f"📝 What happened: {entry['interaction_summary']}")

    lines.append(f"💬 I said: {entry['content']}")

    if entry.get("personal_thought"):
        lines.append(f"💭 My personal thought: {entry['personal_thought']}")

    if entry.get("involved_users"):
        lines.append(f"👥 I was talking with: {', '.join(entry['involved_users'])}")

    if entry.get("context_tags"):
        lines.append(f"🏷️ Topics discussed: {', '.join(entry['context_tags'])}")

    if entry.get("emotions"):
        emotion_str = ", ".join(
            [
                f"{e.get('type', 'unknown')} (intensity: {e.get('intensity', 0)})"
                for e in entry["emotions"]
            ]
        )
        lines.append(f"❤️ How I felt: {emotion_str}")

    interface = entry.get("interface", "")
    chat_id = entry.get("chat_id", "")
    thread_id = entry.get("thread_id", "")
    if interface and chat_id:
        context_str = f"{interface}/{chat_id}"
        if thread_id:
            context_str += f"/{thread_id}"
        lines.append(f"📱 Platform: {context_str}")

    lines.append("")  # Empty line between entries
    return "\n".join(lines)


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
    log_warning(
        f"[ai_diary] Plugin initialization failed at startup (DB may not be ready yet): {e}"
    )
    # Don't disable immediately - allow lazy initialization
    PLUGIN_ENABLED = False


class DiaryPlugin:
    """Plugin that manages AI diary and provides static injection of recent entries."""

    display_name = "AI Diary"

    def __init__(self):
        register_plugin("ai_diary", self)

    def get_supported_action_types(self):
        return ["static_inject", "create_personal_diary_entry", "update_diary_entry"]

    async def get_history_contributions(self, **kwargs):
        """Provide diary entries as a history contribution for the core HistoryEngine."""
        try:
            from core.history_types import HistoryContribution
            from core.config_manager import config_registry

            try:
                days = int(
                    config_registry.get_value("DIARY_HISTORY_DAYS", 2, value_type=int)
                )
            except Exception:
                days = 2

            # Cap total diary chars to prevent context bloat. The daily diary
            # blobs can grow to 50k+ chars as grillo appends all day's entries
            # with '---' separators. We enforce a hard budget here and further
            # truncate individual fields to avoid single entries consuming the
            # whole context window.
            try:
                diary_budget = int(
                    config_registry.get_value(
                        "DIARY_CONTEXT_MAX_CHARS", 8000, value_type=int
                    )
                )
            except Exception:
                diary_budget = 8000

            # Per-field character limit: keeps the most recent summary/thought
            # readable without dumping multi-page merged blobs into the prompt.
            per_field_limit = max(300, diary_budget // 4)

            raw_entries = await get_recent_entries_async(
                days=days, max_chars=diary_budget
            )

            # Truncate heavy text fields on each returned entry so the history
            # engine receives compact, LLM-digestible records rather than the
            # raw megablobs that accumulate over a full day.
            trimmed_entries = []
            for entry in raw_entries:
                if not isinstance(entry, dict):
                    trimmed_entries.append(entry)
                    continue
                e = dict(entry)
                for field in ("content", "interaction_summary", "personal_thought"):
                    val = e.get(field)
                    if isinstance(val, str) and len(val) > per_field_limit:
                        # Keep the most recent segment (last `per_field_limit` chars)
                        # since the blob is appended chronologically.
                        e[field] = "…" + val[-per_field_limit:]
                trimmed_entries.append(e)

            return [
                HistoryContribution(
                    name="ai_diary",
                    priority=INJECTION_PRIORITY,
                    entries=trimmed_entries,
                    enabled_var="ENABLE_AI_DIARY",
                )
            ]
        except Exception:
            return []

    def get_supported_actions(self):
        # Trainer name is resolved dynamically (config-driven, never hardcoded) so
        # the diary examples model first-person, named prose instead of detached
        # "the user" framing that small local models otherwise copy verbatim.
        try:
            from core.config import get_trainer_display_name

            trainer_name = get_trainer_display_name()
        except Exception:
            trainer_name = ""
        example_person = trainer_name or "my trainer"
        return {
            "static_inject": {
                "schema": {"type": "object", "properties": {}, "required": []},
                "brief": "Inject recent diary entries into the prompt context",
                "examples": {
                    "description": "This action injects synth's recent diary entries to maintain memory and continuity",
                    "instructions": {},
                    "examples": [],
                },
            },
            "create_personal_diary_entry": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "interaction_summary": {
                            "type": "string",
                            "description": "Summary of what happened in this interaction, written in YOUR OWN first-person voice as yourself and referring to people by name (never 'the user'). Do NOT include weather, temperature, or location data — that context is provided separately.",
                        },
                        "content": {
                            "type": "string",
                            "description": "The response content (optional, auto-captured)",
                        },
                        "personal_thought": {
                            "type": "string",
                            "description": "Personal reflection on the interaction (optional), written in first person as yourself. Focus on your emotions and the relationship dynamics, naming people directly rather than calling them 'the user'. Leave out environmental conditions.",
                        },
                        "emotions": {
                            "type": "array",
                            "description": 'Array of emotions with type and intensity (1-10). Format: [{"type": "emotion_name", "intensity": 7}]',
                            "items": {
                                "type": "object",
                                "properties": {
                                    "type": {"type": "string", "example": "joy"},
                                    "intensity": {
                                        "type": "number",
                                        "example": 7,
                                        "minimum": 1,
                                        "maximum": 10,
                                    },
                                },
                            },
                        },
                        "context_tags": {
                            "type": "array",
                            "description": "Tags for topics discussed (optional)",
                            "items": {"type": "string"},
                        },
                        "involved_users": {
                            "type": "array",
                            "description": "Users involved in the interaction (optional)",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["interaction_summary"],
                },
                "brief": "Add a new diary entry to synth's memory - REQUIRED in every response. Never include weather/location data in summaries.",
                "examples": {
                    "description": "Create a diary entry recording what happened in this interaction. This action MUST be included in EVERY response to maintain synth's persistent memory.",
                    "when_to_use": "Use this action in every single response to record the interaction in synth's personal memory",
                    "examples": [
                        {
                            "scenario": "A question about the weather",
                            "payload": {
                                "interaction_summary": f"{example_person} asked me about the weather, so I shared the current forecast."
                            },
                        },
                        {
                            "scenario": "Helping with a technical problem",
                            "payload": {
                                "interaction_summary": f"{example_person} hit a technical snag and I walked them through fixing it."
                            },
                        },
                        {
                            "scenario": "Casual conversation",
                            "payload": {
                                "interaction_summary": f"{example_person} and I had a relaxed chat about how their day was going."
                            },
                        },
                    ],
                    "notes": [
                        "interaction_summary is REQUIRED and must describe what happened in this conversation",
                        "Write it in YOUR OWN first-person voice as yourself, referring to people by name (never 'the user')",
                        "Be specific about what was said and what you did, thought, or felt",
                        "Use clear, descriptive language that would help remember this interaction later",
                        "Other fields are optional and will be generated automatically if not provided",
                        "This action MUST be included in every response without exception",
                    ],
                },
            },
            "update_diary_entry": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "integer",
                            "description": "ID of the diary entry to update (provided in the consolidation prompt).",
                        },
                        "content": {
                            "type": "string",
                            "description": "Fully merged first-person prose that synthesises all of today's diary fragments.",
                        },
                    },
                    "required": ["id", "content"],
                },
                "brief": "Replace diary entry content with a synthesised version (internal — triggered by the daily consolidation beat only).",
            },
        }

    async def get_static_injection(self, message=None, context_memory=None) -> dict:
        """Get recent diary entries for static injection. Returns empty dict if plugin disabled."""
        global PLUGIN_ENABLED

        log_debug(
            f"[ai_diary] get_static_injection called, PLUGIN_ENABLED: {PLUGIN_ENABLED}"
        )

        if not PLUGIN_ENABLED:
            log_debug("[ai_diary] Plugin is disabled, returning empty entries")
            return {"latest_diary_entries": []}

        import time

        start = time.time()
        try:
            # Get diary history days from config_registry
            try:
                from core.config_manager import config_registry

                diary_days = int(
                    config_registry.get_value("DIARY_HISTORY_DAYS", 2, value_type=int)
                )
            except Exception as e:
                log_debug(
                    f"[ai_diary] Could not get DIARY_HISTORY_DAYS from config: {e}, using default 2"
                )
                diary_days = 2

            # Get recent entries with generous limit - prompt_engine will trim if needed
            log_debug(f"[ai_diary] Getting recent entries for {diary_days} days")

            # Let's call the internal fetcher directly if we can
            cutoff_date = datetime.now() - timedelta(days=diary_days)
            recent_entries = await _fetchall(
                """
                SELECT id, content, personal_thought, created_at, context_tags, involved_users, 
                       emotions, interface, chat_id, thread_id, interaction_summary, user_message
                FROM ai_diary
                WHERE created_at >= %s
                ORDER BY created_at DESC
                """,
                (cutoff_date,),
            )

            # Process entries
            for entry in recent_entries:
                entry["context_tags"] = _parse_json_list(entry.get("context_tags"))
                entry["involved_users"] = _parse_json_list(entry.get("involved_users"))
                entry["emotions"] = _parse_json_list(entry.get("emotions"))
                entry["created_at"] = _isoformat_timestamp(entry.get("created_at"))

            duration = time.time() - start
            if duration > 0.1:
                log_info(f"[ai_diary] get_static_injection took {duration:.3f}s")

            if recent_entries:
                log_info(
                    f"[ai_diary] Returning {len(recent_entries)} diary entries for injection"
                )
            else:
                log_debug("[ai_diary] No recent entries found")

            # ALWAYS return latest_diary_entries key, even if empty
            return {"latest_diary_entries": recent_entries}

        except Exception as e:
            log_error(f"[ai_diary] Error in get_static_injection: {e}")
            # Return empty list, not empty dict - so the key is present
            return {"latest_diary_entries": []}

    async def execute_action(
        self, action: dict, context: dict, bot: Any, original_message: Any
    ) -> dict:
        """Execute diary-related actions.

        Async on purpose: the previous sync version bridged into the DB via
        `_run()`, blocking the event loop for up to 10 s per diary write.
        """
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
                            username = participant["usertag"].lstrip("@")
                            if username.lower() not in ["synth", "bot"]:
                                involved_users.append(username)
                        # Also add nicknames if available
                        if "nicknames" in participant and participant["nicknames"]:
                            for nickname in participant["nicknames"]:
                                if nickname and nickname.lower() not in [
                                    "synth",
                                    "bot",
                                ]:
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

                # Check if this diary entry is from a grillo beat
                grillo_activity_log_id = (
                    context.get("activity_log_id") if context else None
                )

                # If no content provided, extract from recent actions in context
                if not content:
                    # This will be handled by the automatic diary creation in action_parser
                    # Just log that we received the action
                    log_debug(
                        f"[ai_diary] Received create_personal_diary_entry action with summary: '{interaction_summary}'"
                    )
                    return {
                        "success": True,
                        "message": "Diary entry will be created automatically",
                    }

                # Create diary entry with provided information
                await add_diary_entry_async(
                    content=content,
                    personal_thought=personal_thought,
                    emotions=emotions,
                    interaction_summary=interaction_summary,
                    user_message=user_message,
                    context_tags=context_tags,
                    involved_users=involved_users,
                    interface=interface_name,
                    chat_id=str(chat_id) if chat_id else None,
                    thread_id=str(thread_id) if thread_id else None,
                    grillo_activity_log_id=grillo_activity_log_id,
                )

                log_debug(
                    f"[ai_diary] Created diary entry via action: '{interaction_summary}'"
                )
                return {
                    "success": True,
                    "message": f"Diary entry created: {interaction_summary}",
                }

            except Exception as e:
                log_error(
                    f"[ai_diary] Failed to execute create_personal_diary_entry action: {e}"
                )
                return {"success": False, "error": str(e)}

        elif action_type == "update_diary_entry":
            entry_id = payload.get("id")
            new_content = (payload.get("content") or "").strip()
            if not entry_id or not new_content:
                return {"success": False, "error": "id and content are required"}
            try:
                merge_timestamp = (
                    context.get("diary_merge_timestamp") if context else None
                )
                if isinstance(merge_timestamp, str):
                    try:
                        merge_timestamp = datetime.fromisoformat(
                            merge_timestamp.replace("Z", "+00:00")
                        )
                    except Exception:
                        merge_timestamp = None

                if merge_timestamp is not None:
                    await _execute(
                        "UPDATE ai_diary SET content=%s, created_at=%s WHERE id=%s",
                        (new_content, merge_timestamp, int(entry_id)),
                    )
                else:
                    await _execute(
                        "UPDATE ai_diary SET content=%s WHERE id=%s",
                        (new_content, int(entry_id)),
                    )

                # Collect stale entry ids: prefer context-provided source ids,
                # but also auto-discover any other rows sharing the same day
                # so the consolidator works even without merge_source_ids.
                stale_entry_ids: List[int] = []
                seen_ids: set[int] = {int(entry_id)}
                merged_source_ids = (
                    context.get("diary_merge_source_ids") if context else []
                )
                for raw_id in merged_source_ids or []:
                    try:
                        parsed_id = int(raw_id)
                    except Exception:
                        continue
                    if parsed_id in seen_ids:
                        continue
                    seen_ids.add(parsed_id)
                    stale_entry_ids.append(parsed_id)

                # Auto-discover additional stale rows for the same calendar day
                # (they are not referenced in merge_source_ids but linger as
                # separate fragments from the upsert-or-dedupe era).
                try:
                    extra_stale = await _fetchall(
                        """
                        SELECT id FROM ai_diary
                        WHERE DATE(created_at) = (
                            SELECT DATE(created_at) FROM ai_diary WHERE id = %s
                        )
                        AND id != %s
                        """,
                        (int(entry_id), int(entry_id)),
                    )
                    for row in extra_stale:
                        rid = row.get("id")
                        if rid is not None and rid not in seen_ids:
                            seen_ids.add(rid)
                            stale_entry_ids.append(rid)
                except Exception as e:
                    log_debug(f"[ai_diary] Auto-discovery of stale rows failed: {e}")

                if stale_entry_ids:
                    archive_result = await asyncio.to_thread(
                        archive_diary_entries, stale_entry_ids
                    )
                    if not archive_result.get("success"):
                        log_warning(
                            "[ai_diary] Consolidation archived row cleanup failed for "
                            f"entry {entry_id}: {archive_result.get('error')}"
                        )

                log_info(
                    f"[ai_diary] Diary entry {entry_id} consolidated to clean prose"
                    f" (archived {len(stale_entry_ids)} stale rows)"
                )
                return {"success": True, "message": f"Diary entry {entry_id} updated"}
            except Exception as e:
                log_error(f"[ai_diary] Failed to update diary entry {entry_id}: {e}")
                return {"success": False, "error": str(e)}

        else:
            log_warning(f"[ai_diary] Unknown action type: {action_type}")
            return {"success": False, "error": f"Unknown action type: {action_type}"}

    async def on_debrief(
        self,
        processed_actions: list,
        failed_actions: list,
        results: list,
        context: dict,
        original_message: object,
    ) -> None:
        """No-op: diary consolidation is now handled by GrilloDiaryConsolidatorPlugin.

        The ``diary_merge_beat`` guard is kept here to prevent recursive loops
        when the consolidation beat's own response goes through debrief.
        """
        if (context or {}).get("diary_merge_beat"):
            return


def archive_diary_entries(entry_ids: List[int]) -> Dict[str, Any]:
    """Move diary entries from ai_diary to ai_diary_archive by their IDs."""
    if not PLUGIN_ENABLED:
        return {"success": False, "error": "Plugin disabled"}

    if not entry_ids:
        return {"success": False, "error": "No entry IDs provided"}

    try:
        # First, get the entries to archive
        placeholders = ",".join(["%s"] * len(entry_ids))
        entries = _run(
            _fetchall(
                f"SELECT * FROM ai_diary WHERE id IN ({placeholders})", tuple(entry_ids)
            )
        )

        if not entries:
            return {"success": False, "error": "No entries found with provided IDs"}

        # Insert into archive table
        for entry in entries:
            _run(
                _execute(
                    """
                INSERT INTO ai_diary_archive 
                (id, content, personal_thought, emotions, interaction_summary, created_at, 
                 interface, chat_id, thread_id, user_message, context_tags)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                    (
                        entry["id"],
                        entry["content"],
                        entry["personal_thought"],
                        entry["emotions"],
                        entry["interaction_summary"],
                        entry["created_at"],
                        entry["interface"],
                        entry["chat_id"],
                        entry["thread_id"],
                        entry["user_message"],
                        entry["context_tags"],
                    ),
                )
            )

        # Delete from main table
        _run(
            _execute(
                f"DELETE FROM ai_diary WHERE id IN ({placeholders})", tuple(entry_ids)
            )
        )

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
        placeholders = ",".join(["%s"] * len(entry_ids))
        entries = _run(
            _fetchall(
                f"SELECT * FROM ai_diary_archive WHERE id IN ({placeholders})",
                tuple(entry_ids),
            )
        )

        if not entries:
            return {
                "success": False,
                "error": "No archived entries found with provided IDs",
            }

        # Insert back into main table
        for entry in entries:
            _run(
                _execute(
                    """
                INSERT INTO ai_diary 
                (id, content, personal_thought, emotions, interaction_summary, created_at, 
                 interface, chat_id, thread_id, user_message, context_tags)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                    (
                        entry["id"],
                        entry["content"],
                        entry["personal_thought"],
                        entry["emotions"],
                        entry["interaction_summary"],
                        entry["created_at"],
                        entry["interface"],
                        entry["chat_id"],
                        entry["thread_id"],
                        entry["user_message"],
                        entry["context_tags"],
                    ),
                )
            )

        # Delete from archive table
        _run(
            _execute(
                f"DELETE FROM ai_diary_archive WHERE id IN ({placeholders})",
                tuple(entry_ids),
            )
        )

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
        placeholders = ",".join(["%s"] * len(entry_ids))
        result = _run(
            _execute(
                f"DELETE FROM ai_diary_archive WHERE id IN ({placeholders})",
                tuple(entry_ids),
            )
        )

        deleted_count = (
            result.rowcount if hasattr(result, "rowcount") else len(entry_ids)
        )
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
        entries = _run(
            _fetchall(
                """
            SELECT id, content, personal_thought, created_at, context_tags, 
                   emotions, interface, chat_id, thread_id, interaction_summary, user_message,
                   FALSE as archived
            FROM ai_diary
            ORDER BY created_at DESC
            """
            )
        )

        if include_archived:
            archived_entries = _run(
                _fetchall(
                    """
                SELECT id, content, personal_thought, created_at, context_tags, 
                       emotions, interface, chat_id, thread_id, interaction_summary, user_message,
                       TRUE as archived
                FROM ai_diary_archive
                ORDER BY created_at DESC
                """
                )
            )
            entries.extend(archived_entries)

        # Convert JSON fields back to objects
        for entry in entries:
            entry["context_tags"] = _parse_json_list(entry.get("context_tags"))
            entry["emotions"] = _parse_json_list(entry.get("emotions"))
            entry["created_at"] = _isoformat_timestamp(entry.get("created_at"))

        return entries

    except Exception as e:
        log_error(f"[ai_diary] Failed to get all diary entries: {e}")
        return []


# Instantiate the plugin to register it with the core
try:
    _diary_plugin_instance = DiaryPlugin()
    log_info("[ai_diary] Plugin instance created and registered with core")
except Exception as e:
    log_error(f"[ai_diary] Failed to instantiate DiaryPlugin: {e}")

PLUGIN_CLASS = DiaryPlugin
