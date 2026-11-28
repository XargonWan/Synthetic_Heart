"""A repository served as the assets of a rolling GitHub release."""

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
from core.extensions.repo.config import Repository
from core.extensions.repo.installer import Installer
from core.extensions.repo.pack import build_release, release_asset_name
from core.extensions.repo.transports import GithubReleaseTransport, TransportError
from tests.test_extensions_repo import make_source

REPO = "Owner/plugins"
TAG = "latest-develop"


@pytest.fixture
def released(tmp_path: Path) -> tuple[Path, bytes, str]:
    """Assets of a release built from two extensions, with a signed catalog."""
    private, public = repo_index.generate_keypair()
    sources = [
        make_source(tmp_path / "src", "official/alpha", "1.2.0"),
        make_source(tmp_path / "src", "official/beta", "0.3.1"),
    ]
    assets = build_release(
        sources,
        tmp_path / "dist",
        name="official",
        private_key=private,
        default_install=("official/alpha",),
        bundle_libs=False,
    )
    assert assets
    return tmp_path / "dist", private, public


class TestBuildRelease:
    def test_every_package_is_one_zip_named_without_a_version(
        self, released: tuple[Path, bytes, str]
    ) -> None:
        dist, _, _ = released
        assert sorted(p.name for p in dist.iterdir()) == [
            "official-alpha.zip",
            "official-beta.zip",
            "synth-repo.json",
            "synth-repo.json.sig",
        ]

    def test_the_catalog_names_each_asset_and_carries_the_versions(
        self, released: tuple[Path, bytes, str]
    ) -> None:
        dist, _, _ = released
        catalog = repo_index.parse_index((dist / "synth-repo.json").read_bytes())
        rows = {p.id: (p.version, p.file, p.default_install) for p in catalog.packages}
        assert rows == {
            "official/alpha": ("1.2.0", "official-alpha.zip", True),
            "official/beta": ("0.3.1", "official-beta.zip", False),
        }

    def test_the_catalog_signature_verifies_and_a_change_breaks_it(
        self, released: tuple[Path, bytes, str]
    ) -> None:
        dist, _, public = released
        raw = (dist / "synth-repo.json").read_bytes()
        sig = (dist / "synth-repo.json.sig").read_bytes()
        assert repo_index.verify_index(raw, sig, public) is None
        assert repo_index.verify_index(raw + b" ", sig, public) is not None

    def test_a_second_build_replaces_the_first(self, tmp_path: Path) -> None:
        src = make_source(tmp_path / "src", "official/alpha", "1.0.0")
        build_release([src], tmp_path / "dist", name="o", bundle_libs=False)
        newer = make_source(tmp_path / "src2", "official/alpha", "2.0.0")
        build_release([newer], tmp_path / "dist", name="o", bundle_libs=False)
        catalog = repo_index.parse_index(
            (tmp_path / "dist" / "synth-repo.json").read_bytes()
        )
        assert [(p.id, p.version) for p in catalog.packages] == [
            ("official/alpha", "2.0.0")
        ]
        assert not (tmp_path / "dist" / "synth-repo.json.sig").exists()

    def test_an_extension_that_is_no_longer_built_is_not_left_in_the_catalog(
        self, tmp_path: Path
    ) -> None:
        a = make_source(tmp_path / "src", "official/alpha", "1.0.0")
        b = make_source(tmp_path / "src", "official/beta", "1.0.0")
        build_release([a, b], tmp_path / "dist", name="o", bundle_libs=False)
        build_release([a], tmp_path / "dist", name="o", bundle_libs=False)
        catalog = repo_index.parse_index(
            (tmp_path / "dist" / "synth-repo.json").read_bytes()
        )
        assert [p.id for p in catalog.packages] == ["official/alpha"]
        assert not (tmp_path / "dist" / "official-beta.zip").exists()

    def test_two_sources_for_one_extension_are_refused(self, tmp_path: Path) -> None:
        from core.extensions.repo.pack import PackError

        a = make_source(tmp_path / "a", "official/alpha", "1.0.0")
        b = make_source(tmp_path / "b", "official/alpha", "1.0.1")
        with pytest.raises(PackError, match="both"):
            build_release([a, b], tmp_path / "dist", name="o", bundle_libs=False)

    def test_the_asset_name(self) -> None:
        from core.extensions.manifest import parse_manifest

        manifest = parse_manifest(
            {
                "extension": {"id": "acme/thing", "name": "Thing", "version": "9.9"},
                "compat": {"core_api": ">=0.1"},
            },
            source="t",
        )
        assert release_asset_name(manifest) == "acme-thing.zip"


