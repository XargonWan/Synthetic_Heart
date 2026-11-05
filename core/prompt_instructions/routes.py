"""Structural route ids for the shared JSON instruction block.

A *route* names where a turn came from. It is always decided by WHICH builder is
assembling the prompt, plus structural flags that builder has already computed
(``is_grillo_internal``, the Vessel lite-mode probe, the voice-input flag, the
delivery payload shape) - never by looking at the text of a user message. That
keeps the instruction set safe in a multi-language deployment and keeps it
independent of what anyone happens to say.

Each route maps to a subset of ``rules.RULE_ORDER`` plus, optionally, overlay
text (``overlays.OVERLAYS``). See ``prompt_instructions.__init__.build_instructions``.
"""

from __future__ import annotations

from core.prompt_instructions.rules import RULE_ORDER

#: A normal human conversation turn on any chat interface. The superset: every
#: other route is derived from it, so an unrecognised route fails OPEN to this
#: set rather than losing a rule.
ROUTE_CHAT = "chat"

#: A chat turn whose input was spoken aloud (``input_source == "voice"``).
ROUTE_CHAT_VOICE = "chat_voice"

#: An embodiment turn in a Rift Vessel world.
ROUTE_VESSEL = "vessel"

#: An internal autonomous G.R.I.L.L.O. beat: not user-facing, forbidden to emit
#: any ``message_*`` / ``send_message`` action.
ROUTE_GRILLO_INTERNAL = "grillo_internal"

#: The proactive chat-observer outreach beat (carries its own instruction
#: constants in ``plugins/grillo/common_instructions.py`` alongside this set).
ROUTE_OBSERVER = "observer"

#: The delivery turn: summarise the results of a completed action for the user.
ROUTE_DELIVERY = "delivery"

#: A live voice session.
ROUTE_LIVE = "live"

#: The Agent Lane / Drone, which assembles its own tool-calling text.
ROUTE_AGENT = "agent"


def _without(*drop: str) -> tuple[str, ...]:
    """``RULE_ORDER`` minus ``drop``, order preserved.

    Used to express a route as "the shared set, except …" so a rule added to the
    shared set automatically reaches every route that does not explicitly opt out.
    """
    excluded = set(drop)
    return tuple(rule for rule in RULE_ORDER if rule not in excluded)


#: Route -> rule ids.
#:
#: Every route starts from the shared set and subtracts only what it provably
#: cannot act on, so adding a rule to ``RULE_ORDER`` reaches all of them.
#:
#: * ``grillo_internal`` — the Grillo guard already states that this is not a
#:   user chat and forbids ``message_*`` / ``send_message``; the reply obligation
#:   says the opposite ("internal-only actions are a hard failure"), and the
#:   worked example shows exactly the ``send_message`` the guard forbids. Both
#:   are removed. ``INPUT METADATA`` is KEPT: an internal beat's own user body
#:   does carry the ``[lang:… | grillo:true | beat:…]`` routing bracket.
#: * ``observer`` — the outreach beat must send a message, so the reply
#:   obligation is correct for it and stays. The worked example is dropped
#:   because its own instruction constants carry one.
#: * ``delivery`` — summarising an action's results: it sends a message but does
#:   not stir emotions or write a diary entry, so the emotion obligation and the
#:   human-chat example go (the delivery task block supplies its own example).
ROUTE_RULES: dict[str, tuple[str, ...]] = {
    ROUTE_CHAT: RULE_ORDER,
    ROUTE_CHAT_VOICE: RULE_ORDER,
    ROUTE_VESSEL: RULE_ORDER,
    ROUTE_GRILLO_INTERNAL: _without(
        "RULE_CHAT_REPLY_REQUIRED",
        "RULE_RESPONSE_EXAMPLE_LEAD",
        "RULE_RESPONSE_EXAMPLE",
    ),
    ROUTE_OBSERVER: _without(
        "RULE_RESPONSE_EXAMPLE_LEAD",
        "RULE_RESPONSE_EXAMPLE",
    ),
    ROUTE_DELIVERY: _without(
        "RULE_EMOTION_UPDATES",
        "RULE_RESPONSE_EXAMPLE_LEAD",
        "RULE_RESPONSE_EXAMPLE",
    ),
    ROUTE_LIVE: RULE_ORDER,
    ROUTE_AGENT: RULE_ORDER,
}


def rules_for_route(route: str | None) -> tuple[str, ...]:
    """Rule ids for ``route``.

    Fail-open by design: an unknown or missing route returns the full
    ``RULE_ORDER`` superset, so a route that cannot be determined costs a few
    hundred characters rather than a missing rule.
    """
    if not route:
        return RULE_ORDER
    return ROUTE_RULES.get(str(route), RULE_ORDER)
