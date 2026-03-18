# core/live_api_logger.py
"""Bidirectional logger for Gemini Live API sessions.

Writes a dedicated log file (``logs/live_api.log``) that records every
message sent to and received from the Gemini Live API WebSocket.  Designed
for debugging context injection, audio flow, and turn management.

    ═══ SEND  [2026-03-15 21:40:01] guild=123 type=context_update ═══
    [System Context Update] [Document: readme.md]
    ...
    ─── RECV  [2026-03-15 21:40:02] guild=123 turn=1 msg#3 ────────────
    model_turn=True  has_audio=True (1920B)  has_text=False
    input_tx=None  output_tx=None

Usage::

    from core.live_api_logger import log_live_send, log_live_recv
"""

from __future__ import annotations

import logging
import os
import textwrap
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any

_DEFAULT_LOG_DIR = os.path.join(os.getcwd(), "logs")
_LOG_DIR = os.getenv("LOG_DIR", _DEFAULT_LOG_DIR)
_LOG_FILE = os.path.join(_LOG_DIR, "live_api.log")

_logger: logging.Logger | None = None

_WIDTH = 90


def _get_logger() -> logging.Logger:
    """Lazily initialise and return the live-API file logger."""
    global _logger
    if _logger is not None:
        return _logger

    os.makedirs(_LOG_DIR, exist_ok=True)

    logger = logging.getLogger("synth.live_api")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if not logger.handlers:
        handler = RotatingFileHandler(
            _LOG_FILE,
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=3,
            encoding="utf-8",
        )
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)

    _logger = logger
    return logger


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _truncate(text: str, limit: int = 2000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} chars truncated]"


# ── Outgoing (bot → Gemini) ──────────────────────────────────────────────


def log_live_send(
    guild_id: int,
    *,
    msg_type: str,
    content: str | None = None,
    audio_bytes: int = 0,
    extra: dict[str, Any] | None = None,
) -> None:
    """Log an outgoing message to the Gemini Live API."""
    logger = _get_logger()
    sep = "═" * _WIDTH
    lines = [
        f"\n{sep}",
        f"  SEND  [{_ts()}]  guild={guild_id}  type={msg_type}",
        f"{sep}",
    ]
    if content:
        lines.append(_truncate(content))
    if audio_bytes:
        lines.append(f"[audio: {audio_bytes} bytes PCM]")
    if extra:
        for k, v in extra.items():
            lines.append(f"  {k}: {v}")
    logger.debug("\n".join(lines))


# ── Incoming (Gemini → bot) ──────────────────────────────────────────────


def log_live_recv(
    guild_id: int,
    *,
    turn: int = 0,
    msg_num: int = 0,
    model_turn: bool = False,
    turn_complete: bool | None = None,
    interrupted: bool | None = None,
    audio_bytes: int = 0,
    text: str | None = None,
    input_transcript: str | None = None,
    output_transcript: str | None = None,
    tool_call: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Log an incoming message from the Gemini Live API."""
    logger = _get_logger()
    sep = "─" * _WIDTH
    lines = [
        f"{sep}",
        f"  RECV  [{_ts()}]  guild={guild_id}  turn={turn}  msg#{msg_num}",
        f"{sep}",
    ]

    flags: list[str] = []
    flags.append(f"model_turn={model_turn}")
    if turn_complete is not None:
        flags.append(f"turn_complete={turn_complete}")
    if interrupted is not None:
        flags.append(f"interrupted={interrupted}")
    if audio_bytes:
        flags.append(f"audio={audio_bytes}B")
    lines.append("  ".join(flags))

    if text:
        lines.append(f"TEXT: {_truncate(text)}")
    if input_transcript:
        lines.append(f"INPUT_TX: {input_transcript}")
    if output_transcript:
        lines.append(f"OUTPUT_TX: {output_transcript}")
    if tool_call:
        lines.append(f"TOOL_CALL: {tool_call}")
    if extra:
        for k, v in extra.items():
            lines.append(f"  {k}: {v}")

    logger.debug("\n".join(lines))


def log_live_session_event(
    guild_id: int,
    event: str,
    detail: str = "",
) -> None:
    """Log a session lifecycle event (start, stop, reconnect, etc.)."""
    logger = _get_logger()
    sep = "═" * _WIDTH
    lines = [
        f"\n{sep}",
        f"  SESSION  [{_ts()}]  guild={guild_id}  event={event}",
        f"{sep}",
    ]
    if detail:
        lines.append(textwrap.fill(detail, width=120))
    logger.debug("\n".join(lines))