@pytest.fixture
def github(released: tuple[Path, bytes, str], tmp_path: Path) -> Iterator[str]:
    """A fake GitHub: release downloads redirect to a CDN path, like the real one."""
    dist = released[0]
    served: list[str] = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            prefix = f"/{REPO}/releases/download/{TAG}/"
            if self.path.startswith(prefix):
                served.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/cdn/" + self.path[len(prefix) :])
                self.end_headers()
            elif self.path.startswith("/cdn/"):
                self.path = self.path[len("/cdn") :]
                super().do_GET()
            else:
                self.send_error(404)

        def log_message(self, *args: Any) -> None:
            return None

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(Handler, directory=str(dist))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def transport(base: str, **kw: Any) -> GithubReleaseTransport:
    return GithubReleaseTransport(REPO, TAG, base=base, allow_loopback_http=True, **kw)


class TestTransport:
    def test_assets_are_read_through_the_cdn_redirect(self, github: str) -> None:
        t = transport(github)
        assert json.loads(t.read("synth-repo.json", 1_000_000))["name"] == "official"
        assert t.describe() == f"github:{REPO}@{TAG}"

    def test_an_asset_is_fetched_to_a_file(self, github: str, tmp_path: Path) -> None:
        dest = tmp_path / "got.zip"
        transport(github).fetch("official-alpha.zip", dest, 10_000_000)
        assert dest.read_bytes()[:2] == b"PK"

    def test_a_missing_asset_and_an_oversized_one_are_errors(
        self, github: str, tmp_path: Path
    ) -> None:
        with pytest.raises(TransportError, match="404"):
            transport(github).read("nope.zip", 1000)
        with pytest.raises(TransportError, match="larger"):
            transport(github).read("official-alpha.zip", 10)

    @pytest.mark.parametrize("name", ["../x", "a/b.zip", "/abs", "", "a\\b"])
    def test_an_asset_is_a_plain_name(self, github: str, name: str) -> None:
        with pytest.raises(TransportError, match="unsafe|plain name"):
            transport(github).read(name, 1000)

    @pytest.mark.parametrize(
        ("repo", "tag"),
        [
            ("justaname", "latest"),
            ("a/b/c", "latest"),
            ("a/b", "../x"),
            ("a/b", "-x"),
            ("a b/c", "t"),
        ],
    )
    def test_a_malformed_repository_or_tag_is_refused(
        self, repo: str, tag: str
    ) -> None:
        with pytest.raises(TransportError):
            GithubReleaseTransport(repo, tag)

    def test_the_default_tag_is_latest(self) -> None:
        assert GithubReleaseTransport(REPO, "").tag == "latest"

    @staticmethod
    def _serve(
        handler: type,
    ) -> tuple[http.server.ThreadingHTTPServer, threading.Thread]:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def test_a_token_reads_a_private_release_through_the_api_and_stays_home(
        self,
    ) -> None:
        seen: dict[str, tuple[str | None, str | None]] = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                seen[self.path] = (
                    self.headers.get("Authorization"),
                    self.headers.get("Accept"),
                )
                port = self.server.server_port
                if self.path == "/repos/o/r/releases/tags/t":
                    body = json.dumps(
                        {
                            "assets": [
                                {
                                    "name": "a",
                                    "url": f"http://127.0.0.1:{port}/assets/9",
                                }
                            ]
                        }
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/assets/9":
                    self.send_response(302)
                    # 127.0.0.1 and localhost are different hosts to the client
                    self.send_header("Location", f"http://localhost:{port}/cdn/a")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"ok")

            def log_message(self, *args: Any) -> None:
                return None

        server, thread = self._serve(Handler)
        try:
            root = f"http://127.0.0.1:{server.server_port}"
            t = GithubReleaseTransport(
                "o/r",
                "t",
                token="secret",
                base=root,
                api_base=root,
                allow_loopback_http=True,
            )
            assert t.read("a", 100) == b"ok"
        finally:
            server.shutdown()
            thread.join(timeout=5)
        assert seen["/repos/o/r/releases/tags/t"][0] == "Bearer secret"
        assert seen["/assets/9"] == ("Bearer secret", "application/octet-stream")
        assert seen["/cdn/a"][0] is None

    def test_without_a_token_nothing_credential_is_sent(self) -> None:
        seen: list[str | None] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args: Any) -> None:
                return None

        server, thread = self._serve(Handler)
        try:
            t = GithubReleaseTransport(
                "o/r",
                "t",
                base=f"http://127.0.0.1:{server.server_port}",
                allow_loopback_http=True,
            )
            assert t.read("a", 100) == b"ok"
        finally:
            server.shutdown()
            thread.join(timeout=5)
        assert seen == [None]

    @pytest.mark.parametrize("status", [403, 429])
    def test_a_refused_download_says_where_to_enter_a_token(self, status: int) -> None:
        t = GithubReleaseTransport("o/r", "t")

        def refuse(relpath: str, limit: int):
            raise TransportError(f"download failed: HTTP {status}")

        t._stream = refuse  # type: ignore[method-assign]
        with pytest.raises(TransportError) as info:
            t.read("a", 10)
        message = str(info.value)
        assert "Settings > Extensions > GitHub access token" in message
        assert "Enter a GitHub access token" in message

    def test_a_refusal_with_a_token_asks_to_check_it_instead(self) -> None:
        t = GithubReleaseTransport("o/r", "t", token="x")

        def refuse(relpath: str, limit: int):
            raise TransportError("download failed: HTTP 403")

        t._stream = refuse  # type: ignore[method-assign]
        with pytest.raises(TransportError, match="Check the token"):
            t.read("a", 10)

    def test_a_404_without_a_token_mentions_private_repositories(self) -> None:
        t = GithubReleaseTransport("o/r", "t")

        def missing(relpath: str, limit: int):
            raise TransportError("download failed: HTTP 404")

        t._stream = missing  # type: ignore[method-assign]
        with pytest.raises(TransportError, match="private"):
            t.read("a", 10)


