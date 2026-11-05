# tests/test_prompt_instruction_budget.py
"""Guards for the shared JSON instruction block: exact text and size budgets.

Two jobs.

1. **Exact text.** ``tests/fixtures/prompt_instructions_chat_*_current.txt`` are
   the verbatim output of ``load_json_instructions()`` for the two trainer-name
   shapes. Any wording change must regenerate them in the same commit, so an
   accidental edit to a rule cannot slip through, and a deliberate one is a
   reviewable diff. The ``_precompression`` fixtures are kept beside them as the
   record of what the block looked like before the instruction-budget work
   (8 377 -> 4 691 chars) — they are provenance, not assertions. The
   ``registry_default`` fixtures are not synthetic: the pre-compression one was
   verified byte-identical (same sha256) to the instruction body of live Langfuse
   trace ``7058fddb-fa36-4eca-b7dc-125a3ffbfb24``.

2. **Size budgets.** The shared block renders on every route and was previously
   unbounded — it had grown to 8 377 characters behind a single stale guard
   asserting ``< 7500``, which was itself failing. The per-route budgets make
   growth a test failure instead of a silent regression, and the route tests
   below assert the route split in BOTH directions (a rule that should be routed
   away must be absent, and a rule that still applies must be present), so an
   over-broad trim fails just as loudly as an under-trim.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.config as config_module  # noqa: E402
from core.prompt_instructions import (  # noqa: E402
    INSTRUCTION_BUDGETS,
    build_instructions,
    instruction_budget,
)
from core.prompt_instructions.routes import (  # noqa: E402
    ROUTE_CHAT,
    ROUTE_CHAT_VOICE,
    ROUTE_DELIVERY,
    ROUTE_GRILLO_INTERNAL,
    ROUTE_LIVE,
    ROUTE_OBSERVER,
    ROUTE_VESSEL,
)
from core.prompt_engine import load_json_instructions  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixtures"

# trainer_name (what ``get_trainer_display_name`` returns, substituted into the
# autonomy rule) -> recorded rendering. "Scarlet, Zahej" is the live deployment's
# own TRAINER_NAME value, so that fixture is production text.
FIXTURES = {
    "Scarlet, Zahej": "prompt_instructions_chat_registry_default_current.txt",
    "": "prompt_instructions_chat_no_trainer_current.txt",
}

ALL_ROUTES = (
    ROUTE_CHAT,
    ROUTE_CHAT_VOICE,
    ROUTE_VESSEL,
    ROUTE_GRILLO_INTERNAL,
    ROUTE_OBSERVER,
    ROUTE_DELIVERY,
    ROUTE_LIVE,
)


def _read_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("trainer_name,fixture_name", sorted(FIXTURES.items()))
def test_chat_route_renders_the_recorded_text(
    monkeypatch: pytest.MonkeyPatch, trainer_name: str, fixture_name: str
) -> None:
    """The chat route must reproduce the pre-extraction text byte for byte."""
    monkeypatch.setattr(config_module, "get_trainer_display_name", lambda: trainer_name)

    rendered = load_json_instructions()

    expected = _read_fixture(fixture_name)
    assert rendered == expected, (
        "The shared instruction text changed. If that was deliberate, the "
        f"fixture {fixture_name} must be regenerated in the same commit; "
        "otherwise a rule was altered by accident."
    )


def test_naming_hint_is_not_hardcoded_in_the_rule_text() -> None:
    """No trainer name may live in the rule constants — it is substituted only."""
    from core.prompt_instructions.rules import RULES

    for rule_id, text in RULES.items():
        assert "Scarlet" not in text, f"trainer name hardcoded in {rule_id}"
        assert "Zahej" not in text, f"trainer name hardcoded in {rule_id}"


def test_output_stays_single_line_and_minified() -> None:
    """Every caller depends on a minified single-line instruction string."""
    for route in ALL_ROUTES:
        rendered = build_instructions(
            route, naming_hint=" Name people, not 'the user'."
        )
        assert "\n" not in rendered, f"{route} rendered a newline"
        assert "  " not in rendered, f"{route} rendered a double space"
        assert rendered == rendered.strip(), f"{route} rendered leading/trailing space"
        assert rendered, f"{route} rendered nothing"


def test_unknown_route_renders_the_superset() -> None:
    """Fail-open: an undetermined route must never lose a rule."""
    unknown = build_instructions(
        "not-a-real-route", naming_hint=" Name people, not 'the user'."
    )
    chat = build_instructions(ROUTE_CHAT, naming_hint=" Name people, not 'the user'.")
    assert unknown == chat


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_route_stays_within_its_instruction_budget(route: str) -> None:
    """The instruction block for every route must fit its declared budget."""
    rendered = build_instructions(route)
    budget = instruction_budget(route)
    assert len(rendered) <= budget, (
        f"instruction block for route {route!r} is {len(rendered)} chars, "
        f"over its {budget}-char budget. Either compress it or raise the budget "
        "deliberately (INSTRUCTION_BUDGETS) — do not let it grow silently."
    )


def test_every_budgeted_route_has_a_budget() -> None:
    """A route without a budget would be silently unbounded."""
    for route in ALL_ROUTES:
        assert route in INSTRUCTION_BUDGETS, f"no budget declared for {route!r}"


# ---------------------------------------------------------------------------
# Route scoping, asserted in both directions
#
# Each case names a rule that must be ABSENT on a route (because the route can
# never act on it) and a rule that must be PRESENT (because it still applies).
# Asserting only the absence would pass a trim that went too far; asserting only
# the presence would pass a route that never got scoped at all.
# ---------------------------------------------------------------------------

GRILLO_INTERNAL_MUST_NOT = (
    "CHAT REPLY REQUIRED",
    "Example of a complete human-chat response",
)
GRILLO_INTERNAL_MUST_KEEP = (
    "MASTER INSTRUCTION",
    "INPUT METADATA",
    "RESPONSE FORMAT",
)


def test_grillo_internal_beat_drops_the_human_chat_rules_but_keeps_its_own() -> None:
    """An internal beat is not a user chat.

    The reply obligation and the worked example contradict its own guard, which
    forbids any ``message_*`` / ``send_message`` action. ``INPUT METADATA`` must
    stay, because an internal beat's user body really does carry the routing
    bracket.
    """
    rendered = build_instructions(ROUTE_GRILLO_INTERNAL)
    for absent in GRILLO_INTERNAL_MUST_NOT:
        assert absent not in rendered, (
            f"{absent!r} still renders on a Grillo internal beat, where the guard "
            "forbids the very action it asks for"
        )
    for present in GRILLO_INTERNAL_MUST_KEEP:
        assert present in rendered, f"{present!r} is missing from the internal route"


def test_delivery_route_drops_emotion_and_diary_but_keeps_the_reply_rule() -> None:
    """A delivery turn sends a message but stirs no emotion and writes no diary."""
    rendered = build_instructions(ROUTE_DELIVERY)
    assert "EMOTION UPDATES" not in rendered
    assert "Example of a complete human-chat response" not in rendered
    assert "CHAT REPLY REQUIRED" in rendered
    assert "RESPONSE FORMAT" in rendered


def test_observer_route_keeps_the_reply_rule_but_drops_the_example() -> None:
    """Outreach must send a message, so the reply obligation applies to it."""
    rendered = build_instructions(ROUTE_OBSERVER)
    assert "CHAT REPLY REQUIRED" in rendered
    assert "Example of a complete human-chat response" not in rendered


def test_voice_style_only_renders_on_a_voice_turn() -> None:
    """The spoken-register rule is gated on input_source.

    A text turn can never satisfy that condition, so it must not pay for the
    clause; a voice turn must still receive it verbatim.
    """
    assert "VOICE INPUT STYLE" not in build_instructions(ROUTE_CHAT)
    assert "VOICE INPUT STYLE" in build_instructions(ROUTE_CHAT_VOICE)


def test_vessel_speak_clause_only_renders_on_an_embodiment_turn() -> None:
    """A disconnected turn's catalog has no ``vessel_*_say`` to call.

    So the in-world speak clause is inert there, while an embodiment turn must
    still get it.
    """
    assert "embodiment speak action" not in build_instructions(ROUTE_CHAT)
    assert "embodiment speak action" in build_instructions(ROUTE_VESSEL)


def test_moved_clauses_are_verbatim() -> None:
    """Clauses moved into an overlay must not be reworded in transit."""
    from core.prompt_instructions.overlays import (
        VESSEL_SPEAK_OVERLAY,
        VOICE_INPUT_OVERLAY,
    )

    for fragment in (
        "the way to reply in that world is the embodiment speak action "
        "(a vessel_* say/emote action), NOT a message_* action — reply there in-world",
        "When a player in the world speaks to you, you MUST answer them with a "
        "vessel_* say action addressed to that same player in this turn",
        "staying silent or replying only with internal/observe actions is a hard failure",
    ):
        assert fragment in VESSEL_SPEAK_OVERLAY, f"vessel overlay lost: {fragment!r}"

    for fragment in (
        'VOICE INPUT STYLE: When input.payload.input_source is "voice", the user '
        "spoke their message aloud",
        "avoid markdown, bullet points, headers, and code blocks",
        "This rule applies ONLY to the current message",
    ):
        assert fragment in VOICE_INPUT_OVERLAY, f"voice overlay lost: {fragment!r}"


# ---------------------------------------------------------------------------
# The persona block must survive prompt assembly untouched
#
# The instruction-budget work rearranged everything AROUND the persona, so this
# pins that the persona itself is still emitted verbatim, immediately ahead of
# the shared instruction block. It uses a synthetic persona rather than the
# deployment's own text: the real persona legitimately changes (an operator edits
# it), and a test that pinned it would fail for that reason instead of this one.
# ---------------------------------------------------------------------------


async def test_persona_is_emitted_verbatim_ahead_of_the_instruction_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The persona is passed through byte-for-byte, then the shared rules."""
    from core.prompt_engine import build_prompt_request, load_json_instructions

    persona = 'PERSONA: 2B — she\'s "mine", the mother of 2D.'

    async def mock_gather_static_injections(message, context_memory):
        return {"persona": persona}

    monkeypatch.setattr(
        "core.action_parser.gather_static_injections", mock_gather_static_injections
    )

    msg = SimpleNamespace(
        interface_path="telegram_bot/123",
        text="hello",
        message_id=42,
        date=SimpleNamespace(isoformat=lambda: "2026-05-27T00:00:00Z"),
    )
    res = await build_prompt_request(
        message=msg, context_memory={}, interface_name="telegram_bot"
    )
    instructions = res["instructions"]

    # Verbatim persona, and the instruction block straight after it. Whitespace
    # runs collapse to a single space in the final minify, so the separator is
    # one space, not the two newlines of the f-string that built it.
    assert (
        f"=== CRITICAL SYSTEM IDENTITY === {persona} === JSON RESPONSE INSTRUCTIONS ==="
        in (instructions)
    ), "the persona block no longer precedes the instruction block verbatim"

    # And the shared rules really are the ones that landed after it — rendered
    # for this turn's own interface path, which the routing rule and the worked
    # example now carry instead of a template token.
    shared = load_json_instructions(reply_path="telegram_bot/123")
    assert shared in instructions, (
        "the assembled prompt does not carry the shared block"
    )


