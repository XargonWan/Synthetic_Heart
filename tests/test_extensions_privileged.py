"""Privileged sandbox tests: run **as root** (platform milestone M2).

The dedicated-user part of the sandbox (what keeps an extension away from the
Core's files, from other extensions' data and from signalling the Core) can only
be exercised when the Core can change the user of a child process, i.e. as root,
which is how the container runs. These tests are skipped otherwise.

Run them as root with the project environment, for example inside the container::

    docker compose exec synth /app/venv/bin/python -m pytest \\
        tests/test_extensions_privileged.py -q

or on a Linux host (the interpreter, the SDK and the repository must be readable
by other users, otherwise the test reports why it cannot run)::

    sudo -E env "PATH=$PATH" uv run pytest tests/test_extensions_privileged.py -q
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import textwrap
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio

from core.extensions.api import register_core_api
from core.extensions.gateway import Gateway
from core.extensions.grants import MemoryGrantStore
from core.extensions.launcher import Launcher
from core.extensions.memory_ports import memory_ports
from core.extensions.sandbox import (
    SDK_DIR,
    RuntimeLayout,
    SandboxPolicy,
    sandbox_problems,
)
from core.extensions.supervisor import RestartPolicy, State, Supervisor

from tests.test_extensions_runtime import ALL_CAPS, Harness

pytestmark = pytest.mark.skipif(
    not (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs root: the dedicated-user sandbox cannot be tested as an ordinary user",
)

PROBE = """
    import os, stat, resource, signal
    def attempt(fn):
        try:
            return ["ok", fn()]
        except OSError as exc:
            return ["denied", type(exc).__name__]
    async def main(ctx):
        out = {"uid": os.getuid(), "gid": os.getgid(), "groups": os.getgroups()}
        out["read_core_secret"] = attempt(lambda: open(SECRET, "rb").read().decode())
        out["write_install_dir"] = attempt(lambda: open(os.path.join(INSTALL, "evil.py"), "w").write("x"))
        out["write_own_data"] = attempt(lambda: open(os.path.join(ctx.data_dir, "mine"), "w").write("x"))
        out["read_other_data"] = attempt(lambda: os.listdir(OTHER_DATA))
        out["signal_core"] = attempt(lambda: os.kill(os.getppid(), 0))
        out["read_core_environ"] = attempt(lambda: open(f"/proc/{os.getppid()}/environ", "rb").read())
        out["nproc"] = resource.getrlimit(resource.RLIMIT_NPROC)[0]
        out["data_dir_owner"] = os.stat(ctx.data_dir).st_uid
        await ctx.storage.set("probe", out)
        await ctx.stopping.wait()
