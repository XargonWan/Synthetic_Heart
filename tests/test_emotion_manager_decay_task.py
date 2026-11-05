"""The decay loop is tracked, restart-safe and cancelled on stop()."""

import asyncio

import pytest

from plugins.emotion_manager.emotion_manager import EmotionManager


async def _noop(*_a: object, **_k: object) -> None:
    return None


@pytest.mark.asyncio
async def test_decay_task_is_tracked_and_cancelled_on_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = EmotionManager()
    monkeypatch.setattr(mgr, "_ensure_table_exists", _noop)
    monkeypatch.setattr(mgr, "sync_emotions_from_all_sources", _noop)

    await mgr.start()
    first = mgr._decay_task
    assert first is not None and not first.done()

    await mgr.start()  # restart must replace, not stack, the loop
    await asyncio.sleep(0)  # let the cancellation run
    assert first.done()
    second = mgr._decay_task
    assert second is not None and second is not first

    await mgr.stop()
    assert mgr._decay_task is None
    assert second.done()
