"""Per-route instruction overlays.

An overlay is rule text that only makes sense on one route. It renders *after*
that route's rules, still inside the one minified instruction line.

Why a rule lives here rather than in ``rules.py``: a rule whose own condition can
never hold on a route, or which names an action that route's action catalog does
not contain, is inert text there. Moving it into the route's overlay keeps the
route that CAN act on it byte-for-byte unchanged while the routes that cannot
stop paying for it.

Both overlays below are the moved clauses *verbatim* — the routes that render
them see exactly the wording they saw before the split.
"""

from __future__ import annotations

from core.prompt_instructions.routes import (
    ROUTE_CHAT_VOICE,
    ROUTE_LIVE,
    ROUTE_VESSEL,
)

#: How to reply when embodied in a Rift Vessel world. Inert on every other
#: route: a non-vessel turn's action catalog contains no ``vessel_*_say`` at all
#: (a disconnected turn exposes only ``vessel_connect``), so the clause names an
#: action the model cannot emit there.
VESSEL_SPEAK_OVERLAY = (
    "When you are embodied in a world (the incoming message and current_chat come "
    "through a vessel interface), the way to reply in that world is the embodiment "
    "speak action (a vessel_* say/emote action), NOT a message_* action — reply "
    "there in-world. When a player in the world speaks to you, you MUST answer them "
    "with a vessel_* say action addressed to that same player in this turn (you may "
    "also move toward or follow them); staying silent or replying only with "
    "internal/observe actions is a hard failure."
)

#: Spoken-register style, for turns whose input was speech. Rendered by the
#: voice-input chat route and by live voice sessions. Inert on a text turn: the
#: condition it names (``input.payload.input_source == "voice"``) cannot hold
#: there, so the rule switched itself off after costing the whole clause.
VOICE_INPUT_OVERLAY = (
    'VOICE INPUT STYLE: When input.payload.input_source is "voice", the user spoke '
    "their message aloud. Respond in a natural, conversational spoken style: avoid "
    "markdown, bullet points, headers, and code blocks. Keep the reply concise and "
    "suitable for text-to-speech synthesis. This rule applies ONLY to the current "
    "message — do NOT assume past messages in chat_history were also voice."
)

#: Route -> overlay text. Absent means the route adds nothing.
OVERLAYS: dict[str, str] = {
    ROUTE_VESSEL: VESSEL_SPEAK_OVERLAY,
    ROUTE_CHAT_VOICE: VOICE_INPUT_OVERLAY,
    ROUTE_LIVE: VOICE_INPUT_OVERLAY,
}

#: Routes that have an overlay slot (documentation aid: makes the intended set
#: explicit even if a slot is temporarily empty).
OVERLAY_ROUTES: tuple[str, ...] = (ROUTE_VESSEL, ROUTE_CHAT_VOICE, ROUTE_LIVE)


def overlay_for_route(route: str | None) -> str:
    """Overlay text for ``route`` (empty string when there is none)."""
    if not route:
        return ""
    return OVERLAYS.get(str(route), "")
