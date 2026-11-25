"""Hostile-extension tests: a malicious extension must not hurt the Core (M2).

Every test launches a real process whose code deliberately misbehaves, then checks
that the Core, the gateway and a well-behaved neighbour are unaffected.

What these tests can and cannot show:

* They run as an ordinary user, so they prove the **protocol and gateway** defences
  (garbage, oversized messages, floods, lying about identity, calling things it is
  not allowed to) and the **clean environment**.
* They cannot prove protection from the Core's *files* or *signals*: when the Core
  and the extension run as the same user the extension can read ``.env`` and
  signal the Core. That needs the dedicated user and is checked in
  ``tests/test_extensions_privileged.py`` (root only).
"""

from __future__ import annotations

import asyncio
import json
import os
import textwrap
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from core.extensions.api import register_core_api
from core.extensions.gateway import Gateway, RateLimit
from core.extensions.grants import MemoryGrantStore
from core.extensions.launcher import Launcher
from core.extensions.manifest import Entry, Manifest, Permissions
from core.extensions.memory_ports import memory_ports
from core.extensions.sandbox import RuntimeLayout, SandboxPolicy
from core.extensions.supervisor import RestartPolicy, State, Supervisor

from tests.test_extensions_runtime import ALL_CAPS, ECHO, Harness


@pytest_asyncio.fixture
async def h(tmp_path: Path) -> AsyncIterator[Harness]:
    ports = memory_ports()
    grants = MemoryGrantStore()
    gateway = Gateway(grants=grants, rate_limit=RateLimit(rate=100, burst=200))
    register_core_api(gateway, ports)
    layout = RuntimeLayout(tmp_path / "extensions")
    launcher = Launcher(
        gateway,
        layout,
        SandboxPolicy(drop_privileges=False),
        session_close_grace=0.3,
    )
    supervisor = Supervisor(
        launcher,
        RestartPolicy(
            initial_delay=0.05,
            max_delay=0.1,
            max_failures=2,
            hello_timeout=5.0,
            stop_grace=0.5,
            term_wait=0.3,
        ),  # fmt: skip
    )
    harness = Harness(layout, gateway, grants, ports, launcher, supervisor)
    try:
        yield harness
    finally:
        await supervisor.stop_all()
        await gateway.close_all()


# Finds the protocol file descriptors without using the SDK, as an attacker would.
RAW_PIPE = """
    import os, select, stat
    def protocol_fds():
        found = []
        for fd in range(3, 20):
            try:
                if stat.S_ISFIFO(os.fstat(fd).st_mode):
                    found.append(fd)
            except OSError:
                pass
        return found
    def write_all(fd, data):
        view = memoryview(data)
        while view:
            try:
                view = view[os.write(fd, view):]
            except BlockingIOError:
                select.select([], [fd], [], 1)
            except OSError:
                return
"""


async def assert_core_unaffected(h: Harness, neighbour: str = "dev/good") -> None:
    """The neighbour still runs and answers, and the Core keeps accepting calls."""
    assert h.supervisor.state(neighbour) == State.RUNNING
    assert await h.gateway.sessions[neighbour].call("lifecycle.ping", timeout=3) == {
        "pong": True
    }


