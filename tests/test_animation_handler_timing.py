from datetime import datetime, timezone, timedelta

from core.animation_handler import AnimationState, KaradaStateServer


def test_get_current_animation_state_timing_computed():
    ah = KaradaStateServer()
    ah.current_state = AnimationState.THINK
    ah._current_animation_file = "Thinking.fbx"
    ah._current_animation_descriptor = {
        "fps": 24,
        "loop": {"start_frame": 0, "end_frame": 48},
    }
    ah._current_animation_started_at = datetime.now(timezone.utc) - timedelta(seconds=2)

    state = ah.get_current_animation_state()
    assert state["state"] == "think"
    assert state["descriptor"] == "rei/think/thinking"
    assert isinstance(state["started_at"], float)
    assert (
        state["started_at"]
        >= (datetime.now(timezone.utc) - timedelta(seconds=3)).timestamp()
    )
