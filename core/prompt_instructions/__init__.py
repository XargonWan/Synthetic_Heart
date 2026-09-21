"""Route-aware assembly of SyntH's shared JSON instruction block.

``build_instructions()`` renders the rule set for one route as the single
minified line every engine receives. It replaces the previously inline literal in
``core.prompt_engine.load_json_instructions``, which is kept as a thin facade so
no caller had to change.

Design notes
------------
* **Route scoping is structural.** A route comes from the builder that is
  assembling the turn, never from message content (see ``routes``).
* **Fail-open.** An unknown route renders the full rule set - the superset - so
  a misclassified turn costs characters, never a missing rule. Any internal
  error degrades to the same superset rather than raising inside prompt assembly.
* **The output stays one minified line** (no newlines, no double spaces), which
  several callers and tests depend on.

Budgets
-------
``INSTRUCTION_BUDGETS`` records the character ceiling per route. They are
asserted by ``tests/test_prompt_instruction_budget.py`` so that drift is a test
failure rather than a silent regression: the shared block was previously
unbounded (it had grown to 8 377 chars with a single stale guard asserting
``< 7500``, which was itself failing).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.prompt_instructions.overlays import overlay_for_route
from core.prompt_instructions.routes import (
    ROUTE_AGENT,
    ROUTE_CHAT,
    ROUTE_CHAT_VOICE,
    ROUTE_DELIVERY,
    ROUTE_GRILLO_INTERNAL,
    ROUTE_LIVE,
    ROUTE_OBSERVER,
    ROUTE_VESSEL,
    rules_for_route,
)
from core.prompt_instructions.rules import NAMING_HINT_TOKEN, RULES

#: Character ceilings for the rendered instruction string, per route. Set from
#: the measured size of each route plus ~400 characters of headroom, so ordinary
#: wording tweaks pass and real growth fails.
INSTRUCTION_BUDGETS: dict[str, int] = {
    ROUTE_CHAT: 5100,
    ROUTE_CHAT_VOICE: 5500,
    ROUTE_VESSEL: 5600,
    ROUTE_GRILLO_INTERNAL: 4350,
    ROUTE_OBSERVER: 4600,
    ROUTE_DELIVERY: 4200,
    ROUTE_LIVE: 5500,
    ROUTE_AGENT: 5100,
}

#: Trainer-reference hint, in both supported shapes. Kept here (not in the rule
#: text) so no trainer name is ever hardcoded into a rule constant.
_NAMING_HINT_PLAIN = " Name people, not 'the user'."
_NAMING_HINT_WITH_TRAINER = " Name people, not 'the user' (your trainer: %s)."


def resolve_naming_hint() -> str:
    """Return the config-driven trainer reference for the autonomy rule.

    Read live from the config registry via ``core.config.get_trainer_display_name``
    (imported lazily to keep this module free of a load-time dependency on the
    config layer). Returns the name-free variant when no real trainer name is
    configured, so the placeholder ``"Trainer"`` never reaches a prompt.
    """
    try:
        from core.config import get_trainer_display_name

        trainer_name = str(get_trainer_display_name() or "").strip()
    except Exception:
        trainer_name = ""
    if trainer_name:
        return _NAMING_HINT_WITH_TRAINER % trainer_name
    return _NAMING_HINT_PLAIN


def _minify(parts: list[str]) -> str:
    """Join rule text into the single-line form every caller expects."""
    return " ".join(part.strip() for part in parts if part and part.strip())


def build_instructions(
    route: str = ROUTE_CHAT,
    *,
    naming_hint: str | None = None,
    extra_parts: Mapping[str, Any] | None = None,
) -> str:
    """Render the shared instruction block for ``route``.

    Args:
        route: Structural route id (see ``core.prompt_instructions.routes``).
            Unknown values render the full rule set.
        naming_hint: Override for the trainer reference. Defaults to
            :func:`resolve_naming_hint`. Only used by tests and by callers that
            have already resolved the name.
        extra_parts: Reserved for route-specific prefixes that must render
            before the rule set (e.g. a route banner). Values are stringified
            and appended in insertion order.

    Returns:
        The minified instruction string: one line, no double spaces, no
        trailing whitespace. Never raises.
    """
    try:
        hint = resolve_naming_hint() if naming_hint is None else naming_hint
        parts: list[str] = []
        if extra_parts:
            for value in extra_parts.values():
                text = "" if value is None else str(value)
                if text.strip():
                    parts.append(text)
        for rule_id in rules_for_route(route):
            text = RULES.get(rule_id, "")
            if text:
                parts.append(text.replace(NAMING_HINT_TOKEN, hint))
        overlay = overlay_for_route(route)
        if overlay.strip():
            parts.append(overlay)
        rendered = _minify(parts)
        if rendered:
            return rendered
    except Exception:
        pass
    # Fail-open: fall back to the full shared set rather than an empty
    # instruction string, which would leave a turn with no output contract.
    try:
        hint = resolve_naming_hint() if naming_hint is None else naming_hint
        return _minify([RULES[r].replace(NAMING_HINT_TOKEN, hint) for r in RULES])
    except Exception:
        return ""


def instruction_budget(route: str | None) -> int:
    """Character ceiling for ``route`` (the chat ceiling when unknown)."""
    return INSTRUCTION_BUDGETS.get(str(route or ""), INSTRUCTION_BUDGETS[ROUTE_CHAT])


__all__ = [
    "INSTRUCTION_BUDGETS",
    "build_instructions",
    "instruction_budget",
    "resolve_naming_hint",
    "ROUTE_AGENT",
    "ROUTE_CHAT",
    "ROUTE_CHAT_VOICE",
    "ROUTE_DELIVERY",
    "ROUTE_GRILLO_INTERNAL",
    "ROUTE_LIVE",
    "ROUTE_OBSERVER",
    "ROUTE_VESSEL",
]
