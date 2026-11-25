"""Integration tests: real extension processes through launcher and supervisor (M2).

Each test starts genuine subprocesses with the sandboxed command line, so these
exercise the wire protocol, the SDK runtime, the clean environment, the gateway
and the restart policy together. They run as an ordinary user: the user-change
part of the sandbox needs root and is covered separately.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import textwrap
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from core.extensions.api import register_core_api
from core.extensions.gateway import Gateway
from core.extensions.grants import MemoryGrantStore
from core.extensions.launcher import Launcher
from core.extensions.manifest import (
    Entry,
    InterfaceProvision,
    Manifest,
    Permissions,
    Provides,
)
from core.extensions.memory_ports import memory_ports
from core.extensions.ports import ApiPorts
from core.extensions.sandbox import RuntimeLayout, SandboxPolicy
from core.extensions.supervisor import RestartPolicy, State, Supervisor

ALL_CAPS = ("log", "config", "storage", "stats", "events")


@dataclass
class Harness:
    layout: RuntimeLayout
    gateway: Gateway
    grants: MemoryGrantStore
    ports: ApiPorts
    launcher: Launcher
    supervisor: Supervisor
    events: list[tuple[str, str, str]] = field(default_factory=list)
    stderr: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def install(
        self,
        name: str,
        code: str,
        *,
        caps: tuple[str, ...] = ALL_CAPS,
        grant: tuple[str, ...] | None = None,
        interfaces: tuple[InterfaceProvision, ...] = (),
    ) -> Manifest:
        ext_id = f"dev/{name}"
        directory = self.layout.install_dir(ext_id)
        directory.mkdir(parents=True)
        (directory / "ext_main.py").write_text(textwrap.dedent(code), encoding="utf-8")
        manifest = Manifest(
            id=ext_id,
            name=name,
            version="1.0.0",
            core_api=">=0.1",
            entry=Entry(module="ext_main", function="main"),
            permissions=Permissions(capabilities=caps),
            provides=Provides(interfaces=interfaces),
        )
        self.grants.grant(ext_id, *(caps if grant is None else grant))
        self.supervisor.register(manifest)
        return manifest

    async def wait_for(self, predicate: Any, timeout: float = 8.0) -> None:
        async def poll() -> None:
            while not predicate():
                await asyncio.sleep(0.02)

        await asyncio.wait_for(poll(), timeout)

    def stored(self, ext_id: str, key: str) -> Any:
        text = self.ports.storage._data.get(ext_id, {}).get(key)  # type: ignore[attr-defined]  # noqa: SLF001
        return json.loads(text) if text is not None else None


FAST = RestartPolicy(
    initial_delay=0.05,
    max_delay=0.2,
    max_failures=3,
    window=60.0,
    hello_timeout=5.0,
    stop_grace=0.5,
    term_wait=0.4,
)


@pytest_asyncio.fixture
async def h(tmp_path: Path) -> AsyncIterator[Harness]:
    ports = memory_ports()
    grants = MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    layout = RuntimeLayout(tmp_path / "extensions")
    stderr: list[tuple[str, str]] = []
    warnings: list[str] = []
    launcher = Launcher(
        gateway,
        layout,
        SandboxPolicy(drop_privileges=False),
        on_stderr=lambda ext, line: stderr.append((ext, line)),
        on_warning=warnings.append,
    )
    events: list[tuple[str, str, str]] = []
    supervisor = Supervisor(
        launcher, FAST, on_event=lambda e, k, d: events.append((e, k, d))
    )
    harness = Harness(
        layout, gateway, grants, ports, launcher, supervisor, events, stderr, warnings
    )
    try:
        yield harness
    finally:
        await supervisor.stop_all()
        await gateway.close_all()


ECHO = """
    async def main(ctx):
        await ctx.storage.set("hello", {"from": ctx.extension_id, "granted": sorted(ctx.granted)})
        ctx.log.info("started")
        ctx.stats.incr("starts")
        await ctx.stopping.wait()
