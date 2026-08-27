"""Capability-drop reporting for the unified ``send_message`` action.

When an interface receives a unified ``send_message`` payload requesting a
feature it does not support (e.g. ``send_as_voice`` on Matrix), the feature is
dropped with a log warning and the rest of the message is delivered anyway.
The drop is *reported back to the LLM*: the dispatcher attaches structured
``capability_drops`` to the action result, the message chain aggregates them
into the turn context, and the prompt engine renders them as a short
informational block so Synth can acknowledge the limitation in its own words.

The drop shape is structural: ``{"feature": str, "reason": str,
"interface": str}``. Never free-form user text.
"""

from __future__ import annotations

from typing import Any, Dict, List

from core.logging_utils import log_warning

# Maximum drops rendered into one prompt context to avoid bloat when a model
# repeatedly emits unsupported features.
MAX_DROPS_PER_TURN = 3

DROP_VOICE = "send_as_voice"
DROP_MEDIA = "media"
DROP_REPLY = "reply_to"


def make_drop(feature: str, reason: str, interface: str) -> Dict[str, str]:
    """Build one capability-drop record."""
    return {"feature": str(feature), "reason": str(reason), "interface": str(interface)}


def collect_capability_drops(action_results: List[Any]) -> List[Dict[str, str]]:
    """Collect and dedupe ``capability_drops`` from a list of action results.

    Each result may carry ``{"capability_drops": [...]}``; results that are
    ``None`` or lack the key are ignored. Deduplication is on the full
    ``(feature, interface, reason)`` triple.
    """
    drops: List[Dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for result in action_results or []:
        if not isinstance(result, dict):
            continue
        raw = result.get("capability_drops")
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            feature = str(item.get("feature") or "")
            reason = str(item.get("reason") or "")
            interface = str(item.get("interface") or "")
            if not feature:
                continue
            key = (feature, interface, reason)
            if key in seen:
                continue
            seen.add(key)
            drops.append(make_drop(feature, reason, interface))
    return drops[:MAX_DROPS_PER_TURN]


def render_capability_drops_block(drops: List[Dict[str, str]]) -> str:
    """Render the prompt-context block for a list of drops; empty when none."""
    usable = [d for d in (drops or []) if isinstance(d, dict) and d.get("feature")]
    if not usable:
        return ""
    lines = ["CAPABILITY DROPS (previous turn):"]
    for drop in usable:
        iface = drop.get("interface") or "unknown"
        reason = drop.get("reason") or "not supported"
        lines.append(f"- {drop['feature']}: {reason} (interface `{iface}`)")
    lines.append(
        "These requested delivery features were unavailable in that chat and "
        "were skipped; the rest of your message was delivered. You may "
        "naturally acknowledge this to the person if relevant — in your own "
        "words."
    )
    return "\n".join(lines)


def log_and_build_drop(feature: str, reason: str, interface: str) -> Dict[str, str]:
    """Log the warning and return the drop record (helper for interfaces)."""
    log_warning(
        f"[send_message] Dropped '{feature}' on interface '{interface}': {reason}"
    )
    return make_drop(feature, reason, interface)


# ---------------------------------------------------------------------------
# Recent-drops memory: capability drops happen *after* the LLM turn that
# emitted the action, so they must surface on the NEXT turn's prompt. A small
# per-interface_path in-memory cache bridges the turns.
# ---------------------------------------------------------------------------

_RECENT_DROPS_TTL_SEC = 3600.0
_recent_drops: dict[str, tuple[float, List[Dict[str, str]]]] = {}


def remember_drops(interface_path: str, drops: List[Dict[str, Any]]) -> None:
    """Store drops for ``interface_path`` so the next prompt can render them."""
    import time

    usable = [d for d in (drops or []) if isinstance(d, dict) and d.get("feature")]
    if not usable or not interface_path:
        return
    _recent_drops[str(interface_path)] = (time.monotonic(), usable[:MAX_DROPS_PER_TURN])


def get_recent_drops(interface_path: str) -> List[Dict[str, str]]:
    """Return and consume the most recent drops recorded for ``interface_path``.

    Drops are consumed on read so the acknowledgement block appears at most
    once per occurrence.
    """
    import time

    key = str(interface_path or "")
    entry = _recent_drops.pop(key, None)
    if not entry:
        return []
    stamp, drops = entry
    if time.monotonic() - stamp > _RECENT_DROPS_TTL_SEC:
        return []
    return drops


__all__ = [
    "MAX_DROPS_PER_TURN",
    "DROP_MEDIA",
    "DROP_REPLY",
    "DROP_VOICE",
    "collect_capability_drops",
    "get_recent_drops",
    "log_and_build_drop",
    "make_drop",
    "remember_drops",
    "render_capability_drops_block",
]
