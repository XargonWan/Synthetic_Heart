"""
Peer SyntH policy — prevents cascade loops when multiple SyntH instances
share a Telegram group (or other multi-bot space).

When two bots are in the same group each bot's message can trigger alias
matching in the other, causing an infinite response chain. This module
intercepts those messages at the middleware layer — the LLM is never invoked.

Config keys (set via WebUI or DB):
  SYNTH_PEERS       JSON array of {"id": <telegram user id>, "name": <display
                    name>} objects, one per other SyntH instance sharing spaces
                    with this one. Example:
                    [{"id": 1234567, "name": "Aria"}, {"id": 8901234, "name": "Sol"}]

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
from datetime import datetime, timezone
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


def _get_peer_rows() -> list[dict[str, Any]]:
    """Return the raw configured peer rows from ``SYNTH_PEERS``.

    Expects a JSON array of ``{"id": <int>, "name": <str>}`` objects. Falls
    back gracefully to an empty list on parse errors.
    """
    raw = _read_config("SYNTH_PEERS", [])
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except Exception:
            log_warning(f"[peer_policy] SYNTH_PEERS is not valid JSON: {raw!r}")
            return []
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def get_peer_ids() -> frozenset[int]:
    """Return the configured set of peer SyntH bot user IDs."""
    result: set[int] = set()
    for row in _get_peer_rows():
        raw_id = row.get("id")
        if raw_id is None:
            continue
        try:
            result.add(int(raw_id))
        except (TypeError, ValueError):
            log_warning(f"[peer_policy] Skipping non-integer peer ID: {raw_id!r}")
    return frozenset(result)


def get_peer_names() -> dict[int, str]:
    """Return configured mapping of peer bot IDs to their SyntH display names."""
    result: dict[int, str] = {}
    for row in _get_peer_rows():
        raw_id = row.get("id")
        if raw_id is None:
            continue
        try:
            peer_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        name = row.get("name")
        if name:
            result[peer_id] = str(name)
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


async def peer_already_responded(interface_path: str, since: datetime) -> bool:
    """Return True if any peer SyntH bot posted in this chat after *since*.

    Used for turn-taking coordination: if a peer already responded to the
    triggering message, suppress this instance's turn.  Fails open (returns
    False) so a DB error never permanently silences a SyntH.
    """
    if not is_peer_mode_enabled():
        return False
    peer_ids = get_peer_ids()
    if not peer_ids:
        return False

    peer_id_strs = [str(pid) for pid in peer_ids]
    placeholders = ", ".join(["%s"] * len(peer_id_strs))

    since_utc = since
    if since_utc.tzinfo is None:
        since_utc = since_utc.replace(tzinfo=timezone.utc)

    # Match chat-level path (first two segments) so thread IDs don't fragment results.
    parts = interface_path.split("/")
    path_prefix = "/".join(parts[:2]) + "%"

    try:
        from core.db import get_conn_ctx

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT COUNT(*) FROM chat_history_cache "
                    f"WHERE interface_path LIKE %s "
                    f"AND sender_id IN ({placeholders}) "
                    f"AND timestamptz > %s",
                    (path_prefix, *peer_id_strs, since_utc),
                )
                row = await cur.fetchone()
                found = bool(row and row[0] > 0)
                if found:
                    log_debug(
                        "[peer_policy] Peer response detected — suppressing this turn"
                    )
                return found
    except Exception as e:
        log_warning(
            f"[peer_policy] peer_already_responded check failed (failing open): {e}"
        )
        return False


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