class TestGithubTokenSetting:
    def test_the_configured_token_reaches_a_github_repository_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.extensions.repo import installer as installer_module
        from core.extensions.repo.config import Repository

        monkeypatch.setattr(installer_module, "github_token", lambda: "global-token")
        inst = Installer(tmp_path / "runtime", core_api_version="0.1.0")
        github = inst._default_transport(
            Repository(name="g", location="github:o/r", kind="github", branch="t")
        )
        assert github._token == "global-token"  # type: ignore[attr-defined]
        http_repo = inst._default_transport(
            Repository(name="h", location="https://example.org/r", kind="http")
        )
        assert http_repo._token is None  # type: ignore[attr-defined]

    def test_a_repository_own_token_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.extensions.repo import installer as installer_module
        from core.extensions.repo.config import Repository

        monkeypatch.setattr(installer_module, "github_token", lambda: "global-token")
        inst = Installer(tmp_path / "runtime", core_api_version="0.1.0")
        transport_ = inst._default_transport(
            Repository(
                name="g", location="github:o/r", kind="github", token="own-token"
            )
        )
        assert transport_._token == "own-token"  # type: ignore[attr-defined]

    def test_the_setting_is_a_sensitive_core_key(self) -> None:
        from core.config_manager import config_registry
        from core.extensions.repo.github_access import TOKEN_KEY, github_token

        github_token()
        definition = config_registry._definitions[TOKEN_KEY]  # noqa: SLF001
        assert definition.sensitive is True


