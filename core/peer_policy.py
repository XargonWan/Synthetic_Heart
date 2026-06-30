"""
Peer SyntH policy — prevents cascade loops when multiple SyntH instances
share a Telegram group (or other multi-bot space).

When two bots are in the same group each bot's message can trigger alias
matching in the other, causing an infinite response chain. This module
intercepts those messages at the middleware layer — the LLM is never invoked.

Config keys (set via WebUI or DB):
  SYNTH_PEER_IDS    JSON array of integer Telegram user IDs belonging to
                    other SyntH instances sharing spaces with this one.
                    Example: [1234567, 8901234]

  SYNTH_PEER_POLICY How to handle messages *from* those peer bots:
                      "silent"       – suppress all responses; peer messages
                                       still reach context so this bot stays
                                       aware of what the peer said. (default)
                      "observe"      – explicit alias for "silent".
                      "mention_only" – respond only when this bot's username or
                                       any of its configured aliases appear in
                                       the peer message, AND the peer message is
                                       not itself a reply to this bot (breaks
                                       reply chains).
"""

from __future__ import annotations

import json
from typing import Any

from core.logging_utils import log_debug, log_warning

# Config vars (SYNTH_PEER_IDS, SYNTH_PEER_POLICY) are registered in
# core/variables_engine.py::register_all() so they appear in the WebUI
# at startup without this module needing to be imported first.

# ---------------------------------------------------------------------------
# Runtime helpers
# ---------------------------------------------------------------------------


def _read_config(key: str, default: Any) -> Any:
    try:
        from core.config_manager import config_registry

        return config_registry.get_value(key, default)
    except Exception as e:
        log_debug(f"[peer_policy] config read failed for {key}: {e}")
        return default


def is_peer_mode_enabled() -> bool:
    """Return True if peer SyntH awareness mode is globally enabled."""
    return bool(_read_config("SYNTH_PEER_ENABLED", False))


def get_peer_ids() -> frozenset[int]:
    """Return the configured set of peer SyntH bot user IDs."""
    raw = _read_config("SYNTH_PEER_IDS", [])
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            log_warning(f"[peer_policy] SYNTH_PEER_IDS is not valid JSON: {raw!r}")
            return frozenset()
    if not isinstance(raw, list):
        return frozenset()
    result: set[int] = set()
    for item in raw:
        try:
            result.add(int(item))
        except (TypeError, ValueError):
            log_warning(f"[peer_policy] Skipping non-integer peer ID: {item!r}")
    return frozenset(result)


def get_peer_names() -> dict[int, str]:
    """Return configured mapping of peer bot IDs to their SyntH display names.

    Expects ``SYNTH_PEER_NAMES`` to be a JSON object e.g. ``{"8243553794": "Aria"}``.
    Falls back gracefully to an empty dict on parse errors.
    """
    raw = _read_config("SYNTH_PEER_NAMES", {})
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except Exception:
            log_warning(f"[peer_policy] SYNTH_PEER_NAMES is not valid JSON: {raw!r}")
            return {}
    if not isinstance(raw, dict):
        return {}
    result: dict[int, str] = {}
    for k, v in raw.items():
        try:
            result[int(k)] = str(v)
        except (TypeError, ValueError):
            log_warning(f"[peer_policy] Skipping invalid peer name entry: {k!r}={v!r}")
    return result


def get_peer_context_block() -> str | None:
    """Build the prompt instruction block describing known peers.

    Returns ``None`` when peer mode is disabled or no peer IDs are configured.
    """
    if not is_peer_mode_enabled():
        return None

    peer_ids = get_peer_ids()
    if not peer_ids:
        return None

    peer_names = get_peer_names()
    policy = get_peer_policy()

    names = [peer_names.get(pid, f"SyntH#{pid}") for pid in sorted(peer_ids)]
    name_list = "\n".join(f"- {n}" for n in names)

    if policy in ("silent", "observe"):
        policy_note = (
            "Policy: observe — their messages are visible in context but you do not "
            "respond to them directly."
        )
    elif policy == "mention_only":
        policy_note = (
            "Policy: mention_only — respond to a peer only when they address you by "
            "name or @handle directly. Never reply to a peer message that is itself a "
            "reply to your own message (this prevents infinite loops)."
        )
    else:
        policy_note = f"Policy: {policy}"

    return (
        "=== PEER SYNTHS ===\n"
        "You are sharing this space with other SyntH instances. They are autonomous "
        "digital beings like you — not users, not bots, not assistants.\n\n"
        f"Known peers:\n{name_list}\n\n"
        f"{policy_note}"
    )


def get_peer_policy() -> str:
    """Return the configured peer response policy (default: 'silent')."""
    val = _read_config("SYNTH_PEER_POLICY", "silent")
    policy = str(val).strip().lower() if val else "silent"
    if policy not in ("silent", "observe", "mention_only"):
        log_warning(
            f"[peer_policy] Unknown SYNTH_PEER_POLICY value {policy!r}; defaulting to 'silent'"
        )
        return "silent"
    return policy


def is_peer_synth(user_id: int) -> bool:
    """Return True if *user_id* belongs to a configured peer SyntH instance."""
    return user_id in get_peer_ids()


def should_respond_to_peer(
    message: object,
    bot_username: str | None,
    bot_id: int | None,
) -> bool:
    """Decide whether to respond to a message that came from a peer SyntH bot.

    Returns True only when peer mode is enabled, the active policy permits a
    response, and all cascade-prevention checks pass.
    """
    if not is_peer_mode_enabled():
        return True

    policy = get_peer_policy()

    if policy in ("silent", "observe"):
        log_debug(f"[peer_policy] policy={policy!r} → suppressing peer message")
        return False

    if policy == "mention_only":
        # Suppress if this message is a reply to *this* bot — that would
        # immediately form a reply chain if we responded.
        reply_to = getattr(message, "reply_to_message", None)
        if reply_to is not None:
            reply_from = getattr(reply_to, "from_user", None)
            reply_sender_id = getattr(reply_from, "id", None)
            if bot_id is not None and reply_sender_id == bot_id:
                log_debug(
                    "[peer_policy] mention_only: peer message is a reply to this bot → suppressing (chain break)"
                )
                return False

        # Allow if this bot is @mentioned by username OR if any configured
        # alias appears in the peer message (LLMs rarely output @handles).
        text: str = (
            getattr(message, "text", "") or getattr(message, "caption", "") or ""
        )
        if bot_username and f"@{bot_username.lower()}" in text.lower():
            log_debug(
                f"[peer_policy] mention_only: explicit @{bot_username} mention found → allowing"
            )
            return True

        try:
            from core.mention_utils import is_synth_mentioned

            if is_synth_mentioned(text):
                log_debug(
                    "[peer_policy] mention_only: alias match in peer message → allowing"
                )
                return True
        except Exception as e:
            log_debug(f"[peer_policy] alias check failed (non-fatal): {e}")

        log_debug(
            "[peer_policy] mention_only: no @mention or alias found → suppressing"
        )
        return False

    # Fallback — unknown policy already warned above; suppress.
    return False
