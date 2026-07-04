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

  NOTE: for a peer bot's messages to reach this instance at all, "Bot-to-Bot
  Communication Mode" must be enabled for both bots in BotFather, AND this
  bot must have Group Privacy Mode disabled (or admin rights) in the shared
  group -- otherwise Telegram never delivers the peer's messages here in the
  first place, and everything below is moot. See docs/peer_synths.rst.

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

import asyncio
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

# Per-chat wake signal so wait_for_peer_reply() reacts immediately when a new
# message is recorded, instead of blindly sleeping a fixed interval. Keyed by
# the chat-level interface_path prefix (same normalization as
# peer_already_responded's path_prefix) so thread IDs share one event.
# Each entry also remembers the event loop it was created on: asyncio.Event
# is bound to whichever loop is running when it's first waited on, so a stale
# entry from a previous/foreign loop (e.g. across a loop restart, or between
# independent test runs) must never be reused -- it would raise instead of
# just failing open.
_message_arrival_events: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Event]] = {}


def _chat_key(interface_path: str) -> str:
    parts = interface_path.split("/")
    return "/".join(parts[:2])


def notify_message_arrived(interface_path: str) -> None:
    """Wake any pending :func:`wait_for_peer_reply` call for this chat.

    Call this right after a message is recorded into chat history/context so
    relay waits react immediately rather than on the next poll tick. Safe to
    call unconditionally (no-op if nothing is currently waiting on this chat).
    """
    cached = _message_arrival_events.get(_chat_key(interface_path))
    if cached is None:
        return
    loop, event = cached
    if loop is asyncio.get_event_loop():
        event.set()


def _get_arrival_event(interface_path: str) -> asyncio.Event:
    key = _chat_key(interface_path)
    loop = asyncio.get_event_loop()
    cached = _message_arrival_events.get(key)
    if cached is not None and cached[0] is loop:
        return cached[1]
    event = asyncio.Event()
    _message_arrival_events[key] = (loop, event)
    return event


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
        "There is no separate channel for them: a peer's turn arrives in the same "
        "message flow as your trainer's and is marked with a `[PeerName]: ` prefix "
        "at the very start of the message (e.g. `[SynthA]: ...`). That prefix always "
        "means the named peer said it — never your trainer — even though it shares "
        "the same message channel. A message with no such prefix is your trainer "
        "speaking directly.\n\n"
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


async def peer_already_responded(
    interface_path: str,
    since: datetime,
    peer_ids: frozenset[int] | None = None,
) -> bool:
    """Return True if a peer SyntH bot posted in this chat after *since*.

    Checks all configured peers by default; pass *peer_ids* to narrow the
    check to one or more specific peers (used by :func:`wait_for_peer_reply`
    for mention-order relay). Fails open (returns False) so a DB error never
    permanently silences a SyntH.
    """
    if not is_peer_mode_enabled():
        return False
    if peer_ids is None:
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
                    f"AND timestamp > %s",
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


def get_relay_wait_peer(text: str) -> int | None:
    """Return the peer bot ID this instance should wait on before replying.

    Implements mention-order turn relay: when a single message addresses
    multiple SyntHs in sequence (e.g. "SynthA, ... SynthB, ..."), the later-addressed
    instance should wait for the earlier one to actually post its reply
    before generating its own — so the earlier reply is already present in
    this instance's own chat history/context (added unconditionally when the
    peer's message arrives, see peer_synths.rst) by the time this instance
    responds.

    Returns the peer mentioned closest before this bot's own mention, or
    None if this bot isn't mentioned, no configured peer precedes it, or
    peer mode is disabled.
    """
    if not text or not is_peer_mode_enabled():
        return None

    peer_names = get_peer_names()
    if not peer_names:
        return None

    text_lower = text.lower()

    peer_positions: list[tuple[int, int]] = []
    for pid, name in peer_names.items():
        if not name:
            continue
        idx = text_lower.find(name.lower())
        if idx != -1:
            peer_positions.append((idx, pid))
    if not peer_positions:
        return None

    try:
        from core.mention_utils import get_current_aliases

        my_positions = [
            text_lower.find(alias.lower()) for alias in get_current_aliases() if alias
        ]
    except Exception as e:
        log_debug(f"[peer_policy] get_relay_wait_peer alias lookup failed: {e}")
        return None
    my_positions = [p for p in my_positions if p != -1]
    if not my_positions:
        # This bot isn't named in the text at all -- normal mention/attention
        # logic decides whether it responds; relay ordering doesn't apply.
        return None
    my_pos = min(my_positions)

    preceding = [(pos, pid) for pos, pid in peer_positions if pos < my_pos]
    if not preceding:
        return None

    # If more than one peer precedes us, wait on the closest (last-mentioned)
    # one -- in a well-behaved relay chain its reply implies the earlier
    # peers already went too, so we don't stack up N sequential waits.
    preceding.sort()
    return preceding[-1][1]