"""


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_extension_starts_uses_the_api_and_stops(self, h: Harness) -> None:
        h.install("echo", ECHO)
        assert await h.supervisor.start("dev/echo") == State.RUNNING
        await h.wait_for(lambda: h.stored("dev/echo", "hello") is not None)
        assert h.stored("dev/echo", "hello") == {
            "from": "dev/echo",
            "granted": sorted(ALL_CAPS),
        }
        await h.wait_for(lambda: bool(h.ports.log.records))  # type: ignore[attr-defined]
        assert h.ports.log.records[0] == ("dev/echo", "info", "started")  # type: ignore[attr-defined]
        assert h.ports.stats.calls[0][:2] == ("incr", "ext.dev.echo.starts")  # type: ignore[attr-defined]
        pid = h.supervisor.status()["dev/echo"]["pid"]
        assert isinstance(pid, int)

        await h.supervisor.stop("dev/echo")
        assert h.supervisor.state("dev/echo") == State.STOPPED
        assert "dev/echo" not in h.gateway.sessions
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    @pytest.mark.asyncio
    async def test_a_clean_exit_is_finished_not_restarted(self, h: Harness) -> None:
        h.install("short", "async def main(ctx):\n    ctx.log.info('done')\n")
        await h.supervisor.start("dev/short")
        await h.wait_for(lambda: h.supervisor.state("dev/short") == State.FINISHED)
        await asyncio.sleep(0.3)
        assert h.supervisor.status()["dev/short"]["restarts"] == 0
        assert h.supervisor.state("dev/short") == State.FINISHED

    @pytest.mark.asyncio
    async def test_the_core_can_ping_a_running_extension(self, h: Harness) -> None:
        h.install("echo", ECHO)
        await h.supervisor.start("dev/echo")
        session = h.gateway.sessions["dev/echo"]
        assert await session.call("lifecycle.ping", timeout=3) == {"pong": True}

    @pytest.mark.asyncio
    async def test_start_all_follows_the_given_order(self, h: Harness) -> None:
        for name in ("one", "two", "three"):
            h.install(name, ECHO)
        result = await h.supervisor.start_all(["dev/two", "dev/three", "dev/one"])
        assert list(result) == ["dev/two", "dev/three", "dev/one"]
        assert set(result.values()) == {State.RUNNING}
        started = [e for e, kind, _ in h.events if kind == "running"]
        assert started == ["dev/two", "dev/three", "dev/one"]

    @pytest.mark.asyncio
    async def test_stderr_is_captured_with_the_extension_id(self, h: Harness) -> None:
        h.install(
            "noisy",
            """
            import sys
            async def main(ctx):
                print("to stdout")
                sys.stderr.write("to stderr\\n")
                await ctx.stopping.wait()
            """,
        )
        await h.supervisor.start("dev/noisy")
        await h.wait_for(lambda: ("dev/noisy", "to stderr") in h.stderr)
        # A stray print is redirected to stderr and cannot corrupt the protocol.
        assert ("dev/noisy", "to stdout") in h.stderr
        assert h.supervisor.state("dev/noisy") == State.RUNNING


class TestFailureContainment:
    @pytest.mark.asyncio
    async def test_a_crashing_extension_is_restarted_then_given_up_on(
        self, h: Harness
    ) -> None:
        h.install("crash", "async def main(ctx):\n    raise RuntimeError('boom')\n")
        h.install("good", ECHO)
        await h.supervisor.start("dev/good")
        await h.supervisor.start("dev/crash")
        await h.wait_for(lambda: h.supervisor.state("dev/crash") == State.FAILED)
        status = h.supervisor.status()["dev/crash"]
        assert status["failures"] == 3 and status["restarts"] == 2
        assert "exited with code 1" in str(status["last_error"])
        kinds = [k for e, k, _ in h.events if e == "dev/crash"]
        assert kinds.count("backoff") == 2 and kinds[-1] == "failed"
        # The neighbour never noticed.
        assert h.supervisor.state("dev/good") == State.RUNNING
        assert await h.gateway.sessions["dev/good"].call("lifecycle.ping", timeout=3)

    @pytest.mark.asyncio
    async def test_the_traceback_reaches_the_core_log_not_the_pipe(
        self, h: Harness
    ) -> None:
        h.install(
            "crash", "async def main(ctx):\n    raise RuntimeError('boom-marker')\n"
        )
        await h.supervisor.start("dev/crash")
        await h.wait_for(
            lambda: any("boom-marker" in m for _, _, m in h.ports.log.records)  # type: ignore[attr-defined]
        )

    @pytest.mark.asyncio
    async def test_an_entry_that_cannot_be_imported_is_a_failure(
        self, h: Harness
    ) -> None:
        h.install("bad", "this is not python (\n")
        await h.supervisor.start("dev/bad")
        await h.wait_for(lambda: h.supervisor.state("dev/bad") == State.FAILED)
        assert h.supervisor.status()["dev/bad"]["failures"] == 3

    @pytest.mark.asyncio
    async def test_no_handshake_within_the_timeout_is_a_failure(
        self, h: Harness, tmp_path: Path
    ) -> None:
        # A "python" that starts but never speaks the protocol.
        silent = tmp_path / "silent.sh"
        silent.write_text("#!/bin/sh\nexec sleep 30\n")
        silent.chmod(silent.stat().st_mode | stat.S_IXUSR)
        launcher = Launcher(
            h.gateway,
            h.layout,
            SandboxPolicy(drop_privileges=False),
            interpreter=str(silent),
        )
        supervisor = Supervisor(
            launcher,
            RestartPolicy(
                initial_delay=0.05,
                max_failures=2,
                hello_timeout=0.4,
                stop_grace=0.2,
                term_wait=0.2,
            ),
        )
        manifest = h.install("mute", ECHO)
        supervisor.register(manifest)
        try:
            assert await supervisor.start("dev/mute") == State.FAILED
            assert "no handshake" in str(supervisor.status()["dev/mute"]["last_error"])
            assert "dev/mute" not in h.gateway.sessions
        finally:
            await supervisor.stop_all()

    @pytest.mark.asyncio
    async def test_a_missing_install_directory_is_a_contained_failure(
        self, h: Harness
    ) -> None:
        manifest = Manifest(
            id="dev/ghost",
            name="ghost",
            version="1.0.0",
            core_api=">=0.1",
            entry=Entry(module="ext_main"),
        )
        h.supervisor.register(manifest)
        assert await h.supervisor.start("dev/ghost") == State.FAILED
        assert "does not exist" in str(h.supervisor.status()["dev/ghost"]["last_error"])

    @pytest.mark.asyncio
    async def test_an_unresponsive_extension_is_killed_with_its_children(
        self, h: Harness
    ) -> None:
        h.install(
            "stuck",
            """
            import os, signal, subprocess, time
            async def main(ctx):
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                child = subprocess.Popen(["sleep", "60"])
                await ctx.storage.set("child", child.pid)
                time.sleep(60)   # blocks the whole event loop: it cannot even hear "stop"
            """,
        )
        await h.supervisor.start("dev/stuck")
        await h.wait_for(lambda: h.stored("dev/stuck", "child") is not None)
        pid = h.supervisor.status()["dev/stuck"]["pid"]
        child = h.stored("dev/stuck", "child")
        await h.supervisor.stop("dev/stuck")
        assert h.supervisor.state("dev/stuck") == State.STOPPED
        await asyncio.sleep(0.2)
        for process_id in (pid, child):
            assert isinstance(process_id, int)
            with pytest.raises(ProcessLookupError):
                os.kill(process_id, 0)

    @pytest.mark.asyncio
    async def test_an_extension_that_ignores_stop_is_cancelled_after_the_grace(
        self, h: Harness
    ) -> None:
        h.install(
            "stubborn",
            """
            import asyncio
            async def main(ctx):
                while True:
                    await asyncio.sleep(0.05)   # never looks at ctx.stopping
            """,
        )
        await h.supervisor.start("dev/stubborn")
        await h.supervisor.stop("dev/stubborn")
        assert h.supervisor.state("dev/stubborn") == State.STOPPED

    @pytest.mark.asyncio
    async def test_restart_after_failed_state(self, h: Harness) -> None:
        h.install("crash", "async def main(ctx):\n    raise RuntimeError('x')\n")
        await h.supervisor.start("dev/crash")
        await h.wait_for(lambda: h.supervisor.state("dev/crash") == State.FAILED)
        # Starting again clears the failure history and tries again.
        await h.supervisor.start("dev/crash")
        await h.wait_for(lambda: h.supervisor.state("dev/crash") == State.FAILED)
        assert h.supervisor.status()["dev/crash"]["failures"] == 3


class TestPermissionsThroughTheRealPipe:
    @pytest.mark.asyncio
    async def test_a_denied_capability_raises_permission_denied_in_the_extension(
        self, h: Harness
    ) -> None:
        h.install(
            "nostore",
            """
            from synth_sdk.runtime import PermissionDenied
            async def main(ctx):
                try:
                    await ctx.storage.set("k", 1)
                except PermissionDenied as exc:
                    ctx.log.info("denied:" + str(exc.code))
                await ctx.stopping.wait()
            """,
            caps=("log", "storage"),
            grant=("log",),  # declared but not granted
        )
        await h.supervisor.start("dev/nostore")
        await h.wait_for(lambda: bool(h.ports.log.records))  # type: ignore[attr-defined]
        assert h.ports.log.records[0][2] == "denied:-32001"  # type: ignore[attr-defined]
        assert h.stored("dev/nostore", "k") is None
        assert any(r.outcome == "denied" for r in h.gateway.audit)

    @pytest.mark.asyncio
    async def test_events_reach_a_running_extension(self, h: Harness) -> None:
        h.install(
            "listener",
            """
            async def main(ctx):
                async def on_event(topic, payload):
                    await ctx.storage.set("got", {"topic": topic, "payload": payload})
                await ctx.events.subscribe("core.config", on_event)
                await ctx.storage.set("ready", True)
                await ctx.stopping.wait()
            """,
        )
        await h.supervisor.start("dev/listener")
        await h.wait_for(lambda: h.stored("dev/listener", "ready") is True)
        h.gateway.events.publish("core.config", {"key": "VALUE"})
        await h.wait_for(lambda: h.stored("dev/listener", "got") is not None)
        assert h.stored("dev/listener", "got") == {
            "topic": "core.config",
            "payload": {"key": "VALUE"},
        }


class TestSandboxInTheRealProcess:
    @pytest.mark.asyncio
    async def test_environment_is_clean_and_python_is_isolated(
        self, h: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DB_PASSWORD", "super-secret")
        monkeypatch.setenv("SYNTH_TEST_SECRET", "shh")
        monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
        h.install(
            "probe",
            """
            import os, sys, resource
            async def main(ctx):
                try:
                    import core
                    core_importable = True
                except ImportError:
                    core_importable = False
                try:
                    import synth_sdk
                    sdk_importable = True
                except ImportError:
                    sdk_importable = False
                data = sys.stdin.read()          # stdin is empty
                await ctx.storage.set("probe", {
                    "env": sorted(os.environ),
                    "cwd": os.getcwd(),
                    "home": os.environ.get("HOME"),
                    "core_importable": core_importable,
                    "sdk_importable": sdk_importable,
                    "stdin": data,
                    "nofile": resource.getrlimit(resource.RLIMIT_NOFILE)[0],
                    "core_limit": resource.getrlimit(resource.RLIMIT_CORE)[0],
                    "umask": oct(os.umask(0o077)),
                    "has_site": any("site-packages" in p for p in sys.path),
                    "path0": sys.path[:2],
                })
                await ctx.stopping.wait()
            """,
        )
        await h.supervisor.start("dev/probe")
        await h.wait_for(lambda: h.stored("dev/probe", "probe") is not None)
        probe = h.stored("dev/probe", "probe")
        data_dir = str(h.layout.data_dir("dev/probe"))
        assert (
            "DB_PASSWORD" not in probe["env"]
            and "SYNTH_TEST_SECRET" not in probe["env"]
        )
        assert "PYTHONPATH" not in probe["env"]
        assert set(probe["env"]) <= {
            "PATH", "LANG", "LC_ALL", "HOME", "TMPDIR", "PYTHONDONTWRITEBYTECODE",
            "PYTHONUNBUFFERED", "SYNTH_EXTENSION_ID", "SYNTH_EXTENSION_DATA",
            "SYNTH_EXTENSION_ENTRY", "SYNTH_EXT_LIMITS", "LC_CTYPE",
        }  # fmt: skip
        assert probe["cwd"] == data_dir and probe["home"] == data_dir
        assert (
            probe["core_importable"] is False
        )  # the repository root is not on sys.path
        assert probe["sdk_importable"] is True
        assert probe["stdin"] == ""
        assert probe["nofile"] == 256 and probe["core_limit"] == 0
        assert probe["umask"] == "0o77"
        assert probe["has_site"] is False

    @pytest.mark.asyncio
    async def test_data_directory_is_private_and_created(self, h: Harness) -> None:
        h.install("dirs", ECHO)
        await h.supervisor.start("dev/dirs")
        data_dir = h.layout.data_dir("dev/dirs")
        assert data_dir.is_dir() and (data_dir / "tmp").is_dir()
        assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700

    @pytest.mark.asyncio
    async def test_running_as_the_core_user_is_reported(self, h: Harness) -> None:
        h.install("a", ECHO)
        h.install("b", ECHO)
        await h.supervisor.start("dev/a")
        await h.supervisor.start("dev/b")
        assert len(h.warnings) == 1  # once, not per extension
        assert "same user as the Core" in h.warnings[0]
        assert ".env" in h.warnings[0]

    @pytest.mark.asyncio
    async def test_the_interpreter_runs_without_site_or_user_environment(
        self, h: Harness
    ) -> None:
        assert h.launcher.interpreter == sys.executable
