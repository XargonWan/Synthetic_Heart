"""Tests for the Debrief situational-notes plugin.

The plugin extracts short-lived, time-bounded circumstances from a finished turn
and hands them to the SOUL store. It owns the extraction only: the model, the
persistence and the prompt injection stay in SOUL.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from plugins.debrief.debrief_situational_notes import DebriefSituationalNotesPlugin


def _plugin_config_value(
    key: str,
    default: Any = None,
    value_type: Any = None,
    **_: Any,
) -> Any:
    overrides = {
        "SITUATIONAL_NOTES_DEBRIEF_ENABLED": True,
        "SITUATIONAL_NOTES_MAX": 8,
    }
    return overrides.get(key, default)


class _RecordingRepository:
    """Minimal stand-in for the SOUL repository."""

    def __init__(self) -> None:
        self.notes: list[Any] = []
        self.resolved: list[tuple[str, str]] = []

    async def upsert_situational_note(self, note: Any) -> str:
        self.notes.append(note)
        return note.id or "generated"

    async def list_active_situational_notes(
        self, now: Any, subject: Any = None
    ) -> list[Any]:
        return [n for n in self.notes if getattr(n, "status", "active") == "active"]

    async def resolve_situational_note(
        self, note_id: str, new_status: str, summary_delta: Any = None
    ) -> None:
        self.resolved.append((note_id, new_status))
        for note in self.notes:
            if note.id == note_id:
                note.status = new_status


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    llm_text: str,
    repository: Any | None,
    corrector_text: str | None = None,
) -> dict[str, Any]:
    """Wire config, cortex engine, plugin registry and corrector for one turn."""
    monkeypatch.setattr(
        "plugins.debrief.debrief_situational_notes.config_registry.get_value",
        _plugin_config_value,
    )

    calls: dict[str, Any] = {"generated": 0, "corrected": 0}

    class DummyEngine:
        async def generate_response(self, messages: list[dict[str, Any]]) -> str:
            calls["generated"] += 1
            calls["messages"] = messages
            return llm_text

    class DummyRegistry:
        def get_engine(self, name: str) -> DummyEngine:
            return DummyEngine()

        def load_engine(self, name: str) -> DummyEngine:
            return DummyEngine()

    async def fake_active_cortex_engine(scope: Any = None) -> str:
        return "dummy"

    async def fake_corrector(
        text: str,
        bot: Any = None,
        context: dict[str, Any] | None = None,
        chat_id: Any = None,
        thread_id: Any = None,
    ) -> str:
        calls["corrected"] += 1
        return corrector_text or ""

    monkeypatch.setattr("core.config.derive_cortex_scope", lambda ctx: "base")
    monkeypatch.setattr(
        "core.config.get_active_cortex_engine", fake_active_cortex_engine
    )
    monkeypatch.setattr(
        "core.cortex_registry.get_cortex_registry", lambda: DummyRegistry()
    )
    monkeypatch.setattr(
        "plugins.debrief.debrief_situational_notes.run_corrector_middleware",
        fake_corrector,
    )

    soul = SimpleNamespace(get_repository=lambda: repository)
    registry = {"soul_plugin": soul} if repository is not None else {}
    monkeypatch.setattr("core.core_initializer.PLUGIN_REGISTRY", registry)

    return calls


def _turn() -> tuple[Any, dict[str, Any]]:
    original_message = SimpleNamespace(
        text="Domani ho il dentista e la settimana prossima parto per Osaka",
        chat_id=42,
        thread_id=7,
        interface_path="telegram/42",
        from_cortex=True,
        session_id="sess-1",
    )
    context = {
        "from_cortex": True,
        "original_user_message": original_message.text,
        "llm_response_text": "Me lo segno, in bocca al lupo per il dentista.",
        "interface_path": "telegram/42",
        "session_id": "sess-1",
    }
    return original_message, context


@pytest.mark.asyncio
async def test_extracted_notes_reach_the_soul_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A well-formed extraction is validated and persisted."""
    repo = _RecordingRepository()
    _install(
        monkeypatch,
        llm_text=(
            '{"notes":[{"note_type":"EVENT","subject":"dentist",'
            '"summary":"Dentist appointment tomorrow afternoon",'
            '"priority":1,"confidence":0.8,'
            '"valid_until":"2026-09-12T00:00:00+00:00"}]}'
        ),
        repository=repo,
    )

    original_message, context = _turn()
    result = await DebriefSituationalNotesPlugin().on_debrief(
        processed_actions=[],
        failed_actions=[],
        results={},
        context=context,
        original_message=original_message,
    )

    # The plugin feeds the store; it proposes no recovery actions.
    assert result is None
    assert len(repo.notes) == 1
    stored = repo.notes[0]
    assert stored.note_type == "EVENT"
    assert stored.summary == "Dentist appointment tomorrow afternoon"
    assert stored.session_id == "sess-1"
    # The id is left for the repository to derive, so the same circumstance
    # seen twice updates one row instead of accumulating duplicates.
    assert stored.id == ""
    assert stored.valid_from.tzinfo is not None


