from datetime import date, datetime, timezone

import pytest

from core.soul.llm_strategies import LlmDspBuilder, LlmDspExtractor
from core.soul.models import DspExtraction


class FakeEngine:
    def __init__(self, response: str = "", *, raise_on_call: bool = False) -> None:
        self.response = response
        self.raise_on_call = raise_on_call
        self.prompts: list[dict] = []

    async def generate_response(self, prompt: object) -> str:
        self.prompts.append(prompt if isinstance(prompt, dict) else {})
        if self.raise_on_call:
            raise RuntimeError("engine down")
        return self.response


async def _resolve_to(engine: FakeEngine | None) -> FakeEngine | None:
    return engine


def _extraction(
    *,
    facts: list[str],
    prefs: list[str] | None = None,
    self_facts: list[str] | None = None,
) -> DspExtraction:
    return DspExtraction(
        id=f"dsp-extract:{len(facts) if facts else 'empty'}:{len(facts)}",
        session_id="session-x",
        extracted_at=datetime(2026, 5, 5, tzinfo=timezone.utc),
        user_facts=facts,
        user_preferences=prefs or [],
        ai_self_facts=self_facts or [],
    )


def _twice(fact: str) -> list[DspExtraction]:
    return [_extraction(facts=[fact]), _extraction(facts=[fact])]


@pytest.mark.asyncio
async def test_build_initial_uses_llm_biography() -> None:
    engine = FakeEngine(
        response='{"biography": "The user is a developer working on SynthHeart."}'
    )
    builder = LlmDspBuilder(resolve_engine=lambda: _resolve_to(engine))

    result = await builder.build_initial(extractions=_twice("User works on SynthHeart"))

    assert result.startswith("<user_profile>")
    assert "The user is a developer working on SynthHeart." in result
    assert result.endswith("</user_profile>")


@pytest.mark.asyncio
async def test_build_initial_passes_recurrence_hints() -> None:
    engine = FakeEngine(response='{"biography": "Some biography."}')
    builder = LlmDspBuilder(resolve_engine=lambda: _resolve_to(engine))
    extractions = [
        _extraction(facts=["User works on SynthHeart", "User is fixing it now"]),
        _extraction(facts=["User works on SynthHeart"]),
    ]

    await builder.build_initial(extractions=extractions)

    prompt = engine.prompts[0]
    assert prompt["input"]["type"] == "dsp_build_initial"
    user_facts = {
        entry["fact"]: entry["occurrences"]
        for entry in prompt["input"]["payload"]["user_facts"]
    }
    assert user_facts["User is fixing it now"] == 1
    assert user_facts["User works on SynthHeart"] == 2


@pytest.mark.asyncio
async def test_build_initial_falls_back_when_no_engine() -> None:
    builder = LlmDspBuilder(resolve_engine=lambda: _resolve_to(None))

    result = await builder.build_initial(extractions=_twice("User works on SynthHeart"))

    assert "<user_profile>" in result
    assert "User works on SynthHeart" in result


@pytest.mark.asyncio
async def test_build_initial_falls_back_when_generation_raises() -> None:
    engine = FakeEngine(raise_on_call=True)
    builder = LlmDspBuilder(resolve_engine=lambda: _resolve_to(engine))

    result = await builder.build_initial(extractions=_twice("User works on SynthHeart"))

    assert "User works on SynthHeart" in result


@pytest.mark.asyncio
async def test_build_initial_falls_back_on_bad_json() -> None:
    engine = FakeEngine(response="not json at all")
    builder = LlmDspBuilder(resolve_engine=lambda: _resolve_to(engine))

    result = await builder.build_initial(extractions=_twice("User works on SynthHeart"))

    assert "User works on SynthHeart" in result


@pytest.mark.asyncio
async def test_build_initial_empty_evidence_delegates_to_fallback() -> None:
    engine = FakeEngine(response='{"biography": "Should never be used."}')
    builder = LlmDspBuilder(resolve_engine=lambda: _resolve_to(engine))
    one_off = _extraction(facts=["User says they are fixing it now"])

    result = await builder.build_initial(extractions=[one_off])

    assert result.startswith("<user_profile>")
    assert "No stable facts yet." in result
    assert not engine.prompts


@pytest.mark.asyncio
async def test_build_update_self_heals_profile() -> None:
    current_dsp = (
        "<user_profile>User works on SynthHeart; User wants to try setting a "
        "minecraft goal from here</user_profile>"
    )
    engine = FakeEngine(response='{"biography": "The user works on SynthHeart."}')
    builder = LlmDspBuilder(resolve_engine=lambda: _resolve_to(engine))

    result = await builder.build_update(
        current_dsp=current_dsp,
        extractions=_twice("User works on SynthHeart"),
    )

    assert "SynthHeart" in result
    assert "minecraft goal" not in result
    assert result.startswith("<user_profile>")
    assert result.endswith("</user_profile>")


