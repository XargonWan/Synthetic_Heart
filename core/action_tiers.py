"""Two-tier action catalogue: full schemas for what a turn can use, compact for the rest.

A chat prompt used to carry the full JSON schema of every action the scope gate left
visible: on a live store 39 actions, about 37,000 characters, which was more than half
of a 65,000-character prompt (the recalled memories were about 5,000). Most turns use
two or three of them.

The catalogue is now sent in two forms:

* **full** (brief + schema): the actions every turn may need (each plugin registers
  its own), plus the ones the turn's Recon call chose (``recon_action_needs``) or
  the turn's own state calls for (also registered by the owning plugin);
* **compact** (brief + payload field names + required fields): everything else. The
  model still sees that the action exists, what it is for and which fields it takes,
  so it can use it; the full definition is what validates the reply afterwards, as
  it already does for Prompt Lite Mode.

Nothing here looks at message text: the choice of optional actions is the Recon
model's, made from the conversation, and the structural additions below depend on the
turn's state (a pending proposal), never on words.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from typing import Any

from core.config_manager import config_registry

# What is sent in full is not decided here by name. The core registers only what it
# owns itself (the unified ``send_message``, the per-interface ``message_*`` actions
# and the persona's ``use_animation``); every plugin registers the actions it owns
# that a turn always needs, and the state that calls for one of its other actions,
# from its own module:
#
#     from core.action_tiers import register_always_full_actions, register_state_actions
#     register_always_full_actions("update_emotion_state")
#     register_state_actions("self_growth_pending_proposal", "apply_growth_proposal")
_always_full: set[str] = {"send_message", "use_animation"}
_always_full_prefixes: tuple[str, ...] = ("message_",)
_state_actions: dict[str, set[str]] = {}


def register_always_full_actions(*names: str) -> None:
    """Declare actions that are sent with their full schema on every turn."""
    _always_full.update(n for n in names if n)


def register_state_actions(context_key: str, *names: str) -> None:
    """Declare actions a turn needs in full whenever ``context_key`` is in its context.

    Structural: the key is present because the turn carries that state (for example
    a pending proposal); nothing here reads message text.
    """
    if context_key and names:
        _state_actions.setdefault(context_key, set()).update(n for n in names if n)


_BRIEF_CHARS = 110


def tiering_enabled() -> bool:
    """Whether the catalogue is tiered (``PROMPT_ACTIONS_TIERED``, default on)."""
    try:
        return bool(
            config_registry.get_value("PROMPT_ACTIONS_TIERED", True, value_type=bool)
        )
    except Exception:
        return True


def is_core_action(name: str) -> bool:
    """Actions always sent in full: the registered ones and ``message_<interface>``."""
    return name in _always_full or name.startswith(_always_full_prefixes)


def short_brief(brief: object, limit: int = _BRIEF_CHARS) -> str:
    """The first sentence of ``brief``, cut to ``limit`` characters."""
    text = " ".join(str(brief or "").split())
    for stop in (". ", "! ", "? "):
        cut = text.find(stop)
        if 0 < cut < limit:
            return text[: cut + 1]
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def compact_action(normalized: Mapping[str, Any]) -> dict[str, Any]:
    """Brief, payload field names and required fields of a normalised action."""
    compact: dict[str, Any] = {"brief": normalized.get("brief", "")}
    schema = normalized.get("schema")
    if isinstance(schema, dict):
        properties = schema.get("properties") or {}
        if isinstance(properties, dict) and properties:
            compact["payload_keys"] = list(properties.keys())
        required = schema.get("required") or []
        if isinstance(required, list) and required:
            compact["required_payload_keys"] = list(required)
    return compact


def optional_action_briefs(
    available_actions: Mapping[str, Any], *, skip: Collection[str] = ()
) -> dict[str, str]:
    """``name -> one-line brief`` of the actions that are not always sent in full.

    This is what the Recon call is shown so it can say which ones the turn needs.
    ``skip`` names actions that must not be offered (system-only ones).
    """
    from core.action_schema_converter import normalize_action_schema

    briefs: dict[str, str] = {}
    for name, definition in available_actions.items():
        if is_core_action(name) or name in skip:
            continue
        try:
            brief = normalize_action_schema(name, definition).get("brief", "")
        except Exception:
            brief = ""
        briefs[name] = short_brief(brief)
    return briefs


def select_full_schema_actions(
    available_actions: Iterable[str],
    recon_needs: Collection[str],
    context_keys: Collection[str] = (),
) -> set[str]:
    """Names to send in full: core, the Recon-chosen ones, and the state-driven ones."""
    available = set(available_actions)
    chosen = {name for name in available if is_core_action(name)}
    chosen |= {name for name in recon_needs if name in available}
    for key, names in _state_actions.items():
        if key in context_keys:
            chosen |= {name for name in names if name in available}
    return chosen