"""


@pytest.fixture
def open_root() -> Iterator[Path]:
    """A root directory every user can traverse (pytest's tmp_path is private)."""
    root = Path(tempfile.mkdtemp(dir="/tmp"))
    root.chmod(0o755)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest_asyncio.fixture
async def h(open_root: Path) -> AsyncIterator[Harness]:
    ports = memory_ports()
    grants = MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    layout = RuntimeLayout(open_root / "extensions")
    layout.root.mkdir(parents=True)
    layout.root.chmod(0o755)
    policy = SandboxPolicy(drop_privileges=True, uid_base=201_000, uid_count=100)
    launcher = Launcher(gateway, layout, policy)
    supervisor = Supervisor(
        launcher,
        RestartPolicy(
            initial_delay=0.05,
            max_failures=2,
            hello_timeout=8.0,
            stop_grace=1.0,
            term_wait=0.5,
        ),
    )
    harness = Harness(layout, gateway, grants, ports, launcher, supervisor)
    try:
        yield harness
    finally:
        await supervisor.stop_all()
        await gateway.close_all()


def _skip_if_unrunnable(h: Harness, install: Path) -> None:
    problems = sandbox_problems(
        h.launcher.policy,
        interpreter=Path(sys.executable),
        sdk_dir=SDK_DIR,
        install_dir=install,
    )
    if problems:
        pytest.skip(
            "cannot run an extension as another user here: " + "; ".join(problems)
        )


class TestDedicatedUser:
    @pytest.mark.asyncio
    async def test_extension_is_confined_by_its_own_user(
        self, h: Harness, open_root: Path
    ) -> None:
        secret = open_root / "core.env"
        secret.write_text("DB_PASSWORD=hunter2\n")
        secret.chmod(0o600)  # root-only, like the Core's .env and tokens
        other = h.layout.data_dir("dev/neighbour")
        other.mkdir(parents=True)
        other.chmod(0o700)
        (other / "private").write_text("not yours")

        code = (
            f"SECRET = {str(secret)!r}\n"
            f"INSTALL = {str(h.layout.install_dir('dev/probe'))!r}\n"
            f"OTHER_DATA = {str(other)!r}\n" + textwrap.dedent(PROBE)
        )
        manifest = h.install("probe", code, caps=ALL_CAPS)
        h.layout.install_dir("dev/probe").chmod(0o755)
        (h.layout.install_dir("dev/probe") / "ext_main.py").chmod(0o644)
        _skip_if_unrunnable(h, h.layout.install_dir("dev/probe"))

        await h.supervisor.start("dev/probe")
        await h.wait_for(lambda: h.stored("dev/probe", "probe") is not None, timeout=15)
        probe = h.stored("dev/probe", "probe")

        uid = h.launcher._uids.allocate("dev/probe")  # noqa: SLF001
        assert probe["uid"] == uid and probe["gid"] == uid
        assert probe["uid"] != 0 and probe["groups"] == []
        assert probe["read_core_secret"][0] == "denied"
        assert probe["write_install_dir"][0] == "denied"
        assert probe["write_own_data"][0] == "ok"
        assert probe["read_other_data"][0] == "denied"
        assert probe["signal_core"][0] == "denied"  # cannot even signal the Core
        assert probe["read_core_environ"][0] == "denied"
        assert probe["nproc"] == 128
        assert probe["data_dir_owner"] == uid
        assert stat.S_IMODE(h.layout.data_dir(manifest.id).stat().st_mode) == 0o700

    @pytest.mark.asyncio
    async def test_each_extension_gets_its_own_user(self, h: Harness) -> None:
        for name in ("one", "two"):
            h.install(
                name,
                "import os\nasync def main(ctx):\n"
                "    await ctx.storage.set('uid', os.getuid())\n    await ctx.stopping.wait()\n",
            )
            h.layout.install_dir(f"dev/{name}").chmod(0o755)
            (h.layout.install_dir(f"dev/{name}") / "ext_main.py").chmod(0o644)
        _skip_if_unrunnable(h, h.layout.install_dir("dev/one"))
        await h.supervisor.start("dev/one")
        await h.supervisor.start("dev/two")
        await h.wait_for(lambda: h.stored("dev/one", "uid") is not None)
        await h.wait_for(lambda: h.stored("dev/two", "uid") is not None)
        assert h.stored("dev/one", "uid") != h.stored("dev/two", "uid")
        assert 0 not in (h.stored("dev/one", "uid"), h.stored("dev/two", "uid"))

    @pytest.mark.asyncio
    async def test_a_dedicated_user_means_no_same_user_warning(
        self, h: Harness
    ) -> None:
        h.install("quiet", "async def main(ctx):\n    await ctx.stopping.wait()\n")
        h.layout.install_dir("dev/quiet").chmod(0o755)
        (h.layout.install_dir("dev/quiet") / "ext_main.py").chmod(0o644)
        _skip_if_unrunnable(h, h.layout.install_dir("dev/quiet"))
        assert await h.supervisor.start("dev/quiet") == State.RUNNING
        assert h.warnings == []
        await asyncio.sleep(0)


class TestAttachmentsAsAnotherUser:
    """File ownership checks that only mean something with a dedicated user."""

    @pytest.mark.asyncio
    async def test_files_cross_the_boundary_as_the_extensions_user(
        self, h: Harness, open_root: Path
    ) -> None:
        from core.extensions.media import MediaBridge

        sandbox = open_root / "core_sandbox"
        sandbox.mkdir()
        sandbox.chmod(0o755)
        (sandbox / "chart.png").write_bytes(b"CHART")

        def resolve(raw: str) -> tuple[Path | None, str | None]:
            path = Path(raw).resolve()
            ok = sandbox in path.parents and path.is_file()
            return (path, None) if ok else (None, "refused")

        bridge = MediaBridge(
            data_dir=h.layout.data_dir,
            uid=h.launcher.uid_for,
            inbound_root=open_root / "core_media",
            resolve_outbound=resolve,
        )
        h.install(
            "filer",
            """
            import os
            async def main(ctx):
                mine = ctx.inbox_path("mine.bin")
                mine.write_bytes(b"from the extension")
                await ctx.storage.set("inbox", {"path": str(mine), "uid": os.getuid()})
                await ctx.stopping.wait()
            """,
        )
        h.layout.install_dir("dev/filer").chmod(0o755)
        (h.layout.install_dir("dev/filer") / "ext_main.py").chmod(0o644)
        _skip_if_unrunnable(h, h.layout.install_dir("dev/filer"))
        await h.supervisor.start("dev/filer")
        await h.wait_for(lambda: h.stored("dev/filer", "inbox") is not None, timeout=15)
        info = h.stored("dev/filer", "inbox")
        uid = h.launcher.uid_for("dev/filer")
        assert info["uid"] == uid

        # Inbound: the file is owned by the extension's user, so the Core accepts it.
        [copy] = await bridge.import_inbound("dev/filer", [{"path": info["path"]}])
        assert Path(copy["path"]).read_bytes() == b"from the extension"

        # A file the Core (root) puts in that directory is NOT the extension's: refused.
        planted = h.layout.data_dir("dev/filer") / "planted.bin"
        planted.write_bytes(b"root made this")
        with pytest.raises(Exception, match="not owned"):
            await bridge.import_inbound("dev/filer", [{"path": str(planted)}])

        # Outbound: the staged copy is owned by, and readable by, the extension's user.
        staged = await bridge.stage_outbound("dev/filer", [str(sandbox / "chart.png")])
        try:
            [entry] = staged.items
            assert os.stat(entry["path"]).st_uid == uid
        finally:
            staged.cleanup()
