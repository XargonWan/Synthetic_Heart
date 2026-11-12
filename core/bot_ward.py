"""Ward against endless private conversations with bot accounts.

A bot answers every message it receives, and in a private chat every incoming
message is "directed" at the synth. Two accounts can therefore keep each other
talking forever: the synth writes, the bot replies, the reply starts a turn, the
turn writes again. Nothing else in core catches it (``peer_policy`` knows only
the SyntH peers it is configured with, ``rate_limit`` and the blocklist are per
user and know nothing about bots).

The ward sits in core, in front of the queue (``core.message_queue.enqueue``),
and judges each **incoming** message from a bot in a **private** chat:

* normally the message is accepted one priority band **below** ordinary chat
  (``demote``), so a person is always answered first and other chats never wait
  behind a bot;
* once ``BOT_WARD_MAX_MESSAGES`` bot messages were accepted inside
  ``BOT_WARD_WINDOW_SECONDS``, the conversation goes on a cooldown of
  ``BOT_WARD_COOLDOWN_SECONDS``: its messages are still kept in the chat history
  (the interface stores them before queueing) but start no turn
  (``history_only``).

State is per conversation, so one chatty bot never silences anything else. It
lives in memory: a restart forgets it, and a loop that survives one is stopped
again after another ``BOT_WARD_MAX_MESSAGES`` messages. Fail-open: any error
means ``allow``.

Who counts as a bot: the sender's ``is_bot`` flag, or — on Telegram, where the
rule is enforced by the platform — a username ending in ``bot``. A private chat
is a path with a single non-negative id (``telegram/123``, ``discord_bot/<user>``);
groups are out of scope (``peer_policy`` handles the known peers there).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from core.config_manager import config_registry
from core.interface_path_utils import parse_interface_path
from core.logging_utils import log_debug, log_warning
from core.variables_engine import register_exposed_var

ALLOW = "allow"
DEMOTE = "demote"
HISTORY_ONLY = "history_only"

#: Shown to the model on a turn whose sender is a bot (``source.note``).
BOT_NOTE = (
    "automated account: it replies to every message, so keep it short and stop "
    "once the task is done"
)


@dataclass(frozen=True)
class Verdict:
    """What to do with one incoming message."""

    action: str = ALLOW
    #: Priority bands to subtract from the ordinary chat priority (``demote``).
    priority_drop: int = 0
    reason: str = ""


_DEFAULTS: dict[str, Any] = {
    "BOT_WARD_ENABLED": True,
    "BOT_WARD_MAX_MESSAGES": 10,
    "BOT_WARD_WINDOW_SECONDS": 600,
    "BOT_WARD_COOLDOWN_SECONDS": 1800,
    "BOT_WARD_PRIORITY_DROP": 1,
}

_META: dict[str, tuple[str, str, type, str]] = {
    "BOT_WARD_ENABLED": (
        "Bot Ward",
        "Stop endless private conversations with bot accounts (see the other "
        "Bot Ward options).",
        bool,
        "bool",
    ),
    "BOT_WARD_MAX_MESSAGES": (
        "Bot Ward Messages",
        "Messages a bot may send in a private chat, inside the window, before the "
        "conversation is paused.",
        int,
        "number",
    ),
    "BOT_WARD_WINDOW_SECONDS": (
        "Bot Ward Window (s)",
        "Length of the window in which a bot's messages are counted.",
        int,
        "number",
    ),
    "BOT_WARD_COOLDOWN_SECONDS": (
        "Bot Ward Pause (s)",
        "How long a conversation stays paused: the bot's messages are kept in "
        "the history but start no reply.",
        int,
        "number",
    ),
    "BOT_WARD_PRIORITY_DROP": (
        "Bot Ward Priority Drop",
        "Priority bands below ordinary chat at which a bot's messages are "
        "processed, so people are always answered first.",
        int,
        "number",
    ),
}

_VARS: dict[str, Any] = {}
for _key, _default in _DEFAULTS.items():
    _label, _description, _type, _ui = _META[_key]
    register_exposed_var(
        _key,
        label=_label,
        default=_default,
        value_type=_type,
        ui_type=_ui,
        description=_description,
        scope="core",
        component="bot_ward",
        advanced=True,
    )
    _VARS[_key] = config_registry.get_var(
        _key,
        _default,
        label=_label,
        description=_description,
        value_type=_type,
        component="bot_ward",
        advanced=True,
    )


def _cfg_int(key: str, minimum: int = 0) -> int:
    default = int(_DEFAULTS[key])
    try:
        value = _VARS[key].value
        if isinstance(value, bool):
            return default
        return max(minimum, int(value))
    except Exception:
        return default


def _enabled() -> bool:
    try:
        value = _VARS["BOT_WARD_ENABLED"].value
        if isinstance(value, str):
            return value.strip().lower() not in ("", "0", "false", "no", "off")
        return bool(value)
    except Exception:
        return True


# conversation path -> monotonic times of the bot messages accepted in the window
_arrivals: dict[str, deque[float]] = {}
# conversation path -> monotonic time the pause ends
_cooldowns: dict[str, float] = {}


def _now() -> float:
    return time.monotonic()


def is_private_path(interface_path: Any) -> bool:
    """True for a one-to-one chat: a single, non-negative id after the interface."""
    if not isinstance(interface_path, str):
        return False
    name, levels = parse_interface_path(interface_path)
    return bool(name) and len(levels) == 1 and not levels[0].startswith("-")


def is_bot_sender(message: Any, interface_path: Any = None) -> bool:
    """True when the sender of ``message`` is a bot account."""
    sender = getattr(message, "from_user", None)
    if sender is None:
        return False
    if getattr(sender, "is_bot", False) is True:
        return True
    # Telegram obliges every bot username to end in "bot" (Foo_bot, FooBot).
    path = interface_path or getattr(message, "interface_path", None)
    name = parse_interface_path(path)[0] if isinstance(path, str) else ""
    username = getattr(sender, "username", None)
    return (
        name.startswith("telegram")
        and isinstance(username, str)
        and username.lower().endswith("bot")
    )


def path_for(message: Any, interface_id: Any) -> str:
    """The conversation path of an incoming message (``interface/chat_id``)."""
    existing = getattr(message, "interface_path", None)
    if isinstance(existing, str) and existing:
        return existing
    chat_id = getattr(message, "chat_id", None)
    if interface_id and chat_id is not None:
        return f"{interface_id}/{chat_id}"
    return ""


def _minutes(seconds: int) -> str:
    return f"{max(1, round(seconds / 60))} min"


async def check_inbound(interface_path: str, message: Any) -> Verdict:
    """Judge one incoming message. Never raises; any failure is ``allow``."""
    try:
        if not _enabled() or not interface_path:
            return Verdict()
        if not is_private_path(interface_path) or not is_bot_sender(
            message, interface_path
        ):
            return Verdict()

        now = _now()
        until = _cooldowns.get(interface_path)
        if until is not None:
            if now < until:
                return Verdict(
                    HISTORY_ONLY,
                    reason=f"paused for another {int(until - now)}s",
                )
            _cooldowns.pop(interface_path, None)
            _arrivals.pop(interface_path, None)

        window = _cfg_int("BOT_WARD_WINDOW_SECONDS", 1)
        limit = _cfg_int("BOT_WARD_MAX_MESSAGES", 1)
        arrivals = _arrivals.setdefault(interface_path, deque())
        while arrivals and now - arrivals[0] > window:
            arrivals.popleft()

        if len(arrivals) >= limit:
            cooldown = _cfg_int("BOT_WARD_COOLDOWN_SECONDS", 1)
            _cooldowns[interface_path] = now + cooldown
            arrivals.clear()
            detail = (
                f"for {_minutes(cooldown)} after {limit} messages in {_minutes(window)}"
            )
            log_warning(f"[bot_ward] {interface_path} is a bot: paused {detail}")
            from core.system_events import record_event

            await record_event("bot_ward", component=interface_path, detail=detail)
            return Verdict(HISTORY_ONLY, reason=f"paused {detail}")

        arrivals.append(now)
        return Verdict(DEMOTE, priority_drop=_cfg_int("BOT_WARD_PRIORITY_DROP"))
    except Exception as exc:  # pragma: no cover - defensive, fail-open
        log_debug(f"[bot_ward] check skipped: {exc}")
        return Verdict()


def reset() -> None:
    """Forget every window and pause (tests, manual recovery)."""
    _arrivals.clear()
    _cooldowns.clear()
