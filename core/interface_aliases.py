"""Readable aliases for the numeric ids inside interface paths.

``telegram/31321637`` says nothing to a person or to the model; ``telegram/@jay``
does. The numeric id stays the *canonical* key everywhere it is stored (history,
memories, ``interface_paths``, trainer checks), because an alias can change or
be missing. This module keeps the id <-> alias pairs and converts a path in both
directions:

* :func:`to_display_path` — what the model is shown and asked to build
  (``telegram/@jay/12``);
* :func:`to_canonical_path` — what the model sends back, mapped to the stored
  form (``telegram/31321637/12``) before anything routes or compares it.

Pairs are learnt from incoming traffic (:func:`remember_message_alias`): the
sender's public username in a private chat, the chat's own username for a public
group or channel. A username is unique per interface, a target has one current
alias, and a renamed account simply replaces its old alias.

Fail-open throughout: no alias, an unknown alias or a database error returns the
path unchanged.
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional

from core.interface_path_utils import parse_interface_path
from core.logging_utils import log_debug

#: Telegram-style public handle: 5-32 chars, letters/digits/underscore, letter first.
_ALIAS_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{4,31})$")

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS interface_aliases (
    interface VARCHAR(64) NOT NULL,
    alias_key VARCHAR(128) NOT NULL,
    alias VARCHAR(128) NOT NULL,
    target VARCHAR(128) NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (interface, alias_key),
    INDEX idx_interface_aliases_target (interface, target)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

#: How long "no alias known" is trusted before the database is asked again.
_MISS_TTL_SECONDS = 300.0

_table_ready = False
_by_alias: dict[tuple[str, str], str] = {}  # (interface, alias_key) -> target
_by_target: dict[tuple[str, str], str] = {}  # (interface, target) -> alias
_misses: dict[tuple[str, str, str], float] = {}  # (kind, interface, value) -> when


def normalize_alias(alias: Any) -> str:
    """Return the lowercase handle without ``@``, or ``""`` when it is not one."""
    match = _ALIAS_RE.match(str(alias or "").strip())
    return match.group(1).lower() if match else ""


def _is_id_segment(segment: str) -> bool:
    return segment.lstrip("-").isdigit()


async def _ensure_table() -> None:
    global _table_ready
    if _table_ready:
        return
    from core.db import get_conn_ctx

    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_TABLE_SQL)
        try:
            await conn.commit()
        except Exception:
            pass
    _table_ready = True


def _recently_missed(kind: str, interface: str, value: str) -> bool:
    stamp = _misses.get((kind, interface, value))
    return stamp is not None and time.monotonic() - stamp < _MISS_TTL_SECONDS


def _cache_pair(interface: str, alias: str, target: str) -> None:
    old_alias = _by_target.get((interface, target))
    if old_alias and old_alias.lower() != alias.lower():
        _by_alias.pop((interface, old_alias.lower()), None)
    old_target = _by_alias.get((interface, alias.lower()))
    if old_target and old_target != target:
        _by_target.pop((interface, old_target), None)
    _by_alias[(interface, alias.lower())] = target
    _by_target[(interface, target)] = alias
    _misses.pop(("alias", interface, alias.lower()), None)
    _misses.pop(("target", interface, target), None)


async def remember_alias(interface: str, alias: Any, target: Any) -> None:
    """Record that ``alias`` currently names ``target`` on ``interface``."""
    clean = normalize_alias(alias)
    target_str = str(target or "").strip()
    if not interface or not clean or not target_str:
        return
    if _by_alias.get((interface, clean)) == target_str and (
        _by_target.get((interface, target_str), "").lower() == clean
    ):
        return  # already known: no database traffic per message
    _cache_pair(interface, clean, target_str)
    try:
        await _ensure_table()
        from core.db import get_conn_ctx

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM interface_aliases "
                    "WHERE interface = %s AND (alias_key = %s OR target = %s)",
                    [interface, clean, target_str],
                )
                await cur.execute(
                    "INSERT INTO interface_aliases (interface, alias_key, alias, target) "
                    "VALUES (%s, %s, %s, %s)",
                    [interface, clean, clean, target_str],
                )
            try:
                await conn.commit()
            except Exception:
                pass
    except Exception as exc:
        log_debug(f"[interface_aliases] alias kept in memory only: {exc}")


async def resolve_alias(interface: str, alias: Any) -> Optional[str]:
    """The canonical id behind ``alias`` on ``interface`` (None if unknown)."""
    clean = normalize_alias(alias)
    if not clean:
        return None
    hit = _by_alias.get((interface, clean))
    if hit or _recently_missed("alias", interface, clean):
        return hit
    found: Optional[str] = None
    try:
        await _ensure_table()
        from core.db import get_conn_ctx

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT target FROM interface_aliases "
                    "WHERE interface = %s AND alias_key = %s",
                    [interface, clean],
                )
                row = await cur.fetchone()
        found = str(row[0]) if row and row[0] is not None else None
    except Exception as exc:
        log_debug(f"[interface_aliases] resolve failed: {exc}")
    if found:
        _cache_pair(interface, clean, found)
    else:
        _misses[("alias", interface, clean)] = time.monotonic()
    return found


async def alias_for(interface: str, target: Any) -> Optional[str]:
    """The current alias of ``target`` on ``interface`` (None if it has none)."""
    target_str = str(target or "").strip()
    if not target_str:
        return None
    hit = _by_target.get((interface, target_str))
    if hit or _recently_missed("target", interface, target_str):
        return hit
    found: Optional[str] = None
    try:
        await _ensure_table()
        from core.db import get_conn_ctx

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT alias FROM interface_aliases "
                    "WHERE interface = %s AND target = %s",
                    [interface, target_str],
                )
                row = await cur.fetchone()
        found = str(row[0]) if row and row[0] is not None else None
    except Exception as exc:
        log_debug(f"[interface_aliases] lookup failed: {exc}")
    if found:
        _cache_pair(interface, found, target_str)
    else:
        _misses[("target", interface, target_str)] = time.monotonic()
    return found


def _join(interface: str, levels: list[str]) -> str:
    return "/".join([interface, *levels])


async def to_display_path(path: Any) -> Any:
    """``telegram/31321637/12`` -> ``telegram/@jay/12`` when an alias is known.

    Anything that is not a string path, has no numeric first segment, or has no
    alias, comes back unchanged.
    """
    if not isinstance(path, str) or "/" not in path:
        return path
    try:
        interface, levels = parse_interface_path(path)
        if not interface or not levels or not _is_id_segment(levels[0]):
            return path
        alias = await alias_for(interface, levels[0])
        if not alias:
            return path
        return _join(interface, [f"@{alias}", *levels[1:]])
    except Exception as exc:  # pragma: no cover - defensive
        log_debug(f"[interface_aliases] display path failed: {exc}")
        return path


async def to_canonical_path(path: Any) -> Any:
    """``telegram/@jay/12`` -> ``telegram/31321637/12`` when the alias is known."""
    if not isinstance(path, str) or "/@" not in path:
        return path
    try:
        interface, levels = parse_interface_path(path)
        if not interface or not levels or not levels[0].startswith("@"):
            return path
        target = await resolve_alias(interface, levels[0])
        if not target:
            return path
        return _join(interface, [target, *levels[1:]])
    except Exception as exc:  # pragma: no cover - defensive
        log_debug(f"[interface_aliases] canonical path failed: {exc}")
        return path


async def remember_message_alias(interface: str, message: Any) -> None:
    """Learn the alias a Telegram-style incoming ``message`` reveals. Never raises.

    In a private chat the chat id is the person's id, so the sender's username
    names the conversation. In a group or channel it is the chat's own username
    (only public ones have one).
    """
    try:
        chat = getattr(message, "chat", None)
        chat_id = getattr(message, "chat_id", None) or getattr(chat, "id", None)
        if chat is None or chat_id is None:
            return
        chat_type = getattr(chat, "type", "")
        chat_type = getattr(chat_type, "value", chat_type)
        if str(chat_type or "").lower() == "private":
            alias = getattr(getattr(message, "from_user", None), "username", None)
        else:
            alias = getattr(chat, "username", None)
        if alias:
            await remember_alias(interface, alias, chat_id)
    except Exception as exc:  # pragma: no cover - defensive
        log_debug(f"[interface_aliases] message alias not learnt: {exc}")
