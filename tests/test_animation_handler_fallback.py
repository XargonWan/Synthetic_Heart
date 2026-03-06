import asyncio
from datetime import datetime, timezone

import pytest

from core.animation_handler import get_karada_state_server, AnimationState


def _make_descriptor(intro: float, loop: float, outro: float, fps: float = 30.0):
    # helper: frames computed to give the durations in seconds
    return {
        "fps": fps,
        "intro": {"start_frame": 0, "end_frame": intro * fps},
        "loop": {"start_frame": intro * fps, "end_frame": (intro + loop) * fps},
        "outro": {"start_frame": (intro + loop) * fps, "end_frame": (intro + loop + outro) * fps},
    }


@pytest.mark.asyncio
async def test_non_loop_fallback_duration_sum_and_buffer(monkeypatch):
    """Ensure _non_loop_fallback receives full clip duration + buffer.

    The handler should sum the lengths of intro+loop+outro sections and add
    1.5 seconds of buffer.  A descriptor providing those sections is used,
    and the scheduled duration is intercepted.
    """
    handler = get_karada_state_server()

    # monkeypatch descriptor resolver to return our artificial descriptor
    desc = _make_descriptor(intro=1.0, loop=2.0, outro=1.0, fps=30.0)

    async def fake_resolver(anim: str):
        return anim, desc

    handler._resolve_animation_descriptor = fake_resolver  # type: ignore

    captured: list[float] = []

    async def fake_fallback(session_id, state, animation_file, duration):
        captured.append(duration)
        # do nothing else

    handler._non_loop_fallback = fake_fallback  # type: ignore

    # trigger play_animation with loop=False so effective_loop is False
    await handler.play_animation(
        AnimationState.WRITE,
        session_id="sess",
        loop=False,
        context_id="ctx",
        priority=1,
    )

    # allow scheduled task to be created
    await asyncio.sleep(0.01)

    assert captured, "fallback did not run"
    dur = captured[0]
    # expected = intro+loop+outro (4s) + 1.5 buffer
    assert dur >= 5.5, f"duration too small ({dur})"


@pytest.mark.asyncio
async def test_non_loop_fallback_default_when_no_descriptor(monkeypatch):
    """When no descriptor is returned, a conservative default of 3s + buffer is used."""
    handler = get_karada_state_server()

    async def fake_resolver(anim: str):
        return anim, None

    handler._resolve_animation_descriptor = fake_resolver  # type: ignore
    captured: list[float] = []

    async def fake_fallback(session_id, state, animation_file, duration):
        captured.append(duration)

    handler._non_loop_fallback = fake_fallback  # type: ignore

    await handler.play_animation(
        AnimationState.WRITE,
        session_id="sess",
        loop=False,
        context_id="ctx",
        priority=1,
    )
    await asyncio.sleep(0.01)

    assert captured
    dur = captured[0]
    assert dur >= 4.5, f"default duration too small ({dur})"
