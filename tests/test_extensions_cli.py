"""The ``synth-ext`` command line, end to end on a local repository."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.extensions import cli
from core.extensions.repo.config import Repository
from tests.test_extensions_repo import Repo, make_source


def run(tmp_path: Path, *argv: str) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = cli.run(["--root", str(tmp_path / "runtime"), *argv], lines.append)
    return code, lines


def text(lines: list[str]) -> str:
    return "\n".join(lines)


class TestBuildAndPublish:
    def test_keygen_pack_index_then_add_install_update_remove(
        self, tmp_path: Path
    ) -> None:
        key = tmp_path / "publisher.key"
        code, lines = run(tmp_path, "keygen", str(key))
        assert code == 0 and oct(key.stat().st_mode & 0o777) == "0o600"
        public = next(
            line.split()[-1] for line in lines if line.startswith("public key")
        )

        repo = tmp_path / "repo"
        source = make_source(tmp_path, "dev/one", "1.0.0")
        assert (
            run(tmp_path, "pack", str(source), "--out", str(repo), "--no-libs")[0] == 0
        )
        code, lines = run(
            tmp_path, "index", str(repo), "--sign", str(key), "--name", "mine"
        )
        assert code == 0 and "(signed)" in text(lines)

        code, lines = run(
            tmp_path, "repo", "add", "mine", str(repo), "--key", public, "--trusted"
        )
        assert (
            code == 0
            and "1 package(s), signed" in text(lines)
            and "pinned key" in text(lines)
        )
        assert "mine" in text(run(tmp_path, "repo", "list")[1]) and "trusted" in text(
            run(tmp_path, "repo", "list")[1]
        )
        assert run(tmp_path, "repo", "refresh")[0] == 0

        code, lines = run(tmp_path, "search", "one")
        assert (
            code == 0 and "dev/one" in text(lines) and "available 1.0.0" in text(lines)
        )

        code, lines = run(tmp_path, "install", "dev/one")
        assert code == 0 and "installed dev/one 1.0.0" in text(lines)
        assert "asks for: log, storage" in text(
            lines
        ) and "nothing is granted yet" in text(lines)
        assert run(tmp_path, "install", "dev/one")[1][0].endswith("already up to date")
        grants = (
            json.loads((tmp_path / "runtime" / "grants.json").read_text())
            if (tmp_path / "runtime" / "grants.json").exists()
            else {}
        )
        assert grants == {}
        assert "granted log, storage" in text(run(tmp_path, "grant", "dev/one")[1])

        newer = make_source(tmp_path, "dev/one", "1.1.0")
        run(tmp_path, "pack", str(newer), "--out", str(repo), "--no-libs")
        run(tmp_path, "index", str(repo), "--sign", str(key), "--name", "mine")
        code, lines = run(tmp_path, "update")
        assert code == 0 and "updated dev/one 1.0.0 -> 1.1.0" in text(lines)
        assert run(tmp_path, "update")[1] == ["everything is up to date"]
        assert "back at 1.0.0" in text(run(tmp_path, "rollback", "dev/one")[1])

        assert "disabled" in text(run(tmp_path, "disable", "dev/one")[1])
        assert "off" in text(run(tmp_path, "list")[1])
        assert "enabled" in text(run(tmp_path, "enable", "dev/one")[1])
        code, lines = run(tmp_path, "remove", "dev/one")
        assert code == 0 and "data is kept" in text(lines)
        assert run(tmp_path, "list")[1] == []


class TestSafety:
    def test_a_repository_that_cannot_be_read_is_not_added(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "empty").mkdir()
        code, lines = run(tmp_path, "repo", "add", "bad", str(tmp_path / "empty"))
        assert code == 1 and "not added" in text(lines)
        assert run(tmp_path, "repo", "list")[1] == []

    def test_a_wrong_key_means_the_repository_is_not_added(
        self, tmp_path: Path
    ) -> None:
        repo = Repo(tmp_path / "repo", tmp_path)
        repo.sign_with_new_key()
        repo.add("dev/one")
        repo.publish()
        other = cli.repo_index.generate_keypair()[1]
        code, lines = run(tmp_path, "repo", "add", "x", str(repo.dir), "--key", other)
        assert code == 1 and "does not match" in text(lines)
        assert run(tmp_path, "repo", "list")[1] == []

    def test_installing_from_an_untrusted_repository_needs_yes(
        self, tmp_path: Path
    ) -> None:
        repo = Repo(tmp_path / "repo", tmp_path)
        repo.add("dev/one")
        repo.publish()
        assert run(tmp_path, "repo", "add", "r", str(repo.dir))[0] == 0
        code, lines = run(tmp_path, "install", "dev/one")
        assert code == 1 and "--yes" in text(lines)
        assert run(tmp_path, "install", "dev/one", "--yes", "--grant")[0] == 0
        assert (
            tmp_path / "runtime" / "installed" / "dev" / "one" / "ext_main.py"
        ).exists()

    def test_errors_are_one_line_and_a_nonzero_exit(self, tmp_path: Path) -> None:
        for argv in (
            ("install", "dev/nothing"),
            ("remove", "dev/nothing"),
            ("rollback", "dev/nothing"),
            ("grant", "dev/nothing"),
            ("enable", "dev/nothing"),
        ):
            code, lines = run(tmp_path, *argv)
            assert code == 1 and len(lines) == 1 and lines[0].startswith("error:")

    def test_an_exact_version_can_be_asked_for(self, tmp_path: Path) -> None:
        repo = Repo(tmp_path / "repo", tmp_path)
        repo.add("dev/one", "1.0.0")
        repo.add("dev/one", "2.0.0")
        repo.publish()
        run(tmp_path, "repo", "add", "r", str(repo.dir), "--trusted")
        assert "installed dev/one 1.0.0" in text(
            run(tmp_path, "install", "dev/one==1.0.0")[1]
        )
        assert run(tmp_path, "install", "dev/one==9.9")[0] == 1

    def test_defaults_install_once_from_the_official_repository(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        official = Repo(tmp_path / "official", tmp_path)
        official.add("dev/core_ui")
        official.publish(name="official", defaults=("dev/core_ui",))
        monkeypatch.setattr(
            "core.extensions.repo.config.builtin_repositories",
            lambda: [
                Repository(
                    name="official",
                    location=str(official.dir),
                    kind="local",
                    require_signature=False,
                    trusted=True,
                    builtin=True,
                )
            ],
        )
        lines: list[str] = []
        code = cli.run(["--root", str(tmp_path / "runtime"), "defaults"], lines.append)
        assert code == 0 and lines == ["installed dev/core_ui"]
        lines.clear()
        cli.run(["--root", str(tmp_path / "runtime"), "defaults"], lines.append)
        assert lines == ["nothing to do"]


def test_the_default_root_follows_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SYNTH_EXT_ROOT", str(tmp_path / "elsewhere"))
    assert cli.default_root() == tmp_path / "elsewhere"
