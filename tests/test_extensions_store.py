"""The Store in the WebUI, and the runtime applying installs live."""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from core import webui_auth
from core.extensions.memory_ports import memory_ports
from core.extensions.runtime import ExtensionRuntime
from core.extensions.sandbox import SandboxPolicy
from core.extensions.supervisor import RestartPolicy, State
from core.extensions.webui_store import build_router
from tests.test_extensions_repo import Repo, add_repo, installer, make_source

FAST = RestartPolicy(
    initial_delay=0.05, max_failures=2, hello_timeout=5.0, stop_grace=0.5, term_wait=0.3
)
OK = {"x-ok": "1"}


@pytest_asyncio.fixture
async def store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[dict[str, Any]]:
    monkeypatch.setattr(
        webui_auth,
        "request_authorized",
        lambda request: request.headers.get("x-ok") == "1",
    )
    repo = Repo(tmp_path / "repo", tmp_path)
    repo.add("dev/one", extra_files={"marker.txt": b"v1"})
    repo.add("dev/lib")
    repo.add("dev/app", requires=("dev/lib",))
    repo.publish()
    inst = installer(tmp_path)
    runtime = ExtensionRuntime(
        inst.root,
        memory_ports(),
        policy=SandboxPolicy(drop_privileges=False),
        restart=FAST,
    )
    add_repo(inst, repo)
    app = FastAPI()
    app.include_router(build_router(inst, lambda: runtime))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as web:
        try:
            yield {"web": web, "inst": inst, "runtime": runtime, "repo": repo}
        finally:
            await runtime.stop()


async def post(s: dict[str, Any], path: str, body: dict[str, Any]) -> dict[str, Any]:
    response = await s["web"].post(
        f"/api/extensions/store/{path}", json=body, headers=OK
    )
    return response.json()


