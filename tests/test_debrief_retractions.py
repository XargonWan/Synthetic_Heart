"""Retractions learned by the situational-notes debrief.

When the human says something the persona believes is false, the debrief stores
a retraction (immediately for the trainer, pending for anyone else), retires what
still stands on the claim, and refuses to file notes that restate it.
"""

import importlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from core.soul.models import SituationalNote
from core.soul.repository import InMemorySoulRepository
from core.soul.retraction_flow import (
    parse_lifted,
    parse_retractions,
)
from plugins.debrief.debrief_situational_notes import (
    _EXTRACT_INSTRUCTIONS,
    DebriefSituationalNotesPlugin,
)

TRIP = "trip to Ca' Senz'Autobus"


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    llm_json: dict[str, Any],
    repository: InMemorySoulRepository,
    extra_plugins: dict[str, Any] | None = None,
) -> dict[str, Any]:
    monkeypatch.setattr(
        "plugins.debrief.debrief_situational_notes.config_registry.get_value",
        lambda key, default=None, value_type=None, **_: default,
    )
    seen: dict[str, Any] = {}

    class _Engine:
        async def generate_response(self, messages: list[dict[str, Any]]) -> str:
            seen["messages"] = messages
            return json.dumps(llm_json)

    class _Registry:
        def get_engine(self, name: str) -> _Engine:
            return _Engine()

        def load_engine(self, name: str) -> _Engine:
            return _Engine()

    async def _active(scope: Any = None) -> str:
        return "dummy"

    monkeypatch.setattr("core.config.derive_cortex_scope", lambda ctx: "base")
    monkeypatch.setattr("core.config.get_active_cortex_engine", _active)
    monkeypatch.setattr("core.cortex_registry.get_cortex_registry", lambda: _Registry())
    registry: dict[str, Any] = {
        "soul_plugin": SimpleNamespace(get_repository=lambda: repository)
    }
    registry.update(extra_plugins or {})
    monkeypatch.setattr("core.core_initializer.PLUGIN_REGISTRY", registry)
    return seen


def _turn(*, trainer: bool, text: str = "Ca' Senz'Autobus non esiste, era una battuta"):
    message = SimpleNamespace(
        text=text,
        chat_id=42,
        thread_id=None,
        interface_path="telegram_bot/42",
        from_cortex=True,
        session_id="s1",
        from_user=SimpleNamespace(id=1, full_name="Jay", username="jay"),
    )
    context = {
        "from_cortex": True,
        "original_user_message": text,
        "llm_response_text": "Ah, vedi? Lo sapevo, partiamo per Ca' Senz'Autobus!",
        "interface_path": "telegram_bot/42",
        "session_id": "s1",
        "is_trainer": trainer,
    }
    return message, context


def _trip_note() -> SituationalNote:
    now = datetime.now(timezone.utc)
    return SituationalNote(
        id="tsc-trip",
        note_type="INTERVAL",
        subject=TRIP,
        summary="Off-grid trip to Ca' Senz'Autobus with Jay.",
        valid_from=now - timedelta(hours=1),
        valid_until=now + timedelta(days=20),
        status="active",
    )


RETRACTION = {
    "subject": TRIP,
    "claim": "Jay and the persona are going to Ca' Senz'Autobus",
    "reason": "it does not exist, it was a joke",
}


def test_instructions_name_the_new_channels_and_the_human_only_rule() -> None:
    assert '"retracted"' in _EXTRACT_INSTRUCTIONS
    assert '"lifted"' in _EXTRACT_INSTRUCTIONS
    assert "Only the human's own words can retract" in _EXTRACT_INSTRUCTIONS
    assert "A note must rest on the HUMAN's words" in _EXTRACT_INSTRUCTIONS


def test_parsers_accept_the_documented_shapes_and_reject_junk() -> None:
    parsed = {"retracted": [RETRACTION, {"subject": ""}, "x", {"claim": "no subject"}]}
    items = parse_retractions(parsed)
    assert [i["subject"] for i in items] == [TRIP]
    assert parse_retractions({"retracted": RETRACTION})[0]["reason"].startswith(
        "it does"
    )
    assert parse_retractions("nonsense") == []
    assert parse_lifted({"lifted": ["a b", {"subject": "c d"}, "  ", 3]}) == [
        "a b",
        "c d",
    ]