async def test_temporal_facts_reach_the_current_turn_as_an_anchor_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: injected temporal fields become an anchor at the turn.

    The authoritative temporal facts are supplied as static injections (the time
    plugin's shape). They must reach the model twice: as the full block in the
    system message, and as the compact per-turn line above the current message —
    because on a long conversation the system block sits thousands of characters
    away from the text being generated, which is what the duplication is for.
    """
    from core.prompt_engine import build_prompt_request
    from core.prompt_renderers import OpenAIRenderer

    async def mock_gather_static_injections(message=None, context_memory=None):
        return {
            "persona": "PERSONA: 2B.",
            "location": "Ljubljana",
            "date": "2026-09-21",
            "time": "11:42",
            "time_of_day": "morning",
            "season": "Early Autumn",
            "day_of_week": "Monday",
        }

    monkeypatch.setattr(
        "core.action_parser.gather_static_injections", mock_gather_static_injections
    )

    msg = SimpleNamespace(
        interface_path="telegram_bot/123",
        text="hi",
        message_id=1,
        date=SimpleNamespace(isoformat=lambda: "2026-09-21T09:42:31Z"),
    )
    res = await build_prompt_request(
        message=msg, context_memory={}, interface_name="telegram_bot"
    )

    expected = (
        "[SYSTEM: REALITY ANCHOR] Monday, September 21, 2026 · 11:42 AM (morning) "
        "· Early Autumn · Ljubljana"
    )
    prompt_request = res["__prompt_request"]
    assert prompt_request.runtime_ctx.reality_anchor == expected

    messages = OpenAIRenderer(prompt_request).render()
    current_turn = messages[-1]["content"]
    assert current_turn.startswith(expected + "\n"), (
        "the anchor line must open the current turn, on its own line"
    )

    # ...and the full block is still in the system message, from the same facts.
    system_message = messages[0]["content"]
    assert "[SYSTEM: REALITY ANCHOR]" in system_message
    assert "- Current Date: Monday, September 21, 2026" in system_message


def test_vessel_turn_is_detected_structurally_on_every_signal() -> None:
    """The embodiment route must be selected, not silently skipped.

    Regression guard for a real defect found while wiring this: the third
    parameter of :func:`core.vessel_focus.is_vessel_turn` is the routing
    ``interface_path``, and the helper only falls back to
    ``message.interface_path`` when that argument is ``None``. Passing the
    interface *name* there (or anything else non-None) hides a vessel turn's own
    path, fails open to the chat route, and drops the in-world speak overlay
    without any error — the exact silent regression the fail-open design would
    otherwise hide.
    """
    from core.prompt_engine import _derive_instruction_route

    # (a) the routing path itself says vessel
    assert (
        _derive_instruction_route(None, {}, "vessel/minecraft", "", False)
        == ROUTE_VESSEL
    )
    # (b) only the message's own path says vessel — the None-fallback branch
    on_message = SimpleNamespace(
        interface_path="vessel/minecraft", chat=SimpleNamespace(type="private")
    )
    assert _derive_instruction_route(on_message, {}, None, "", False) == ROUTE_VESSEL
    # (c) chat.type alone says vessel
    by_chat_type = SimpleNamespace(
        interface_path=None, chat=SimpleNamespace(type="vessel")
    )
    assert _derive_instruction_route(by_chat_type, {}, None, "", False) == ROUTE_VESSEL
    # (d) an explicit vessel_focus flag on the context
    assert _derive_instruction_route(None, {"vessel_focus": True}, None, "", False) == (
        ROUTE_VESSEL
    )
    # (e) and a plain chat turn must NOT be routed to the embodiment set, even
    # though it has a real interface_path and a real interface name.
    plain = SimpleNamespace(
        interface_path="telegram_bot/123", chat=SimpleNamespace(type="private")
    )
    assert (
        _derive_instruction_route(plain, {}, "telegram_bot/123", "", False)
        == ROUTE_CHAT
    )


async def test_an_embodiment_turn_receives_the_in_world_speak_clause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: a vessel turn's instructions carry the speak overlay."""
    from core.prompt_engine import build_prompt_request

    async def mock_gather_static_injections(message=None, context_memory=None):
        return {"persona": "PERSONA: 2B."}

    monkeypatch.setattr(
        "core.action_parser.gather_static_injections", mock_gather_static_injections
    )

    msg = SimpleNamespace(
        interface_path="vessel/minecraft",
        text="hello from the world",
        message_id=7,
        date=SimpleNamespace(isoformat=lambda: "2026-09-21T09:42:31Z"),
    )
    res = await build_prompt_request(
        message=msg, context_memory={}, interface_name="minecraft_vessel"
    )
    instructions = res["instructions"]

    assert (
        "the way to reply in that world is the embodiment speak action" in instructions
    )
    assert "CHAT REPLY REQUIRED" in instructions  # the reply obligation still applies

    # And the same turn must NOT be handed the spoken-register rule meant for
    # voice input, which it cannot satisfy.
    assert "VOICE INPUT STYLE" not in instructions
