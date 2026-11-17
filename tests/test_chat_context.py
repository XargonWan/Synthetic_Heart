"""Interface-agnostic chat facts the core derives from what the interface sent.

Live (2026-11-12): Rekku, writing to a game bot in a DM, kept ending her messages
with "…, Jay!" — the trainer who was not in the chat. The prompt told her who the
sender was only through a terse prefix, while the system prompt said the trainer
was her interlocutor. These tests pin the structural facts the core now derives
from the message (chat kind, sender-is-trainer, chat title) and the prompt pieces
built on them, identically for any interface.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core import chat_context
from core.interfaces_registry import get_interface_registry
from core.prompt_instructions import build_instructions
from core.prompt_instructions.routes import ROUTE_CHAT, ROUTE_GRILLO_INTERNAL


def _msg(chat_type: str | None = None, title: str | None = None, uid: Any = 7) -> Any:
    chat = SimpleNamespace(type=chat_type, title=title)
    return SimpleNamespace(chat=chat, from_user=SimpleNamespace(id=uid))


@pytest.mark.parametrize(
    ("chat_type", "expected"),
    [
        ("private", "dm"),
        ("PRIVATE", "dm"),
        ("group", "group"),
        ("supergroup", "group"),
        ("channel", "group"),
    ],
)
def test_chat_kind_comes_from_the_interface_reported_type(
    chat_type: str, expected: str
) -> None:
    # The path is deliberately misleading: the message's own type must win.
    assert chat_context.resolve_chat_kind(_msg(chat_type), "discord_bot/-1") == expected


def test_chat_kind_falls_back_to_the_telegram_path_only_without_a_type() -> None:
    assert chat_context.resolve_chat_kind(_msg(None), "telegram_bot/-100123") == "group"
    assert chat_context.resolve_chat_kind(_msg(None), "telegram_bot/42") == "dm"
    assert chat_context.resolve_chat_kind(_msg(None), "discord_bot/42") is None
    assert chat_context.resolve_chat_kind(SimpleNamespace(), None) is None


def test_chat_title_is_optional() -> None:
    assert chat_context.resolve_chat_title(_msg("group", " Family ")) == "Family"
    assert chat_context.resolve_chat_title(_msg("group", None)) is None
    assert chat_context.resolve_chat_title(_msg("group", "  ")) is None
    assert chat_context.resolve_chat_title(SimpleNamespace()) is None


def test_sender_is_trainer_distinguishes_known_and_unknown() -> None:
    registry = get_interface_registry()
    previous = dict(registry._trainer_ids)
    try:
        registry.replace_trainer_ids({"telegram_bot": 7})
        assert chat_context.sender_is_trainer(_msg(uid=7), "telegram_bot") is True
        assert chat_context.sender_is_trainer(_msg(uid=8), "telegram_bot") is False
        # No trainer configured for that interface: unknown, never "not trainer".
        assert chat_context.sender_is_trainer(_msg(uid=8), "discord_bot") is None
        # No sender: unknown.
        assert (
            chat_context.sender_is_trainer(
                SimpleNamespace(from_user=None), "telegram_bot"
            )
            is None
        )
    finally:
        registry.replace_trainer_ids(previous)


def test_interlocutor_rule_reaches_chat_but_not_internal_beats() -> None:
    chat = build_instructions(ROUTE_CHAT, naming_hint="")
    beat = build_instructions(ROUTE_GRILLO_INTERNAL, naming_hint="")
    assert "INTERLOCUTOR: you are not always talking with your trainer" in chat
    assert "INTERLOCUTOR:" not in beat