@pytest.mark.asyncio
async def test_trainer_retraction_is_active_and_retires_the_standing_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = InMemorySoulRepository()
    await repo.upsert_situational_note(_trip_note())
    _install(monkeypatch, llm_json={"retracted": [RETRACTION]}, repository=repo)
    message, context = _turn(trainer=True)

    await DebriefSituationalNotesPlugin().on_debrief([], [], {}, context, message)

    (claim,) = await repo.list_retracted_claims()
    assert claim.status == "active"
    assert claim.asserted_by == "Jay"
    assert "telegram_bot/42" in claim.source_ref
    assert repo.situational_notes["tsc-trip"].status == "resolved"


@pytest.mark.asyncio
async def test_other_humans_retraction_waits_for_the_trainer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = InMemorySoulRepository()
    await repo.upsert_situational_note(_trip_note())
    _install(monkeypatch, llm_json={"retracted": [RETRACTION]}, repository=repo)
    message, context = _turn(trainer=False)
    message.from_user = SimpleNamespace(id=99, full_name="Stranger", username=None)

    await DebriefSituationalNotesPlugin().on_debrief([], [], {}, context, message)

    (claim,) = await repo.list_retracted_claims()
    assert claim.status == "pending"
    assert repo.situational_notes["tsc-trip"].status == "active"


@pytest.mark.asyncio
async def test_a_note_restating_a_retracted_claim_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = InMemorySoulRepository()
    first_message, first_ctx = _turn(trainer=True)
    _install(monkeypatch, llm_json={"retracted": [RETRACTION]}, repository=repo)
    await DebriefSituationalNotesPlugin().on_debrief(
        [], [], {}, first_ctx, first_message
    )

    # A later turn: the persona insists, and the model files the trip again.
    now = datetime.now(timezone.utc)
    note = {
        "note_type": "INTERVAL",
        "subject": "upcoming trip to Ca' Senz'Autobus",
        "summary": "Jay and the AI discussed the trip to Ca' Senz'Autobus.",
        "priority": 1,
        "confidence": 1.0,
        "valid_from": now.isoformat(),
        "valid_until": (now + timedelta(days=30)).isoformat(),
    }
    seen = _install(monkeypatch, llm_json={"notes": [note]}, repository=repo)
    message, context = _turn(trainer=False, text="Ca' Senz'Autobus")
    await DebriefSituationalNotesPlugin().on_debrief([], [], {}, context, message)

    assert repo.situational_notes == {}
    prompt = seen["messages"][1]["content"]
    assert "known false claims" in prompt
    assert "Ca' Senz'Autobus" in prompt


@pytest.mark.asyncio
async def test_unrelated_notes_still_get_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = InMemorySoulRepository()
    _install(monkeypatch, llm_json={"retracted": [RETRACTION]}, repository=repo)
    message, context = _turn(trainer=True)
    await DebriefSituationalNotesPlugin().on_debrief([], [], {}, context, message)

    now = datetime.now(timezone.utc)
    note = {
        "note_type": "EVENT",
        "subject": "Dentist appointment",
        "summary": "Dentist on 2026-10-22.",
        "valid_from": now.isoformat(),
        "valid_until": (now + timedelta(days=2)).isoformat(),
    }
    _install(monkeypatch, llm_json={"notes": [note]}, repository=repo)
    message, context = _turn(trainer=True, text="Domani ho il dentista")
    await DebriefSituationalNotesPlugin().on_debrief([], [], {}, context, message)

    assert [n.subject for n in repo.situational_notes.values()] == [
        "Dentist appointment"
    ]


@pytest.mark.asyncio
async def test_only_the_trainer_can_lift_a_retraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = InMemorySoulRepository()
    _install(monkeypatch, llm_json={"retracted": [RETRACTION]}, repository=repo)
    message, context = _turn(trainer=True)
    await DebriefSituationalNotesPlugin().on_debrief([], [], {}, context, message)

    _install(monkeypatch, llm_json={"lifted": [TRIP]}, repository=repo)
    message, context = _turn(trainer=False, text="in realtà ci andiamo")
    await DebriefSituationalNotesPlugin().on_debrief([], [], {}, context, message)
    assert (await repo.list_retracted_claims())[0].status == "active"

    message, context = _turn(trainer=True, text="in realtà ci andiamo")
    await DebriefSituationalNotesPlugin().on_debrief([], [], {}, context, message)
    assert (await repo.list_retracted_claims())[0].status == "lifted"


