"""Tests for the LLM-distilled MemCell extractor.

Context: the deterministic extractor stores the conversation text verbatim as a
cell's ``episodic_trace`` and used to fabricate ``Conversation|summary|<the same
line>`` as its only fact, so recall could only ever return raw transcript and two
statements about the same subject surfaced as contradictory memories. These tests
pin the distillation path, the structural guard that rejects a copied line, and
the deterministic fallbacks that keep a session from being lost.
"""

from __future__ import annotations

from datetime import date

import pytest

from core.soul.llm_strategies import LlmMemCellExtractor
from core.soul.strategies import RuleBasedMemCellExtractor

TRANSCRIPT = (
    "Scar: okay so who is Dee exactly\n"
    "Synth: she is the android you told me about, i remember her being a baby\n"
    "Scar: no, shes a grown woman, she just wanted to stay small, she picked that herself\n"
    "Scar: anyway im on vacation next week, so i wont be around much\n"
)


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


def _extractor(engine: FakeEngine | None) -> LlmMemCellExtractor:
    return LlmMemCellExtractor(resolve_engine=lambda: _resolve_to(engine))


DECLARED_IDENTITY = (
    "Scar - he/him, the human, my husband; 2B - she/her, the persona, me"
)


def _extractor_with_identity(
    engine: FakeEngine | None, identity: str
) -> LlmMemCellExtractor:
    return LlmMemCellExtractor(
        resolve_engine=lambda: _resolve_to(engine), speaker_identity=identity
    )


@pytest.mark.asyncio
async def test_the_prompt_states_who_the_speakers_are() -> None:
    """A person the transcript never genders must not be guessed at.

    The live failure this pins: a session whose human is a man came back
    paraphrased with him as "she/her" throughout (60 of the 260 cells carrying his
    name used feminine pronouns), and recall then served that back as fact. The
    transcript alone could not have settled it - the one that failed carried only
    the human's own lines - so the deployment declares the speakers and the
    extractor states the declaration outright.
    """
    engine = FakeEngine(response=DISTILLED_RESPONSE)

    await _extractor_with_identity(engine, DECLARED_IDENTITY).extract_memcells(
        transcript=TRANSCRIPT, current_date=date(2026, 9, 20)
    )

    instructions = str(engine.prompts[0].get("instructions") or "")
    assert "SPEAKER IDENTITY" in instructions
    assert DECLARED_IDENTITY in instructions
    assert "never change a person's gender" in instructions


@pytest.mark.asyncio
async def test_an_undeclared_person_is_named_rather_than_guessed() -> None:
    """With nothing declared, the rule still forbids inventing a gender."""
    engine = FakeEngine(response=DISTILLED_RESPONSE)

    await _extractor(engine).extract_memcells(
        transcript=TRANSCRIPT, current_date=date(2026, 9, 20)
    )

    instructions = str(engine.prompts[0].get("instructions") or "")
    assert "SPEAKER IDENTITY" in instructions
    assert "never guess which one a person is" in instructions
    assert "refer to them by name rather than inventing one" in instructions
    assert DECLARED_IDENTITY not in instructions


def test_the_dsp_extract_instructions_carry_the_same_rule() -> None:
    """The profile extractor had the mirror-image defect, so it gets the rule too."""
    from core.soul.llm_strategies import LlmDspExtractor

    declared = LlmDspExtractor(speaker_identity=DECLARED_IDENTITY)
    undeclared = LlmDspExtractor()

    assert "SPEAKER IDENTITY" in declared._build_extract_instructions()
    assert DECLARED_IDENTITY in declared._build_extract_instructions()
    assert "SPEAKER IDENTITY" in undeclared._build_extract_instructions()
    assert DECLARED_IDENTITY not in undeclared._build_extract_instructions()