@pytest.mark.asyncio
async def test_turn_outside_cortex_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-cortex turns never reach the LLM."""
    repo = _RecordingRepository()
    calls = _install(monkeypatch, llm_text='{"notes":[]}', repository=repo)

    original_message, context = _turn()
    context["from_cortex"] = False
    original_message.from_cortex = False

    result = await DebriefSituationalNotesPlugin().on_debrief(
        processed_actions=[],
        failed_actions=[],
        results={},
        context=context,
        original_message=original_message,
    )

    assert result is None
    assert calls["generated"] == 0
    assert repo.notes == []


@pytest.mark.asyncio
async def test_missing_soul_plugin_does_not_break_the_debrief(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without SOUL there is nowhere to store notes, and that is not an error.

    Removing a plugin must never break another one, so the extraction simply
    finds no store and the debrief carries on.
    """
    _install(
        monkeypatch,
        llm_text=(
            '{"notes":[{"note_type":"STATE","subject":"moving",'
            '"summary":"Moving house this week","priority":0,"confidence":0.6,'
            '"valid_until":"2026-09-17T00:00:00+00:00"}]}'
        ),
        repository=None,
    )

    original_message, context = _turn()
    result = await DebriefSituationalNotesPlugin().on_debrief(
        processed_actions=[],
        failed_actions=[],
        results={},
        context=context,
        original_message=original_message,
    )

    assert result is None


@pytest.mark.asyncio
async def test_broken_json_goes_through_the_corrector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed output is handed to the corrector before being given up on."""
    repo = _RecordingRepository()
    calls = _install(
        monkeypatch,
        llm_text="{not valid json",
        repository=repo,
        corrector_text=(
            '{"notes":[{"note_type":"INTERVAL","subject":"trip",'
            '"summary":"Travelling to Osaka next week","priority":2,'
            '"confidence":0.7,"valid_until":"2026-09-18T00:00:00+00:00"}]}'
        ),
    )

    original_message, context = _turn()
    await DebriefSituationalNotesPlugin().on_debrief(
        processed_actions=[],
        failed_actions=[],
        results={},
        context=context,
        original_message=original_message,
    )

    assert calls["corrected"] == 1
    assert len(repo.notes) == 1
    assert repo.notes[0].subject == "trip"


@pytest.mark.asyncio
async def test_empty_extraction_skips_the_corrector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Nothing temporal was said" is a valid answer, not something to correct."""
    repo = _RecordingRepository()
    calls = _install(monkeypatch, llm_text='{"notes":[]}', repository=repo)

    original_message, context = _turn()
    await DebriefSituationalNotesPlugin().on_debrief(
        processed_actions=[],
        failed_actions=[],
        results={},
        context=context,
        original_message=original_message,
    )

    assert calls["corrected"] == 0
    assert repo.notes == []


