"""A world's perception may carry what its body SAW: the picture goes to the model with that turn only."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import pytest

PNG = base64.b64encode(b"\xff\xd8\xff\xe0 not a real jpeg").decode()


@pytest.mark.asyncio
async def test_enqueue_perception_puts_the_attachments_on_the_message(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import message_queue
    from interface.vessel_interface import VesselInterface

    seen: list[Any] = []

    async def _enqueue(_iface: Any, message: Any, **_kw: Any) -> None:
        seen.append(message)

    monkeypatch.setattr(message_queue, "enqueue", _enqueue)
    iface = VesselInterface.__new__(VesselInterface)
    att = [{"mime_type": "image/jpeg", "data": PNG, "filename": "what_she_saw.jpg"}]
    await iface._enqueue_perception("vessel/retroxr/root", "You watched the TV.", "retroxr", "watch_done", attachments=att)
    await iface._enqueue_perception("vessel/retroxr/root", "Nothing to see.", "retroxr", "search_done")
    assert seen[0].attachments == att
    assert not hasattr(seen[1], "attachments"), "a perception with no picture carries none"


@pytest.mark.asyncio
async def test_the_attachment_is_extracted_the_way_any_interface_attachment_is() -> None:
    from core.plugin_instance import _extract_multimodal_attachments

    message = SimpleNamespace(attachments=[{"mime_type": "image/jpeg", "data": PNG, "filename": "what_she_saw.jpg"}])
    out = await _extract_multimodal_attachments(None, message, "vessel")
    assert len(out) == 1 and out[0]["mime_type"] == "image/jpeg" and out[0]["data"] == PNG


@pytest.mark.asyncio
async def test_on_world_event_keeps_the_picture_out_of_the_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The base64 frame is a megabyte: it must not reach the activity log or the experience record."""
    from interface import vessel_interface as vi

    recorded: list[dict[str, Any]] = []

    class _Manager:
        async def start_session(self, *_a: Any, **_k: Any) -> str:
            return "s1"

        async def record_experience(self, *_a: Any, **kw: Any) -> None:
            recorded.append(kw)

        def __getattr__(self, name: str) -> Any:  # anything else the interface calls on it
            async def _noop(*_a: Any, **_k: Any) -> None:
                return None

            return _noop

    monkeypatch.setattr(vi, "get_vessel_session_manager", lambda: _Manager())
    iface = vi.VesselInterface.__new__(vi.VesselInterface)
    iface._sessions = {}
    iface._pending_perceptions = {}
    queued: list[dict[str, Any]] = []

    async def _enqueue(**kw: Any) -> None:
        queued.append(kw)

    monkeypatch.setattr(iface, "_enqueue_perception", _enqueue)
    monkeypatch.setattr(iface, "_tracked_path_for_environment", lambda _e: "vessel/retroxr/root", raising=False)
    try:
        await iface.on_world_event(
            "retroxr", "watch_done", "You watched the TV.", world="root",
            data={"seconds": 10, "attachments": [{"mime_type": "image/jpeg", "data": PNG}]},
        )
    except Exception:  # the rest of the pipeline is not under test: what was recorded before it is
        pass
    print("RECORDED", len(recorded), "QUEUED", len(queued))
    assert recorded, "the experience was recorded (the test reached the logging)"
    assert all(PNG not in repr(r) for r in recorded), "the picture reached the experience record"
    if queued:
        assert queued[0]["attachments"] and queued[0]["attachments"][0]["data"] == PNG
