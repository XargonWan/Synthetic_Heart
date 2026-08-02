"""Shared helpers for classifying Grillo autonomous beats.

A Grillo beat is either *internal* (introspection: diary, emotion, memory —
must NOT emit ``message_*`` actions) or *outbound* (user-facing: it targets a
real interface and IS allowed to send a message).

Historically only the ``outreach`` beat was outbound, so the distinction was
scattered across ~11 call sites as the literal ``beat_type == "outreach"`` (or
``not in ("outreach", None)``). The ``outreach`` beat was later folded into the
proactive ``observer`` beat, which is likewise outbound. Centralising the set
here keeps every gate consistent and avoids divergent literals drifting apart.
"""

from __future__ import annotations

# Beat types that are user-facing: they target a real interface and ARE
# permitted to emit ``message_*`` actions (and receive TTS, survive
# concurrent-user-message cancellation, and be excluded from language
# detection on their English system prompt).
#
# - ``observer``: the proactive chat-observer beat (folded-in ``outreach``).
# - ``scheduled_reminder``: a due calendar/scheduled event delivered to Synth as
#   an internal thought; Synth decides if/how/whom to contact, so it must be
#   allowed to emit ``message_*`` actions.
# - ``web_search_result``: a completed background web search delivered as a
#   second turn on the originating interface; Synth reports the findings to the
#   user, so it must be allowed to emit ``message_*`` actions.
OUTBOUND_BEAT_TYPES: frozenset[str] = frozenset(
    {"observer", "scheduled_reminder", "web_search_result"}
)


def is_outbound_beat(beat_type: object) -> bool:
    """Return True if ``beat_type`` denotes a user-facing (outbound) Grillo beat.

    Outbound beats are allowed to send ``message_*`` actions. Internal beats
    (any other Grillo ``beat_type``, or ``None``) are introspection-only.
    """
    return isinstance(beat_type, str) and beat_type in OUTBOUND_BEAT_TYPES


async def collect_routable_targets(limit: int = 8) -> list[dict[str, object]]:
    """Return recently active, text-routable ``interface_path`` targets.

    Network-agnostic: for each recent conversation returns a dict with
    ``interface_path``, ``last_sender`` and ``age_seconds``. Live voice paths
    (audio-only) are excluded. Used by outbound beats (observer, scheduled
    reminders) so the model can pick a real path instead of hallucinating one.

    This is a lightweight, cooldown-free variant of the observer's own target
    collector: a scheduled reminder is an explicit user-created event, so it is
    not subject to anti-spam self-cooldown gates.
    """
    from datetime import datetime, timezone

    targets: list[dict[str, object]] = []
    try:
        from core.chat_history_cache import load_chat_history
        from core.interface_paths import get_recent_interface_paths

        now = datetime.now(timezone.utc)
        recent = await get_recent_interface_paths(limit * 2)
        for item in recent:
            if len(targets) >= limit:
                break
            chat_path = item.get("interface_path")
            if not chat_path:
                continue
            chat_path = str(chat_path)
            # Skip live voice paths — audio-only, cannot receive text.
            if "_live_" in chat_path:
                continue
            try:
                messages = await load_chat_history(chat_path)
            except Exception:
                continue
            if not messages:
                continue

            last_msg = messages[-1] if isinstance(messages[-1], dict) else {}
            last_sender = str(
                last_msg.get("sender_name") or last_msg.get("sender_id") or "unknown"
            )

            age_seconds: float | None = None
            raw_ts = last_msg.get("timestamp")
            last_ts: datetime | None = None
            if isinstance(raw_ts, datetime):
                last_ts = (
                    raw_ts.replace(tzinfo=timezone.utc)
                    if raw_ts.tzinfo is None
                    else raw_ts.astimezone(timezone.utc)
                )
            elif raw_ts is not None:
                try:
                    parsed = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
                    last_ts = (
                        parsed.replace(tzinfo=timezone.utc)
                        if parsed.tzinfo is None
                        else parsed
                    )
                except Exception:
                    last_ts = None
            if last_ts is not None:
                age_seconds = (now - last_ts).total_seconds()

            targets.append(
                {
                    "interface_path": chat_path,
                    "last_sender": last_sender,
                    "age_seconds": age_seconds,
                }
            )
    except Exception:  # pragma: no cover - defensive, logged by caller if needed
        pass
    return targets


def render_routable_targets_block(targets: list[dict[str, object]]) -> str:
    """Render routable targets as a prompt block (interface_path + idle age)."""
    if not targets:
        return (
            "\n\nROUTABLE TARGETS: (none recently active — if you decide to reach "
            "out, you must know a valid interface_path yourself)\n"
        )
    block = (
        "\n\nROUTABLE TARGETS (recently active interface_path values you may reach "
        "out to):\n"
    )
    for t in targets:
        path = t.get("interface_path", "")
        age = t.get("age_seconds")
        try:
            age_h = f"{float(age) / 3600.0:.1f}h" if age is not None else "?"
        except Exception:
            age_h = "?"
        last = t.get("last_sender") or "?"
        block += f"- interface_path={path} | idle={age_h} | last_sender={last}\n"
    return block