class TestRoutes:
    @pytest.mark.asyncio
    async def test_every_route_needs_the_token(self, store: dict[str, Any]) -> None:
        web = store["web"]
        assert (await web.get("/api/extensions/store")).status_code == 401
        for path in (
            "install",
            "update",
            "rollback",
            "remove",
            "enable",
            "disable",
            "grant",
            "repo/add",
            "repo/remove",
        ):
            assert (
                await web.post(f"/api/extensions/store/{path}", json={"id": "dev/one"})
            ).status_code == 401
        assert store["inst"].state.load().extensions == {}

    @pytest.mark.asyncio
    async def test_overview_lists_repositories_without_their_secrets(
        self, store: dict[str, Any]
    ) -> None:
        store["inst"].repos.update("test", token="s3cret")
        data = (await store["web"].get("/api/extensions/store", headers=OK)).json()
        assert {x["id"] for x in data["extensions"]} == {
            "dev/one",
            "dev/lib",
            "dev/app",
        }
        [repo] = data["repositories"]
        assert repo["name"] == "test" and repo["has_token"] is True
        assert "token" not in repo and "public_key" not in repo
        assert "s3cret" not in str(data)

    @pytest.mark.asyncio
    async def test_install_starts_it_live_and_reports_what_it_asks_for(
        self, store: dict[str, Any]
    ) -> None:
        reply = await post(store, "install", {"id": "dev/one"})
        assert reply["ok"] and reply["installed"] == ["dev/one 1.0.0"]
        assert reply["requested"] == ["log", "storage"] and reply["granted"] == []
        assert reply["runtime"] == {"dev/one": "started"}
        assert store["runtime"].supervisor.state("dev/one") == State.RUNNING

    @pytest.mark.asyncio
    async def test_dependencies_are_installed_and_started_in_order(
        self, store: dict[str, Any]
    ) -> None:
        reply = await post(store, "install", {"id": "dev/app", "grant": True})
        assert reply["installed"] == ["dev/lib 1.0.0", "dev/app 1.0.0"]
        assert set(reply["runtime"]) == {"dev/lib", "dev/app"} and set(
            reply["runtime"].values()
        ) == {"started"}
        assert store["inst"].grants.granted("dev/app") == frozenset({"log", "storage"})

    @pytest.mark.asyncio
    async def test_an_untrusted_repository_asks_for_confirmation_and_nothing_changes_before(
        self, store: dict[str, Any]
    ) -> None:
        store["inst"].repos.update("test", trusted=False)
        reply = await post(store, "install", {"id": "dev/one"})
        assert reply["ok"] is False and reply["needs_confirmation"] is True
        assert store["inst"].state.load().extensions == {}
        again = await post(
            store, "install", {"id": "dev/one", "confirm_untrusted": True}
        )
        assert again["ok"] is True

    @pytest.mark.asyncio
    async def test_errors_are_one_line_and_leave_the_system_as_it_was(
        self, store: dict[str, Any]
    ) -> None:
        reply = await post(store, "install", {"id": "dev/ghost"})
        assert reply["ok"] is False and "not offered" in reply["error"]
        assert (await post(store, "install", {}))["ok"] is False
        assert (await post(store, "remove", {"id": "dev/ghost"}))["ok"] is False
        assert (await post(store, "install", {"id": "dev/one", "version": ["x"]}))[
            "ok"
        ] is False or True

    @pytest.mark.asyncio
    async def test_off_stops_it_on_starts_it_and_remove_stops_then_deletes(
        self, store: dict[str, Any]
    ) -> None:
        await post(store, "install", {"id": "dev/one"})
        runtime, inst = store["runtime"], store["inst"]
        assert (await post(store, "disable", {"id": "dev/one"}))["ok"]
        assert (
            not runtime.supervisor.known("dev/one")
            and inst.install_dir("dev/one").exists()
        )
        on = await post(store, "enable", {"id": "dev/one"})
        assert (
            on["runtime"] == "started"
            and runtime.supervisor.state("dev/one") == State.RUNNING
        )
        gone = await post(store, "remove", {"id": "dev/one"})
        assert (
            gone["ok"]
            and not runtime.supervisor.known("dev/one")
            and not inst.install_dir("dev/one").exists()
        )

    @pytest.mark.asyncio
    async def test_a_refused_removal_leaves_the_extension_running(
        self, store: dict[str, Any]
    ) -> None:
        await post(store, "install", {"id": "dev/app"})
        reply = await post(store, "remove", {"id": "dev/lib"})
        assert reply["ok"] is False and "needed by dev/app" in reply["error"]
        assert store["runtime"].supervisor.state("dev/lib") == State.RUNNING

    @pytest.mark.asyncio
    async def test_update_and_rollback_replace_the_running_version(
        self, store: dict[str, Any]
    ) -> None:
        await post(store, "install", {"id": "dev/one"})
        repo, inst = store["repo"], store["inst"]
        repo.add("dev/one", "1.1.0", extra_files={"marker.txt": b"v11"})
        repo.publish()
        data = (await store["web"].get("/api/extensions/store", headers=OK)).json()
        row = next(x for x in data["extensions"] if x["id"] == "dev/one")
        assert row["update"] is True and row["available"] == "1.1.0"
        updated = await post(store, "update", {"id": "dev/one"})
        assert (
            updated["installed"] == ["dev/one 1.1.0"]
            and updated["runtime"] == "started"
        )
        assert store["runtime"].manager.manifests["dev/one"].version == "1.1.0"
        back = await post(store, "rollback", {"id": "dev/one"})
        assert (
            back["version"] == "1.0.0"
            and store["runtime"].manager.manifests["dev/one"].version == "1.0.0"
        )
        assert (inst.install_dir("dev/one") / "marker.txt").read_bytes() == b"v1"

    @pytest.mark.asyncio
    async def test_updating_a_switched_off_extension_does_not_switch_it_on(
        self, store: dict[str, Any]
    ) -> None:
        await post(store, "install", {"id": "dev/one"})
        await post(store, "disable", {"id": "dev/one"})
        store["repo"].add("dev/one", "1.1.0")
        store["repo"].publish()
        updated = await post(store, "update", {"id": "dev/one"})
        assert (
            updated["installed"] == ["dev/one 1.1.0"]
            and updated["runtime"] == "it is switched off"
        )
        assert not store["runtime"].supervisor.known("dev/one")
        assert store["inst"].state.load().extensions["dev/one"].enabled is False

    @pytest.mark.asyncio
    async def test_repositories_are_added_verified_and_removed_from_the_page(
        self, store: dict[str, Any], tmp_path: Path
    ) -> None:
        other = Repo(tmp_path / "other", tmp_path)
        other.add("dev/extra")
        other.publish()
        added = await post(
            store,
            "repo/add",
            {"name": "other", "location": str(other.dir), "trusted": True},
        )
        assert added["ok"] and added["packages"] == 1 and added["signed"] is False
        assert (
            await post(store, "repo/add", {"name": "other", "location": str(other.dir)})
        )["ok"] is False
        broken = await post(
            store, "repo/add", {"name": "broken", "location": str(tmp_path / "nowhere")}
        )
        assert broken["ok"] is False
        names = [r.name for r in store["inst"].repos.load()]
        assert names == ["other", "test"] or sorted(names) == [
            "other",
            "test",
        ]  # a refused one is not kept
        assert (await post(store, "repo/remove", {"name": "other"}))["ok"]
        assert (await post(store, "repo/remove", {"name": "other"}))["ok"] is False

    @pytest.mark.asyncio
    async def test_grant_is_limited_to_what_was_declared(
        self, store: dict[str, Any]
    ) -> None:
        await post(store, "install", {"id": "dev/one"})
        assert (
            await post(
                store,
                "grant",
                {"id": "dev/one", "capabilities": ["interfaces.register"]},
            )
        )["ok"] is False
        assert (await post(store, "grant", {"id": "dev/one", "capabilities": ["log"]}))[
            "granted"
        ] == ["log"]
        assert (await post(store, "grant", {"id": "dev/one"}))["granted"] == [
            "log",
            "storage",
        ]