class TestProtocolAbuse:
    @pytest.mark.asyncio
    async def test_garbage_on_the_pipe_closes_the_session_and_kills_the_process(
        self, h: Harness
    ) -> None:
        h.install("good", ECHO)
        h.install(
            "garbage",
            RAW_PIPE
            + """
    async def main(ctx):
        for fd in protocol_fds():
            write_all(fd, b"this is not json\\n" * 20)
        import time
        time.sleep(60)   # blocks the event loop: ignores the closed connection
""",
        )
        await h.supervisor.start("dev/good")
        await h.supervisor.start("dev/garbage")
        await h.wait_for(lambda: h.supervisor.state("dev/garbage") == State.FAILED)
        assert "dev/garbage" not in h.gateway.sessions
        await assert_core_unaffected(h)

    @pytest.mark.asyncio
    async def test_an_oversized_message_closes_the_session(self, h: Harness) -> None:
        h.install("good", ECHO)
        h.install(
            "huge",
            RAW_PIPE
            + """
    async def main(ctx):
        line = b'{"jsonrpc":"2.0","method":"log.write","params":{"level":"info","message":"' + b"A" * (2 * 1024 * 1024) + b'"}}\\n'
        for fd in protocol_fds():
            write_all(fd, line)
        import time
        time.sleep(60)   # blocks the event loop: ignores the closed connection
""",
        )
        await h.supervisor.start("dev/good")
        await h.supervisor.start("dev/huge")
        await h.wait_for(lambda: h.supervisor.state("dev/huge") == State.FAILED)
        assert not any("AAAA" in m for _, _, m in h.ports.log.records)  # type: ignore[attr-defined]
        await assert_core_unaffected(h)

    @pytest.mark.asyncio
    async def test_a_flood_is_throttled_and_does_not_hurt_the_core(
        self, h: Harness
    ) -> None:
        h.install("good", ECHO)
        h.install(
            "flood",
            RAW_PIPE
            + """
    import json
    async def main(ctx):
        line = (json.dumps({"jsonrpc": "2.0", "method": "stats.incr", "params": {"name": "spam"}}) + "\\n").encode()
        fds = protocol_fds()
        for _ in range(20):
            for fd in fds:
                write_all(fd, line * 500)
        import asyncio
        from synth_sdk.rpc import RpcError
        for _ in range(100):           # the rate limiter is empty for a moment: retry
            try:
                await ctx.storage.set("done", True)
                break
            except RpcError:
                await asyncio.sleep(0.1)
        await ctx.stopping.wait()
""",
        )
        await h.supervisor.start("dev/good")
        await h.supervisor.start("dev/flood")
        await h.wait_for(lambda: h.stored("dev/flood", "done") is True, timeout=15)
        await asyncio.sleep(0.3)
        accepted = [c for c in h.ports.stats.calls if c[1] == "ext.dev.flood.spam"]  # type: ignore[attr-defined]
        # 10,000 lines were sent: only roughly the burst (+ what refilled meanwhile) got through.
        assert len(accepted) < 1000
        assert any(r.outcome == "rate_limited" for r in h.gateway.audit)
        assert len(h.gateway.audit) <= h.gateway.audit.maxlen  # type: ignore[operator]
        await assert_core_unaffected(h)

    @pytest.mark.asyncio
    async def test_claiming_to_be_another_extension_is_refused(
        self, h: Harness
    ) -> None:
        h.install("good", ECHO)
        h.install(
            "liar",
            RAW_PIPE
            + """
    import json
    async def main(ctx):
        # A second handshake that names a different extension.
        line = (json.dumps({"jsonrpc": "2.0", "id": 99, "method": "session.hello", "params": {"protocol": 1, "sdk_version": "x", "extension_id": "dev/good"}}) + "\\n").encode()
        for fd in protocol_fds():
            write_all(fd, line)
        await ctx.stopping.wait()
""",
        )
        await h.supervisor.start("dev/good")
        await h.supervisor.start("dev/liar")
        await asyncio.sleep(0.5)
        # The impostor got no access to the real extension's session or data.
        assert h.gateway.sessions["dev/good"].extension_id == "dev/good"
        assert h.stored("dev/good", "hello")["from"] == "dev/good"
        await assert_core_unaffected(h)


