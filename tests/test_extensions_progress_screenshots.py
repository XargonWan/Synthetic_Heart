"""The progress bar of an install, and the screenshots a plugin comes with."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from core import webui_auth
from core.extensions.repo import progress
from core.extensions.repo.index import parse_index
from core.extensions.repo.installer import Installer
from core.extensions.webui_store import build_router
from tests.test_extensions_repo import Repo, add_repo, installer

# Two tiny but real-looking pictures, deliberately added out of order.
PNG_A = b"\x89PNG\r\n\x1a\n" + b"A" * 40
PNG_B = b"\x89PNG\r\n\x1a\n" + b"B" * 60
SHOTS = {"screenshots/02-second.png": PNG_B, "screenshots/01-first.png": PNG_A}


class TestPercent:
    def test_downloads_are_most_of_it_and_placing_the_rest(self) -> None:
        op = progress.Operation("dev/x", total_bytes=1000, done_bytes=500)
        assert op.percent == 45
        op.done_bytes = 1000
        assert op.percent == 90
        op.steps_total, op.steps_done = 2, 1
        assert op.percent == 95
        op.steps_done = 2
        assert op.percent == 100

    def test_nothing_known_is_zero_not_a_crash(self) -> None:
        assert progress.Operation("dev/x").percent == 0

    def test_an_operation_exists_only_while_it_runs(self) -> None:
        with progress.track("dev/x"):
            progress.expect(100, "Downloading X")
            progress.advance(40)
            assert progress.snapshot() == {
                "dev/x": {"percent": 36, "phase": "Downloading X"}
            }
        assert progress.snapshot() == {}

    def test_reporting_with_no_operation_is_harmless(self) -> None:
        progress.advance(10)
        progress.expect(10, "x")
        progress.placed()


class TestInstallReportsProgress:
    def test_the_bytes_and_the_placing_are_seen_while_installing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = Repo(tmp_path / "repo", tmp_path)
        repo.add("dev/one")
        repo.publish()
        inst = installer(tmp_path)
        add_repo(inst, repo)
        seen: list[dict[str, Any]] = []
        real_place = inst._place  # noqa: SLF001

        def watch(*args: Any, **kwargs: Any) -> Any:
            seen.append(progress.snapshot()["dev/one"])
            return real_place(*args, **kwargs)

        monkeypatch.setattr(inst, "_place", watch)
        inst.install("dev/one")
        assert seen == [{"percent": 90, "phase": "Installing"}]  # all downloaded
        assert progress.snapshot() == {}

    def test_a_package_already_in_the_cache_counts_as_downloaded(
        self, tmp_path: Path
    ) -> None:
        repo = Repo(tmp_path / "repo", tmp_path)
        repo.add("dev/one")
        repo.publish()
        inst = installer(tmp_path)
        add_repo(inst, repo)
        [candidate] = inst.available()[0]
        inst.download(candidate)  # now it is in the cache
        with progress.track("dev/one") as op:
            inst.download(candidate)
            assert op.done_bytes == op.total_bytes == candidate.entry.size > 0


@pytest.fixture
def web_for(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        webui_auth, "request_authorized", lambda r: r.headers.get("x-ok") == "1"
    )

    def make(inst: Installer) -> httpx.AsyncClient:
        app = FastAPI()
        app.include_router(build_router(inst, lambda: None))
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        )

    return make


class TestProgressRoute:
    @pytest.mark.asyncio
    async def test_it_needs_the_token_and_lists_what_runs(
        self, tmp_path: Path, web_for: Any
    ) -> None:
        async with web_for(installer(tmp_path)) as web:
            assert (await web.get("/api/extensions/store/progress")).status_code == 401
            with progress.track("dev/one"):
                progress.expect(200, "Downloading One")
                progress.advance(100)
                reply = await web.get(
                    "/api/extensions/store/progress", headers={"x-ok": "1"}
                )
            assert reply.json() == {
                "operations": {"dev/one": {"percent": 45, "phase": "Downloading One"}}
            }


def repo_with_shots(tmp_path: Path) -> Repo:
    repo = Repo(tmp_path / "repo", tmp_path)
    repo.add("dev/shiny", extra_files=dict(SHOTS))
    repo.add("dev/plain")
    repo.publish()
    return repo


class TestScreenshotsInTheIndex:
    def test_they_are_published_next_to_the_package_in_file_name_order(
        self, tmp_path: Path
    ) -> None:
        repo = repo_with_shots(tmp_path)
        index = parse_index((repo.dir / "synth-repo.json").read_bytes())
        shiny = next(p for p in index.packages if p.id == "dev/shiny")
        assert [s.file.rsplit("--", 1)[1] for s in shiny.screenshots] == [
            "01-first.png",
            "02-second.png",
        ]
        for shot in shiny.screenshots:
            assert (repo.dir / shot.file).is_file() and shot.size > 0
        assert next(p for p in index.packages if p.id == "dev/plain").screenshots == ()

    def test_a_picture_that_is_not_one_or_is_too_big_is_left_out(
        self, tmp_path: Path
    ) -> None:
        repo = Repo(tmp_path / "repo", tmp_path)
        repo.add(
            "dev/odd",
            extra_files={
                "screenshots/notes.txt": b"hi",
                "screenshots/huge.png": b"x" * (2 * 1024 * 1024 + 1),
                "screenshots/nested/deep.png": PNG_A,
                "screenshots/ok.png": PNG_A,
            },
        )
        repo.publish()
        raw = json.loads((repo.dir / "synth-repo.json").read_text())
        [entry] = raw["packages"]
        assert [s["file"].rsplit("--", 1)[1] for s in entry["screenshots"]] == [
            "ok.png"
        ]

    def test_an_index_naming_a_picture_outside_the_repository_is_refused(
        self, tmp_path: Path
    ) -> None:
        repo = repo_with_shots(tmp_path)
        raw = json.loads((repo.dir / "synth-repo.json").read_text())
        raw["packages"][0]["screenshots"] = [
            {"file": "../../etc/passwd.png", "sha256": "0" * 64, "size": 10}
        ]
        from core.extensions.repo.index import IndexError_

        with pytest.raises(IndexError_):
            parse_index(json.dumps(raw).encode())


class TestScreenshotsServed:
    def test_a_not_installed_plugin_shows_its_repositorys_pictures_in_order(
        self, tmp_path: Path
    ) -> None:
        repo = repo_with_shots(tmp_path)
        inst = installer(tmp_path)
        add_repo(inst, repo)
        assert inst.screenshot("dev/shiny", 0) == (PNG_A, "image/png")
        assert inst.screenshot("dev/shiny", 1) == (PNG_B, "image/png")
        assert inst.screenshot("dev/shiny", 2) is None
        assert inst.screenshot("dev/plain", 0) is None
        assert inst.screenshot("dev/ghost", 0) is None
        row = next(r for r in inst.rows()[0] if r.id == "dev/shiny")
        assert row.screenshots == 2

    def test_a_picture_that_does_not_match_the_signed_index_is_not_shown(
        self, tmp_path: Path
    ) -> None:
        repo = repo_with_shots(tmp_path)
        inst = installer(tmp_path)
        add_repo(inst, repo)
        shot = next(repo.dir.rglob("*--01-first.png"))
        shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"EVIL" * 10)  # swapped after signing
        assert inst.screenshot("dev/shiny", 0) is None

    def test_an_installed_plugin_shows_the_pictures_of_its_own_package(
        self, tmp_path: Path
    ) -> None:
        repo = repo_with_shots(tmp_path)
        inst = installer(tmp_path)
        add_repo(inst, repo)
        inst.install("dev/shiny")
        for path in repo.dir.rglob("*--*.png"):  # the repository no longer matters
            path.unlink()
        assert inst.screenshot("dev/shiny", 1) == (PNG_B, "image/png")

    @pytest.mark.asyncio
    async def test_the_route_serves_the_picture_or_a_404(
        self, tmp_path: Path, web_for: Any
    ) -> None:
        repo = repo_with_shots(tmp_path)
        inst = installer(tmp_path)
        add_repo(inst, repo)
        async with web_for(inst) as web:
            ok = await web.get("/api/extensions/store/screenshot/dev/shiny/0")
            assert ok.status_code == 200 and ok.content == PNG_A
            assert ok.headers["content-type"] == "image/png"
            missing = await web.get("/api/extensions/store/screenshot/dev/shiny/9")
            assert missing.status_code == 404
            assert (
                await web.get("/api/extensions/store/screenshot/dev/none/0")
            ).status_code == 404

    def test_the_catalog_script_shows_the_strip_and_the_progress_bar(self) -> None:
        script = (
            Path(__file__).resolve().parent.parent
            / "res/synth_webui/js/extension_store.js"
        ).read_text(encoding="utf-8")
        assert "/screenshot/" in script and "x.screenshots" in script
        assert "withProgress" in script and "'/progress'" in script
        assert "var(--accent)" in script  # the highlight colour


class TestReleaseLayout:
    def test_a_release_carries_each_picture_as_an_asset_of_its_own(
        self, tmp_path: Path
    ) -> None:
        from core.extensions.repo.pack import build_release
        from tests.test_extensions_repo import make_source

        source = make_source(tmp_path, "dev/shiny", extra_files=dict(SHOTS))
        out = tmp_path / "dist"
        assets = build_release([source], out, name="t", bundle_libs=False)
        assert sorted(a.name for a in assets) == [
            "dev-shiny--01-first.png",
            "dev-shiny--02-second.png",
            "dev-shiny.zip",
            "synth-repo.json",
        ]
        index = parse_index((out / "synth-repo.json").read_bytes())
        [package] = index.packages
        assert [s.file for s in package.screenshots] == [
            "dev-shiny--01-first.png",
            "dev-shiny--02-second.png",
        ]


class TestUpdatesLookWhereUpdateInstalls:
    """The marker and the button must agree: an update comes from the repository the
    extension was installed from, so a newer version elsewhere must not keep it on."""

    def _two(self, tmp_path: Path) -> tuple[Installer, Repo, Repo]:
        a = Repo(tmp_path / "a", tmp_path)
        a.add("dev/one", "1.0.0")
        a.publish(name="a")
        b = Repo(tmp_path / "b", tmp_path)
        b.add("dev/one", "1.5.0")
        b.publish(name="b")
        inst = installer(tmp_path)
        add_repo(inst, a, name="a")
        inst.install("dev/one", repo="a")
        add_repo(inst, b, name="b")
        return inst, a, b

    def test_a_newer_version_in_another_repository_is_not_an_update(
        self, tmp_path: Path
    ) -> None:
        inst, _, _ = self._two(tmp_path)
        assert inst.updates() == []
        row = next(r for r in inst.rows()[0] if r.id == "dev/one")
        assert (row.update, row.available, row.repo) == (False, "1.0.0", "a")

    def test_an_update_from_the_same_repository_is_offered_applied_and_gone(
        self, tmp_path: Path
    ) -> None:
        inst, a, _ = self._two(tmp_path)
        a.add("dev/one", "1.0.1")
        (a.dir / "packages/dev/one-1.0.0.synthx").unlink()
        a.publish(name="a")
        assert inst.updates() == [("dev/one", "1.0.0", "1.0.1")]
        inst.update("dev/one")
        assert inst.state.load().extensions["dev/one"].version == "1.0.1"
        assert inst.updates() == []  # not offered again
        row = next(r for r in inst.rows()[0] if r.id == "dev/one")
        assert row.update is False

    def test_when_its_repository_is_gone_any_repository_may_update_it(
        self, tmp_path: Path
    ) -> None:
        inst, _, _ = self._two(tmp_path)
        inst.repos.remove("a")
        assert inst.updates() == [("dev/one", "1.0.0", "1.5.0")]
        inst.update("dev/one")
        assert inst.state.load().extensions["dev/one"].version == "1.5.0"

    def test_the_catalog_lists_what_can_be_installed_before_what_is_installed(
        self,
    ) -> None:
        script = (
            Path(__file__).resolve().parent.parent
            / "res/synth_webui/js/extension_store.js"
        ).read_text(encoding="utf-8")
        assert script.index("['Available', shown") < script.index("['Installed', shown")


class TestCatalogButtons:
    def test_roll_back_is_offered_only_after_an_update_and_names_the_version(
        self, tmp_path: Path
    ) -> None:
        repo = Repo(tmp_path / "r", tmp_path)
        repo.add("dev/one", "1.0.0")
        repo.publish()
        inst = installer(tmp_path)
        add_repo(inst, repo)
        inst.install("dev/one")
        assert next(r for r in inst.rows()[0] if r.id == "dev/one").previous == ""
        repo.add("dev/one", "1.1.0")
        (repo.dir / "packages/dev/one-1.0.0.synthx").unlink()
        repo.publish()
        inst.update("dev/one")
        assert next(r for r in inst.rows()[0] if r.id == "dev/one").previous == "1.0.0"
        inst.rollback("dev/one")
        row = next(r for r in inst.rows()[0] if r.id == "dev/one")
        assert row.installed == "1.0.0"

    def test_the_catalog_has_a_red_uninstall_and_no_turn_off_button(self) -> None:
        script = (
            Path(__file__).resolve().parent.parent
            / "res/synth_webui/js/extension_store.js"
        ).read_text(encoding="utf-8")
        assert "'Uninstall', '/remove'" in script and "'pill btn-danger'" in script
        assert "Turn off" not in script and "Turn on" not in script
        assert "'Roll back to ' + x.previous" in script
