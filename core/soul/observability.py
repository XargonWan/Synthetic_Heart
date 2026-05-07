from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from importlib import import_module
from typing import Any, Iterator


LANGFUSE_ENABLED = os.getenv("LANGFUSE_ENABLED", "false").lower() == "true"
_runtime_logger: logging.Logger | None = None
_langfuse_warning_keys: set[str] = set()


def _get_runtime_logger() -> logging.Logger:
    global _runtime_logger
    if _runtime_logger is None:
        _runtime_logger = logging.getLogger("synth.langfuse")
    return _runtime_logger


def _warn_langfuse_once(
    key: str,
    message: str,
    *,
    exc: Exception | None = None,
) -> None:
    if key in _langfuse_warning_keys:
        return
    _langfuse_warning_keys.add(key)

    logger = _get_runtime_logger()
    if exc is None:
        logger.warning(message)
        return
    logger.warning("%s: %s", message, exc, exc_info=exc)


@contextmanager
def maybe_langfuse_trace(name: str, **trace_kwargs: Any) -> Iterator[object | None]:
    """Create a Langfuse trace if enabled and available.

    Extra keyword arguments are forwarded to ``client.trace()`` so callers can
    attach ``input``, ``metadata``, or ``tags`` at construction time.

    This helper must never crash the application. It always yields either a
    trace object or None.
    """

    trace = None
    client: object | None = None
    if LANGFUSE_ENABLED:
        try:
            # Lazy import keeps Langfuse optional.
            langfuse_module = import_module("langfuse")
            Langfuse = getattr(langfuse_module, "Langfuse")
            client = Langfuse()
            trace = client.trace(name=name, **trace_kwargs)
        except Exception as exc:
            _warn_langfuse_once(
                f"soul-langfuse-init:{type(exc).__name__}",
                f"Failed to initialize SOUL Langfuse trace for {name}",
                exc=exc,
            )
            trace = None

    try:
        yield trace
    finally:
        if trace is not None:
            try:
                flush_fn = getattr(client, "flush", None)
                if callable(flush_fn):
                    flush_fn()
                else:
                    trace_flush = getattr(trace, "flush", None)
                    if callable(trace_flush):
                        trace_flush()
            except Exception as exc:
                # Observability failure must never impact runtime.
                _warn_langfuse_once(
                    f"soul-langfuse-flush:{type(exc).__name__}",
                    f"Failed to flush SOUL Langfuse trace for {name}",
                    exc=exc,
                )