@pytest.mark.asyncio
async def test_a_stranger_cannot_reopen_a_claim_the_trainer_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = InMemorySoulRepository()
    _install(monkeypatch, llm_json={"retracted": [RETRACTION]}, repository=repo)
    message, context = _turn(trainer=False)
    await DebriefSituationalNotesPlugin().on_debrief([], [], {}, context, message)
    (claim,) = await repo.list_retracted_claims()
    await repo.set_retraction_status(claim.id, "rejected")

    await DebriefSituationalNotesPlugin().on_debrief([], [], {}, context, message)
    assert (await repo.list_retracted_claims())[0].status == "rejected"


@pytest.mark.asyncio
async def test_an_active_retraction_abandons_goals_and_discards_the_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = InMemorySoulRepository()
    abandoned: list[str] = []

    async def _abandon(matcher: Any, note: str = "") -> int:
        goal_texts = [
            "Prepare for the off-grid trip to Ca' Senz'Autobus with Jay",
            "Build a wool bed",
        ]
        hits = [t for t in goal_texts if matcher(t)]
        abandoned.extend(hits)
        return len(hits)

    monkeypatch.setattr(
        importlib.import_module("plugins.goals.goals"),
        "abandon_goals_matching",
        _abandon,
    )

    class _Growth:
        async def discard_pending_proposal_matching(self, claims: list[Any]) -> bool:
            return True

    _install(
        monkeypatch,
        llm_json={"retracted": [RETRACTION]},
        repository=repo,
        extra_plugins={"grillo_growth": _Growth()},
    )
    message, context = _turn(trainer=True)
    await DebriefSituationalNotesPlugin().on_debrief([], [], {}, context, message)

    assert abandoned == ["Prepare for the off-grid trip to Ca' Senz'Autobus with Jay"]


@pytest.mark.asyncio
async def test_the_trainer_confirms_a_pending_retraction_in_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = InMemorySoulRepository()
    await repo.upsert_situational_note(_trip_note())
    _install(monkeypatch, llm_json={"retracted": [RETRACTION]}, repository=repo)
    message, context = _turn(trainer=False)
    await DebriefSituationalNotesPlugin().on_debrief([], [], {}, context, message)
    assert (await repo.list_retracted_claims())[0].status == "pending"
    assert repo.situational_notes["tsc-trip"].status == "active"

    # The trainer later says the same thing; the extractor sees the pending list.
    seen = _install(monkeypatch, llm_json={"retracted": [RETRACTION]}, repository=repo)
    message, context = _turn(trainer=True, text="Confermo, non esiste")
    await DebriefSituationalNotesPlugin().on_debrief([], [], {}, context, message)

    assert "pending retractions" in seen["messages"][1]["content"]
    (claim,) = await repo.list_retracted_claims()
    assert claim.status == "active"
    assert repo.situational_notes["tsc-trip"].status == "resolved"


@pytest.mark.asyncio
async def test_the_trainer_refusing_a_pending_retraction_rejects_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = InMemorySoulRepository()
    _install(monkeypatch, llm_json={"retracted": [RETRACTION]}, repository=repo)
    message, context = _turn(trainer=False)
    await DebriefSituationalNotesPlugin().on_debrief([], [], {}, context, message)

    _install(monkeypatch, llm_json={"lifted": [TRIP]}, repository=repo)
    message, context = _turn(trainer=True, text="No, ci andiamo davvero")
    await DebriefSituationalNotesPlugin().on_debrief([], [], {}, context, message)
    assert (await repo.list_retracted_claims())[0].status == "rejected"


@pytest.mark.asyncio
async def test_operator_decisions_move_a_claim_between_statuses() -> None:
    from core.soul.models import RetractedClaim
    from core.soul.retraction_flow import apply_retractions, decide_retraction

    repo = InMemorySoulRepository()
    await repo.upsert_situational_note(_trip_note())
    await apply_retractions(repo, [RETRACTION], trainer=False, asserted_by="x")
    (claim,) = await repo.list_retracted_claims()
    assert claim.status == "pending"

    confirmed = await decide_retraction(repo, claim.id, "confirm")
    assert isinstance(confirmed, RetractedClaim) and confirmed.status == "active"
    assert repo.situational_notes["tsc-trip"].status == "resolved"

    assert (await decide_retraction(repo, claim.id, "lift")).status == "lifted"
    assert (await decide_retraction(repo, claim.id, "restore")).status == "active"
    assert (await decide_retraction(repo, claim.id, "reject")).status == "rejected"
    assert await decide_retraction(repo, "rtr-missing", "lift") is None
    with pytest.raises(ValueError):
        await decide_retraction(repo, claim.id, "delete")
