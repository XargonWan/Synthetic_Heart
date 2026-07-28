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