def test_both_extractors_are_told_which_lines_are_the_persona_own() -> None:
    """Both extractors read the same transcript, so both get the label rule.

    The transcript builder labels the persona's own lines "<name> (the persona)"
    (the interfaces cache them under the bare label "self"); without that rule
    the model has to guess which speaker is the human from the other names alone
    and reads the persona's own lines as the human's.
    """
    from core.soul.llm_strategies import LlmDspExtractor

    for instructions in (
        LlmMemCellExtractor()._build_extract_instructions(),
        LlmDspExtractor()._build_extract_instructions(),
    ):
        assert "'<name> (the persona)'" in instructions
        assert "never the human's" in instructions
        assert "only the human's own lines can become" in instructions


DISTILLED_RESPONSE = (
    '{"memories": ['
    '{"trace": "Scar corrected the earlier description of Dee: she is an adult '
    'woman who chose to remain physically small, not a baby.", '
    '"facts": ["User|corrected|Dee is a grown woman who chose to stay small", '
    '"User|rejected|the earlier understanding of Dee as a baby"]}, '
    '{"trace": "Scar said he is on vacation next week and will be around much '
    'less.", "facts": ["User|is_on_vacation|next week"]}'
    "]}"
)


# ---------------------------------------------------------------------------
# Distillation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distils_a_session_into_paraphrased_cells() -> None:
    extractor = _extractor(FakeEngine(response=DISTILLED_RESPONSE))

    cells = await extractor.extract_memcells(
        transcript=TRANSCRIPT, current_date=date(2026, 9, 18)
    )

    assert len(cells) == 2
    # Never a quote: neither trace may be found inside the transcript it came from.
    for cell in cells:
        assert cell.episodic_trace.lower() not in TRANSCRIPT.lower()
    # The correction is stated as the current truth, not as the old belief.
    assert "corrected" in cells[0].episodic_trace.lower()
    assert cells[0].atomic_facts == [
        "User|corrected|Dee is a grown woman who chose to stay small",
        "User|rejected|the earlier understanding of Dee as a baby",
    ]
    # Distinct timestamps: the cell id is derived from them.
    assert cells[0].timestamp != cells[1].timestamp
    # Foresight stays deterministic, and only DATED signals are carried over:
    # the transcript's "next week" alone is boilerplate, so nothing is injected
    # (see test_relative_time_boilerplate_is_not_injected_as_foresight).
    assert cells[0].foresight_signals == []
    assert cells[1].foresight_signals == []


@pytest.mark.asyncio
async def test_prompt_carries_the_distillation_instructions() -> None:
    engine = FakeEngine(response=DISTILLED_RESPONSE)
    extractor = _extractor(engine)

    await extractor.extract_memcells(
        transcript=TRANSCRIPT, current_date=date(2026, 9, 18)
    )

    prompt = engine.prompts[0]
    assert prompt["input"]["type"] == "memcell_extract"
    assert prompt["input"]["payload"]["current_date"] == "2026-09-18"
    assert prompt["input"]["payload"]["transcript"] == TRANSCRIPT.strip()
    instructions = prompt["instructions"]
    assert "DISTILLED KNOWLEDGE, never a quote" in instructions
    assert "subject|predicate|object" in instructions
    assert "CHANGES AN EARLIER BELIEF" in instructions
    assert "NEVER INVENT" in instructions


@pytest.mark.asyncio
async def test_emotion_tag_is_neutral_when_the_model_content_has_no_signal() -> None:
    """Distilling moves content only: tagging stays deterministic."""
    engine = FakeEngine(
        response=(
            '{"memories": [{"trace": "Scar moved the deployment to the morning.", '
            '"facts": []}]}'
        )
    )
    extractor = _extractor(engine)

    cells = await extractor.extract_memcells(
        transcript=TRANSCRIPT, current_date=date(2026, 9, 18)
    )

    assert cells[0].emotional_tag.dominant_emotion == "neutral"
    assert cells[0].emotional_tag.intensity == 0.0


