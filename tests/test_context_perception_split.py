"""Tests for the vessel-perception / conversation split in chat_context_manager.

Option B (AGENTS.md §5c, CHANGELOG): Synth's own autonomous world perceptions
(marked with ``metadata["vessel_perception"]``) are stored in a SEPARATE
in-memory ring buffer so a burst of perceptions (e.g. repeated environmental
damage while drowning) can never *evict* a player's chat from the bounded
conversational deque. The split is structural (a persisted metadata flag),
never keyword matching.
"""

from unittest.mock import AsyncMock, patch

import pytest

import core.chat_context_manager as ccm


@pytest.fixture(autouse=True)
def _clean_stores() -> None:
    ccm.get_context_memory().clear()
    ccm.get_perception_memory().clear()
    yield
    ccm.get_context_memory().clear()
    ccm.get_perception_memory().clear()


async def _add(path: str, text: str, *, perception: bool, name: str = "u") -> None:
    meta = (
        {"vessel_perception": True, "vessel_event_type": "damage"}
        if perception
        else None
    )
    # Avoid DB persistence / interface-path touch side effects during the test.
    with (
        patch("core.chat_history_cache.save_chat_message", new=AsyncMock()),
        patch("core.interface_paths.touch_interface_path", new=AsyncMock()),
    ):
        await ccm.add_message_to_context(
            interface_path=path,
            message_text=text,
            sender_name=name,
            sender_id=name,
            metadata=meta,
        )


@pytest.mark.asyncio
async def test_perceptions_do_not_evict_player_chat() -> None:
    path = "vessel/minecraft"
    # One player chat, then a big burst of autonomous damage perceptions far
    # exceeding the conversational deque maxlen.
    await _add(path, "XargonWan: Rekku, ci sei?", perception=False, name="XargonWan")
    for _ in range(50):
        await _add(path, "Took damage", perception=True, name="Rekku")

    conv = ccm.get_or_create_chat_context(path)
    perc = ccm.get_or_create_perception_context(path)

    # The player's chat must still be present in the conversational deque.
    conv_texts = [m.get("text") for m in conv]
    assert "XargonWan: Rekku, ci sei?" in conv_texts
    # No perception ever landed in the conversational deque.
    assert all(
        not (
            isinstance(m.get("metadata"), dict)
            and m["metadata"].get("vessel_perception")
        )
        for m in conv
    )
    # Perceptions went to their own buffer and are bounded by its maxlen.
    assert len(perc) == ccm._PERCEPTION_MEMORY_MAXLEN
    assert all(
        isinstance(m.get("metadata"), dict) and m["metadata"].get("vessel_perception")
        for m in perc
    )


@pytest.mark.asyncio
async def test_plain_conversation_untouched_when_no_vessel() -> None:
    path = "telegram_bot/123"
    await _add(path, "hi", perception=False)
    await _add(path, "there", perception=False)

    conv = ccm.get_or_create_chat_context(path)
    assert [m.get("text") for m in conv] == ["hi", "there"]
    # No perception buffer created for a non-vessel path.
    assert path not in ccm.get_perception_memory()
