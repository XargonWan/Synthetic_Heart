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

    async def upsert_situational_note(self, note: Any) -> str:
        self.notes.append(note)
        return note.id or "generated"


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
