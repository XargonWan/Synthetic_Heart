"""Tests for ``core.extensions.sandbox``: policy and plumbing, no process needed."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.extensions import sandbox
from core.extensions.gateway import Gateway
from core.extensions.grants import MemoryGrantStore
from core.extensions.launcher import LaunchError, Launcher
from core.extensions.manifest import Entry, Manifest
from core.extensions.sandbox import (
    ResourceLimits,
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


class TestUidAllocator:
    def test_allocates_sequentially_and_is_stable(self, tmp_path: Path) -> None:
        allocator = UidAllocator(tmp_path / "uids.json", base=1000, count=10)
        assert allocator.allocate("a/one") == 1000
        assert allocator.allocate("b/two") == 1001
        assert allocator.allocate("a/one") == 1000

    def test_survives_a_restart(self, tmp_path: Path) -> None:
        path = tmp_path / "uids.json"
        UidAllocator(path, 1000, 10).allocate("a/one")
        UidAllocator(path, 1000, 10).allocate("b/two")
        again = UidAllocator(path, 1000, 10)
        assert again.allocate("b/two") == 1001
        assert again.allocate("c/three") == 1002

    def test_two_extensions_never_share_a_user(self, tmp_path: Path) -> None:
        allocator = UidAllocator(tmp_path / "uids.json", 5000, 50)
        uids = {allocator.allocate(f"p/e{i}") for i in range(50)}
        assert len(uids) == 50

    def test_exhaustion_is_an_error_not_a_reuse(self, tmp_path: Path) -> None:
        allocator = UidAllocator(tmp_path / "uids.json", 1000, 2)
        allocator.allocate("a/one")
        allocator.allocate("b/two")
        with pytest.raises(SandboxError, match="no free user ids"):
            allocator.allocate("c/three")

    def test_the_table_file_is_private(self, tmp_path: Path) -> None:
        path = tmp_path / "uids.json"
        UidAllocator(path, 1000, 10).allocate("a/one")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert json.loads(path.read_text()) == {"a/one": 1000}

    def test_a_corrupt_table_starts_over_instead_of_crashing(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "uids.json"
        path.write_text("{not json")
        assert UidAllocator(path, 1000, 10).allocate("a/one") == 1000
        path.write_text("[1, 2]")
        assert UidAllocator(path, 1000, 10).allocate("a/one") == 1000


class TestEnvironment:
    def test_nothing_is_inherited(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key in (
            "DB_PASSWORD",
            "AWS_SECRET_ACCESS_KEY",
            "SSH_AUTH_SOCK",
            "PYTHONPATH",
        ):
            monkeypatch.setenv(key, "secret")
        env = build_environment(
            "dev/x",
            tmp_path / "data",
            "m:main",
            effective_limits(SandboxPolicy(), dedicated_user=True),
        )
        assert set(env) == {
            "PATH", "LANG", "LC_ALL", "HOME", "TMPDIR", "PYTHONDONTWRITEBYTECODE",
            "PYTHONUNBUFFERED", "SYNTH_EXTENSION_ID", "SYNTH_EXTENSION_DATA",
            "SYNTH_EXTENSION_ENTRY", "SYNTH_EXT_LIMITS",
        }  # fmt: skip
        assert "secret" not in json.dumps(env)
        assert env["HOME"] == str(tmp_path / "data")
        assert env["TMPDIR"] == str(tmp_path / "data" / "tmp")
        assert env["SYNTH_EXTENSION_ENTRY"] == "m:main"

    def test_limits_are_serialised_without_unset_values(self) -> None:
        limits = ResourceLimits(nofile=10, nproc=None, fsize=None, core=0)
        assert json.loads(limits.to_json()) == {"nofile": 10, "core": 0}

    def test_nproc_is_only_applied_with_a_dedicated_user(self) -> None:
        policy = SandboxPolicy()
        assert effective_limits(policy, dedicated_user=True).nproc == 128
        # nproc counts every process of the user: sharing the Core's user it would
        # break forking for the extension (and the Core).
        assert effective_limits(policy, dedicated_user=False).nproc is None


class TestPrivilegeKwargs:
    def test_no_user_change_without_a_uid(self) -> None:
        assert popen_privilege_kwargs(None) == {}

    def test_dedicated_user_group_and_no_supplementary_groups(self) -> None:
        assert popen_privilege_kwargs(200123) == {
            "user": 200123,
            "group": 200123,
            "extra_groups": [],
        }

    def test_policy_auto_follows_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sandbox, "is_root", lambda: True)
        assert SandboxPolicy().will_drop_privileges() is True
        monkeypatch.setattr(sandbox, "is_root", lambda: False)
        assert SandboxPolicy().will_drop_privileges() is False
        assert SandboxPolicy(drop_privileges=True).will_drop_privileges() is True
        assert SandboxPolicy(drop_privileges=False).will_drop_privileges() is False


class TestDirectories:
    def test_layout(self, tmp_path: Path) -> None:
        layout = RuntimeLayout(tmp_path)
        assert layout.install_dir("pub/name") == tmp_path / "installed" / "pub" / "name"
        assert layout.data_dir("pub/name") == tmp_path / "data" / "pub" / "name"
        assert layout.uid_file == tmp_path / "uids.json"

    def test_data_directories_are_private(self, tmp_path: Path) -> None:
        data = tmp_path / "data" / "pub" / "name"
        prepare_data_dir(data, None)
        for path in (data, data / "tmp"):
            assert path.is_dir()
            assert stat.S_IMODE(path.stat().st_mode) == 0o700


@pytest.fixture
def open_tree() -> Iterator[Path]:
    """A directory tree under /tmp whose every level other users may traverse."""
    root = Path(tempfile.mkdtemp(dir="/tmp"))
    root.chmod(0o755)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def only_this_tree(open_tree: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Judge only ``open_tree``; the real ``/tmp`` mode depends on the host."""
    real = sandbox._readable_by_others  # noqa: SLF001

    def scoped(path: Path, *, directory: bool) -> bool:
        if path == open_tree or open_tree in path.parents:
            return real(path, directory=directory)
        return True

    monkeypatch.setattr(sandbox, "_readable_by_others", scoped)
    return open_tree