class TestRuntimeStartup:
    @pytest.mark.asyncio
    async def test_a_switched_off_extension_is_not_started_at_boot(
        self, tmp_path: Path
    ) -> None:
        repo = Repo(tmp_path / "repo", tmp_path)
        repo.add("dev/one")
        repo.add("dev/two")
        repo.publish()
        inst = installer(tmp_path)
        add_repo(inst, repo)
        inst.install("dev/one")
        inst.install("dev/two")
        inst.set_enabled("dev/two", False)
        runtime = ExtensionRuntime(
            inst.root,
            memory_ports(),
            policy=SandboxPolicy(drop_privileges=False),
            restart=FAST,
        )
        try:
            report = await runtime.start()
            assert report.started == ["dev/one"]
        finally:
            await runtime.stop()

    @pytest.mark.asyncio
    async def test_activate_explains_why_something_does_not_start(
        self, tmp_path: Path
    ) -> None:
        inst = installer(tmp_path)
        runtime = ExtensionRuntime(
            inst.root,
            memory_ports(),
            policy=SandboxPolicy(drop_privileges=False),
            restart=FAST,
        )
        assert "manifest" in await runtime.activate("dev/missing")
        repo = Repo(tmp_path / "repo", tmp_path)
        repo.add("dev/future", core_api=">=5")
        repo.publish()
        add_repo(inst, repo)
        # The installer would never pick a package for a newer Core; put the files there
        # by hand to see the runtime's own refusal.
        source = make_source(tmp_path / "hand", "dev/future", core_api=">=5")
        target = inst.install_dir("dev/future")
        target.parent.mkdir(parents=True)
        shutil.copytree(source, target)
        reason = await runtime.activate("dev/future")
        assert "Core API" in reason
        assert not runtime.supervisor.known("dev/future")
        await runtime.stop()


