"""The Core services behind ``turns.request``, ``voice.*`` and Recon questions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.extensions.core_interfaces import CoreReconHost, CoreTurns, CoreVoice
from core.extensions.interfaces import CONTEXT_KEY


@pytest.mark.asyncio
async def test_a_turn_is_queued_like_background_work(monkeypatch) -> None:
    from core import message_queue

    queued: list[dict[str, Any]] = []

    async def fake(bot, message, **kwargs):  # noqa: ANN001
        queued.append({"bot": bot, "message": message, **kwargs})

    monkeypatch.setattr(message_queue, "enqueue_low_priority", fake)
    await CoreTurns().request(
        extension_id="dev/radio",
        interface_id="radio",
        path="radio/-2",
        text="Comment on the song.",
        sender_name="Radio",
        priority="elevated",
        context={"track": "A"},
        allowed_actions=["speak"],
        history_recent_max=4,
    )
    (item,) = queued
    assert item["priority"] == message_queue.PRIORITY_ELEVATED
    assert item["priority"] < message_queue.PRIORITY_TRAINER  # never above people
    assert item["interface_id"] == "radio" and item["original_message"] is None
    message = item["message"]
    assert message.chat_id == -2 and message.text == "Comment on the song."
    assert message.from_user.username == "radio"
    memory = item["context_memory"]
    assert memory["skip_history"] is True
    assert memory["history_recent_max"] == 4
    assert memory["allowed_action_types"] == ["speak"]
    # an extension's turn is not an outbound beat: it cannot message anyone
    from core.beat_utils import is_outbound_beat

    assert not is_outbound_beat(memory["beat_type"])
    assert memory[CONTEXT_KEY] == {
        "extension_id": "dev/radio",
        "interface_id": "radio",
        "context": {"track": "A"},
    }


@pytest.mark.asyncio
async def test_every_named_band_maps_below_the_trainer(monkeypatch) -> None:
    from core import message_queue

    seen: list[int] = []

    async def fake(bot, message, **kwargs):  # noqa: ANN001
        seen.append(kwargs["priority"])

    monkeypatch.setattr(message_queue, "enqueue_low_priority", fake)
    for band in ("background", "low", "ambient", "general", "elevated"):
        await CoreTurns().request(
            extension_id="dev/x",
            interface_id="x",
            path="x/room",
            text="t",
            sender_name="X",
            priority=band,
            context={},
            allowed_actions=["a"],
            history_recent_max=0,
        )
    assert seen == sorted(seen) and len(set(seen)) == 5
    assert max(seen) < message_queue.PRIORITY_TRAINER
    assert message_queue.priority_label(max(seen)) == "Elevated"


class FakeVox:
    def __init__(self, audio: Path | None, engine: str = "http") -> None:
        self._active_engine_name = engine
        self.audio = audio
        self.calls: list[dict[str, Any]] = []
        self.broadcast: list[dict[str, Any]] = []

    async def speak(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"audio_path": str(self.audio)} if self.audio else {"status": "error"}

    async def broadcast_audio_to_webui(self, **kwargs: Any) -> bool:
        self.broadcast.append(kwargs)
        return True


def with_vox(monkeypatch, vox: FakeVox | None) -> None:  # noqa: ANN001
    from core.core_initializer import PLUGIN_REGISTRY

    monkeypatch.delitem(PLUGIN_REGISTRY, "vox_plugin", raising=False)
    monkeypatch.delitem(PLUGIN_REGISTRY, "vox", raising=False)
    if vox is not None:
        monkeypatch.setitem(PLUGIN_REGISTRY, "vox_plugin", vox)


@pytest.mark.asyncio
async def test_the_voice_is_made_by_vox_without_sending_it_anywhere(
    monkeypatch, tmp_path: Path
) -> None:
    clip = tmp_path / "a.wav"
    clip.write_bytes(b"RIFF")
    vox = FakeVox(clip)
    with_vox(monkeypatch, vox)
    assert await CoreVoice().synthesize("  Hello  ", "") == str(clip)
    assert vox.calls == [{"text": "Hello", "engine_name": None, "generate_only": True}]


@pytest.mark.asyncio
async def test_the_core_does_not_clean_text_for_an_engine_by_name(
    monkeypatch, tmp_path: Path
) -> None:
    """What an engine cannot say is the engine's to clean: the Core passes the text on."""
    clip = tmp_path / "a.wav"
    clip.write_bytes(b"RIFF")
    vox = FakeVox(clip, engine="kitten")
    with_vox(monkeypatch, vox)
    await CoreVoice().synthesize("Ciao ☺ 世界 amici", "")
    assert vox.calls[-1]["text"] == "Ciao ☺ 世界 amici"


@pytest.mark.asyncio
async def test_no_vox_or_no_file_is_none_not_an_error(
    monkeypatch, tmp_path: Path
) -> None:
    with_vox(monkeypatch, None)
    assert await CoreVoice().synthesize("hi", "") is None
    assert await CoreVoice().play("x", "", "") is False
    with_vox(monkeypatch, FakeVox(tmp_path / "missing.wav"))
    assert await CoreVoice().synthesize("hi", "") is None


@pytest.mark.asyncio
async def test_a_clip_is_played_on_the_stage_through_vox(monkeypatch) -> None:
    vox = FakeVox(None)
    with_vox(monkeypatch, vox)
    assert await CoreVoice().play("/core/copy.wav", "caption", "kitten") is True
    assert vox.broadcast == [
        {"audio_path": "/core/copy.wav", "text": "caption", "engine_name": "kitten"}
    ]


def test_recon_proxies_live_where_the_core_looks_and_only_theirs_are_removed() -> None:
    from core.core_initializer import PLUGIN_REGISTRY

    host, mine, other = CoreReconHost(), object(), object()
    host.add("ext_recon_test", mine)
    try:
        assert PLUGIN_REGISTRY["ext_recon_test"] is mine
        host.remove("ext_recon_test", other)  # not the one that was added
        assert PLUGIN_REGISTRY["ext_recon_test"] is mine
        host.remove("ext_recon_test", mine)
        assert "ext_recon_test" not in PLUGIN_REGISTRY
    finally:
        PLUGIN_REGISTRY.pop("ext_recon_test", None)
    assert isinstance(host.taken_keys(), set)
