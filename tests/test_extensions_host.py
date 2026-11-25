"""Tests for ``core.extensions.runtime``: the runtime facade and host helpers (M2)."""

from __future__ import annotations

import asyncio
import stat
import textwrap
from pathlib import Path
from typing import Any

import pytest

from core.extensions import runtime as runtime_module
from core.extensions.grants import FileGrantStore
from core.extensions.memory_ports import memory_ports
from core.extensions.runtime import (
    ExtensionRuntime,
    start_extension_runtime,
    stop_extension_runtime,
)
from core.extensions.sandbox import RuntimeLayout, SandboxPolicy, secure_runtime_tree
from core.extensions.supervisor import RestartPolicy, State

FAST = RestartPolicy(
    initial_delay=0.05, max_failures=2, hello_timeout=5.0, stop_grace=0.5, term_wait=0.3
)

MANIFEST = """
[extension]
id = "{id}"
name = "{id}"
version = "1.0.0"

[compat]
core_api = ">=0.1"

[entry]
module = "ext_main"

[permissions]
capabilities = ["storage", "log"]
{extra}
"""

# An extension that calls no API, so it needs no grant and never crashes.
ECHO = """
async def main(ctx):
    await ctx.stopping.wait()
"""


def install(root: Path, ext_id: str, code: str = ECHO, extra: str = "") -> Path:
    directory = root / "installed" / ext_id
    directory.mkdir(parents=True)
    (directory / "synth-extension.toml").write_text(
        MANIFEST.format(id=ext_id, extra=extra)
    )
    (directory / "ext_main.py").write_text(textwrap.dedent(code))
    return directory


def make_runtime(root: Path, **kwargs: Any) -> ExtensionRuntime:
    ports = memory_ports()
    runtime = ExtensionRuntime(
        root,
        ports,
        grants=kwargs.pop("grants", None),
        policy=SandboxPolicy(drop_privileges=False),
        restart=FAST,
        **kwargs,
    )
    return runtime