class TestSandboxProblems:
    def _tree(self, root: Path) -> tuple[Path, Path, Path]:
        interpreter = root / "bin" / "python"
        interpreter.parent.mkdir()
        interpreter.write_text("#!/bin/sh\n")
        sdk = root / "sdk"
        sdk.mkdir()
        (sdk / "__init__.py").write_text("")
        install = root / "install"
        install.mkdir()
        for path in (interpreter.parent, sdk, install):
            path.chmod(0o755)
        interpreter.chmod(0o755)
        return interpreter, sdk, install

    def test_nothing_to_check_when_not_changing_user(self, tmp_path: Path) -> None:
        problems = sandbox_problems(
            SandboxPolicy(drop_privileges=False),
            interpreter=Path("/nonexistent"),
            sdk_dir=tmp_path,
            install_dir=tmp_path,
        )
        assert problems == []

    def test_requiring_a_dedicated_user_without_root_is_a_clear_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sandbox, "is_root", lambda: False)
        problems = sandbox_problems(
            SandboxPolicy(drop_privileges=True),
            interpreter=Path("/usr/bin/python3"),
            sdk_dir=tmp_path,
            install_dir=tmp_path,
        )
        assert len(problems) == 1 and "not root" in problems[0]

    def test_readable_tree_has_no_problems(
        self, only_this_tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sandbox, "is_root", lambda: True)
        interpreter, sdk, install = self._tree(only_this_tree)
        assert (
            sandbox_problems(
                SandboxPolicy(),
                interpreter=interpreter,
                sdk_dir=sdk,
                install_dir=install,
            )
            == []
        )

    def test_unreadable_code_directory_is_reported(
        self, only_this_tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sandbox, "is_root", lambda: True)
        interpreter, sdk, install = self._tree(only_this_tree)
        install.chmod(0o700)
        problems = sandbox_problems(
            SandboxPolicy(), interpreter=interpreter, sdk_dir=sdk, install_dir=install
        )
        assert any("extension code" in p for p in problems)

    def test_interpreter_under_a_private_directory_is_reported(
        self, only_this_tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sandbox, "is_root", lambda: True)
        interpreter, sdk, install = self._tree(only_this_tree)
        interpreter.parent.chmod(0o700)
        problems = sandbox_problems(
            SandboxPolicy(), interpreter=interpreter, sdk_dir=sdk, install_dir=install
        )
        assert any("interpreter" in p and "not accessible" in p for p in problems)


class TestLauncherRefusals:
    @pytest.mark.asyncio
    async def test_requiring_a_dedicated_user_without_root_refuses_to_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sandbox, "is_root", lambda: False)
        layout = RuntimeLayout(tmp_path)
        manifest = Manifest(
            id="dev/x",
            name="x",
            version="1.0.0",
            core_api=">=0.1",
            entry=Entry(module="m"),
        )
        layout.install_dir("dev/x").mkdir(parents=True)
        launcher = Launcher(
            Gateway(grants=MemoryGrantStore()),
            layout,
            SandboxPolicy(drop_privileges=True),
        )
        with pytest.raises(SandboxError, match="not root"):
            await launcher.launch(manifest)
        assert not layout.data_dir("dev/x").exists()  # nothing was created

    @pytest.mark.asyncio
    async def test_a_manifest_without_entry_cannot_be_launched(
        self, tmp_path: Path
    ) -> None:
        layout = RuntimeLayout(tmp_path)
        manifest = Manifest(id="dev/x", name="x", version="1.0.0", core_api=">=0.1")
        launcher = Launcher(
            Gateway(grants=MemoryGrantStore()),
            layout,
            SandboxPolicy(drop_privileges=False),
        )
        with pytest.raises(LaunchError, match="no \\[entry\\]"):
            await launcher.launch(manifest)

    def test_the_sdk_directory_is_the_repositorys_sdk_and_not_the_root(self) -> None:
        assert sandbox.SDK_DIR.name == "synth_sdk"
        assert not (sandbox.SDK_DIR / "core").exists()
        assert (sandbox.SDK_DIR / "_launch.py").is_file()
        assert os.path.isabs(sandbox.SDK_DIR)
