from datetime import datetime, timezone, timedelta

from core.animation_handler import get_karada_state_server


def test_get_current_animation_state_timing_computed():
    ah = get_karada_state_server()
    ah._current_animation_file = "Thinking.fbx"
    ah._current_animation_descriptor = {
        "fps": 24,
        "loop": {"start_frame": 0, "end_frame": 48},
    }
    ah._current_animation_started_at = datetime.utcnow().replace(
        tzinfo=timezone.utc
    ) - timedelta(seconds=2)

    state = ah.get_current_animation_state()
    anim_state = state.get("animation_state")
    assert anim_state is not None
    timing = anim_state.get("timing")
    assert timing is not None
    assert timing.get("time_in_clip", 0) >= 2.0 - 0.5  # allow small drift
    assert isinstance(timing.get("current_frame"), int)