class TestRuntimeFlow:
    @pytest.mark.asyncio
    async def test_scan_resolve_start_stop(self, tmp_path: Path) -> None:
        root = tmp_path / "ext"
        install(root, "dev/echo")
        grants = FileGrantStore(root / "grants.json")
        grants.save({"dev/echo": ["storage", "log"]})
        runtime = make_runtime(root, grants=grants)
        try:
            report = await runtime.start()
            assert report.started == ["dev/echo"]
            assert (
                report.excluded == {}
                and report.failed == {}
                and report.load_problems == []
            )
            assert runtime.started is True
            assert "dev/echo" in runtime.gateway.sessions
        finally:
            await runtime.stop()
        assert runtime.started is False
        assert runtime.gateway.sessions == {}

    @pytest.mark.asyncio
    async def test_dependencies_start_first_and_unresolvable_ones_are_reported(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "ext"
        needs_lib = '[[requires.extensions]]\nid = "dev/lib"\n'
        needs_absent = '[[requires.extensions]]\nid = "dev/absent"\n'
        install(root, "dev/app", extra=needs_lib)
        install(root, "dev/lib")
        install(root, "dev/orphan", extra=needs_absent)
        runtime = make_runtime(root)
        started_order: list[str] = []
        runtime.supervisor._on_event = lambda e, k, d: (  # noqa: SLF001
            started_order.append(e) if k == "running" else None
        )
        try:
            report = await runtime.start()
            assert started_order == ["dev/lib", "dev/app"]
            assert report.started == ["dev/lib", "dev/app"]
            assert list(report.excluded) == ["dev/orphan"]
            assert "missing_dependency" in report.excluded["dev/orphan"][0]
        finally:
            await runtime.stop()

    @pytest.mark.asyncio
    async def test_a_broken_extension_does_not_stop_the_others(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "ext"
        install(root, "dev/good")
        bad = root / "installed" / "dev" / "bad"
        bad.mkdir(parents=True)
        (bad / "synth-extension.toml").write_text("[extension]\n")
        install(
            root, "dev/crash", "async def main(ctx):\n    raise RuntimeError('x')\n"
        )
        runtime = make_runtime(root)
        try:
            report = await runtime.start()
            assert "dev/good" in report.started
            assert any("extension.id" in p for p in report.load_problems)
            # The crash is contained and ends in a final state, never raised.
            for _ in range(200):
                if runtime.supervisor.state("dev/crash") in (
                    State.FAILED,
                    State.FINISHED,
                ):
                    break
                await asyncio.sleep(0.05)
            assert runtime.supervisor.state("dev/crash") == State.FAILED
            assert runtime.supervisor.state("dev/good") == State.RUNNING
        finally:
            await runtime.stop()

    @pytest.mark.asyncio
    async def test_no_grants_means_no_access_by_default(self, tmp_path: Path) -> None:
        root = tmp_path / "ext"
        install(root, "dev/echo")
        runtime = make_runtime(root)  # grants.json does not exist: nothing is granted
        ports_storage = runtime.gateway.methods["storage.set"]
        assert ports_storage.capability == "storage"
        try:
            await runtime.start()
            session = runtime.gateway.sessions["dev/echo"]
            assert session.effective_capabilities() == []
        finally:
            await runtime.stop()


class TestHostHelpers:
    @pytest.mark.asyncio
    async def test_disabled_by_default_does_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime_module, "extensions_enabled", lambda: False)
        assert await start_extension_runtime() is None
        assert runtime_module.get_extension_runtime() is None
        await stop_extension_runtime()  # no-op, no error

    @pytest.mark.asyncio
    async def test_enabled_starts_once_and_stops(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(runtime_module, "extensions_enabled", lambda: True)
        monkeypatch.setenv("SYNTH_DATA_ROOT", str(tmp_path))
        report = await start_extension_runtime()
        try:
            assert report is not None and report.started == []
            runtime = runtime_module.get_extension_runtime()
            assert runtime is not None and runtime.started
            assert await start_extension_runtime() is None  # idempotent
            assert runtime_module.get_extension_runtime() is runtime
        finally:
            await stop_extension_runtime()
        assert runtime_module.get_extension_runtime() is None

    @pytest.mark.asyncio
    async def test_a_failure_never_escapes_to_the_caller(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom() -> bool:
            raise RuntimeError("config store down")

        monkeypatch.setattr(runtime_module, "extensions_enabled", boom)
        assert await start_extension_runtime() is None


class TestSecureRuntimeTree:
    def test_loosened_permissions_are_restored(self, tmp_path: Path) -> None:
        """The container's ``chmod a+rwx -R /config`` must not leave the tree open."""
        layout = RuntimeLayout(tmp_path / "extensions")
        code = layout.install_dir("dev/x")
        code.mkdir(parents=True)
        (code / "ext_main.py").write_text("x")
        (layout.root / "data").mkdir()
        (layout.root / "grants.json").write_text("{}")
        (layout.root / "uids.json").write_text("{}")
        for path in [layout.root, *layout.root.rglob("*")]:
            path.chmod(0o777)

        changed = secure_runtime_tree(layout)

        assert changed
        assert stat.S_IMODE((layout.root / "grants.json").stat().st_mode) == 0o600
        assert stat.S_IMODE((layout.root / "uids.json").stat().st_mode) == 0o600
        assert stat.S_IMODE(layout.root.stat().st_mode) == 0o755
        assert stat.S_IMODE((code / "ext_main.py").stat().st_mode) & 0o022 == 0
        assert stat.S_IMODE(code.stat().st_mode) & 0o022 == 0
        assert stat.S_IMODE(code.stat().st_mode) & 0o500 == 0o500  # still readable

    def test_an_already_safe_tree_is_left_alone(self, tmp_path: Path) -> None:
        layout = RuntimeLayout(tmp_path / "extensions")
        secure_runtime_tree(layout)
        assert secure_runtime_tree(layout) == []

    def test_symlinks_are_not_followed(self, tmp_path: Path) -> None:
        layout = RuntimeLayout(tmp_path / "extensions")
        secure_runtime_tree(layout)
        outside = tmp_path / "outside.txt"
        outside.write_text("keep")
        outside.chmod(0o666)
        (layout.root / "installed" / "link").symlink_to(outside)
        secure_runtime_tree(layout)
        assert stat.S_IMODE(outside.stat().st_mode) == 0o666  # untouched