async def wait_for_peer_reply(
    interface_path: str,
    peer_id: int,
    since: datetime,
    timeout_seconds: float | None = None,
    poll_interval: float = 1.5,
) -> bool:
    """Wait until *peer_id* replies in *interface_path* after *since*, or timeout.

    Used for mention-order turn relay (see :func:`get_relay_wait_peer`). Reacts
    immediately when :func:`notify_message_arrived` fires for this chat (called
    right after any message is recorded into chat history), so a fast peer
    reply doesn't sit idle for the rest of a fixed poll interval; *poll_interval*
    is only a safety-net upper bound between checks, covering the case where a
    notification is missed. Fails open: always returns once *timeout_seconds*
    elapses so an offline/slow peer can never permanently block this instance's
    turn.
    """
    if timeout_seconds is None:
        timeout_seconds = float(_read_config("SYNTH_PEER_RELAY_TIMEOUT_SECONDS", 60.0))
    if timeout_seconds <= 0:
        return False

    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_seconds
    target = frozenset({peer_id})
    event = _get_arrival_event(interface_path)

    while True:
        if await peer_already_responded(interface_path, since, peer_ids=target):
            return True
        remaining = deadline - loop.time()
        if remaining <= 0:
            log_debug(
                f"[peer_policy] Relay wait for peer {peer_id} timed out after "
                f"{timeout_seconds:.0f}s -- proceeding anyway"
            )
            return False
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=min(remaining, poll_interval))
        except asyncio.TimeoutError:
            pass


def is_peer_synth(user_id: int) -> bool:
    """Return True if *user_id* belongs to a configured peer SyntH instance."""
    return user_id in get_peer_ids()


async def _self_replied_recently(chat_id: int, cooldown_seconds: float) -> bool:
    """Return True if this bot itself sent a message in *chat_id* within the
    last *cooldown_seconds*.

    Used to throttle mention_only peer responses: in an active multi-bot
    group, a human message and a peer's message often land seconds apart and
    each independently mentions this bot's alias, causing two replies in
    quick succession. Fails open (False) so a DB error never permanently
    silences a SyntH.
    """
    if cooldown_seconds <= 0:
        return False
    try:
        from core.db import get_conn_ctx

        path_prefix = f"telegram_bot/{chat_id}%"
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT MAX(timestamp) FROM chat_history_cache "
                    "WHERE interface_path LIKE %s AND sender_id = 'self'",
                    (path_prefix,),
                )
                row = await cur.fetchone()
                if not row or not row[0]:
                    return False
                last_ts = row[0]
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - last_ts).total_seconds()
                return elapsed < cooldown_seconds
    except Exception as e:
        log_warning(
            f"[peer_policy] self_replied_recently check failed (failing open): {e}"
        )
        return False


async def should_respond_to_peer(
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

        # Suppress if we already replied in this chat very recently. A human
        # message and a peer's message routinely land a few seconds apart in
        # an active group and each independently mention this bot's alias --
        # without this, both would trigger a full response, producing two
        # replies back-to-back for what reads as one conversational beat.
        cooldown_seconds = float(
            _read_config("SYNTH_PEER_MENTION_COOLDOWN_SECONDS", 20.0)
        )
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        if (
            cooldown_seconds > 0
            and chat_id is not None
            and await _self_replied_recently(chat_id, cooldown_seconds)
        ):
            log_debug(
                f"[peer_policy] mention_only: self replied within cooldown "
                f"({cooldown_seconds:.0f}s) → suppressing"
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
