"""Vessel perception classification for reactive-turn context assembly.

Regression coverage for the Minecraft "Synth replies off-topic to player
chat" bug: autonomous **will/action beat** turns are enqueued as perceptions
(so they never evict player chat from the conversational deque), but their
persisted text is a solitary self-instruction ("this is a private moment, do
NOT speak, return no ``say`` action"). If that leaks into a reactive
player-chat turn's context it suppresses the reply.

``history_engine._is_vessel_beat_perception`` isolates those beat turns
structurally (persisted ``metadata.vessel_event_type`` ending in ``_beat``) so
they can be excluded from the perception merge, while genuine world-grounding
perceptions (sightings/movement/damage/status) are kept. Detection is purely
structural — never keyword matching on the message text (multi-language safe).
"""

from __future__ import annotations

import pytest

from core.history_engine import (
    _is_vessel_autonomous_perception,
    _is_vessel_beat_perception,
)


def _perception(event_type: str, text: str = "…") -> dict:
    """Build a ring-buffer message dict as persisted by the live/DB paths."""
    return {
        "text": text,
        "timestamp": "2026-07-27T12:00:00",
        "metadata": {"vessel_perception": True, "vessel_event_type": event_type},
    }


def test_beat_perceptions_are_detected() -> None:
    assert _is_vessel_beat_perception(_perception("will_beat")) is True
    assert _is_vessel_beat_perception(_perception("action_beat")) is True
    # Any future ``*_beat`` type is covered by the structural suffix check.
    assert _is_vessel_beat_perception(_perception("curiosity_beat")) is True


def test_world_grounding_perceptions_are_not_beats() -> None:
    for event_type in ("sighting", "movement", "damage", "status", "session_start"):
        assert _is_vessel_beat_perception(_perception(event_type)) is False


def test_player_chat_is_neither_perception_nor_beat() -> None:
    # A real in-world player chat is deliberately left untagged (metadata=None).
    player_chat = {
        "text": "Rekku, rispondi Fragole",
        "timestamp": "2026-07-27T12:00:01",
        "username": "XargonWan",
    }
    assert _is_vessel_autonomous_perception(player_chat) is False
    assert _is_vessel_beat_perception(player_chat) is False


def test_beat_is_also_an_autonomous_perception() -> None:
    # Beats carry ``vessel_perception`` too — they are a *subset* of autonomous
    # perceptions, and the beat filter narrows within that set.
    beat = _perception("will_beat")
    assert _is_vessel_autonomous_perception(beat) is True
    assert _is_vessel_beat_perception(beat) is True


def test_non_dict_and_missing_metadata_are_safe() -> None:
    assert _is_vessel_beat_perception(None) is False  # type: ignore[arg-type]
    assert _is_vessel_beat_perception("not a dict") is False
    assert _is_vessel_beat_perception({"text": "no metadata"}) is False
    assert (
        _is_vessel_beat_perception({"metadata": {"vessel_perception": True}}) is False
    )
    # A non-string event type must not blow up the suffix check.
    assert _is_vessel_beat_perception({"metadata": {"vessel_event_type": 123}}) is False


def test_beat_exclusion_keeps_only_grounding() -> None:
    """Mirror the history_engine merge: exclude beats, keep world grounding."""
    buffer = [
        _perception("will_beat", "a quiet moment to reflect…"),
        _perception("sighting", "You notice a block nearby: oak_log"),
        _perception("action_beat", "a moment to ACT, not to talk…"),
        _perception("damage", "You took fall damage"),
    ]
    grounding = [m for m in buffer if not _is_vessel_beat_perception(m)]
    kinds = {m["metadata"]["vessel_event_type"] for m in grounding}
    assert kinds == {"sighting", "damage"}


@pytest.mark.asyncio
async def test_player_chat_stays_last_even_with_none_timestamp_perceptions(
    monkeypatch,
) -> None:
    """A reactive player-chat turn must keep the player's line LAST.

    Regression for the "Synth babbles/observes instead of answering" bug: on a
    vessel-focus turn ``build_context`` merges ambient grounding perceptions
    with the conversation. Perceptions carry ``timestamp=None``; a prior
    chronological sort keyed on the string timestamp made ``str(None) == "None"``
    sort AFTER a real ISO player-chat timestamp (``"2026-…"``), shoving a
    ``Mined …`` perception past the player's question and burying it mid-list.
    A weak embodiment model then continued its autonomous pattern instead of
    replying. The fix places grounding perceptions BEFORE the conversation, so
    the player's chat is always the last line read.
    """
    from collections import deque
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from core.history_engine import HistoryEngine

    interface_path = "vessel/minecraft"

    # Conversational window: Synth's own line, then the player's question (last).
    context_memory = {
        interface_path: deque(
            [
                {
                    "sender_name": "self",
                    "text": "The night is quiet, Jay.",
                    "timestamp": "2026-07-28T20:42:00+00:00",
                    "interface_path": interface_path,
                },
                {
                    "sender_name": "CoachAgent",
                    "text": "Rekku, vieni qui?",
                    "timestamp": "2026-07-28T20:43:00+00:00",
                    "interface_path": interface_path,
                },
            ]
        )
    }

    # Ring-buffer perceptions carry ``timestamp=None`` (the exact live shape).
    perception = {
        "text": "Mined short_dry_grass (no drop collected)",
        "timestamp": None,
        "sender_name": "self",
        "interface_path": interface_path,
        "metadata": {"vessel_perception": True, "vessel_event_type": "status"},
    }

    monkeypatch.setattr(
        "core.chat_history_cache.load_chat_history",
        AsyncMock(return_value=deque()),
    )
    monkeypatch.setattr(
        "core.chat_history_cache.load_global_chat_history",
        AsyncMock(return_value=deque()),
    )
    monkeypatch.setattr("core.core_initializer.PLUGIN_REGISTRY", {})
    monkeypatch.setattr(
        "core.chat_context_manager.get_perception_memory",
        lambda: {interface_path: deque([perception])},
    )

    context = await HistoryEngine().build_context(
        message=SimpleNamespace(interface_path=interface_path),
        context_memory=context_memory,
        interface_name="vessel",
        text="Rekku, vieni qui?",
    )

    lines = context["history_current_chat"]
    assert lines, "expected a non-empty current-chat history"
    # The grounding perception must appear BEFORE the player's question, and the
    # player's question MUST be the last conversational line the model reads.
    assert "Rekku, vieni qui?" in lines[-1]
    assert any("Mined short_dry_grass" in line for line in lines)
    perception_idx = next(
        i for i, line in enumerate(lines) if "Mined short_dry_grass" in line
    )
    player_idx = next(i for i, line in enumerate(lines) if "Rekku, vieni qui?" in line)
    assert perception_idx < player_idx
