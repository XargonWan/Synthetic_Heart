from core.soul.emotion_engine import EmotionalEngine
from core.soul.models import EmotionalEvent, EmotionalProfile, EmotionalState


def test_apply_event_updates_state() -> None:
    engine = EmotionalEngine()
    state = EmotionalState(joy=0.0, fear=0.0, sad=0.0, anger=0.0)

    event = EmotionalEvent(
        source="user_message",
        factor_deltas={
            "social_connection": 0.8,
            "concern_for_user": 0.5,
            "anxiety": 0.2,
        },
        intensity=0.9,
        context="Warm greeting from user",
    )

    updated = engine.apply_event(state, event)

    assert updated.joy > state.joy
    assert updated.fear >= state.fear


def test_apply_time_decay_builds_loneliness() -> None:
    engine = EmotionalEngine()
    state = EmotionalState(joy=0.7, fear=0.0, sad=0.1, anger=0.0)

    decayed = engine.apply_time_decay(state, hours_elapsed=48)

    assert decayed.joy < state.joy
    assert decayed.sad > state.sad


def test_turn_delta_payload_shape() -> None:
    engine = EmotionalEngine()
    state = EmotionalState(joy=0.25, fear=0.1, sad=0.2, anger=0.05)

    payload = engine.to_turn_delta_payload(state)

    assert "e" in payload
    assert set(payload["e"].keys()) == {"joy", "fear", "sad", "anger"}


def test_emotional_profile_from_dict_applies_overrides() -> None:
    profile = EmotionalProfile.from_dict({"anxiety": 0.9, "loneliness": 0.1})
    assert profile.anxiety == 0.9
    assert profile.loneliness == 0.1
    assert profile.concern_for_user == 0.90


def test_emotional_profile_from_dict_clamps_out_of_range() -> None:
    profile = EmotionalProfile.from_dict({"anxiety": 2.5, "self_preservation": -1.0})
    assert profile.anxiety == 1.0
    assert profile.self_preservation == 0.0


def test_emotional_profile_from_dict_empty_uses_all_defaults() -> None:
    profile = EmotionalProfile.from_dict({})
    assert profile.as_dict() == EmotionalProfile().as_dict()