class TestEndToEnd:
    def test_an_installation_points_at_the_release_and_installs_from_it(
        self,
        github: str,
        released: tuple[Path, bytes, str],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, _, public = released
        inst = Installer(
            tmp_path / "runtime",
            core_api_version="0.1.0",
            transport_for=lambda repo: transport(github),
        )
        monkeypatch.setattr(
            "core.extensions.repo.config.builtin_repositories",
            lambda: [
                Repository(
                    name="official",
                    location=REPO,
                    kind="github",
                    branch=TAG,
                    public_key=public,
                    require_signature=True,
                    trusted=True,
                    builtin=True,
                )
            ],
        )
        candidates, problems = inst.available()
        assert problems == [] and {c.entry.id for c in candidates} == {
            "official/alpha",
            "official/beta",
        }
        assert inst.install("official/beta").installed == ["official/beta 0.3.1"]
        assert "official/beta" in inst.state.load().extensions

    def test_a_catalog_signed_by_someone_else_is_refused(
        self, github: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, other_public = repo_index.generate_keypair()
        inst = Installer(
            tmp_path / "runtime",
            core_api_version="0.1.0",
            transport_for=lambda repo: transport(github),
        )
        monkeypatch.setattr(
            "core.extensions.repo.config.builtin_repositories",
            lambda: [
                Repository(
                    name="official",
                    location=REPO,
                    kind="github",
                    branch=TAG,
                    public_key=other_public,
                    require_signature=True,
                    trusted=True,
                    builtin=True,
                )
            ],
        )
        candidates, problems = inst.available()
        assert candidates == [] and "signature" in problems[0]

    def test_the_default_transport_is_a_github_release_one(
        self, tmp_path: Path
    ) -> None:
        inst = Installer(tmp_path / "runtime", core_api_version="0.1.0")
        t = inst._default_transport(  # noqa: SLF001
            Repository(
                name="o",
                location="github:Owner/plugins",
                kind="github",
                branch="latest",
            )
        )
        assert isinstance(t, GithubReleaseTransport)
        assert (t.repo, t.tag) == ("Owner/plugins", "latest")

    def test_the_repo_command_accepts_the_kind_and_checks_the_catalog(
        self,
        github: str,
        released: tuple[Path, bytes, str],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from core.extensions.cli import run

        monkeypatch.setattr(
            Installer, "_default_transport", lambda self, repo: transport(github)
        )
        lines: list[str] = []
        code = run(
            [
                "--root",
                str(tmp_path / "root"),
                "repo",
                "add",
                "gh",
                "github:Owner/plugins",
                "--branch",
                TAG,
                "--key",
                released[2],
            ],
            lines.append,
        )
        assert code == 0, lines
        [repo] = Installer(tmp_path / "root").repos.load()
        assert (repo.kind, repo.location, repo.branch) == (
            "github",
            "github:Owner/plugins",
            TAG,
        )

    def test_an_unreachable_release_is_not_added(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.extensions.cli import run

        monkeypatch.setattr(
            Installer,
            "_default_transport",
            lambda self, repo: transport("http://127.0.0.1:1"),
        )
        lines: list[str] = []
        code = run(
            [
                "--root",
                str(tmp_path),
                "repo",
                "add",
                "gh",
                "github:Owner/plugins",
                "--unsigned",
            ],
            lines.append,
        )
        assert code == 1 and Installer(tmp_path).repos.load() == []


def test_a_release_that_is_not_published_says_so_instead_of_a_bare_404() -> None:
    from core.extensions.repo.transports import GithubReleaseTransport, TransportError

    transport = GithubReleaseTransport("Owner/plugins", "latest-develop")

    def refuse(relpath: str, limit: int):
        raise TransportError(f"{relpath!r}: HTTP 404")

    transport._stream = refuse  # type: ignore[method-assign]
    with pytest.raises(TransportError, match="nothing is published yet") as info:
        transport.read("synth-repo.json", 1000)
    assert "'latest-develop'" in str(info.value) and "Owner/plugins" in str(info.value)
    other = GithubReleaseTransport("Owner/plugins")

    def broken(relpath: str, limit: int):
        raise TransportError("download failed: HTTP 500")

    other._stream = broken  # type: ignore[method-assign]
    with pytest.raises(TransportError, match="HTTP 500"):
        other.read("synth-repo.json", 1000)
