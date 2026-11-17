"""Presence indicators: show that the synth is typing, recording or sending a file.

While a reply is being generated the person on the other side sees nothing; a
chat client can show "typing…", "recording a voice message…" or "sending a
file…" instead. This module keeps such an indicator alive for as long as the
work lasts, on any interface that can show one.

Interface contract (optional, structural):

``async def send_activity(self, interface_path: str, kind: str) -> None``
    Show one indicator pulse in the conversation ``interface_path``. ``kind`` is
    one of ``KIND_*`` below; an interface that has no wording for a kind uses the
    nearest one (Discord has only "typing"). Chat clients drop the indicator after
    a few seconds, so core repeats the pulse every ``REFRESH_SECONDS``.

Core calls:

* :func:`begin` / :func:`end` bracket a turn (``core.message_queue``): typing
  while the model generates.
* :func:`show` brackets a phase that has its own wording (``core.action_parser``):
  recording while a voice note is synthesised, sending while a file goes out.

Fail-open: an interface without ``send_activity``, a path that is not a chat, a
send that errors — none of them can touch the turn. The pulse stops by itself
after repeated failures or ``MAX_SECONDS``.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

from core.config_manager import config_registry
from core.interface_path_utils import parse_interface_path
from core.logging_utils import log_debug
from core.variables_engine import register_exposed_var

KIND_TYPING = "typing"
KIND_RECORD_AUDIO = "record_audio"
KIND_UPLOAD_AUDIO = "upload_audio"
KIND_UPLOAD_FILE = "upload_file"

#: Chat clients clear an indicator after ~5 s; pulse a little sooner than that.
REFRESH_SECONDS = 4.0
#: No indicator outlives a (generous) turn, whatever happens to the stop call.
MAX_SECONDS = 240.0
#: Consecutive failed pulses after which the indicator gives up.
MAX_FAILURES = 3
_SEND_TIMEOUT_SECONDS = 5.0

register_exposed_var(
    "CHAT_ACTIVITY_ENABLED",
    label="Show Typing / Recording Status",
    default=True,
    value_type=bool,
    ui_type="bool",
    description=(
        "While the synth is writing a reply, recording a voice note or sending a "
        'file, show it in the chat ("typing…") on interfaces that support it.'
    ),
    scope="core",
    component="core",
)


@dataclass
class _Pulse:
    kind: str
    task: Optional["asyncio.Task[None]"] = field(default=None, repr=False)


_pulses: dict[str, _Pulse] = {}


def _enabled() -> bool:
    try:
        return bool(
            config_registry.get_value("CHAT_ACTIVITY_ENABLED", True, value_type=bool)
        )
    except Exception:
        return True


def _sender_for(interface_path: str) -> Any:
    """The active interface behind ``interface_path`` if it can show activity."""
    try:
        from core.core_initializer import INTERFACE_REGISTRY, core_initializer

        name, levels = parse_interface_path(interface_path)
        if not name or not levels or name not in core_initializer.active_interfaces:
            return None
        iface = INTERFACE_REGISTRY.get(name)
        return iface if callable(getattr(iface, "send_activity", None)) else None
    except Exception as exc:
        log_debug(f"[chat_activity] no sender for {interface_path}: {exc}")
        return None


async def _run(interface_path: str, iface: Any, pulse: _Pulse) -> None:
    started = time.monotonic()
    failures = 0
    while time.monotonic() - started < MAX_SECONDS:
        try:
            await asyncio.wait_for(
                iface.send_activity(interface_path, pulse.kind),
                timeout=_SEND_TIMEOUT_SECONDS,
            )
            failures = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures += 1
            log_debug(f"[chat_activity] pulse failed for {interface_path}: {exc!r}")
            if failures >= MAX_FAILURES:
                break
        await asyncio.sleep(REFRESH_SECONDS)
    if _pulses.get(interface_path) is pulse:
        _pulses.pop(interface_path, None)


async def begin(interface_path: Any, kind: str = KIND_TYPING) -> None:
    """Start (or switch) the indicator for a conversation. Never raises."""
    if not isinstance(interface_path, str) or not interface_path or not _enabled():
        return
    existing = _pulses.get(interface_path)
    if existing is not None:
        existing.kind = kind
        return
    iface = _sender_for(interface_path)
    if iface is None:
        return
    pulse = _Pulse(kind=kind)
    pulse.task = asyncio.get_running_loop().create_task(
        _run(interface_path, iface, pulse)
    )
    _pulses[interface_path] = pulse


async def end(interface_path: Any) -> None:
    """Stop the indicator for a conversation. Never raises."""
    if not isinstance(interface_path, str):
        return
    pulse = _pulses.pop(interface_path, None)
    if pulse is not None and pulse.task is not None:
        pulse.task.cancel()
        await asyncio.gather(pulse.task, return_exceptions=True)


@asynccontextmanager
async def show(interface_path: Any, kind: str) -> AsyncIterator[None]:
    """Show ``kind`` for the duration of a phase, then go back to what it was.

    Inside a running turn this switches the typing indicator to "recording…" and
    back; on its own it starts and stops an indicator.
    """
    previous = None
    if isinstance(interface_path, str):
        existing = _pulses.get(interface_path)
        previous = existing.kind if existing is not None else None
    await begin(interface_path, kind)
    try:
        yield
    finally:
        if previous is not None and isinstance(interface_path, str):
            pulse = _pulses.get(interface_path)
            if pulse is not None:
                pulse.kind = previous
        else:
            await end(interface_path)


def kind_for_action(action_type: Any, payload: Any) -> Optional[str]:
    """The indicator an action deserves while it runs (None = none).

    ``tts_speak`` synthesises a voice note; a message carrying ``media`` uploads
    a file. Anything else gets no wording of its own.
    """
    if action_type == "tts_speak":
        return KIND_RECORD_AUDIO
    if isinstance(payload, dict) and payload.get("media"):
        return KIND_UPLOAD_FILE
    return None
