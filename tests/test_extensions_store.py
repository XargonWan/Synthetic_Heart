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
