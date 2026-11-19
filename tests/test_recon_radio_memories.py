from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from core.soul.compiler import NoopEmbedder
from core.soul.models import EmotionalTag, MemCell
from core.soul.repository import InMemorySoulRepository
from core.soul.service import SoulService
from plugins.radio_host.recon_radio_status import (
    RADIO_SESSION_PREFIXES,
    ReconRadioStatusPlugin,
)
from plugins.soul_plugin import SoulPlugin, _SessionState


def _cell(cell_id: str, session: str, text: str) -> MemCell:
    when = datetime.now(timezone.utc)
    return MemCell(
        id=cell_id,
        episodic_trace=text,
        atomic_facts=[],
        emotional_tag=EmotionalTag(
            state_snapshot={"joy": 0.0, "fear": 0.0, "sad": 0.0, "anger": 0.0},
            dominant_emotion="neutral",
            intensity=0.0,
            valence=0.0,
        ),
        foresight_signals=[],
        event_timestamp=when,
        session_id=session,
        consolidated=True,
        distilled_at=when,
    )


async def _store() -> InMemorySoulRepository:
    repo = InMemorySoulRepository()
    embedder = NoopEmbedder()
    for cell in (
        _cell(
            "r1",
            "radio_host_-2",
            "on air I played a synthwave playlist set for the listeners",
        ),
        _cell("c1", "telegram_bot_-5", "the synthwave playlist we discussed last week"),
    ):
        cell.embedding = await embedder.embed(cell.episodic_trace)
        repo.memcells[cell.id] = cell
    return repo


@pytest.mark.asyncio
async def test_passive_recall_leaves_the_radio_out_and_on_demand_recall_finds_it() -> (
    None
):
    repo = await _store()
    embedder = NoopEmbedder()

    plugin = SoulPlugin()
    plugin._repo = repo
    plugin._compiler = SimpleNamespace(embedder=embedder)
    passive = await plugin._recall_memories(
        interface_path="telegram_bot/9",
        incoming_text="synthwave playlist",
        session=_SessionState(),
    )
    assert passive and not any("on air" in line for line in passive)

    service = SoulService(repo, embedder)
    on_demand = await service.recall(
        "synthwave playlist",
        session_prefixes=RADIO_SESSION_PREFIXES,
    )
    assert [m.cell.id for m in on_demand] == ["r1"]


@pytest.mark.asyncio
async def test_a_radio_turn_adds_radio_memories_and_other_turns_do_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = await _store()
    service = SoulService(repo, NoopEmbedder())
    monkeypatch.setattr(SoulService, "from_registry", classmethod(lambda cls: service))

    recon = ReconRadioStatusPlugin()

    async def _status() -> dict[str, Any]:
        return {"online": True, "station_name": "Test FM"}

    monkeypatch.setattr(
        recon, "_find_radio_plugin", lambda: SimpleNamespace(get_live_status=_status)
    )

    contributions = await recon.parse_recon_response(
        {"radio_status": True}, text="synthwave playlist"
    )
    kinds = [c["type"] for c in contributions]
    assert kinds[0] == "snippet" and "memory" in kinds
    memory = next(c for c in contributions if c["type"] == "memory")
    assert memory["content"]["source"] == "radio_memory"
    assert "on air" in memory["content"]["snippet"]
    assert all(
        "discussed last week" not in c["content"].get("snippet", "")
        for c in contributions
        if c["type"] == "memory"
    )

    # Not about the radio: the verdict is False, nothing is asked for.
    assert await recon.parse_recon_response({"radio_status": False}, text="hello") == []