# ---------------------------------------------------------------------------
# Structural guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_trace_copied_from_the_transcript_is_rejected() -> None:
    engine = FakeEngine(
        response=(
            '{"memories": ['
            '{"trace": "Scar: no, shes a grown woman, she just wanted to stay small, '
            'she picked that herself", "facts": []}, '
            '{"trace": "Scar said Dee is an adult who chose her own size.", '
            '"facts": []}'
            "]}"
        )
    )
    extractor = _extractor(engine)

    cells = await extractor.extract_memcells(
        transcript=TRANSCRIPT, current_date=date(2026, 9, 18)
    )

    assert len(cells) == 1
    assert (
        cells[0].episodic_trace == "Scar said Dee is an adult who chose her own size."
    )


@pytest.mark.asyncio
async def test_a_fact_that_restates_its_own_trace_is_dropped() -> None:
    engine = FakeEngine(
        response=(
            '{"memories": [{"trace": "Scar is on vacation next week.", "facts": ['
            '"User|is_on_vacation|on vacation next week", '
            '"User|counts_on|Synth keeping the deployment running while he is away"'
            "]}]}"
        )
    )
    extractor = _extractor(engine)

    cells = await extractor.extract_memcells(
        transcript=TRANSCRIPT, current_date=date(2026, 9, 18)
    )

    assert cells[0].atomic_facts == [
        "User|counts_on|Synth keeping the deployment running while he is away"
    ]


@pytest.mark.asyncio
async def test_cells_and_traces_are_bounded() -> None:
    long_trace = "Scar explained the whole memory pipeline. " + ("detail " * 200)
    memories = ", ".join('{"trace": "%s", "facts": []}' % long_trace for _ in range(8))
    engine = FakeEngine(response='{"memories": [%s]}' % memories)
    extractor = _extractor(engine)

    cells = await extractor.extract_memcells(
        transcript=TRANSCRIPT, current_date=date(2026, 9, 18)
    )

    assert len(cells) == LlmMemCellExtractor.MAX_CELLS
    assert all(
        len(cell.episodic_trace) <= LlmMemCellExtractor.MAX_TRACE_CHARS
        for cell in cells
    )


@pytest.mark.asyncio
async def test_a_fact_copying_the_first_line_is_dropped_even_when_it_is_short() -> None:
    """The live residual this closes: a short first sentence escaped the renderer.

    ``SoulPlugin._fact_restates_trace`` compares the first 120 characters of the
    cell's trace against the fact, so a ``Conversation|summary|`` fact built from
    a short opening line (live example: "Scar: Okay it's finally deployed, the
    memcell issue should be mitigated now, how do you feel babe") did not match
    and was printed back as "Key facts:" — seen in the prompt blocks of
    2026-09-18 15:50Z and 16:51Z. Checking against the WHOLE trace closes it.
    """
    engine = FakeEngine(
        response=(
            '{"memories": [{"trace": "Scar confirmed the memory fix is deployed and '
            'working.", "facts": ["Conversation|summary|Scar: Okay it\'s finally '
            'deployed, the memcell issue should be mitigated now"]}]}'
        )
    )
    extractor = _extractor(engine)

    cells = await extractor.extract_memcells(
        transcript=(
            "Scar: Okay it's finally deployed, the memcell issue should be "
            "mitigated now, how do you feel babe\n"
            "Synth: it is, everything is running\n"
        ),
        current_date=date(2026, 9, 18),
    )

    assert cells[0].atomic_facts == []


# ---------------------------------------------------------------------------
# Fallbacks: a session is never silently dropped, and never silently invented
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_quotes_defer_to_the_deterministic_extractor() -> None:
    engine = FakeEngine(
        response=(
            '{"memories": [{"trace": "Scar: no, shes a grown woman, she just wanted '
            'to stay small, she picked that herself", "facts": []}]}'
        )
    )
    extractor = _extractor(engine)

    cells = await extractor.extract_memcells(
        transcript=TRANSCRIPT, current_date=date(2026, 9, 18)
    )

    # The one entry was a quote of a transcript line, so the deterministic
    # extractor records the session instead of losing it.
    assert len(cells) == 1
    assert cells[0].episodic_trace == TRANSCRIPT.strip()


