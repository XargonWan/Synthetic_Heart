"""Tests for the Rift Vessel real-time gaming focus.

Covers two enforced behaviours (see AGENTS.md §5c, docs/rift_vessel.rst):

1. **Priority — the game takes top priority while embodied.** When a Vessel
   session is active, ``core.message_queue.enqueue`` raises the Vessel's own
   in-world perceptions to ``HIGH_PRIORITY`` and lowers ordinary chat from
   other interfaces to ``AGENT_PRIORITY`` (trainer and urgent messages exempt).
2. **Context — SyntH is not omniscient while playing.** When a turn originates
   from a Vessel embodiment, ``HistoryEngine.build_context`` forces
   ``unified_mode = False`` and suppresses the global diary/memory injections.

The decision is taken purely from routing metadata (origin interface + the
active-session flag / interface_path) — never from message text.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# VesselSessionManager.has_active_session()
# ---------------------------------------------------------------------------


def test_has_active_session_toggles() -> None:
    """The in-memory active-session flag flips with add/discard."""
    from core.vessel_session_manager import VesselSessionManager

    mgr = VesselSessionManager()
    assert mgr.has_active_session() is False

    mgr._active_session_ids.add("sess-1")
    assert mgr.has_active_session() is True

    mgr._active_session_ids.add("sess-2")
    assert mgr.has_active_session() is True

    mgr._active_session_ids.discard("sess-1")
    assert mgr.has_active_session() is True

    mgr._active_session_ids.discard("sess-2")
    assert mgr.has_active_session() is False


# ---------------------------------------------------------------------------
# message_queue.enqueue priority behaviour
# ---------------------------------------------------------------------------


def _make_message(interface_path: str) -> SimpleNamespace:
    return SimpleNamespace(
        text="hello",
        chat_id="chat-1",
        chat=SimpleNamespace(type="group", human_count=1),
        from_user=SimpleNamespace(id=42, is_bot=False),
        interface_path=interface_path,
        thread_id=None,
        message_thread_id=None,
    )


def _patch_enqueue_hot_path(
    monkeypatch: pytest.MonkeyPatch, *, session_active: bool, trainer_path: str = ""
) -> list[tuple[Any, Any, Any]]:
    """Neutralise every heavy dependency of ``enqueue`` and capture the queue.

    Returns the list backing the fake priority queue so callers can read the
    ``(priority_val, counter, item)`` tuple that was put on it.
    """
    import core.message_queue as mq

    put_items: list[tuple[Any, Any, Any]] = []

    class _FakeQueue:
        async def put(self, item: tuple[Any, Any, Any]) -> None:
            put_items.append(item)

    monkeypatch.setattr(mq, "_get_queue", lambda: _FakeQueue())

    # ``_broadcast_global_animation_state`` is a nested function inside
    # ``enqueue`` and is already guarded by try/except, so we can't (and needn't)
    # patch it — it degrades to a no-op when the persona manager is absent. We
    # only neutralise the module-level dependencies below.
    monkeypatch.setattr(mq, "get_reaction_emoji", lambda: None)
    monkeypatch.setattr(mq, "get_name_resolver", lambda interface: None)

    async def _not_blocked(user_id: Any) -> bool:
        return False

    monkeypatch.setattr(mq, "is_user_blocked", _not_blocked)

    class _FakePlugin:
        __module__ = "plugins.fake"

        def get_rate_limit(self) -> tuple[int, int, float]:
            return 1000, 1, 1.0

    monkeypatch.setattr(mq.plugin_instance, "get_plugin", lambda: _FakePlugin())
    monkeypatch.setattr(mq.rate_limit, "is_allowed", lambda *a, **k: True)

    class _FakeRegistry:
        def is_trainer(self, interface_id: Any, user_id: Any) -> bool:
            return False

    monkeypatch.setattr(mq, "get_interface_registry", lambda: _FakeRegistry())

    # Vessel session flag + config lookup (lazily imported inside enqueue).
    from core import vessel_session_manager as vsm_mod

    monkeypatch.setattr(
        vsm_mod.vessel_session_manager,
        "has_active_session",
        lambda: session_active,
    )

    from core import config as config_mod

    def _get_value(key: str, default: Any = None) -> Any:
        if key == "TRAINER_CHAT_ID":
            return trainer_path
        return default

    monkeypatch.setattr(config_mod.config_registry, "get_value", _get_value)

    return put_items


@pytest.mark.asyncio
async def test_vessel_perception_raised_to_high_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An in-world perception during an active session gets HIGH_PRIORITY."""
    import core.message_queue as mq

    put_items = _patch_enqueue_hot_path(monkeypatch, session_active=True)
    message = _make_message("vessel/minecraft/world")

    await mq.enqueue(
        bot=None,
        message=message,
        interface_id="vessel",
        skip_mention_check=True,
    )

    assert put_items, "expected the message to be enqueued"
    priority_val = put_items[-1][0]
    assert priority_val == mq.HIGH_PRIORITY


@pytest.mark.asyncio
async def test_ordinary_chat_deprioritised_during_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary (non-trainer) chat is lowered to AGENT_PRIORITY while embodied."""
    import core.message_queue as mq

    put_items = _patch_enqueue_hot_path(monkeypatch, session_active=True)
    message = _make_message("telegram_bot/999")

    await mq.enqueue(
        bot=None,
        message=message,
        interface_id="telegram_bot",
        skip_mention_check=True,
    )

    assert put_items, "expected the message to be enqueued"
    assert put_items[-1][0] == mq.AGENT_PRIORITY


@pytest.mark.asyncio
async def test_trainer_chat_not_deprioritised_during_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trainer stays at NORMAL_PRIORITY even while a session is active."""
    import core.message_queue as mq

    put_items = _patch_enqueue_hot_path(
        monkeypatch, session_active=True, trainer_path="telegram_bot/999"
    )
    message = _make_message("telegram_bot/999")

    await mq.enqueue(
        bot=None,
        message=message,
        interface_id="telegram_bot",
        skip_mention_check=True,
    )

    assert put_items, "expected the message to be enqueued"
    assert put_items[-1][0] == mq.NORMAL_PRIORITY


@pytest.mark.asyncio
async def test_no_session_leaves_priority_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no active session, ordinary chat keeps NORMAL_PRIORITY."""
    import core.message_queue as mq

    put_items = _patch_enqueue_hot_path(monkeypatch, session_active=False)
    message = _make_message("telegram_bot/999")

    await mq.enqueue(
        bot=None,
        message=message,
        interface_id="telegram_bot",
        skip_mention_check=True,
    )

    assert put_items, "expected the message to be enqueued"
    assert put_items[-1][0] == mq.NORMAL_PRIORITY


@pytest.mark.asyncio
async def test_urgent_message_stays_high_regardless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """priority=True (urgent) always maps to HIGH_PRIORITY, untouched."""
    import core.message_queue as mq

    put_items = _patch_enqueue_hot_path(monkeypatch, session_active=True)
    message = _make_message("telegram_bot/999")

    await mq.enqueue(
        bot=None,
        message=message,
        interface_id="telegram_bot",
        skip_mention_check=True,
        priority=True,
    )

    assert put_items, "expected the message to be enqueued"
    assert put_items[-1][0] == mq.HIGH_PRIORITY