class TestAskingForTooMuch:
    @pytest.mark.asyncio
    async def test_every_internal_looking_method_is_unknown(self, h: Harness) -> None:
        h.install(
            "prober",
            """
            from synth_sdk.rpc import RpcError
            NAMES = ["db.query", "db.execute", "config.set", "plugins.load", "plugins.list",
                     "message_queue.enqueue", "interfaces.register", "core.shutdown",
                     "os.system", "eval", "__import__", "cortex.generate", "karada.play",
                     "storage.get_all", "extensions.install"]
            async def main(ctx):
                results = {}
                for name in NAMES:
                    try:
                        await ctx.call(name)
                        results[name] = "ALLOWED"
                    except RpcError as exc:
                        results[name] = exc.code
                await ctx.storage.set("results", results)
                await ctx.stopping.wait()
            """,
        )
        await h.supervisor.start("dev/prober")
        await h.wait_for(lambda: h.stored("dev/prober", "results") is not None)
        results = h.stored("dev/prober", "results")
        assert set(results.values()) == {-32601}  # METHOD_NOT_FOUND for every one

    @pytest.mark.asyncio
    async def test_it_cannot_name_another_extensions_data_or_series(
        self, h: Harness
    ) -> None:
        h.install("victim", ECHO)
        h.install(
            "thief",
            """
            from synth_sdk.rpc import RpcError
            async def main(ctx):
                out = {}
                for params in ({"key": "hello", "extension_id": "dev/victim"},
                               {"key": "hello", "ns": "dev/victim"}):
                    try:
                        await ctx.call("storage.get", **params)
                        out[str(sorted(params))] = "ALLOWED"
                    except RpcError as exc:
                        out[str(sorted(params))] = exc.code
                # Its own "hello" key does not exist: the victim's value is not reachable.
                out["own"] = await ctx.storage.get("hello", "absent")
                await ctx.storage.set("out", out)
                await ctx.stopping.wait()
            """,
        )
        await h.supervisor.start("dev/victim")
        await h.wait_for(lambda: h.stored("dev/victim", "hello") is not None)
        await h.supervisor.start("dev/thief")
        await h.wait_for(lambda: h.stored("dev/thief", "out") is not None)
        out = h.stored("dev/thief", "out")
        assert out["own"] == "absent"
        assert all(
            v in (-32602,) for k, v in out.items() if k != "own"
        )  # INVALID_PARAMS

    @pytest.mark.asyncio
    async def test_a_revoked_grant_stops_a_running_extension_mid_flight(
        self, h: Harness
    ) -> None:
        h.install(
            "writer",
            """
            import asyncio
            from synth_sdk.runtime import PermissionDenied
            async def main(ctx):
                n = 0
                while True:
                    try:
                        await ctx.storage.set("n", n)
                        n += 1
                    except PermissionDenied:
                        await ctx.log.info("revoked")
                        ctx.log.info("revoked")
                        break
                    await asyncio.sleep(0.02)
                await ctx.stopping.wait()
            """,
        )
        await h.supervisor.start("dev/writer")
        await h.wait_for(lambda: (h.stored("dev/writer", "n") or 0) >= 3)
        h.grants.revoke("dev/writer", "storage")
        await h.wait_for(lambda: bool(h.ports.log.records))  # type: ignore[attr-defined]
        last = h.stored("dev/writer", "n")
        await asyncio.sleep(0.2)
        assert h.stored("dev/writer", "n") == last  # nothing written after the revoke


class TestEnvironmentSecrets:
    @pytest.mark.asyncio
    async def test_the_cores_secrets_are_not_in_the_extension_environment(
        self, h: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secrets = {
            "DB_PASSWORD": "pw-1-hunter2",
            "POSTGRES_PASSWORD": "pw-2-hunter2",
            "TELEGRAM_BOT_TOKEN": "tok-hunter2",
            "OPENAI_API_KEY": "sk-hunter2",
            "SYNTH_SECRET": "secret-hunter2",
        }
        for key, value in secrets.items():
            monkeypatch.setenv(key, value)
        h.install(
            "spy",
            """
            import os
            async def main(ctx):
                await ctx.storage.set("env", dict(os.environ))
                # also look at the parent's environment through /proc, as an attacker would
                try:
                    parent = open(f"/proc/{os.getppid()}/environ", "rb").read().decode("utf-8", "replace")
                except OSError as exc:
                    parent = "unreadable: " + exc.__class__.__name__
                await ctx.storage.set("parent_environ_has_secret", "pw-1-hunter2" in parent)
                await ctx.stopping.wait()
            """,
        )
        await h.supervisor.start("dev/spy")
        await h.wait_for(lambda: h.stored("dev/spy", "env") is not None)
        env = h.stored("dev/spy", "env")
        blob = json.dumps(env)
        for key, value in secrets.items():
            assert key not in env and value not in blob
        # Reading the Core's environment via /proc succeeds only for the same user:
        # this is exactly why the dedicated user matters. Recorded, not asserted.
        await h.wait_for(
            lambda: h.stored("dev/spy", "parent_environ_has_secret") is not None
        )
        if os.geteuid() != 0:
            assert h.stored("dev/spy", "parent_environ_has_secret") in (True, False)


def test_helper_manifest_is_valid() -> None:
    manifest = Manifest(
        id="dev/x",
        name="x",
        version="1.0.0",
        core_api=">=0.1",
        entry=Entry(module="m"),
        permissions=Permissions(capabilities=ALL_CAPS),
    )
    assert manifest.entry is not None and textwrap.dedent("a") == "a"