@pytest.mark.asyncio
async def test_turn_content_reaches_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the human said must actually appear in the prompt sent to the LLM.

    Regression: the turn was passed as a JSON string. An OpenAI-compatible
    backend parsed it as structured content, kept only the keys it recognised
    and dropped the rest, so the model received the instructions alone and
    answered with an empty note list — with no error logged anywhere.
    """
    repo = _RecordingRepository()
    calls = _install(monkeypatch, llm_text='{"notes":[]}', repository=repo)

    original_message, context = _turn()
    await DebriefSituationalNotesPlugin().on_debrief(
        processed_actions=[],
        failed_actions=[],
        results={},
        context=context,
        original_message=original_message,
    )

    sent = calls["messages"]
    user_part = next(m["content"] for m in sent if m["role"] == "user")
    assert original_message.text in user_part
    assert context["llm_response_text"] in user_part
    # A bare JSON object is exactly what got swallowed before.
    assert not user_part.strip().startswith("{")


def test_extract_instructions_scope_notes_to_the_human_and_canonical_subjects() -> None:
    """The prompt must carry the rules the live store was missing.

    Two live defects came from the prompt, not the code: notes about the
    persona's own state and about third parties ("2D recovering from an intense
    night of drinking") were stored as the human's situation, and one event was
    re-described under a dozen different subjects ("Gathering at Sandro's",
    "Gathering tonight", "Human", "upcoming outing"), so the store accumulated
    twelve active notes for a single evening.
    """
    from plugins.debrief.debrief_situational_notes import _EXTRACT_INSTRUCTIONS

    assert "EVERY NOTE MUST BE ABOUT THE HUMAN'S CIRCUMSTANCES" in _EXTRACT_INSTRUCTIONS
    assert "SHORT canonical noun phrase" in _EXTRACT_INSTRUCTIONS
    assert "Never use a bare person's name" in _EXTRACT_INSTRUCTIONS
    assert "belong to the persona's diary" in _EXTRACT_INSTRUCTIONS


def _older_note(subject: str, summary: str) -> Any:
    from datetime import datetime, timezone

    from core.soul.models import situational_note_from_extraction

    note = situational_note_from_extraction(
        note_type="EVENT",
        subject=subject,
        summary=summary,
        valid_until=datetime(2026, 9, 19, tzinfo=timezone.utc),
        now=datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc),
    )
    note.id = "tsc-older-account"
    return note


@pytest.mark.asyncio
async def test_a_re_description_supersedes_the_older_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One circumstance, many accounts: the newest wins, the older ones retire.

    Live effect of the old behaviour (2026-09-18): twelve active notes for one
    evening gathering, two of them contradicting each other outright ("took place
    last night and went fine" next to "expected tonight").
    """
    repo = _RecordingRepository()
    repo.notes.append(
        _older_note(
            "Gathering at Sandro's",
            "A gathering at Sandro's is happening tonight.",
        )
    )
    _install(
        monkeypatch,
        llm_text=(
            '{"notes":[{"note_type":"EVENT","subject":"Gathering at Sandro\'s tonight",'
            '"summary":"The gathering at Sandro\'s happened last night and went fine.",'
            '"priority":1,"confidence":0.85,'
            '"valid_until":"2026-09-19T06:00:00+00:00"}]}'
        ),
        repository=repo,
    )

    original_message, context = _turn()
    await DebriefSituationalNotesPlugin().on_debrief(
        processed_actions=[],
        failed_actions=[],
        results={},
        context=context,
        original_message=original_message,
    )

    assert repo.resolved == [("tsc-older-account", "superseded")]
    assert repo.notes[0].status == "superseded"
    # The note just stored stays active; it is never retired by its own write.
    assert repo.notes[-1].status == "active"


@pytest.mark.asyncio
async def test_unrelated_notes_are_not_superseded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _RecordingRepository()
    repo.notes.append(
        _older_note(
            "Scar takes twice-daily medication",
            "Scar takes pills twice a day before sleeping.",
        )
    )
    _install(
        monkeypatch,
        llm_text=(
            '{"notes":[{"note_type":"EVENT","subject":"Gathering at Sandro\'s tonight",'
            '"summary":"A gathering at Sandro\'s is happening tonight.",'
            '"priority":1,"confidence":0.8,'
            '"valid_until":"2026-09-19T06:00:00+00:00"}]}'
        ),
        repository=repo,
    )

    original_message, context = _turn()
    await DebriefSituationalNotesPlugin().on_debrief(
        processed_actions=[],
        failed_actions=[],
        results={},
        context=context,
        original_message=original_message,
    )

    assert repo.resolved == []
    assert repo.notes[0].status == "active"


def _note(note_id: str, subject: str, summary: str) -> Any:
    from datetime import datetime, timezone

    from core.soul.models import situational_note_from_extraction

    note = situational_note_from_extraction(
        note_type="STATE",
        subject=subject,
        summary=summary,
        valid_until=datetime(2026, 9, 20, tzinfo=timezone.utc),
        now=datetime(2026, 9, 19, 0, 0, tzinfo=timezone.utc),
    )
    note.id = note_id
    return note


