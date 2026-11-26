"""Logging for the modules of this extension: one place that forwards to ``ctx.log``.

``bind`` is called once at start-up; before that (or in a test) the lines are dropped.
"""

from __future__ import annotations

from typing import Any

_log: Any = None


def bind(log: Any) -> None:
    global _log
    _log = log


def _emit(level: str, message: str) -> None:
    if _log is not None:
        getattr(_log, level)(message)


def log_debug(message: str) -> None:
    _emit("debug", message)


def log_info(message: str) -> None:
    _emit("info", message)


def log_warning(message: str) -> None:
    _emit("warning", message)


def log_error(message: str) -> None:
    _emit("error", message)
