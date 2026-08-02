from core.db import get_conn_ctx
from core.db import ensure_core_tables
import aiomysql
import time
import re
from core.logging_utils import log_debug, log_info, log_warning
import json
from pathlib import Path
from core.abstract_context import AbstractContext
from typing import Union, Optional, Callable

MAX_ENTRIES = 100
_metadata = {}
# Keyed by str(chat_id): chat ids may be ints (Telegram) or strings
# (webui sessions, UUIDs), and JSON persistence stringifies keys anyway.
chat_path_map: dict[str, str] = {}

_CHAT_MAP_PATH = Path(__file__).with_name("chat_paths.json")


def _save_chat_paths():
    try:
        with _CHAT_MAP_PATH.open("w", encoding="utf-8") as f:
            json.dump(chat_path_map, f)
        log_debug(
            f"[recent_chats] Saved chat path map with {len(chat_path_map)} entries"
        )
    except Exception as e:  # pragma: no cover - best effort
        log_warning(f"[recent_chats] Failed to save chat path map: {e}")


if _CHAT_MAP_PATH.exists():
    try:
        with _CHAT_MAP_PATH.open("r", encoding="utf-8") as f:
            # Keys stay strings: int(k) here would discard the whole map when
            # any non-numeric chat id (webui session, UUID) was persisted.
            chat_path_map = {str(k): v for k, v in json.load(f).items()}
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
        async with get_conn_ctx() as conn:
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
    except Exception as e:
        log_warning(f"[recent_chats] DB unavailable or error: {e}")

    # In-memory fallback/update
    if metadata:
        _metadata[chat_id] = metadata


async def reset_chat(chat_id: Union[int, str], interface_name: str):
    # Attempt to remove from persistent storage; if DB unavailable just remove in-memory
    try:
        await ensure_core_tables()
        async with get_conn_ctx() as conn:
            try:
                async with conn.cursor() as cur:
                    chat_id_str = str(chat_id)
                    await cur.execute(
                        "DELETE FROM recent_chats WHERE chat_id = %s", (chat_id_str,)
                    )
                    await conn.commit()
            except Exception as e:
                log_warning(f"[recent_chats] Failed to reset chat in DB: {e}")
    except Exception as e:
        log_warning(f"[recent_chats] Unexpected error in reset_chat: {e}")

    _metadata.pop(chat_id, None)
    if chat_path_map.pop(str(chat_id), None) is not None:
        _save_chat_paths()


def set_chat_path(chat_id: Union[int, str], chat_path: str) -> None:
    # No-op when the mapping is already current: this is called for every
    # message (see chat_context_manager.add_message_to_context), so skipping
    # unchanged entries avoids rewriting chat_paths.json on each turn.
    key = str(chat_id)
    if chat_path_map.get(key) == chat_path:
        return
    chat_path_map[key] = chat_path
    _save_chat_paths()


def get_chat_path(chat_id: Union[int, str]) -> str | None:
    return chat_path_map.get(str(chat_id))


def clear_chat_path(chat_id: Union[int, str]) -> None:
    """Remove chat path mapping for the given chat_id."""
    key = str(chat_id)
    if key in chat_path_map:
        del chat_path_map[key]
        _save_chat_paths()
        log_info(f"[recent_chats] Cleared chat path for chat_id: {chat_id}")
    else:
        log_debug(f"[recent_chats] No chat path found for chat_id: {chat_id}")


async def get_last_active_chats(n=10):
    try:
        async with get_conn_ctx() as conn:
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
        log_warning(
            f"[recent_chats] DB unavailable, falling back to in-memory chats: {e}"
        )
        # Best-effort fallback: return recently cached metadata keys
        try:
            keys = list(_metadata.keys())[:n]
            return keys
        except Exception:
            return []


def format_chat_entry_generic(
    chat_id: Union[int, str], chat_name: Optional[str] = None
):
    """Generic format for chat entries."""
    name = chat_name or str(chat_id)
    safe_name = escape_markdown(name)
    return f"{safe_name} — `{chat_id}`"


async def last_chats_command(
    abstract_context: AbstractContext,
    reply_fn: Optional[Callable] = None,
    get_chat_info_fn: Optional[Callable] = None,
):
    """Last chats command that works with any interface."""
    if not abstract_context.is_trainer():
        return

    lines = ["\U0001f553 *Last active chats:*"]

    for chat_id in await get_last_active_chats():
        chat_name = None
        if get_chat_info_fn:
            try:
                chat_info = await get_chat_info_fn(chat_id)
                chat_name = chat_info.get("name") if chat_info else None
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
    escape_chars = r"\_*[]()~`>#+-=|{}.!"
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", text)
