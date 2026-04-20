from __future__ import annotations

import os
from importlib import import_module
from contextlib import contextmanager
from typing import Iterator


LANGFUSE_ENABLED = os.getenv("LANGFUSE_ENABLED", "false").lower() == "true"


@contextmanager
def maybe_langfuse_trace(name: str) -> Iterator[object | None]:
    """Create a Langfuse trace if enabled and available.

    This helper must never crash the application. It always yields either a
    trace object or None.
    """

    trace = None
    if LANGFUSE_ENABLED:
        try:
            # Lazy import keeps Langfuse optional.
            langfuse_module = import_module("langfuse")
            Langfuse = getattr(langfuse_module, "Langfuse")
            client = Langfuse()
            trace = client.trace(name=name)
        except Exception:
            trace = None

    try:
        yield trace
    finally:
        if trace is not None:
            try:
                trace.flush()
            except Exception:
                # Observability failure must never impact runtime.
                pass
