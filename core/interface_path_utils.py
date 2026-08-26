# core/interface_path_utils.py
"""Utilities for managing interface paths.

Interface paths identify message routing through the system in a hierarchical format:
interface_name/level1/level2/level3/...

Examples:
- telegram_bot/123456/2 - Telegram bot, channel 123456, thread 2
- discord_bot/ab1234/cd423/4 - Discord bot, server ab1234, channel cd423, post 4
- telegram_bot/6789 - Telegram bot, user 6789
- discord_bot/Xargon - Discord bot, user Xargon
"""

from typing import Optional, List, Tuple, Dict, Any
from core.logging_utils import log_debug


def build_interface_path(interface_name: str, *levels: Any) -> str:
    """Build an interface path from components.

    Args:
        interface_name: The interface name (e.g., 'telegram_bot', 'discord_bot')
        *levels: Variable number of hierarchical levels

    Returns:
        Complete interface path as a string

    Examples:
        >>> build_interface_path('telegram_bot', '123456', '2')
        'telegram_bot/123456/2'
        >>> build_interface_path('discord_bot', 'ab1234')
        'discord_bot/ab1234'
    """
    # Filter out None values and convert all to strings
    valid_levels = [str(level) for level in levels if level is not None]

    if not valid_levels:
        return interface_name

    path = f"{interface_name}/{'/'.join(valid_levels)}"
    log_debug(f"[interface_path] Built path: {path}")
    return path


def build_interface_path_from_legacy(
    interface_name: str,
    chat_id: Optional[Any] = None,
    thread_id: Optional[Any] = None,
    **extra_levels,
) -> str:
    """Build interface path from legacy chat_id/thread_id format.

    Args:
        interface_name: The interface name
        chat_id: Legacy chat ID
        thread_id: Legacy thread ID (optional)
        **extra_levels: Additional hierarchical levels

    Returns:
        Interface path string
    """
    levels = []

    if chat_id is not None:
        levels.append(str(chat_id))

    if thread_id is not None:
        levels.append(str(thread_id))

    # Add any extra levels
    for key in sorted(extra_levels.keys()):
        if extra_levels[key] is not None:
            levels.append(str(extra_levels[key]))

    return build_interface_path(interface_name, *levels)


def parse_interface_path(interface_path: str) -> Tuple[str, List[str]]:
    """Parse an interface path into components.

    Args:
        interface_path: The interface path string

    Returns:
        Tuple of (interface_name, [levels])

    Examples:
        >>> parse_interface_path('telegram_bot/123456/2')
        ('telegram_bot', ['123456', '2'])
        >>> parse_interface_path('discord_bot/Xargon')
        ('discord_bot', ['Xargon'])
    """
    if not interface_path:
        return ("", [])

    parts = interface_path.split("/")
    interface_name = parts[0] if parts else ""
    levels = parts[1:] if len(parts) > 1 else []

    log_debug(f"[interface_path] Parsed: interface={interface_name}, levels={levels}")
    return interface_name, levels


def extract_legacy_ids(interface_path: str) -> Dict[str, Optional[str]]:
    """Extract legacy chat_id and thread_id from interface path.

    This is for backward compatibility. Typically:
    - level 1 = chat_id (channel/user/server)
    - level 2 = thread_id (thread/topic)

    Args:
        interface_path: The interface path

    Returns:
        Dict with 'interface', 'chat_id', 'thread_id'
    """
    interface_name, levels = parse_interface_path(interface_path)

    result = {
        "interface": interface_name,
        "chat_id": levels[0] if len(levels) >= 1 else None,
        "thread_id": levels[1] if len(levels) >= 2 else None,
    }

    log_debug(f"[interface_path] Extracted legacy IDs: {result}")
    return result


def get_interface_from_path(interface_path: str) -> str:
    """Extract just the interface name from a path.

    Args:
        interface_path: The interface path string

    Returns:
        The interface name (level 0)

    Examples:
        >>> get_interface_from_path('telegram_bot/123456/2')
        'telegram_bot'
    """
    interface_name, _ = parse_interface_path(interface_path)
    return interface_name


