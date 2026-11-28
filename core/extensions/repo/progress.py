"""How far an install or an update has got, for the progress bar of the Store.

An install runs in a worker thread and the WebUI asks, every few hundred milliseconds,
``GET /api/extensions/store/progress``. The installer (``Installer.install``) opens an
operation for the extension; the transports report the bytes they write through
:func:`advance`, which finds the operation of the running thread by itself (a context
variable), so no signature on the way down had to change.

The percentage is honest about what is known: downloads are 90 % of it (bytes received over
the sizes the signed index announced; a dependency found later adds its own size), placing
the packages is the last 10 %.
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

_DOWNLOAD_SHARE = 90


@dataclass
class Operation:
    extension_id: str
    total_bytes: int = 0
    done_bytes: int = 0
    steps_total: int = 0
    steps_done: int = 0
    phase: str = "Preparing"

    @property
    def percent(self) -> int:
        if self.steps_total:  # every download is done: placing the packages
            placed = self.steps_done / self.steps_total
            return min(100, int(_DOWNLOAD_SHARE + (100 - _DOWNLOAD_SHARE) * placed))
        if not self.total_bytes:
            return 0
        return min(
            _DOWNLOAD_SHARE, int(_DOWNLOAD_SHARE * self.done_bytes / self.total_bytes)
        )


_lock = threading.Lock()
_operations: dict[str, Operation] = {}
_current: contextvars.ContextVar[Operation | None] = contextvars.ContextVar(
    "synth_extension_operation", default=None
)


@contextmanager
def track(extension_id: str) -> Iterator[Operation]:
    """Open the operation for ``extension_id`` in this thread, close it when done."""
    operation = Operation(extension_id)
    with _lock:
        _operations[extension_id] = operation
    token = _current.set(operation)
    try:
        yield operation
    finally:
        _current.reset(token)
        with _lock:
            if _operations.get(extension_id) is operation:
                del _operations[extension_id]


def expect(size: int, phase: str) -> None:
    """A download of ``size`` bytes is about to start."""
    operation = _current.get()
    if operation is not None:
        operation.total_bytes += max(0, size)
        operation.phase = phase


def advance(size: int) -> None:
    """``size`` more bytes were received (or were already there)."""
    operation = _current.get()
    if operation is not None:
        operation.done_bytes += max(0, size)


def placing(steps: int) -> None:
    """All the packages are here: ``steps`` of them are about to be put in place."""
    operation = _current.get()
    if operation is not None:
        operation.steps_total = max(1, steps)
        operation.steps_done = 0
        operation.phase = "Installing"


def placed() -> None:
    operation = _current.get()
    if operation is not None:
        operation.steps_done += 1


def snapshot() -> dict[str, dict[str, object]]:
    """What is running now: ``{extension id: {"percent": n, "phase": text}}``."""
    with _lock:
        return {
            ext_id: {"percent": op.percent, "phase": op.phase}
            for ext_id, op in _operations.items()
        }
