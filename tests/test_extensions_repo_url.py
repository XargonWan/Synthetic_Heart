"""Packages too big for a Git host are listed with a URL and downloaded from it."""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from core.extensions.repo import index as repo_index
from core.extensions.repo import installer as installer_module
from core.extensions.repo.installer import InstallError, Installer
from core.extensions.repo.pack import build_index
from core.extensions.repo.transports import TransportError, fetch_url
from tests.test_extensions_repo import Repo, add_repo

URL = "https://github.com/example/plugins/releases/download/v1/"


def entry(**changes: Any) -> dict[str, Any]:
    base = {
        "id": "dev/big",
        "version": "1.0.0",
        "file": "packages/dev/big-1.0.0.synthx",
        "size": 10,
        "sha256": "a" * 64,
    }
    base.update(changes)
    return base


def parse(*entries: dict[str, Any]) -> repo_index.RepoIndex:
    doc = {
        "schema": repo_index.SCHEMA,
        "name": "t",
        "generated": "x",
        "packages": list(entries),
    }
    return repo_index.parse_index(json.dumps(doc).encode())


class TestIndex:
    def test_an_https_url_is_kept(self) -> None:
        [pkg] = parse(entry(url=URL + "big.synthx")).packages
        assert pkg.url == URL + "big.synthx"

    def test_no_url_is_the_default(self) -> None:
        assert parse(entry()).packages[0].url == ""

    @pytest.mark.parametrize(
        "bad",
        [
            "http://example.com/a.synthx",
            "ftp://example.com/a.synthx",
            "https://user:pw@example.com/a.synthx",
            "https:///nohost",
            "https://example.com/" + "x" * 600,
            "file:///etc/passwd",
        ],
    )
    def test_anything_but_a_plain_https_url_is_refused(self, bad: str) -> None:
        with pytest.raises(repo_index.IndexError_):
            parse(entry(url=bad))

    def test_a_big_package_gets_a_url_and_a_small_one_does_not(
        self, tmp_path: Path
    ) -> None:
        repo = Repo(tmp_path / "repo", tmp_path)
        repo.add("dev/small", extra_files=None)
        big = repo.add("dev/big", extra_files={"blob.bin": b"x" * 200_000})
        build_index(repo.dir, name="t", url_base=URL, url_above=100_000)
        parsed = repo_index.parse_index((repo.dir / "synth-repo.json").read_bytes())
        urls = {p.id: p.url for p in parsed.packages}
        assert urls == {"dev/big": URL + big.name, "dev/small": ""}

    def test_without_a_base_nothing_gets_a_url(self, tmp_path: Path) -> None:
        repo = Repo(tmp_path / "repo", tmp_path)
        repo.add("dev/big", extra_files={"blob.bin": b"x" * 200_000})
        build_index(repo.dir, name="t", url_above=100)
        assert (
            repo_index.parse_index((repo.dir / "synth-repo.json").read_bytes())
            .packages[0]
            .url
            == ""
        )


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    root = tmp_path / "www"
    root.mkdir()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/redirect-out"):
                self.send_response(302)
                self.send_header("Location", "ftp://example.com/x")
                self.end_headers()
            elif self.path.startswith("/hop"):
                self.send_response(302)
                self.send_header("Location", "/file.bin")
                self.end_headers()
            elif self.path.startswith("/stream"):
                # No Content-Length: the size is only known by counting what arrives.
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"x" * 5000)
            elif self.path.startswith("/loop"):
                self.send_response(302)
                self.send_header("Location", "/loop")
                self.end_headers()
            else:
                super().do_GET()

        def log_message(self, *args: Any) -> None:
            return None

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(Handler, directory=str(root))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", root
    finally:
        server.shutdown()
        thread.join(timeout=5)