def test_ended_subjects_are_parsed_from_both_shapes() -> None:
    """The ended channel accepts plain strings and objects, and ignores junk."""
    plugin = DebriefSituationalNotesPlugin()

    assert plugin._extract_ended_subjects({"ended": ["Sore cock"]}) == ["Sore cock"]
    assert plugin._extract_ended_subjects({"ended": [{"subject": "Sore cock"}]}) == [
        "Sore cock"
    ]
    assert plugin._extract_ended_subjects({"ended": "Sore cock"}) == ["Sore cock"]
    assert plugin._extract_ended_subjects({"notes": []}) == []
    assert plugin._extract_ended_subjects("nonsense") == []
    assert plugin._extract_ended_subjects({"ended": ["  ", 7]}) == []


def test_extract_instructions_ask_for_ended_circumstances() -> None:
    """The prompt must carry the retirement obligation, or nothing resolves."""
    from plugins.debrief.debrief_situational_notes import _EXTRACT_INSTRUCTIONS

    assert "have ENDED or been CONTRADICTED" in _EXTRACT_INSTRUCTIONS
    assert '"ended"' in _EXTRACT_INSTRUCTIONS
    assert "I'm not sore any more" in _EXTRACT_INSTRUCTIONS
    # The old contract promised only a notes list, which is what made a
    # contradiction unexpressible.
    assert "return an empty notes list when nothing time-bounded was said" not in (
        _EXTRACT_INSTRUCTIONS
    )


@pytest.mark.asyncio
async def test_a_contradicted_circumstance_resolves_the_standing_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A note the turn contradicts must stop being injected.

    Live (2026-09-19): the human said "I'm not sore" at 11:27 and again at 13:20,
    the standing STATE note stayed active with ten hours of validity left, and the
    soreness was asserted at 15:16 as present-tense fact ("you're sore, remember?
    So it's hands and mouth only tonight"). Nothing retired a note unless a
    replacement note was written about the same subject, and a correction is not a
    new circumstance.
    """
    repo = _RecordingRepository()
    repo.notes.append(_note("tsc-sore", "Sore cock", "The human's cock is sore."))
    repo.notes.append(_note("tsc-weekend", "Weekend schedule", "The weekend is free."))
    _install(
        monkeypatch,
        llm_text='{"notes":[],"ended":["Sore cock"]}',
        repository=repo,
    )

    original_message, context = _turn()
    await DebriefSituationalNotesPlugin().on_debrief(
        processed_actions=[],
        failed_actions=[],
        results={},
        context=context,
        original_message=original_message,
    )

    assert repo.resolved == [("tsc-sore", "resolved")]
    # A correction carries no new circumstance, so nothing was stored.
    assert repo.notes[0].status == "resolved"
    assert repo.notes[1].status == "active"


@pytest.mark.asyncio
async def test_an_ended_subject_leaves_unrelated_and_personal_notes_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retirement is scoped: an unmatched or too-thin subject resolves nothing."""
    repo = _RecordingRepository()
    repo.notes.append(_note("tsc-weekend", "Weekend schedule", "The weekend is free."))
    _install(
        monkeypatch,
        llm_text='{"notes":[],"ended":["Sore cock","Scar"]}',
        repository=repo,
    )

    original_message, context = _turn()
    await DebriefSituationalNotesPlugin().on_debrief(
        processed_actions=[],
        failed_actions=[],
        results={},
        context=context,
        original_message=original_message,
    )

    # "Sore cock" matches nothing here, and the bare name "Scar" is below the
    # minimum meaningful tokens, so it can never stand in for a circumstance.
    assert repo.resolved == []
    assert repo.notes[0].status == "active"


@pytest.mark.asyncio
async def test_a_shorter_subject_retires_the_longer_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Containment, not equality: the human's shorter wording still matches."""
    repo = _RecordingRepository()
    repo.notes.append(
        _note(
            "tsc-long",
            "Recovery soreness from yesterday",
            "The human is recovering and sore.",
        )
    )
    _install(
        monkeypatch,
        llm_text='{"notes":[],"ended":["Recovery soreness"]}',
        repository=repo,
    )

    original_message, context = _turn()
    await DebriefSituationalNotesPlugin().on_debrief(
        processed_actions=[],
        failed_actions=[],
        results={},
        context=context,
        original_message=original_message,
    )

    assert repo.resolved == [("tsc-long", "resolved")]
    assert repo.notes[0].status == "resolved"
