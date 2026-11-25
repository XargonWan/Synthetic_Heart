"""Keeping extension processes alive, and never letting one hurt the Core (M2).

For each registered extension the supervisor runs a small state machine::

    STOPPED -> STARTING -> RUNNING -> (exit) -> BACKOFF -> STARTING ...
                                  \\-> FINISHED   (clean exit on its own)
                                  \\-> FAILED     (too many crashes: gives up)

* A crash (non-zero exit, no ``session.bye``, failed start, or no handshake within
  ``hello_timeout``) counts as a failure and the extension is restarted after an
  exponentially growing delay. ``max_failures`` failures within ``window`` seconds
  mark it ``FAILED``: it stays down until someone starts it again.
* An extension that finishes cleanly (it said ``session.bye`` and exited 0) is
  ``FINISHED`` and not restarted.
* Whatever happens to one extension is contained: every step is wrapped, and the
  other extensions and the Core are not affected.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from core.extensions.launcher import ExtensionProcess, LaunchError, Launcher
from core.extensions.manifest import Manifest
from core.extensions.sandbox import SandboxError


class State(enum.Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    BACKOFF = "backoff"
    FINISHED = "finished"
    FAILED = "failed"


@dataclass(frozen=True)
class RestartPolicy:
    initial_delay: float = 1.0
    max_delay: float = 60.0
    factor: float = 2.0
    #: Failures within ``window`` seconds after which the supervisor gives up.
    max_failures: int = 5
    window: float = 300.0
    #: Seconds a new process has to complete ``session.hello``.
    hello_timeout: float = 10.0
    stop_grace: float = 5.0
    #: Seconds between SIGTERM and SIGKILL for an extension that ignores a stop.
    term_wait: float = 3.0


@dataclass
class Managed:
    manifest: Manifest
    install_dir: Path | None
    state: State = State.STOPPED
    failures: list[float] = field(default_factory=list)
    last_error: str = ""
    restarts: int = 0
    process: ExtensionProcess | None = None
    task: asyncio.Task[None] | None = None
    running_event: asyncio.Event = field(default_factory=asyncio.Event)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)


EventSink = Callable[[str, str, str], None]


class Supervisor:
    def __init__(
        self,
        launcher: Launcher,
        policy: RestartPolicy | None = None,
        *,
        on_event: EventSink | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.launcher = launcher
        self.policy = policy or RestartPolicy()
        self._on_event = on_event
        self._clock = clock
        self._items: dict[str, Managed] = {}

    # ------------------------------------------------------------- bookkeeping

    def register(self, manifest: Manifest, install_dir: Path | None = None) -> None:
        if manifest.id in self._items:
            raise ValueError(f"{manifest.id} is already registered")
        self._items[manifest.id] = Managed(manifest, install_dir)

    def forget(self, extension_id: str) -> None:
        """Unregister a stopped extension (it is being uninstalled or replaced)."""
        item = self._items.get(extension_id)
        if item is not None and item.state not in (
            State.STOPPED,
            State.FAILED,
            State.FINISHED,
        ):
            raise ValueError(
                f"{extension_id} is still {item.state.value}: stop it first"
            )
        self._items.pop(extension_id, None)

    def known(self, extension_id: str) -> bool:
        return extension_id in self._items

    def state(self, extension_id: str) -> State:
        return self._items[extension_id].state

    def status(self) -> dict[str, dict[str, object]]:
        return {
            ext_id: {
                "state": item.state.value,
                "restarts": item.restarts,
                "failures": len(item.failures),
                "last_error": item.last_error,
                "pid": item.process.pid
                if item.process and item.process.running
                else None,
            }
            for ext_id, item in sorted(self._items.items())
        }

    def _event(self, extension_id: str, kind: str, detail: str = "") -> None:
        if self._on_event is not None:
            with contextlib.suppress(Exception):
                self._on_event(extension_id, kind, detail)

    def _set(self, item: Managed, state: State, detail: str = "") -> None:
        item.state = state
        self._event(item.manifest.id, state.value, detail)

    # ---------------------------------------------------------------- control

    async def start(self, extension_id: str, *, wait: bool = True) -> State:
        """Start an extension.

        With ``wait`` it returns when the extension has completed its handshake
        (``RUNNING``) or the supervisor has given up. It does not wait for the
        extension to *stay* up: crashes are handled by the restart policy.
        """
        item = self._items[extension_id]
        if item.task is not None and not item.task.done():
            return item.state
        item.failures.clear()
        item.last_error = ""
        item.running_event.clear()
        item.stop_event.clear()
        item.task = asyncio.ensure_future(self._run(item))
        if wait:
            await self._wait_settled(item)
        return item.state

    async def _wait_settled(self, item: Managed) -> None:
        waiter = asyncio.ensure_future(item.running_event.wait())
        assert item.task is not None
        try:
            await asyncio.wait({waiter, item.task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()

    async def start_all(self, order: Iterable[str]) -> dict[str, State]:
        """Start extensions in ``order`` (dependencies first), one after the other."""
        result: dict[str, State] = {}
        for extension_id in order:
            if extension_id not in self._items:
                continue
            result[extension_id] = await self.start(extension_id)
        return result

    async def stop(self, extension_id: str) -> None:
        item = self._items[extension_id]
        item.stop_event.set()
        if item.state != State.STOPPED:
            self._set(item, State.STOPPING)
        if item.process is not None and item.process.running:
            with contextlib.suppress(Exception):
                await item.process.stop(self.policy.stop_grace, self.policy.term_wait)
        if item.task is not None:
            item.task.cancel()
            await asyncio.gather(item.task, return_exceptions=True)
        item.process = None
        self._set(item, State.STOPPED)

    async def stop_all(self) -> None:
        for extension_id in reversed(list(self._items)):
            await self.stop(extension_id)

    # ------------------------------------------------------------- main loop

    def _backoff(self, failures: int) -> float:
        p = self.policy
        return float(
            min(p.max_delay, p.initial_delay * (p.factor ** max(0, failures - 1)))
        )

    def _record_failure(self, item: Managed, reason: str) -> bool:
        """Record a failure. Returns True if the supervisor should give up."""
        now = self._clock()
        item.last_error = reason
        item.failures = [t for t in item.failures if now - t <= self.policy.window]
        item.failures.append(now)
        return len(item.failures) >= self.policy.max_failures

    async def _run(self, item: Managed) -> None:
        ext_id = item.manifest.id
        while not item.stop_event.is_set():
            self._set(item, State.STARTING)
            handle: ExtensionProcess | None = None
            try:
                handle = await self.launcher.launch(
                    item.manifest, install_dir=item.install_dir
                )
                item.process = handle
                await self._await_hello(handle)
                self._set(item, State.RUNNING, f"pid {handle.pid}")
                item.running_event.set()
                code = await handle.wait()
            except (LaunchError, SandboxError, ValueError, OSError) as exc:
                reason = f"cannot start: {exc}"
                code = None
            except _NoHandshake as exc:
                reason = str(exc)
                code = None
                if handle is not None:
                    with contextlib.suppress(Exception):
                        await handle.stop(1.0)
            except asyncio.CancelledError:
                if handle is not None and handle.running:
                    with contextlib.suppress(Exception):
                        await asyncio.shield(
                            handle.stop(self.policy.stop_grace, self.policy.term_wait)
                        )
                raise
            except Exception as exc:  # noqa: BLE001 - nothing may escape the supervisor
                reason = f"unexpected error: {type(exc).__name__}: {exc}"
                code = None
            else:
                reason = ""

            item.process = None
            item.running_event.clear()
            if item.stop_event.is_set():
                return
            if code is not None:
                if code == 0 and handle is not None and handle.session.said_bye:
                    self._set(item, State.FINISHED, "exited cleanly")
                    item.running_event.set()
                    return
                tail = ""
                if handle is not None and handle.stderr_tail:
                    tail = " | " + handle.stderr_tail[-1]
                reason = f"exited with code {code}{tail}"
            if self._record_failure(item, reason):
                self._set(item, State.FAILED, f"giving up: {reason}")
                item.running_event.set()
                return
            delay = self._backoff(len(item.failures))
            item.restarts += 1
            self._set(item, State.BACKOFF, f"{reason}; retrying in {delay:.1f}s")
            try:
                await asyncio.wait_for(item.stop_event.wait(), delay)
            except asyncio.TimeoutError:
                pass
        self._event(ext_id, "stopped", "")

    async def _await_hello(self, handle: ExtensionProcess) -> None:
        deadline = self._clock() + self.policy.hello_timeout
        while not handle.session.hello_done:
            if not handle.running:
                raise _NoHandshake("exited before completing the handshake")
            if self._clock() >= deadline:
                raise _NoHandshake(
                    f"no handshake within {self.policy.hello_timeout:.1f}s"
                )
            await asyncio.sleep(0.02)


class _NoHandshake(Exception):
    """The process started but never completed ``session.hello``."""