class TestFetchUrl:
    def test_a_file_is_downloaded_through_a_redirect(
        self, served: tuple[str, Path], tmp_path: Path
    ) -> None:
        base, root = served
        (root / "file.bin").write_bytes(b"hello")
        dest = tmp_path / "out" / "f.bin"
        fetch_url(base + "/hop", dest, 100, allow_loopback_http=True)
        assert dest.read_bytes() == b"hello"

    def test_plain_http_is_refused_by_default(self, tmp_path: Path) -> None:
        with pytest.raises(TransportError, match="https"):
            fetch_url("http://127.0.0.1:1/x", tmp_path / "f", 10)
        with pytest.raises(TransportError, match="https"):
            fetch_url(
                "http://example.com/x", tmp_path / "f", 10, allow_loopback_http=True
            )

    def test_a_redirect_away_from_https_is_refused(
        self, served: tuple[str, Path], tmp_path: Path
    ) -> None:
        with pytest.raises(TransportError, match="redirected away"):
            fetch_url(
                served[0] + "/redirect-out",
                tmp_path / "f",
                10,
                allow_loopback_http=True,
            )

    def test_a_redirect_loop_ends(
        self, served: tuple[str, Path], tmp_path: Path
    ) -> None:
        with pytest.raises(TransportError, match="too many redirects"):
            fetch_url(served[0] + "/loop", tmp_path / "f", 10, allow_loopback_http=True)

    def test_a_download_over_the_limit_is_cut_and_nothing_is_left(
        self, served: tuple[str, Path], tmp_path: Path
    ) -> None:
        base, root = served
        (root / "file.bin").write_bytes(b"x" * 5000)
        dest = tmp_path / "f"
        with pytest.raises(TransportError, match="larger"):
            fetch_url(base + "/file.bin", dest, 100, allow_loopback_http=True)
        assert not dest.exists()

    def test_a_download_with_no_declared_size_is_still_capped(
        self, served: tuple[str, Path], tmp_path: Path
    ) -> None:
        dest = tmp_path / "f"
        with pytest.raises(TransportError, match="larger"):
            fetch_url(served[0] + "/stream", dest, 100, allow_loopback_http=True)
        assert not dest.exists()

    def test_a_missing_file_is_an_error(
        self, served: tuple[str, Path], tmp_path: Path
    ) -> None:
        with pytest.raises(TransportError, match="404"):
            fetch_url(served[0] + "/nope", tmp_path / "f", 10, allow_loopback_http=True)


class TestInstall:
    def make(self, tmp_path: Path) -> tuple[Installer, Repo, Path]:
        repo = Repo(tmp_path / "repo", tmp_path)
        repo.sign_with_new_key()
        archive = repo.add("dev/big", extra_files={"blob.bin": b"x" * 1000})
        build_index(
            repo.dir, name="test", private_key=repo.private, url_base=URL, url_above=100
        )
        return Installer(tmp_path / "runtime", core_api_version="0.1.0"), repo, archive

    def test_a_remote_repository_downloads_from_the_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inst, repo, archive = self.make(tmp_path)
        # Serve the index through a fake "remote" repo that does not hold the file.
        remote = tmp_path / "remote"
        remote.mkdir()
        (remote / "synth-repo.json").write_bytes(
            (repo.dir / "synth-repo.json").read_bytes()
        )
        (remote / "synth-repo.json.sig").write_bytes(
            (repo.dir / "synth-repo.json.sig").read_bytes()
        )
        added: list[str] = []

        def fake_fetch(url: str, dest: Path, limit: int, **kw: Any) -> None:
            added.append(url)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(archive.read_bytes())

        monkeypatch.setattr(installer_module, "fetch_url", fake_fetch)
        from core.extensions.repo.config import Repository

        inst.repos.add(
            Repository(
                name="remote",
                location=str(remote),
                kind="local",
                public_key=repo.public,
                require_signature=True,
                trusted=True,
            )
        )
        result = inst.install("dev/big")
        assert added == [URL + archive.name]
        assert result.installed == ["dev/big 1.0.0"]

    def test_a_local_repository_that_has_the_file_does_not_download(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inst, repo, _ = self.make(tmp_path)
        monkeypatch.setattr(
            installer_module,
            "fetch_url",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not download")),
        )
        add_repo(inst, repo)
        assert inst.install("dev/big").installed == ["dev/big 1.0.0"]

    def test_a_download_that_does_not_match_the_index_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inst, repo, archive = self.make(tmp_path)
        remote = tmp_path / "remote"
        remote.mkdir()
        for name in ("synth-repo.json", "synth-repo.json.sig"):
            (remote / name).write_bytes((repo.dir / name).read_bytes())

        def evil(url: str, dest: Path, limit: int, **kw: Any) -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(archive.read_bytes() + b"tampered")

        monkeypatch.setattr(installer_module, "fetch_url", evil)
        from core.extensions.repo.config import Repository

        inst.repos.add(
            Repository(
                name="remote",
                location=str(remote),
                kind="local",
                public_key=repo.public,
                require_signature=True,
                trusted=True,
            )
        )
        with pytest.raises(InstallError, match="does not match the index"):
            inst.install("dev/big")
        assert "dev/big" not in inst.state.load().extensions