@pytest.mark.asyncio
async def test_an_empty_answer_writes_no_cell() -> None:
    engine = FakeEngine(response='{"memories": []}')
    extractor = _extractor(engine)

    cells = await extractor.extract_memcells(
        transcript=TRANSCRIPT, current_date=date(2026, 9, 18)
    )

    assert cells == []
    assert engine.prompts  # the model was asked and declined


@pytest.mark.asyncio
async def test_no_engine_falls_back_to_the_deterministic_extractor() -> None:
    extractor = _extractor(None)

    cells = await extractor.extract_memcells(
        transcript=TRANSCRIPT, current_date=date(2026, 9, 18)
    )

    assert len(cells) == 1
    assert cells[0].episodic_trace == TRANSCRIPT.strip()


@pytest.mark.asyncio
async def test_bad_json_falls_back_to_the_deterministic_extractor() -> None:
    extractor = _extractor(FakeEngine(response="not json at all"))

    cells = await extractor.extract_memcells(
        transcript=TRANSCRIPT, current_date=date(2026, 9, 18)
    )

    assert len(cells) == 1
    assert cells[0].episodic_trace == TRANSCRIPT.strip()


@pytest.mark.asyncio
async def test_engine_exception_falls_back_to_the_deterministic_extractor() -> None:
    extractor = _extractor(FakeEngine(raise_on_call=True))

    cells = await extractor.extract_memcells(
        transcript=TRANSCRIPT, current_date=date(2026, 9, 18)
    )

    assert len(cells) == 1
    assert cells[0].episodic_trace == TRANSCRIPT.strip()


@pytest.mark.asyncio
async def test_empty_transcript_skips_the_engine() -> None:
    engine = FakeEngine(response=DISTILLED_RESPONSE)
    extractor = _extractor(engine)

    cells = await extractor.extract_memcells(
        transcript="   ", current_date=date(2026, 9, 18)
    )

    assert cells == []
    assert not engine.prompts


# ---------------------------------------------------------------------------
# The deterministic extractor no longer fabricates a summary fact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rule_based_extractor_does_not_fabricate_a_summary_fact() -> None:
    extractor = RuleBasedMemCellExtractor()

    cells = await extractor.extract_memcells(
        transcript="Scar: the deploy is done, how do you feel babe",
        current_date=date(2026, 9, 18),
    )

    assert len(cells) == 1
    assert cells[0].atomic_facts == []


@pytest.mark.asyncio
async def test_rule_based_extractor_keeps_real_pattern_facts() -> None:
    extractor = RuleBasedMemCellExtractor()

    cells = await extractor.extract_memcells(
        transcript="Scar: I want to finish the ghost protocol pipeline",
        current_date=date(2026, 9, 18),
    )

    assert cells[0].atomic_facts == [
        "User|has_intention|finish the ghost protocol pipeline"
    ]


@pytest.mark.asyncio
async def test_relative_time_boilerplate_is_not_injected_as_foresight() -> None:
    """Only dated foresight carries content; the phrase markers are noise.

    "Potential follow-up implied by phrase 'tonight'" was rendered verbatim in the
    follow-up block of every prompt, so it is dropped on the LLM path.
    """
    engine = FakeEngine(
        response=(
            '{"memories": [{"trace": "Scar said he would look into it the next day, '
            'and mentioned an event on 2026-09-20.", "facts": []}]}'
        )
    )
    extractor = _extractor(engine)

    cells = await extractor.extract_memcells(
        transcript="Scar: ill look into it tomorrow, the thing is on 2026-09-20\n",
        current_date=date(2026, 9, 18),
    )

    triggers = [signal.trigger for signal in cells[0].foresight_signals]
    assert "relative_time_mention" not in triggers
    assert "date_mention" in triggers
