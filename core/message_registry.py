"""Unified ``send_message`` action registry.

Every messaging interface exposes itself to the LLM through this single
action. The destination is resolved from ``interface_path`` (which encodes
interface + chat + thread + room, e.g. ``telegram_bot/-3654654848/6578/2``),
falling back to the origin conversation when the action replies to an incoming
message (``original_message`` present).

A message may be text-only, media-only, or both:

* ``text`` — message body; also the caption for media.
* ``media`` — attachment file path(s); single string is normalized to a list.
  Media kind is auto-detected via ``core.outbound_file_utils.classify_media``.
* ``send_as_voice`` — top-level boolean; turns audio media (or a TTS of
  ``text``) into a voice note where supported.
* ``reply_to`` — unified reply id mapped to the interface-native reply field.
  Thread targeting lives inside ``interface_path``, not in a separate field.

Interfaces declare their capabilities via ``get_capabilities()`` (or method
presence, see ``core.interface_capabilities``). Requested features an
interface does not support are dropped with a log warning and reported back to
the model as structured ``capability_drops`` — never a hard error; the rest of
the message still goes out.

This module owns the single canonical schema so every interface returns the
same contract from its ``get_supported_actions()``.
"""

from __future__ import annotations

from typing import Any, Dict, List

SEND_MESSAGE_ACTION = "send_message"

# OR validation: at least one of these fields must be present.
SEND_MESSAGE_ONE_OF: List[List[str]] = [["text", "media"]]

SEND_MESSAGE_REQUIRED_FIELDS: List[str] = []

SEND_MESSAGE_OPTIONAL_FIELDS: List[str] = [
    "interface_path",
    "send_as_voice",
    "reply_to",
]


def get_send_message_schema(
    destinations: List[str] | None = None,
) -> Dict[str, Any]:
    """Return the canonical ``send_message`` action schema.

    ``destinations`` optionally names the active interfaces that advertise the
    text/media capabilities; it is folded into the description so the LLM sees
    where the action can deliver.
    """
    description = (
        "Send a message through any connected chat interface. The destination "
        "is `interface_path` (e.g. 'telegram_bot/<chat_id>[/<thread_id>]', "
        "'discord_bot/<guild_id>/<channel_id>', 'matrix/<room_id>'); when "
        "replying to an incoming message you may omit it and the reply "
        "auto-routes to the origin conversation. Provide `text` and/or "
        "`media` (list of sandbox file paths; auto-detected image/video/"
        "audio/document). `text` doubles as the media caption."
    )
    if destinations:
        description += f" Active destinations: {', '.join(sorted(destinations))}."
    return {
        "description": description,
        "required_fields": list(SEND_MESSAGE_REQUIRED_FIELDS),
        "optional_fields": list(SEND_MESSAGE_OPTIONAL_FIELDS),
        "one_of_groups": [list(group) for group in SEND_MESSAGE_ONE_OF],
        # Deliberately NO external_effects: a chat reply is a pure message
        # action and must stay on the Fast Lane exactly like the retired
        # per-interface message_* actions. Declaring effects here would make
        # _is_tool_call classify every reply as agentic work and route whole
        # conversations through the Agent Lane (live incident 2026-08-26
        # 13:05: a plain reply batch hit the broken agent engine and the
        # user got no answer at all). Sandbox path safety for `media` is
        # enforced at dispatch by core.outbound_file_utils regardless.
    }


def is_message_action(action_type: Any) -> bool:
    """True when ``action_type`` is a user-facing message delivery action.

    Matches the unified ``send_message`` plus any legacy/plugin ``message_*``
    name (e.g. ``message_synth_webui``, ``message_integration``).
    """
    if not isinstance(action_type, str):
        return False
    return action_type == SEND_MESSAGE_ACTION or action_type.startswith("message_")


def get_active_destinations(interface_names: List[str]) -> List[str]:
    """Filter interface names down to plausible send_message destinations.

    Kept trivial on purpose: the capability gate at dispatch time is the real
    enforcement; this only shapes the prompt description.
    """
    return [name for name in interface_names or [] if name != "vessel"]


# Feature -> acceptable capability tokens (any-of). Checked against the
# interface capability set stored at registration time; falls back to direct
# method introspection so a registry miss never silently blocks delivery.
_FEATURE_CAPABILITY_TOKENS: Dict[str, tuple[str, ...]] = {
    "send_as_voice": ("voice_note", "send_voice", "audio"),
    "media": ("media", "send_file"),
    "reply_to": ("reply",),
}


def _interface_supports_feature(interface: Any, feature: str) -> bool:
    """Return True when ``interface`` advertises support for ``feature``.

    Fail-open: any introspection error resolves to True so a broken interface
    is never blocked from delivering the rest of the message.
    """
    try:
        from core.interface_capabilities import (
            interface_capabilities as _caps,
        )

        caps = _caps(interface)
        tokens = _FEATURE_CAPABILITY_TOKENS.get(feature, ())
        if tokens and caps & frozenset(tokens):
            return True
        # Method-presence fallback for registries without stored capabilities
        method_fallbacks = {
            "send_as_voice": ("send_voice", "send_tts_audio"),
            "media": ("send_file",),
            "reply_to": (),
        }
        for method in method_fallbacks.get(feature, ()):
            attr = getattr(interface, method, None)
            if callable(attr):
                return True
        # Unknown features are assumed supported (fail-open)
        return feature not in _FEATURE_CAPABILITY_TOKENS
    except Exception:
        return True


def resolve_capability_drops(
    interface: Any, payload: Dict[str, Any], interface_name: str = ""
) -> List[Dict[str, str]]:
    """Compute structured capability drops for a unified payload.

    Returns one record per requested-but-unsupported feature; empty when
    everything requested is deliverable.
    """
    from core.capability_drops import make_drop

    reasons = {
        "send_as_voice": "voice notes are not supported in this chat",
        "media": "file attachments are not supported in this chat",
        "reply_to": "reply quoting is not supported in this chat",
    }
    drops: List[Dict[str, str]] = []
    for feature in ("send_as_voice", "media"):
        if not payload.get(feature):
            continue
        if _interface_supports_feature(interface, feature):
            continue
        drops.append(make_drop(feature, reasons[feature], interface_name))
    return drops


__all__ = [
    "SEND_MESSAGE_ACTION",
    "SEND_MESSAGE_ONE_OF",
    "SEND_MESSAGE_OPTIONAL_FIELDS",
    "SEND_MESSAGE_REQUIRED_FIELDS",
    "get_active_destinations",
    "get_send_message_schema",
    "is_message_action",
    "resolve_capability_drops",
]