@pytest.mark.asyncio
async def test_build_update_returns_current_when_llm_unchanged() -> None:
    current_dsp = "<user_profile>User works on SynthHeart</user_profile>"
    engine = FakeEngine(response='{"biography": "User works on SynthHeart"}')
    builder = LlmDspBuilder(resolve_engine=lambda: _resolve_to(engine))

    result = await builder.build_update(
        current_dsp=current_dsp,
        extractions=_twice("User works on SynthHeart"),
    )

    assert result == current_dsp


@pytest.mark.asyncio
async def test_build_update_strips_embedded_tags() -> None:
    current_dsp = "<user_profile>User works on SynthHeart</user_profile>"
    engine = FakeEngine(
        response='{"biography": "<user_profile>The user lives in Berlin.</user_profile>"}'
    )
    builder = LlmDspBuilder(resolve_engine=lambda: _resolve_to(engine))

    result = await builder.build_update(
        current_dsp=current_dsp,
        extractions=_twice("User works on SynthHeart"),
    )

    assert result == "<user_profile>The user lives in Berlin.</user_profile>"


@pytest.mark.asyncio
async def test_build_update_falls_back_when_no_engine() -> None:
    current_dsp = (
        "<user_profile>User works on SynthHeart; User wants to try setting a "
        "minecraft goal from here</user_profile>"
    )
    builder = LlmDspBuilder(resolve_engine=lambda: _resolve_to(None))
    one_off = _extraction(facts=["User says they are fixing it now"])

    result = await builder.build_update(current_dsp=current_dsp, extractions=[one_off])

    assert "User works on SynthHeart" in result
    assert "minecraft goal" not in result


# ---------------------------------------------------------------------------
# LlmDspExtractor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_uses_llm_and_structurally_filters_facts() -> None:
    engine = FakeEngine(
        response=(
            '{"user_facts": ["User works on SynthHeart.", '
            '"User wants to try setting a minecraft goal from here"], '
            '"user_preferences": ["User prefers concise technical responses."], '
            '"ai_self_facts": []}'
        )
    )
    extractor = LlmDspExtractor(resolve_engine=lambda: _resolve_to(engine))

    result = await extractor.extract_dsp(
        transcript='[05/05/26:1200] Alice: "I work on SynthHeart."',
        current_date=date(2026, 5, 5),
    )

    # LLM output ends sentences with periods; the structural guard must not drop
    # legitimate facts just for trailing punctuation.
    assert "User works on SynthHeart" in result.user_facts
    assert "User prefers concise technical responses" in result.user_preferences
    assert not any("minecraft goal" in fact for fact in result.user_facts)


@pytest.mark.asyncio
async def test_extract_passes_raw_transcript_to_engine() -> None:
    engine = FakeEngine(
        response='{"user_facts": [], "user_preferences": [], "ai_self_facts": []}'
    )
    extractor = LlmDspExtractor(resolve_engine=lambda: _resolve_to(engine))
    transcript = '[05/05/26:1200] Alice: "I am a developer."'

    await extractor.extract_dsp(transcript=transcript, current_date=date(2026, 5, 5))

    prompt = engine.prompts[0]
    assert prompt["input"]["type"] == "dsp_extract"
    assert transcript in prompt["input"]["payload"]["transcript"]
    assert prompt["input"]["payload"]["current_date"] == "2026-05-05"


@pytest.mark.asyncio
async def test_extract_falls_back_when_no_engine() -> None:
    extractor = LlmDspExtractor(resolve_engine=lambda: _resolve_to(None))
    transcript = '[05/05/26:1200] Alice: "I am a developer"'

    result = await extractor.extract_dsp(
        transcript=transcript, current_date=date(2026, 5, 5)
    )

    assert "User is a developer" in result.user_facts


@pytest.mark.asyncio
async def test_extract_falls_back_on_bad_json() -> None:
    engine = FakeEngine(response="not json at all")
    extractor = LlmDspExtractor(resolve_engine=lambda: _resolve_to(engine))
    transcript = '[05/05/26:1200] Alice: "I am a developer"'

    result = await extractor.extract_dsp(
        transcript=transcript, current_date=date(2026, 5, 5)
    )

    assert "User is a developer" in result.user_facts


@pytest.mark.asyncio
async def test_extract_empty_transcript_skips_engine() -> None:
    engine = FakeEngine(response='{"user_facts": ["User should never be used"]}')
    extractor = LlmDspExtractor(resolve_engine=lambda: _resolve_to(engine))

    result = await extractor.extract_dsp(
        transcript="   ", current_date=date(2026, 5, 5)
    )

    assert result.user_facts == []
    assert result.user_preferences == []
    assert not engine.prompts
