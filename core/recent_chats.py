from core.db import get_conn
from core.db import ensure_core_tables
import aiomysql
import time
import re
from core.logging_utils import log_debug, log_info, log_warning, log_error
import json
from pathlib import Path
from core.interfaces_registry import get_interface_registry
from core.abstract_context import AbstractContext
from typing import Union, Optional, Callable

MAX_ENTRIES = 100
_metadata = {}
chat_path_map = {}

_CHAT_MAP_PATH = Path(__file__).with_name("chat_paths.json")

def _save_chat_paths():
    try:
        with _CHAT_MAP_PATH.open("w", encoding="utf-8") as f:
            json.dump(chat_path_map, f)
        log_debug(f"[recent_chats] Saved chat path map with {len(chat_path_map)} entries")
    except Exception as e:  # pragma: no cover - best effort
        log_warning(f"[recent_chats] Failed to save chat path map: {e}")

if _CHAT_MAP_PATH.exists():
    try:
        with _CHAT_MAP_PATH.open("r", encoding="utf-8") as f:
            chat_path_map = {int(k): v for k, v in json.load(f).items()}
        log_debug(
            f"[recent_chats] Loaded chat path map with {len(chat_path_map)} entries"
        )
    except Exception as e:  # pragma: no cover - best effort
        log_warning(f"[recent_chats] Failed to load chat path map: {e}")

async def track_chat(chat_id: Union[int, str], interface_name: str, metadata=None):
    """
    Track a chat as recently active. This function is resilient to DB failures:
    if the database is unavailable or acquiring a connection times out, it will
    fall back to in-memory tracking and log a warning instead of raising.
    """
    now = time.time()

    # Try to persist to DB, but never raise to the caller if DB is unavailable.
    try:
        await ensure_core_tables()
        try:
            conn = await get_conn()
        except Exception as e:
            log_warning(f"[recent_chats] DB unavailable, skipping persistent tracking: {e}")
            # Still update in-memory metadata so commands depending on it work
            if metadata:
                _metadata[chat_id] = metadata
            return

        try:
            async with conn.cursor() as cur:
                # Convert chat_id to string to handle both int and str uniformly
                chat_id_str = str(chat_id)
                await cur.execute(
                    """
                    INSERT INTO recent_chats (chat_id, last_active)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE last_active = VALUES(last_active)
                    """,
                    (chat_id_str, now),
                )
                await conn.commit()
        except Exception as e:
            log_warning(f"[recent_chats] Failed to persist recent chat: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        # Ensure this function never raises due to DB issues
        log_warning(f"[recent_chats] Unexpected error in track_chat: {e}")

    # In-memory fallback/update
    if metadata:
        _metadata[chat_id] = metadata

async def reset_chat(chat_id: Union[int, str], interface_name: str):
    # Attempt to remove from persistent storage; if DB unavailable just remove in-memory
    try:
        await ensure_core_tables()
        try:
            conn = await get_conn()
        except Exception as e:
            log_warning(f"[recent_chats] DB unavailable, skipping persistent reset: {e}")
            _metadata.pop(chat_id, None)
            if chat_path_map.pop(chat_id, None) is not None:
                _save_chat_paths()
            return

        try:
            async with conn.cursor() as cur:
                chat_id_str = str(chat_id)
                await cur.execute("DELETE FROM recent_chats WHERE chat_id = %s", (chat_id_str,))
                await conn.commit()
        except Exception as e:
            log_warning(f"[recent_chats] Failed to reset chat in DB: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        log_warning(f"[recent_chats] Unexpected error in reset_chat: {e}")

    _metadata.pop(chat_id, None)
    if chat_path_map.pop(chat_id, None) is not None:
        _save_chat_paths()

def set_chat_path(chat_id: Union[int, str], chat_path: str) -> None:
    chat_path_map[chat_id] = chat_path
    _save_chat_paths()

def get_chat_path(chat_id: Union[int, str]) -> str | None:
    return chat_path_map.get(chat_id)

def clear_chat_path(chat_id: Union[int, str]) -> None:
    """Remove chat path mapping for the given chat_id."""
    if chat_id in chat_path_map:
        del chat_path_map[chat_id]
        _save_chat_paths()
        log_info(f"[recent_chats] Cleared chat path for chat_id: {chat_id}")
    else:
        log_debug(f"[recent_chats] No chat path found for chat_id: {chat_id}")

async def get_last_active_chats(n=10):
    try:
        conn = await get_conn()
    except Exception as e:
        log_warning(f"[recent_chats] DB unavailable, falling back to in-memory chats: {e}")
        # Best-effort fallback: return recently cached metadata keys
        try:
            keys = list(_metadata.keys())[:n]
            return keys
        except Exception:
            return []

    try:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """
                SELECT chat_id FROM recent_chats
                ORDER BY last_active DESC
                LIMIT %s
                """,
                (n,),
            )
            rows = await cur.fetchall()
            return [row["chat_id"] for row in rows]
    except Exception as e:
        log_warning(f"[recent_chats] Failed to fetch last active chats from DB: {e}")
        return list(_metadata.keys())[:n]
    finally:
        try:
            conn.close()
        except Exception:
            pass

def format_chat_entry_generic(chat_id: Union[int, str], chat_name: Optional[str] = None):
    """Generic format for chat entries."""
    name = chat_name or str(chat_id)
    safe_name = escape_markdown(name)
    return f"{safe_name} — `{chat_id}`"

async def last_chats_command(abstract_context: AbstractContext, reply_fn: Optional[Callable] = None, get_chat_info_fn: Optional[Callable] = None):
    """Last chats command that works with any interface."""
    if not abstract_context.is_trainer():
        return

    lines = ["\U0001f553 *Last active chats:*"]

    for chat_id in await get_last_active_chats():
        chat_name = None
        if get_chat_info_fn:
            try:
                chat_info = await get_chat_info_fn(chat_id)
                chat_name = chat_info.get('name') if chat_info else None
            except Exception as e:
                log_debug(f"Error retrieving chat {chat_id}: {e}")
        
        lines.append("- " + format_chat_entry_generic(chat_id, chat_name))

    response = "\n".join(lines)
    if reply_fn:
        await reply_fn(response)

async def get_last_active_chats_verbose(n=10, bot=None):
    chat_ids = await get_last_active_chats(n)
    results = []
    for chat_id in chat_ids:
        name = _metadata.get(chat_id)
        if bot and not name:
            try:
                chat = await bot.get_chat(chat_id)
                name = chat.title or chat.username or str(chat_id)
            except Exception:
                pass
        if not name:
            name = str(chat_id)
        results.append((chat_id, name))
    return results

def escape_markdown(text: str) -> str:
    """
    Escape Markdown v1 characters to avoid errors or malformed output.
    """
    escape_chars = r'\_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)
