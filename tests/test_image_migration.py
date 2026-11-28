"""What an installation made with the older image keeps working without anyone doing anything:
the stored SearXNG address, the extensions installed from the old baked-in repository, and
the Node.js runtime the Minecraft Vessel now fetches by itself."""

from __future__ import annotations

import hashlib
import json
import tarfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from core.extensions.repo.installer import Installer
from interface import node_runtime


class _Cursor:
    def __init__(self, log: list[tuple[str, tuple[Any, ...]]], rows: int) -> None:
        self.log, self.rowcount = log, rows

    async def execute(self, sql: str, params: Any = None) -> None:
        self.log.append((" ".join(str(sql).split()), tuple(params or ())))

    async def __aenter__(self) -> "_Cursor":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _Conn:
    def __init__(self, log: list[tuple[str, tuple[Any, ...]]], rows: int) -> None:
        self.log, self.rows, self.commits = log, rows, 0

    def cursor(self) -> _Cursor:
        return _Cursor(self.log, self.rows)

    async def commit(self) -> None:
        self.commits += 1


def _db(monkeypatch: pytest.MonkeyPatch, rows: int) -> tuple[list[Any], _Conn]:
    import core.db as core_db

    log: list[tuple[str, tuple[Any, ...]]] = []
    conn = _Conn(log, rows)

    @asynccontextmanager
    async def ctx():
        yield conn

    monkeypatch.setattr(core_db, "get_conn_ctx", ctx)
    return log, conn


class TestSearxngUrl:
    @pytest.mark.asyncio
    async def test_the_old_default_address_follows_the_compose_service(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.migrations import _migrate_searxng_url

        log, conn = _db(monkeypatch, rows=1)
        await _migrate_searxng_url()
        [(sql, params)] = log
        assert "config_key = 'SEARXNG_URL'" in sql and "value IN" in sql
        assert params[0] == "http://synth-searxng:8080"
        assert "http://127.0.0.1:8888" in params[1:]
        assert conn.commits == 1

    @pytest.mark.asyncio
    async def test_an_address_the_operator_chose_is_not_in_the_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.migrations import _migrate_searxng_url

        log, conn = _db(monkeypatch, rows=0)
        await _migrate_searxng_url()
        assert conn.commits == 0
        assert "https://search.example.org" not in log[0][1]

    def test_it_is_registered(self) -> None:
        from core.migrations import _STARTUP_MIGRATIONS

        assert "migrate_searxng_url" in dict(_STARTUP_MIGRATIONS)


class TestInstalledState:
    def test_an_extension_installed_from_the_old_baked_in_repository_is_official(
        self, tmp_path: Path
    ) -> None:
        inst = Installer(tmp_path / "runtime", core_api_version="0.1.0")
        inst.state.path.parent.mkdir(parents=True, exist_ok=True)
        inst.state.path.write_text(
            json.dumps(
                {
                    "extensions": {
                        "official/zen": {
                            "version": "0.1.1",
                            "sha256": "0" * 64,
                            "repo": "bundled",
                        }
                    }
                }
            )
        )
        assert inst.state.load().extensions["official/zen"].repo == "official"


class TestNodeRuntime:
    @pytest.fixture
    def data(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.setenv("SYNTH_DATA_ROOT", str(tmp_path))
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(node_runtime, "_arch", lambda: "x64")
        return tmp_path

    def _fake_download(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pinned: bool
    ) -> None:
        name = f"node-{node_runtime.NODE_VERSION}-linux-x64"
        tree = tmp_path / "src" / name / "bin"
        tree.mkdir(parents=True)
        (tree / "node").write_text("#!/bin/sh\n")
        (tree / "npm").write_text("#!/bin/sh\n")
        archive = tmp_path / "node.tar.xz"
        with tarfile.open(archive, "w:xz") as bundle:
            bundle.add(tmp_path / "src" / name, arcname=name)
        if pinned:
            monkeypatch.setitem(
                node_runtime.NODE_SHA256,
                "x64",
                hashlib.sha256(archive.read_bytes()).hexdigest(),
            )
        monkeypatch.setattr(
            node_runtime,
            "_download",
            lambda url, target: target.write_bytes(archive.read_bytes()),
        )

    @pytest.mark.asyncio
    async def test_it_is_fetched_once_checked_and_found_afterwards(
        self, data: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._fake_download(monkeypatch, data, pinned=True)
        assert node_runtime.find_node() == (None, None)
        node, npm, why = await node_runtime.ensure_node()
        assert why == "" and node and npm and node.startswith(str(data / "node"))
        assert node_runtime.find_node() == (node, npm)

    @pytest.mark.asyncio
    async def test_a_download_that_is_not_the_pinned_build_is_refused(
        self, data: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._fake_download(monkeypatch, data, pinned=False)
        node, npm, why = await node_runtime.ensure_node()
        assert (node, npm) == (None, None) and "pinned SHA-256" in why
        assert node_runtime.find_node() == (None, None)

    def test_npm_gets_node_first_on_the_path(self) -> None:
        env = node_runtime.environment_for("/x/bin/node", {"PATH": "/usr/bin"})
        assert env["PATH"].startswith("/x/bin:")