def resolve_registered_interface_path(
    interface_path: Optional[str],
    context: Optional[Dict[str, Any]] = None,
    original_message: Any = None,
) -> Optional[str]:
    """Return a routable interface path, falling back to the turn origin.

    Models occasionally hallucinate an ``interface_path`` prefix (e.g.
    ``em_chat_bridge/...``) that matches no registered interface, which makes
    the outbound reply (message, audio, or text fallback) undeliverable.
    When the given path is unregistered, fall back to the chat the turn
    actually arrived in — the same structural guarantee ``message_plugin``
    applies to message actions.

    Args:
        interface_path: Path to validate (payload/context value).
        context: Turn context dict; ``context['interface_path']`` is the origin.
        original_message: Incoming message; ``.interface_path`` is the origin.

    Returns:
        The original path when its interface is registered, else the
        originating chat path, else ``None`` when no routable path exists.
    """
    if not interface_path:
        return None
    path_str = str(interface_path)
    iface_name, _ = parse_interface_path(path_str)
    if iface_name:
        try:
            from core.core_initializer import INTERFACE_REGISTRY

            if not INTERFACE_REGISTRY:
                # Registry not populated yet (early startup / offline tests):
                # cannot validate — keep the given path unchanged.
                return path_str
            if INTERFACE_REGISTRY.get(iface_name):
                return path_str
        except Exception:
            # Registry unavailable: cannot validate, keep the given path.
            return path_str

    # Unregistered prefix — fall back to the turn's originating chat.
    origin = None
    if isinstance(context, dict):
        origin = context.get("interface_path")
    if not origin and original_message is not None:
        origin = getattr(original_message, "interface_path", None)
    if origin and str(origin) != path_str:
        o_iface, _ = parse_interface_path(str(origin))
        if o_iface:
            try:
                from core.core_initializer import INTERFACE_REGISTRY

                if INTERFACE_REGISTRY.get(o_iface):
                    return str(origin)
            except Exception:
                return None
    return None


def is_vessel_interface_path(interface_path: Any) -> bool:
    """Return whether a path belongs to the Rift Vessel interface.

    Interface paths are structural routing metadata.  Match the complete
    interface segment so an unrelated path such as ``vessel_preview/...`` is
    not treated as an embodiment path.
    """
    return isinstance(interface_path, str) and (
        interface_path == "vessel" or interface_path.startswith("vessel/")
    )


def is_vessel_history_entry(entry: Any) -> bool:
    """Return whether a persisted/in-memory history entry is from a Vessel."""
    if not isinstance(entry, dict):
        return False
    return is_vessel_interface_path(
        entry.get("interface_path") or entry.get("source_path")
    )


def get_level_from_path(interface_path: str, level: int) -> Optional[str]:
    """Extract a specific level from an interface path.

    Args:
        interface_path: The interface path string
        level: The level to extract (0-based, where 0 is interface name)

    Returns:
        The level value, or None if level doesn't exist

    Examples:
        >>> get_level_from_path('telegram_bot/123456/2', 0)
        'telegram_bot'
        >>> get_level_from_path('telegram_bot/123456/2', 1)
        '123456'
        >>> get_level_from_path('telegram_bot/123456/2', 2)
        '2'
        >>> get_level_from_path('telegram_bot/123456/2', 3)
        None
    """
    parts = interface_path.split("/")
    if 0 <= level < len(parts):
        return parts[level]
    return None


def is_valid_interface_path(interface_path: str) -> bool:
    """Validate an interface path.

    Args:
        interface_path: The interface path string

    Returns:
        True if valid, False otherwise
    """
    if not interface_path or not isinstance(interface_path, str):
        return False

    parts = interface_path.split("/")
    # Must have at least interface name
    if not parts or not parts[0]:
        return False

    # All parts must be non-empty
    for part in parts:
        if not part or not part.strip():
            return False

    return True


def is_vessel_embodiment_context(context: Optional[Dict[str, Any]]) -> bool:
    """Return True when a turn originates from a Rift Vessel embodiment.

    This is the single, canonical structural detector for "SyntH is in the
    world" turns. Detection is purely from routing metadata — never from
    message text (project rule: no keyword logic) — mirroring
    ``core.history_engine.build_context``:

    * an explicit ``vessel_focus`` context flag, or
    * ``context['interface'] == 'vessel'``, or
    * ``context['interface_path']`` / ``context['chat_id']`` being ``vessel``
      or a ``vessel/...`` path.

    Fully guarded: any failure degrades to ``False`` so the caller's normal
    path is untouched.
    """
    if not isinstance(context, dict):
        return False
    try:
        if context.get("vessel_focus"):
            return True
        iface = context.get("interface")
        if isinstance(iface, str) and iface == "vessel":
            return True
        for key in ("interface_path", "chat_id"):
            val = context.get(key)
            if is_vessel_interface_path(val):
                return True
    except Exception:
        return False
    return False
