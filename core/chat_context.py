"""Interface-agnostic facts about the chat a message arrived in.

Interfaces hand the core raw data on the message (``message.chat.type``,
``message.chat.title``, ``message.from_user``); the core decides what the model
is told. Nothing here knows a platform: it normalises what the message carries.
"""

from __future__ import annotations

from typing import Any

DM = "dm"
GROUP = "group"

_DM_TYPES = frozenset({"private", "dm", "direct"})
_GROUP_TYPES = frozenset({"group", "supergroup", "channel"})


def resolve_chat_kind(message: Any, interface_path: str | None = None) -> str | None:
    """Return ``"dm"`` or ``"group"`` for the chat of ``message``, or ``None``.

    The interface-provided ``message.chat.type`` wins (``private`` is a DM;
    ``group``, ``supergroup`` and ``channel`` are groups). When the message does
    not carry one, fall back to the Telegram path convention (negative chat id
    means group), which is the only path-only signal the core knows about.
    """
    raw = getattr(getattr(message, "chat", None), "type", None)
    if isinstance(raw, str):
        kind = raw.strip().lower()
        if kind in _DM_TYPES:
            return DM
        if kind in _GROUP_TYPES:
            return GROUP
    if interface_path:
        try:
            from core.history_engine import telegram_chat_kind

            return telegram_chat_kind(str(interface_path))
        except Exception:
            return None
    return None


def sender_is_trainer(message: Any, interface_name: str | None) -> bool | None:
    """Whether the sender of ``message`` is the trainer of ``interface_name``.

    ``None`` means "cannot tell": no sender, no interface, or no trainer
    configured for that interface. Callers must treat ``None`` as unknown and
    never as "not the trainer", so a deployment without a trainer id keeps its
    behaviour. Same comparison the message queue uses to pick the trainer cortex.
    """
    try:
        sender = getattr(message, "from_user", None)
        user_id = getattr(sender, "id", None)
        if not interface_name or user_id is None:
            return None

        from core.interfaces_registry import get_interface_registry

        registry = get_interface_registry()
        if registry.get_trainer_id(interface_name) is None:
            return None
        return bool(registry.is_trainer(interface_name, user_id))
    except Exception:
        return None


def resolve_chat_title(message: Any) -> str | None:
    """The chat's title as the interface reported it, when it did."""
    title = getattr(getattr(message, "chat", None), "title", None)
    if isinstance(title, str) and title.strip():
        return title.strip()
    return None
