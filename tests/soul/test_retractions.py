"""Tests for retracted claims (core.soul.retractions + repository)."""

import pytest

from core.soul.models import RetractedClaim, retracted_claim_id
from core.soul.repository import InMemorySoulRepository
from core.soul.retractions import (
    claim_matching,
    render_known_false_block,
    render_pending_block,
)


def _claim(
    subject: str = "Ca' Senz'Autobus trip",
    claim: str = "Jay and the persona are going to Ca' Senz'Autobus",
    status: str = "active",
    reason: str = "it was a joke",
) -> RetractedClaim:
    return RetractedClaim(
        id="", subject=subject, claim=claim, status=status, reason=reason
    )


@pytest.fixture
def repo() -> InMemorySoulRepository:
    return InMemorySoulRepository()


def test_id_is_stable_and_case_insensitive() -> None:
    assert retracted_claim_id("A b", "X") == retracted_claim_id(" a B ", "x")
    assert retracted_claim_id("A b", "X") != retracted_claim_id("A b", "Y")


@pytest.mark.asyncio
async def test_upsert_is_idempotent_and_lists_by_status(
    repo: InMemorySoulRepository,
) -> None:
    first = await repo.upsert_retracted_claim(_claim())
    second = await repo.upsert_retracted_claim(_claim())
    assert first == second
    assert len(await repo.list_retracted_claims()) == 1
    assert len(await repo.list_retracted_claims(("active",))) == 1
    assert await repo.list_retracted_claims(("pending",)) == []


@pytest.mark.asyncio
async def test_pending_does_not_demote_active(repo: InMemorySoulRepository) -> None:
    await repo.upsert_retracted_claim(_claim(status="active"))
    await repo.upsert_retracted_claim(_claim(status="pending"))
    (stored,) = await repo.list_retracted_claims()
    assert stored.status == "active"


@pytest.mark.asyncio
async def test_lift_and_reactivate(repo: InMemorySoulRepository) -> None:
    claim_id = await repo.upsert_retracted_claim(_claim())
    await repo.set_retraction_status(claim_id, "lifted")
    (stored,) = await repo.list_retracted_claims()
    assert stored.status == "lifted"
    assert stored.lifted_at is not None
    assert await repo.list_retracted_claims(("active",)) == []
    await repo.upsert_retracted_claim(_claim(status="active"))
    (stored,) = await repo.list_retracted_claims(("active",))
    assert stored.status == "active"


def test_matching_by_subject_and_text() -> None:
    claims = [_claim()]
    assert claim_matching(claims, subject="Trip to Ca' Senz'Autobus") is claims[0]
    assert (
        claim_matching(claims, text="Preparing for ca senz autobus soon") is claims[0]
    )
    assert claim_matching(claims, subject="Dentist appointment") is None
    assert claim_matching(claims, text="We talked about buses") is None


def test_bare_word_subject_never_matches_free_text() -> None:
    claims = [_claim(subject="Autobus")]
    assert claim_matching(claims, text="the autobus was late") is None


def test_blocks() -> None:
    assert render_known_false_block([]) == ""
    block = render_known_false_block([_claim()])
    assert "FALSE" in block and "Ca' Senz'Autobus" in block and "joke" in block
    pending = render_pending_block([_claim(status="pending")])
    assert "trainer confirms" in pending


def test_strip_claim_sentences_keeps_the_rest() -> None:
    from core.soul.retractions import strip_claim_sentences

    text = (
        "You helped Jay with InsightFace. You are also preparing for your trip to "
        "Ca' Senz'Autobus with Jay, ready for the journey. You like quiet nights."
    )
    out = strip_claim_sentences(text, [_claim()])
    assert "Senz'Autobus" not in out
    assert "InsightFace" in out and "quiet nights" in out
    assert strip_claim_sentences(text, []) == text


def test_strip_claim_sentences_keeps_line_structure() -> None:
    from core.soul.retractions import strip_claim_sentences

    text = "2026-10-01: pizza night\n2026-10-02: packing for Ca' Senz'Autobus trip\n2026-10-03: rain"
    out = strip_claim_sentences(text, [_claim()])
    assert out.split("\n")[0] == "2026-10-01: pizza night"
    assert out.split("\n")[2] == "2026-10-03: rain"
    assert "Senz" not in out


def test_drop_claim_items() -> None:
    from core.soul.retractions import drop_claim_items

    items = ["went to the lake", "trip to Ca' Senz'Autobus planned"]
    assert drop_claim_items(items, [_claim()]) == ["went to the lake"]