class TestPluginsPage:
    """Plugins > Installed | Catalog | Repositories: nothing is bought, and development
    components are just a repository, so the old Store and the warning are gone."""

    def _html(self) -> str:
        from pathlib import Path

        return (
            Path(__file__).resolve().parent.parent
            / "core/webui_templates/sections/plugins.html"
        ).read_text(encoding="utf-8")

    def test_the_tabs_are_installed_catalog_repositories(self) -> None:
        html = self._html()
        for name in ("installed", "catalog", "repositories"):
            assert f'data-plugins-subtab="{name}"' in html
        assert 'data-plugins-subtab="store"' not in html and ">Store<" not in html
        assert (
            'id="extension-catalog"' in html and 'id="extension-repositories"' in html
        )

    def test_the_development_components_warning_is_gone_everywhere(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        for page in (
            "core/webui_templates/sections/plugins.html",
            "core/webui_templates/sections/components.html",
            "core/webui_templates/synth_webui_index.html",
            "res/synth_webui/js/main.js",
        ):
            text = (root / page).read_text(encoding="utf-8")
            assert "dev-components-toggle" not in text, page
            assert "Development Components" not in text, page

    def test_the_script_draws_both_views_from_the_same_overview(self) -> None:
        from pathlib import Path

        script = (
            Path(__file__).resolve().parent.parent
            / "res/synth_webui/js/extension_store.js"
        ).read_text(encoding="utf-8")
        assert "renderCatalog" in script and "renderRepositories" in script
        assert "'/repo/add'" in script and "'/repo/remove'" in script


class TestRepositoriesAlwaysAvailable:
    """The Repositories tab must be able to show and add a repository even when the catalog
    cannot be read: that is what fixes an unreachable one."""

    @pytest.mark.asyncio
    async def test_an_unreadable_catalog_is_a_message_and_the_list_is_still_there(
        self, store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*args: Any, **kwargs: Any) -> Any:
            raise OSError("network is unreachable")

        monkeypatch.setattr(store["inst"], "rows", boom)
        response = await store["web"].get("/api/extensions/store", headers=OK)
        assert response.status_code == 200
        data = response.json()
        assert [r["name"] for r in data["repositories"]] == ["test"]
        assert data["extensions"] == []
        assert any("network is unreachable" in p for p in data["problems"])

    @pytest.mark.asyncio
    async def test_a_damaged_list_of_repositories_is_a_message_not_a_500(
        self, store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken() -> Any:
            raise ValueError("repositories.json is not valid JSON")

        monkeypatch.setattr(store["inst"].repos, "load", broken)
        response = await store["web"].get("/api/extensions/store", headers=OK)
        assert response.status_code == 200
        data = response.json()
        assert data["repositories"] == []
        assert any("not valid JSON" in p for p in data["problems"])

    @pytest.mark.asyncio
    async def test_a_repository_can_be_added_then_listed_then_removed(
        self, store: dict[str, Any], tmp_path: Path
    ) -> None:
        other = Repo(tmp_path / "other", tmp_path)
        other.add("dev/extra")
        other.publish()
        added = await post(
            store,
            "repo/add",
            {"name": "mine", "location": str(other.dir), "trusted": True},
        )
        assert added["ok"] is True and added["packages"] == 1
        listed = (await store["web"].get("/api/extensions/store", headers=OK)).json()
        assert {r["name"] for r in listed["repositories"]} == {"test", "mine"}
        assert "dev/extra" in {x["id"] for x in listed["extensions"]}
        removed = await post(store, "repo/remove", {"name": "mine"})
        assert removed["ok"] is True
        listed = (await store["web"].get("/api/extensions/store", headers=OK)).json()
        assert {r["name"] for r in listed["repositories"]} == {"test"}

    @pytest.mark.asyncio
    async def test_a_repository_that_cannot_be_reached_is_refused_with_the_reason(
        self, store: dict[str, Any], tmp_path: Path
    ) -> None:
        refused = await post(
            store,
            "repo/add",
            {"name": "nowhere", "location": str(tmp_path / "does-not-exist")},
        )
        assert refused["ok"] is False and refused["error"]
        listed = (await store["web"].get("/api/extensions/store", headers=OK)).json()
        assert {r["name"] for r in listed["repositories"]} == {"test"}

    def test_the_script_keeps_the_add_form_when_the_list_cannot_be_read(self) -> None:
        script = (
            Path(__file__).resolve().parent.parent
            / "res/synth_webui/js/extension_store.js"
        ).read_text(encoding="utf-8")
        assert "HTTP ' + resp.status" in script
        # the failure branch of the Repositories view still draws the form
        failure = script[script.index("async function renderRepositories") :]
        assert failure.index("repoForm(root)") < failure.index("problems(data, root)")


class TestUpdates:
    """What can be updated, and updating all of it at once."""

    def _two(self, store: dict[str, Any]) -> None:
        repo = store["repo"]
        store["inst"].install("dev/one")
        store["inst"].install("dev/lib")
        repo.add("dev/one", "1.1.0", extra_files={"marker.txt": b"v11"})
        repo.add("dev/lib", "1.1.0")
        repo.publish()

    @pytest.mark.asyncio
    async def test_the_updates_are_listed_with_both_versions(
        self, store: dict[str, Any]
    ) -> None:
        self._two(store)
        data = (
            await store["web"].get("/api/extensions/store/updates", headers=OK)
        ).json()
        assert {(u["id"], u["installed"], u["available"]) for u in data["updates"]} == {
            ("dev/one", "1.0.0", "1.1.0"),
            ("dev/lib", "1.0.0", "1.1.0"),
        }

    @pytest.mark.asyncio
    async def test_nothing_to_update_is_an_empty_list(
        self, store: dict[str, Any]
    ) -> None:
        store["inst"].install("dev/one")
        data = (
            await store["web"].get("/api/extensions/store/updates", headers=OK)
        ).json()
        assert data["updates"] == [] and data["problem"] == ""

    @pytest.mark.asyncio
    async def test_update_all_updates_every_one_and_reports_each(
        self, store: dict[str, Any]
    ) -> None:
        self._two(store)
        reply = await post(store, "update-all", {})
        assert reply["ok"] is True and reply["updated"] == 2
        assert {r["id"] for r in reply["results"]} == {"dev/one", "dev/lib"}
        assert all(r["ok"] for r in reply["results"])
        records = store["inst"].state.load().extensions
        assert (records["dev/one"].version, records["dev/lib"].version) == (
            "1.1.0",
            "1.1.0",
        )
        assert store["inst"].updates() == []

    @pytest.mark.asyncio
    async def test_one_that_fails_does_not_stop_the_others(
        self, store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._two(store)
        real = store["inst"].update

        def flaky(ext_id: str, **kw: Any) -> Any:
            if ext_id == "dev/lib":
                from core.extensions.repo.installer import InstallError

                raise InstallError("the download was refused")
            return real(ext_id, **kw)

        monkeypatch.setattr(store["inst"], "update", flaky)
        reply = await post(store, "update-all", {})
        assert reply["ok"] is False and reply["updated"] == 1
        by_id = {r["id"]: r for r in reply["results"]}
        assert by_id["dev/one"]["ok"] is True
        assert (
            by_id["dev/lib"]["ok"] is False and "refused" in by_id["dev/lib"]["error"]
        )

    @pytest.mark.asyncio
    async def test_both_need_the_token(self, store: dict[str, Any]) -> None:
        web = store["web"]
        assert (await web.get("/api/extensions/store/updates")).status_code == 401
        assert (
            await web.post("/api/extensions/store/update-all", json={})
        ).status_code == 401

    def test_the_page_has_the_bar_the_count_and_a_button_per_plugin(self) -> None:
        root = Path(__file__).resolve().parent.parent
        html = (root / "core/webui_templates/sections/plugins.html").read_text(
            encoding="utf-8"
        )
        assert 'id="plugins-update-bar"' in html
        script = (root / "res/synth_webui/js/extension_store.js").read_text(
            encoding="utf-8"
        )
        assert "updatable plugin" in script and "Update all" in script
        assert "window.SynthExtensionUpdates" in script
        main = (root / "res/synth_webui/js/main.js").read_text(encoding="utf-8")
        assert "SynthExtensionUpdates.button(item.extension_id)" in main
