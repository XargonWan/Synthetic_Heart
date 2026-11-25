"""Starting and stopping one extension process (platform milestone M2).

:class:`Launcher` builds the sandboxed command line, starts the process, connects
its pipes to the gateway and reads its ``stderr``. :class:`ExtensionProcess` is the
handle used to stop it. Restart policy and ordering belong to the supervisor
(:mod:`core.extensions.supervisor`).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from synth_sdk.rpc import MAX_MESSAGE_BYTES, RpcError

from core.extensions.gateway import Gateway, Session
from core.extensions.manifest import Manifest
from core.extensions.sandbox import (
    SDK_DIR,
    RuntimeLayout,
    SandboxError,
    SandboxPolicy,
    UidAllocator,
    build_environment,
    effective_limits,
    popen_privilege_kwargs,
    prepare_data_dir,
    sandbox_problems,
)

StderrSink = Callable[[str, str], None]
MAX_STDERR_LINE = 4096
STDERR_TAIL = 50


class LaunchError(RuntimeError):
    """The extension process could not be started."""


@dataclass
class ExtensionProcess:
    """A running extension."""

    manifest: Manifest
    process: asyncio.subprocess.Process
    session: Session
    uid: int | None
    started_at: float = field(default_factory=time.monotonic)
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=STDERR_TAIL))
    stderr_task: asyncio.Task[None] | None = None
    watch_task: asyncio.Task[None] | None = None
    stop_requested: bool = False

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def running(self) -> bool:
        return self.process.returncode is None

    async def wait(self) -> int:
        """Wait for the process to exit; returns its exit code."""
        code = await self.process.wait()
        if self.stderr_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self.stderr_task, 1.0)
        return code

    def _signal_group(self, sig: int) -> None:
        if not self.running:
            return
        try:
            if hasattr(os, "killpg"):
                os.killpg(self.process.pid, sig)
            else:  # pragma: no cover - secondary platform
                self.process.send_signal(sig)
        except (ProcessLookupError, PermissionError):
            pass

    async def stop(self, grace: float = 5.0, term_wait: float = 3.0) -> int:
        """Ask the extension to stop, then terminate, then kill its whole group."""
        self.stop_requested = True
        if self.running:
            with contextlib.suppress(RpcError, asyncio.TimeoutError):
                await asyncio.wait_for(
                    self.session.call(
                        "lifecycle.stop", {"grace": grace}, timeout=grace
                    ),
                    grace,
                )
            try:
                await asyncio.wait_for(self.process.wait(), grace + 1.0)
            except asyncio.TimeoutError:
                self._signal_group(signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.process.wait(), term_wait)
                except asyncio.TimeoutError:
                    self._signal_group(getattr(signal, "SIGKILL", signal.SIGTERM))
                    await self.process.wait()
        # Anything the extension left behind in its group goes too.
        self._signal_group(getattr(signal, "SIGKILL", signal.SIGTERM))
        await self.session.close()
        return await self.wait()


class Launcher:
    """Starts extension processes inside the sandbox."""

    def __init__(
        self,
        gateway: Gateway,
        layout: RuntimeLayout,
        policy: SandboxPolicy | None = None,
        *,
        interpreter: str | None = None,
        sdk_dir: Path = SDK_DIR,
        on_stderr: StderrSink | None = None,
        on_warning: Callable[[str], None] | None = None,
        session_close_grace: float = 2.0,
    ) -> None:
        self.session_close_grace = session_close_grace
        self.gateway = gateway
        self.layout = layout
        self.policy = policy or SandboxPolicy()
        self.interpreter = interpreter or sys.executable
        self.sdk_dir = sdk_dir
        self._on_stderr = on_stderr
        self._on_warning = on_warning
        self._uids = UidAllocator(
            layout.uid_file, self.policy.uid_base, self.policy.uid_count
        )
        self._warned_same_user = False

    def uid_for(self, extension_id: str) -> int | None:
        """The numeric user an extension runs as, or ``None`` (same user as the Core)."""
        if not self.policy.will_drop_privileges():
            return None
        return self._uids.allocate(extension_id)

    def _warn(self, message: str) -> None:
        if self._on_warning is not None:
            self._on_warning(message)

    async def launch(
        self, manifest: Manifest, *, install_dir: Path | None = None
    ) -> ExtensionProcess:
        """Start ``manifest``'s extension and attach it to the gateway."""
        if manifest.entry is None:
            raise LaunchError(f"{manifest.id} has no [entry] in its manifest")
        install = install_dir or self.layout.install_dir(manifest.id)
        if not install.is_dir():
            raise LaunchError(
                f"{manifest.id}: install directory {install} does not exist"
            )
        if manifest.id in self.gateway.sessions:
            raise LaunchError(f"{manifest.id} is already running")

        problems = sandbox_problems(
            self.policy,
            interpreter=Path(self.interpreter),
            sdk_dir=self.sdk_dir,
            install_dir=install,
        )
        if problems:
            raise SandboxError("; ".join(problems))
        uid: int | None = None
        if self.policy.will_drop_privileges():
            uid = self._uids.allocate(manifest.id)
        elif not self._warned_same_user:
            self._warned_same_user = True
            self._warn(
                "extensions run as the same user as the Core (not root): they can read "
                "the Core's files, including .env. This is for development only."
            )
        data_dir = self.layout.data_dir(manifest.id)
        prepare_data_dir(data_dir, uid)

        entry = f"{manifest.entry.module}:{manifest.entry.function}"
        env = build_environment(
            manifest.id,
            data_dir,
            entry,
            effective_limits(self.policy, dedicated_user=uid is not None),
        )
        command = [
            self.interpreter,
            "-S",
            "-I",
            "-u",  # -I ignores PYTHONUNBUFFERED, so ask for it explicitly
            str(self.sdk_dir / "_launch.py"),
            str(self.sdk_dir),
            str(install),
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(data_dir),
                start_new_session=True,
                close_fds=True,
                umask=0o077,
                limit=MAX_MESSAGE_BYTES + 1,
                **popen_privilege_kwargs(uid),
            )
        except (OSError, ValueError) as exc:
            raise LaunchError(
                f"{manifest.id}: cannot start the process: {exc}"
            ) from exc
        assert process.stdout is not None and process.stdin is not None
        session = self.gateway.attach(manifest, process.stdout, process.stdin)
        handle = ExtensionProcess(manifest, process, session, uid)
        handle.stderr_task = asyncio.ensure_future(self._read_stderr(handle))
        handle.watch_task = asyncio.ensure_future(self._watch_session(handle))
        asyncio.ensure_future(session.serve())
        return handle

    async def _watch_session(self, handle: ExtensionProcess) -> None:
        """Kill a process whose gateway session has been closed.

        The gateway closes a session on a protocol violation (garbage, oversized
        messages, a false identity). A well-behaved process then sees end of file
        and exits; one that does not is cut off from the Core anyway, so it is not
        left running.
        """
        await handle.session.peer.wait_closed()
        if handle.stop_requested:
            return
        try:
            await asyncio.wait_for(handle.process.wait(), self.session_close_grace)
        except asyncio.TimeoutError:
            handle._signal_group(getattr(signal, "SIGKILL", signal.SIGTERM))  # noqa: SLF001

    async def _read_stderr(self, handle: ExtensionProcess) -> None:
        stream = handle.process.stderr
        if stream is None:
            return
        extension_id = handle.manifest.id
        while True:
            try:
                raw = await stream.readline()
            except (ValueError, asyncio.LimitOverrunError):
                raw = b"<line too long>\n"
                with contextlib.suppress(Exception):
                    await stream.readuntil(b"\n")
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")[:MAX_STDERR_LINE]
            handle.stderr_tail.append(line)
            if self._on_stderr is not None:
                self._on_stderr(extension_id, line)
