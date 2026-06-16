import asyncio
from typing import Any, cast

import pytest

from core.animation_handler import AnimationState, KaradaStateServer
from core.karada_ws_transport import WebSocketTransport


def _make_descriptor(intro: float, loop: float, outro: float, fps: float = 30.0):
    # helper: frames computed to give the durations in seconds
    return {
        "fps": fps,
        "intro": {"start_frame": 0, "end_frame": intro * fps},
        "loop": {"start_frame": intro * fps, "end_frame": (intro + loop) * fps},
        "outro": {
            "start_frame": (intro + loop) * fps,
            "end_frame": (intro + loop + outro) * fps,
        },
    }


@pytest.mark.asyncio
async def test_non_loop_fallback_duration_sum_and_buffer(monkeypatch):
    """Ensure _non_loop_fallback receives full clip duration + buffer.

    The handler should sum the lengths of intro+loop+outro sections and add
    1.5 seconds of buffer.  A descriptor providing those sections is used,
    and the scheduled duration is intercepted.

    Uses a descriptor with intro + outro (no loop section) so that
    effective_loop is False and the fallback logic fires.
    """
    handler = KaradaStateServer()

    # Descriptor with intro + outro only (no loop) → effective_loop=False
    # Duration computation still sums all present sections.
    desc = {
        "fps": 30.0,
        "intro": {"start_frame": 0, "end_frame": 30},  # 1 s
        "outro": {"start_frame": 30, "end_frame": 120},  # 3 s
    }

    def fake_resolver(anim: str):
        return anim, desc

    handler._resolve_animation_descriptor = fake_resolver  # type: ignore
    handler._resolve_animation_descriptor_for_state = (  # type: ignore
        lambda anim, state: fake_resolver(anim)
    )

    # Register a dummy transport so the transport guard is satisfied

    class _FakeWs:
        async def send_json(self, data: dict[str, Any]) -> None:
            pass

    _conns: dict[str, _FakeWs] = {"sess": _FakeWs()}
    handler.add_transport(WebSocketTransport(cast(Any, _conns)))

    captured: list[float] = []

    async def fake_fallback(
        session_id, state, animation_file, duration, context_id=None
    ):
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
    # expected = intro (1s) + outro (3s) = 4s + 1.5 buffer = 5.5
    assert dur >= 5.5, f"duration too small ({dur})"


@pytest.mark.asyncio
async def test_non_loop_fallback_default_when_no_descriptor(monkeypatch):
    """When no descriptor is returned, a conservative default of 3s + buffer is used."""
    handler = KaradaStateServer()

    def fake_resolver(anim: str):
        return anim, None

    handler._resolve_animation_descriptor = fake_resolver  # type: ignore
    handler._resolve_animation_descriptor_for_state = (  # type: ignore
        lambda anim, state: fake_resolver(anim)
    )

    # Register a dummy transport so the transport guard is satisfied

    class _FakeWs2:
        async def send_json(self, data: dict[str, Any]) -> None:
            pass

    _conns2: dict[str, _FakeWs2] = {"sess": _FakeWs2()}
    handler.add_transport(WebSocketTransport(cast(Any, _conns2)))

    captured: list[float] = []

    async def fake_fallback(
        session_id, state, animation_file, duration, context_id=None
    ):
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
