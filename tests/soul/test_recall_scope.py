"""Recall scope: a linked chat counts as the same conversation.

``SOUL_RECALL_LINKED_SESSIONS`` exists because recall judged every other chat by
a stricter bar than the one being answered: no same-chat boost, and a
conjunction floor (``score >= 0.22 AND max(similarity, lexical) >= 0.10``)
against the same-chat disjunction (``score >= 0.16 OR max(...) >= 0.08``).
Measured live on 2026-09-22, that is what kept the morning's ceremony (written in
the group chat) out of the DM's ``[Relevant memories]`` block while three legacy
diary rows and three cells from the previous evening took the slots.

Linking is opt-in per deployment and only widens a turn whose own chat is in the
list, so an unrelated chat keeps the strict cross-chat rule.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.soul.models import EmotionalTag, MemCell, MemCellRecall
from core.soul.repository import (
    InMemorySoulRepository,
    _build_recall_match,
    _passes_recall_floor,
    _same_recall_scope,
)

HERE = "telegram_bot_321"
THERE = "telegram_bot_999"
OTHER = "telegram_bot_777"


def _cell(session_id: str) -> MemCell:
    now = datetime.now(timezone.utc)
    return MemCell(
        id=f"cell-{session_id}",
        episodic_trace="Alice keeps jasmine tea in the blue tin.",
        atomic_facts=[],
        emotional_tag=EmotionalTag(
            state_snapshot={"joy": 0.1, "fear": 0.0, "sad": 0.0, "anger": 0.0},
            dominant_emotion="joy",
            intensity=0.1,
            valence=0.1,
        ),
        foresight_signals=[],
        event_timestamp=now,
        session_id=session_id,
    )


def _match(
    session_id: str,
    *,
    score: float,
    similarity: float = 0.05,
    lexical_score: float = 0.05,
) -> MemCellRecall:
    return MemCellRecall(
        cell=_cell(session_id),
        similarity=similarity,
        lexical_score=lexical_score,
        score=score,
    )


def test_same_recall_scope_covers_the_turn_and_its_linked_chats() -> None:
    scope = frozenset({HERE, THERE})

    assert _same_recall_scope(HERE, HERE, None) is True
    assert _same_recall_scope(THERE, HERE, scope) is True
    assert _same_recall_scope(THERE, HERE, None) is False
    # A scope that does not name the session being answered widens nothing,
    # whichever conversation it was computed for.
    assert _same_recall_scope(THERE, HERE, frozenset({OTHER, THERE})) is False
    assert _same_recall_scope(THERE, OTHER, scope) is False
    # No session to compare against: unchanged, still cross-chat.
    assert _same_recall_scope(HERE, None, scope) is False


def test_recall_floor_treats_a_linked_chat_as_the_same_conversation() -> None:
    """A score of 0.18 clears the same-chat floor and misses the cross-chat one."""
    match = _match(THERE, score=0.18)

    assert _passes_recall_floor(match, THERE) is True
    assert _passes_recall_floor(match, HERE) is False
    assert _passes_recall_floor(match, HERE, frozenset({HERE, THERE})) is True
    assert _passes_recall_floor(match, OTHER, frozenset({HERE, THERE})) is False


def test_linked_chat_gets_the_same_session_boost() -> None:
    now = datetime.now(timezone.utc)
    cell = _cell(THERE)

    own = _build_recall_match(
        cell=cell, similarity=0.4, lexical_score=0.3, session_id=THERE, now=now
    )
    linked = _build_recall_match(
        cell=cell,
        similarity=0.4,
        lexical_score=0.3,
        session_id=HERE,
        linked_session_ids=frozenset({HERE, THERE}),
        now=now,
    )
    unlinked = _build_recall_match(
        cell=cell, similarity=0.4, lexical_score=0.3, session_id=HERE, now=now
    )

    assert linked.score == pytest.approx(own.score)
    assert unlinked.score == pytest.approx(own.score - 0.08)


@pytest.mark.asyncio
async def test_repository_admits_a_linked_chats_cell_where_it_used_to_drop_it() -> None:
    """End to end through the repository, in the live shape.

    Total lexical overlap and zero vector similarity put the cell between the two
    floors (about 0.21): over the same-chat one at 0.16, under the cross-chat one
    at 0.22. That is the band the ceremony cell sat in.
    """
    repo = InMemorySoulRepository()
    cell = _cell(THERE)
    await repo.upsert_memcell(cell)

    unlinked = await repo.recall_memories(
        query_text="jasmine tea blue tin",
        query_embedding=[1.0, 0.0],
        session_id=HERE,
    )
    assert unlinked == [], "without linking this chat, the cross-chat bar holds"

    linked = await repo.recall_memories(
        query_text="jasmine tea blue tin",
        query_embedding=[1.0, 0.0],
        session_id=HERE,
        linked_session_ids=frozenset({HERE, THERE}),
    )
    assert [match.cell.id for match in linked] == [cell.id]
