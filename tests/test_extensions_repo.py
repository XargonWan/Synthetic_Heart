"""Repositories, transports, signatures and the installer (platform milestone M6)."""

from __future__ import annotations

import base64
import functools
import http.server
import json
import threading
import zipfile
from pathlib import Path
from typing import Any

import pytest

from core.extensions.manifest import load_manifest
from core.extensions.repo import index as repo_index
from core.extensions.repo.config import ConfigError, Repository
from core.extensions.repo.config import builtin_repositories as _REAL_BUILTIN
from core.extensions.repo.installer import (
    InstallError,
    Installer,
    UntrustedRepository,
    host_platform,
)
from core.extensions.repo.pack import PackError, build_index, pack_extension
from core.extensions.repo.transports import (
    GitTransport,
    HttpTransport,
    LocalTransport,
    TransportError,
    guess_kind,
)


def make_source(
    base: Path,
    ext_id: str,
    version: str = "1.0.0",
    *,
    requires: tuple[str, ...] = (),
    core_api: str = ">=0.1,<1",
    caps: tuple[str, ...] = ("log", "storage"),
    experimental: bool = False,
    extra_files: dict[str, bytes] | None = None,
) -> Path:
    publisher, name = ext_id.split("/")
    src = base / f"src-{publisher}-{name}-{version}"
    src.mkdir(parents=True, exist_ok=True)
    needs = "".join(f'\n[[requires.extensions]]\nid = "{r}"\n' for r in requires)
    (src / "synth-extension.toml").write_text(
        f"""
[extension]
id = "{ext_id}"
name = "{name.title()}"
version = "{version}"
description = "test"
experimental = {str(experimental).lower()}

[compat]
core_api = "{core_api}"

[entry]
module = "ext_main"

[permissions]
capabilities = {json.dumps(list(caps))}
{needs}
""",
        encoding="utf-8",
    )
    (src / "ext_main.py").write_text(
        "async def main(ctx):\n    await ctx.stopping.wait()\n"
    )
    for relative, data in (extra_files or {}).items():
        target = src / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return src


class Repo:
    """A repository directory under construction."""

    def __init__(self, directory: Path, tmp: Path) -> None:
        self.dir, self.tmp = directory, tmp
        self.dir.mkdir(parents=True, exist_ok=True)
        self.private: bytes | None = None
        self.public = ""

    def sign_with_new_key(self) -> None:
        self.private, self.public = repo_index.generate_keypair()

    def add(self, ext_id: str, version: str = "1.0.0", **kw: Any) -> Path:
        src = make_source(self.tmp / f"s-{self.dir.name}", ext_id, version, **kw)
        archive, _ = pack_extension(src, self.dir, bundle_libs=False)
        return archive

    def publish(self, *, name: str = "test", defaults: tuple[str, ...] = ()) -> Path:
        return build_index(
            self.dir, name=name, private_key=self.private, default_install=defaults
        )


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path / "repo", tmp_path)


def installer(tmp_path: Path, *, core_api: str = "0.1.0") -> Installer:
    return Installer(tmp_path / "runtime", core_api_version=core_api)


def add_repo(inst: Installer, repo: Repo, name: str = "test", **kw: Any) -> None:
    settings: dict[str, Any] = {
        "kind": "local",
        "public_key": repo.public,
        "require_signature": bool(repo.public),
        "trusted": True,
    }
    settings.update(kw)
    inst.repos.add(Repository(name=name, location=str(repo.dir), **settings))


# ------------------------------------------------------------------- index


class TestIndex:
    def test_a_built_index_round_trips(self, tmp_repo: Repo) -> None:
        tmp_repo.add("dev/one")
        tmp_repo.add("dev/one", "1.1.0")
        path = tmp_repo.publish(defaults=("dev/one",))
        parsed = repo_index.parse_index(path.read_bytes())
        assert [(p.id, p.version) for p in parsed.packages] == [
            ("dev/one", "1.0.0"),
            ("dev/one", "1.1.0"),
        ]
        assert parsed.versions_of("dev/one")[0].version == "1.1.0"
        assert all(p.default_install and p.platform == "any" for p in parsed.packages)

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d.update(schema=2),
            lambda d: d["packages"][0].update(id="nopublisher"),
            lambda d: d["packages"][0].update(version="not a version"),
            lambda d: d["packages"][0].update(file="../escape.synthx"),
            lambda d: d["packages"][0].update(file="/abs.synthx"),
            lambda d: d["packages"][0].update(sha256="XYZ"),
            lambda d: d["packages"][0].update(size=0),
            lambda d: d["packages"][0].update(core_api="banana"),
            lambda d: d["packages"].append(dict(d["packages"][0])),
        ],
    )
    def test_a_bad_index_is_refused_with_the_reason(
        self, tmp_repo: Repo, mutate: Any
    ) -> None:
        tmp_repo.add("dev/one")
        data = json.loads(tmp_repo.publish().read_text())
        mutate(data)
        with pytest.raises(repo_index.IndexError_):
            repo_index.parse_index(json.dumps(data).encode())

    def test_garbage_and_oversized_indexes_are_refused(self) -> None:
        for raw in (b"not json", b"[]", b"\xff\xfe"):
            with pytest.raises(repo_index.IndexError_):
                repo_index.parse_index(raw)
        with pytest.raises(repo_index.IndexError_):
            repo_index.parse_index(b" " * (repo_index.MAX_INDEX_BYTES + 1))

    def test_signatures_verify_only_for_the_right_bytes_and_key(self) -> None:
        private, public = repo_index.generate_keypair()
        other_private, other_public = repo_index.generate_keypair()
        data = b'{"schema": 1}'
        signature = repo_index.sign_index(data, private)
        assert repo_index.verify_index(data, signature, public) is None
        assert repo_index.verify_index(data + b" ", signature, public) is not None
        assert repo_index.verify_index(data, signature, other_public) is not None
        assert repo_index.verify_index(data, b"junk", public) is not None
        assert repo_index.verify_index(data, signature, "not base64!") is not None
        assert repo_index.key_fingerprint(public) != repo_index.key_fingerprint(
            other_public
        )
        assert len(repo_index.key_fingerprint(public)) == 32 and other_private

    def test_only_ed25519_keys_are_accepted_for_signing(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        pem = rsa.generate_private_key(65537, 2048).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        with pytest.raises(ValueError):
            repo_index.sign_index(b"x", pem)


# -------------------------------------------------------------- packing


class TestPacking:
    def test_packing_is_reproducible_and_skips_what_does_not_belong(
        self, tmp_path: Path
    ) -> None:
        src = make_source(
            tmp_path,
            "dev/one",
            extra_files={
                "__pycache__/x.pyc": b"x",
                "tests/t.py": b"t",
                "data/keep.txt": b"k",
                ".git/HEAD": b"h",
            },
        )
        first, manifest = pack_extension(src, tmp_path / "a", bundle_libs=False)
        second, _ = pack_extension(src, tmp_path / "b", bundle_libs=False)
        assert first.read_bytes() == second.read_bytes()
        assert manifest.id == "dev/one" and first.name == "one-1.0.0.synthx"
        with zipfile.ZipFile(first) as bundle:
            names = bundle.namelist()
        assert "synth-extension.toml" in names and "data/keep.txt" in names
        assert not any(n.startswith(("__pycache__", "tests", ".git")) for n in names)

    def test_a_native_library_makes_the_package_platform_specific(
        self, tmp_repo: Repo
    ) -> None:
        tmp_repo.add("dev/native", extra_files={"lib/thing.so": b"\x7fELF"})
        tmp_repo.add("dev/pure")
        parsed = repo_index.parse_index(tmp_repo.publish().read_bytes())
        platforms = {p.id: p.platform for p in parsed.packages}
        assert platforms == {"dev/native": host_platform(), "dev/pure": "any"}

    def test_default_install_must_name_a_package_in_the_repository(
        self, tmp_repo: Repo
    ) -> None:
        tmp_repo.add("dev/one")
        with pytest.raises(PackError):
            tmp_repo.publish(defaults=("dev/ghost",))

    def test_a_broken_archive_in_the_repository_is_reported_by_name(
        self, tmp_repo: Repo
    ) -> None:
        (tmp_repo.dir / "bad.synthx").write_bytes(b"not a zip")
        with pytest.raises(PackError, match="bad.synthx"):
            tmp_repo.publish()


# ------------------------------------------------------------ transports


class TestTransports:
    def test_local_reads_and_copies_within_the_repository(self, tmp_path: Path) -> None:
        root = tmp_path / "r"
        (root / "sub").mkdir(parents=True)
        (root / "sub" / "f.bin").write_bytes(b"12345")
        transport = LocalTransport(root)
        assert transport.read("sub/f.bin", 100) == b"12345"
        transport.fetch("sub/f.bin", tmp_path / "out" / "f", 100)
        assert (tmp_path / "out" / "f").read_bytes() == b"12345"
        assert oct((tmp_path / "out" / "f").stat().st_mode & 0o777) == "0o600"

    @pytest.mark.parametrize("bad", ["../x", "/etc/passwd", "a/../../x", "a\\b", ""])
    def test_local_refuses_paths_that_leave_the_repository(
        self, tmp_path: Path, bad: str
    ) -> None:
        (tmp_path / "r").mkdir()
        with pytest.raises(TransportError):
            LocalTransport(tmp_path / "r").read(bad, 10)

    def test_local_refuses_a_symlink_that_leads_out(self, tmp_path: Path) -> None:
        (tmp_path / "secret").write_text("s")
        (tmp_path / "r").mkdir()
        (tmp_path / "r" / "link").symlink_to(tmp_path / "secret")
        with pytest.raises(TransportError, match="outside"):
            LocalTransport(tmp_path / "r").read("link", 10)

    def test_size_limits_hold_and_leave_nothing_behind(self, tmp_path: Path) -> None:
        (tmp_path / "r").mkdir()
        (tmp_path / "r" / "big").write_bytes(b"x" * 100)
        transport = LocalTransport(tmp_path / "r")
        with pytest.raises(TransportError):
            transport.read("big", 10)
        with pytest.raises(TransportError):
            transport.fetch("big", tmp_path / "out", 10)
        assert not (tmp_path / "out").exists()

    def test_http_serves_the_same_format(self, tmp_repo: Repo, tmp_path: Path) -> None:
        tmp_repo.add("dev/one")
        tmp_repo.publish()
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=str(tmp_repo.dir)
        )
        handler.log_message = lambda *a, **k: None  # type: ignore[attr-defined]
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            transport = HttpTransport(base, allow_insecure=True)
            index = repo_index.parse_index(
                transport.read("synth-repo.json", repo_index.MAX_INDEX_BYTES)
            )
            entry = index.packages[0]
            transport.fetch(entry.file, tmp_path / "got.synthx", entry.size)
            assert (tmp_path / "got.synthx").stat().st_size == entry.size
            with pytest.raises(TransportError, match="404"):
                transport.read("missing.json", 100)
            with pytest.raises(TransportError):
                transport.fetch(entry.file, tmp_path / "small", 5)
            assert not (tmp_path / "small").exists()
        finally:
            server.shutdown()

    def test_http_must_be_https_unless_allowed_and_needs_a_host(self) -> None:
        with pytest.raises(TransportError):
            HttpTransport("http://example.org/repo")
        with pytest.raises(TransportError):
            HttpTransport("ftp://example.org/repo")
        with pytest.raises(TransportError):
            HttpTransport("https://")
        assert (
            HttpTransport("https://example.org/repo").describe()
            == "https://example.org/repo/"
        )

    def test_git_urls_are_restricted_and_commands_are_built_safely(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for bad in (
            "file:///etc",
            "/local/path",
            "ext::sh -c id",
            "http://insecure/repo.git",
        ):
            with pytest.raises(TransportError):
                GitTransport(bad, tmp_path)
        with pytest.raises(TransportError):
            GitTransport("https://example.org/r.git", tmp_path, branch="bad branch; rm")
        seen: list[tuple[tuple[str, ...], dict[str, str]]] = []

        def fake_run(cmd: list[str], **kw: Any) -> None:
            seen.append((tuple(cmd), kw["env"]))

        monkeypatch.setattr("core.extensions.repo.transports.subprocess.run", fake_run)
        transport = GitTransport("https://example.org/r.git", tmp_path, branch="main")
        transport.refresh()
        [(cmd, env)] = seen
        assert cmd[:6] == ("git", "clone", "--depth", "1", "--branch", "main")
        assert cmd[cmd.index("--") + 1] == "https://example.org/r.git"
        assert (
            env["GIT_ALLOW_PROTOCOL"] == "https:ssh"
            and env["GIT_TERMINAL_PROMPT"] == "0"
        )

    def test_what_kind_of_location_was_typed(self) -> None:
        assert guess_kind("/home/x/repo") == "local" and guess_kind("./repo") == "local"
        assert guess_kind("https://example.org/r") == "http"
        assert guess_kind("https://github.com/x/y.git") == "git"
        assert guess_kind("git@github.com:x/y.git") == "git"


# -------------------------------------------------------------- the store


class TestStores:
    def test_repositories_are_ordered_validated_and_unique(
        self, tmp_path: Path
    ) -> None:
        inst = installer(tmp_path)
        inst.repos.add(Repository(name="b", location="/b", priority=20))
        inst.repos.add(Repository(name="a", location="/a", priority=10))
        assert [r.name for r in inst.repos.load()] == ["a", "b"]
        with pytest.raises(ConfigError):
            inst.repos.add(Repository(name="a", location="/again"))
        with pytest.raises(ConfigError):
            inst.repos.add(Repository(name="Bad Name", location="/x"))
        with pytest.raises(ConfigError):
            inst.repos.add(Repository(name="c", location="/c", kind="ftp"))
        inst.repos.update("a", enabled=False)
        assert inst.repos.get("a") and inst.repos.get("a").enabled is False  # type: ignore[union-attr]
        with pytest.raises(ConfigError):
            inst.repos.update("a", nonsense=1)
        assert inst.repos.remove("a") and not inst.repos.remove("a")

    def test_the_stores_are_private_files(self, tmp_path: Path) -> None:
        inst = installer(tmp_path)
        inst.repos.add(Repository(name="a", location="/a", token="secret"))
        assert oct(inst.repos.path.stat().st_mode & 0o777) == "0o600"

    def test_a_corrupt_state_file_means_nothing_installed_not_a_crash(
        self, tmp_path: Path
    ) -> None:
        inst = installer(tmp_path)
        inst.state.path.parent.mkdir(parents=True)
        inst.state.path.write_text("{ broken")
        assert inst.state.load().extensions == {}


# ---------------------------------------------------------------- install


class TestInstall:
    def test_install_extracts_records_and_changes_nothing_else(
        self, tmp_repo: Repo, tmp_path: Path
    ) -> None:
        tmp_repo.add("dev/one", caps=("log",))
        tmp_repo.publish()
        inst = installer(tmp_path)
        add_repo(inst, tmp_repo)
        result = inst.install("dev/one")
        assert result.installed == ["dev/one 1.0.0"]
        target = inst.install_dir("dev/one")
        assert (target / "ext_main.py").is_file() and load_manifest(
            target / "synth-extension.toml"
        ).id == "dev/one"
        record = inst.state.load().extensions["dev/one"]
        assert (record.version, record.repo, record.enabled) == ("1.0.0", "test", True)
        assert oct((target / "ext_main.py").stat().st_mode & 0o777) == "0o644"
        # Installing grants nothing.
        assert inst.grants.granted("dev/one") == frozenset()
        # The same package again is a no-op.
        assert inst.install("dev/one").installed == []

    def test_dependencies_come_first_and_a_missing_one_is_a_clear_error(
        self, tmp_repo: Repo, tmp_path: Path
    ) -> None:
        tmp_repo.add("dev/lib")
        tmp_repo.add("dev/app", requires=("dev/lib",))
        tmp_repo.add("dev/orphan", requires=("dev/ghost",))
        tmp_repo.publish()
        inst = installer(tmp_path)
        add_repo(inst, tmp_repo)
        assert inst.install("dev/app").installed == ["dev/lib 1.0.0", "dev/app 1.0.0"]
        with pytest.raises(InstallError, match="dev/ghost"):
            inst.install("dev/orphan")
        assert "dev/orphan" not in inst.state.load().extensions

    def test_an_unknown_extension_names_what_went_wrong(
        self, tmp_repo: Repo, tmp_path: Path
    ) -> None:
        tmp_repo.add("dev/one")
        tmp_repo.publish()
        inst = installer(tmp_path)
        add_repo(inst, tmp_repo)
        with pytest.raises(InstallError, match="not offered"):
            inst.install("dev/nope")
        with pytest.raises(InstallError, match="not published"):
            inst.install("dev/one", version="9.9.9")
        with pytest.raises(InstallError, match="publisher/name"):
            inst.install("nopublisher")

    def test_a_package_that_does_not_match_the_index_is_refused_and_leaves_no_trace(
        self, tmp_repo: Repo, tmp_path: Path
    ) -> None:
        archive = tmp_repo.add("dev/one")
        tmp_repo.publish()
        archive.write_bytes(archive.read_bytes() + b"tamper")
        inst = installer(tmp_path)
        add_repo(inst, tmp_repo)
        with pytest.raises(InstallError, match="does not match the index|larger than"):
            inst.install("dev/one")
        assert not inst.install_dir("dev/one").exists()

    def test_a_same_size_swap_is_caught_by_the_hash(
        self, tmp_repo: Repo, tmp_path: Path
    ) -> None:
        archive = tmp_repo.add("dev/one")
        tmp_repo.publish()
        data = bytearray(archive.read_bytes())
        data[-1] ^= 0xFF
        archive.write_bytes(bytes(data))
        inst = installer(tmp_path)
        add_repo(inst, tmp_repo)
        with pytest.raises(InstallError, match="SHA-256"):
            inst.install("dev/one")

    def test_the_archive_must_hold_what_the_index_promised(
        self, tmp_repo: Repo, tmp_path: Path
    ) -> None:
        tmp_repo.add("dev/one")
        data = json.loads(tmp_repo.publish().read_text())
        data["packages"][0]["version"] = "9.0.0"  # the archive says 1.0.0
        (tmp_repo.dir / "synth-repo.json").write_text(json.dumps(data))
        inst = installer(tmp_path)
        add_repo(inst, tmp_repo)
        with pytest.raises(InstallError, match="the index said"):
            inst.install("dev/one")

    def test_an_unsafe_archive_is_refused(self, tmp_repo: Repo, tmp_path: Path) -> None:
        archive = tmp_repo.add("dev/one")
        with zipfile.ZipFile(archive, "a", compression=zipfile.ZIP_STORED) as bundle:
            bundle.writestr("../../evil.py", "x")
        # the packer would not index it, so write the entry by hand
        from core.extensions.archive import archive_sha256

        entry = {
            "id": "dev/one", "version": "1.0.0", "file": "packages/dev/one-1.0.0.synthx",
            "size": archive.stat().st_size, "sha256": archive_sha256(archive),
        }  # fmt: skip
        (tmp_repo.dir / "synth-repo.json").write_text(
            json.dumps({"schema": 1, "name": "t", "packages": [entry]})
        )
        inst = installer(tmp_path)
        add_repo(inst, tmp_repo)
        with pytest.raises(InstallError, match="unsafe"):
            inst.install("dev/one")
        assert (
            not (tmp_path / "evil.py").exists()
            and not inst.install_dir("dev/one").exists()
        )

    def test_extraction_refuses_a_member_that_leads_out_even_if_the_check_missed_it(
        self, tmp_path: Path
    ) -> None:
        archive = tmp_path / "x.synthx"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
            bundle.writestr("ok.txt", "ok")
            bundle.writestr("sub/../../escape.txt", "no")
        inst = installer(tmp_path)
        destination = tmp_path / "dest"
        destination.mkdir()
        with pytest.raises(InstallError, match="outside"):
            inst._extract(archive, destination)  # noqa: SLF001
        assert not (tmp_path / "escape.txt").exists()

    def test_extraction_has_a_size_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.extensions.repo import installer as module

        archive = tmp_path / "x.synthx"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
            bundle.writestr("big.bin", b"x" * 100)
        monkeypatch.setattr(module, "MAX_EXTRACTED_BYTES", 50)
        destination = tmp_path / "dest"
        destination.mkdir()
        with pytest.raises(InstallError, match="extraction limit"):
            installer(tmp_path)._extract(archive, destination)  # noqa: SLF001

    def test_a_failure_while_swapping_in_keeps_the_old_version_running(
        self, tmp_repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tmp_repo.add("dev/one", "1.0.0", extra_files={"marker.txt": b"old"})
        tmp_repo.publish()
        inst = installer(tmp_path)
        add_repo(inst, tmp_repo)
        inst.install("dev/one")
        tmp_repo.add("dev/one", "2.0.0", extra_files={"marker.txt": b"new"})
        tmp_repo.publish()
        real_replace = __import__("os").replace
        calls = {"n": 0}

        def flaky(src: Any, dst: Any) -> None:
            calls["n"] += 1
            if calls["n"] == 2:  # the move of the staged tree into place
                raise OSError("disk full")
            real_replace(src, dst)

        monkeypatch.setattr("core.extensions.repo.installer.os.replace", flaky)
        with pytest.raises(OSError):
            inst.install("dev/one")
        monkeypatch.undo()
        assert (inst.install_dir("dev/one") / "marker.txt").read_bytes() == b"old"
        assert inst.state.load().extensions["dev/one"].version == "1.0.0"
        assert not [
            p
            for p in inst.install_dir("dev/one").parent.iterdir()
            if p.name.startswith(".")
        ]

    def test_incompatible_api_and_platform_are_not_offered(
        self, tmp_repo: Repo, tmp_path: Path
    ) -> None:
        tmp_repo.add("dev/future", core_api=">=5")
        tmp_repo.add("dev/native", extra_files={"lib/x.so": b"\x7fELF"})
        tmp_repo.publish()
        data = json.loads((tmp_repo.dir / "synth-repo.json").read_text())
        for package in data["packages"]:
            if package["id"] == "dev/native":
                package["platform"] = "plan9-m68k-py3"
        (tmp_repo.dir / "synth-repo.json").write_text(json.dumps(data))
        inst = installer(tmp_path)
        add_repo(inst, tmp_repo)
        with pytest.raises(InstallError, match="needs Core API"):
            inst.install("dev/future")
        with pytest.raises(InstallError, match="built for plan9"):
            inst.install("dev/native")
        rows, _ = inst.rows()
        assert rows == []

    def test_experimental_and_unsigned_come_with_a_warning(
        self, tmp_repo: Repo, tmp_path: Path
    ) -> None:
        tmp_repo.add("dev/wip", experimental=True)
        tmp_repo.publish()
        inst = installer(tmp_path)
        add_repo(inst, tmp_repo)
        warnings = inst.install("dev/wip").warnings
        assert any("experimental" in w for w in warnings) and any(
            "not signed" in w for w in warnings
        )

    def test_declared_python_requirements_without_a_bundled_lib_are_flagged(
        self, tmp_repo: Repo, tmp_path: Path
    ) -> None:
        src = make_source(tmp_path, "dev/needs")
        with open(src / "synth-extension.toml", "a", encoding="utf-8") as handle:
            handle.write('\n[python]\nrequirements = ["somepkg>=1"]\n')
        pack_extension(src, tmp_repo.dir, bundle_libs=False)
        tmp_repo.publish()
        inst = installer(tmp_path)
        add_repo(inst, tmp_repo)
        assert any("no lib/" in w for w in inst.install("dev/needs").warnings)


# --------------------------------------------------------------- trust


class TestTrust:
    def test_a_signed_repository_installs_and_a_tampered_index_does_not(
        self, tmp_repo: Repo, tmp_path: Path
    ) -> None:
        tmp_repo.sign_with_new_key()
        tmp_repo.add("dev/one")
        tmp_repo.publish()
        inst = installer(tmp_path)
        add_repo(inst, tmp_repo)
        assert inst.install("dev/one").warnings == []
        index_file = tmp_repo.dir / "synth-repo.json"
        index_file.write_bytes(index_file.read_bytes().replace(b'"test"', b'"evil"', 1))
        with pytest.raises(InstallError, match="does not match this key"):
            inst.load_index(inst.repos.load()[0])

    def test_a_different_key_than_the_pinned_one_is_a_refusal_not_an_update(
        self, tmp_repo: Repo, tmp_path: Path
    ) -> None:
        tmp_repo.sign_with_new_key()
        tmp_repo.add("dev/one")
        tmp_repo.publish()
        _, someone_elses = repo_index.generate_keypair()
        inst = installer(tmp_path)
        add_repo(inst, tmp_repo, public_key=someone_elses)
        with pytest.raises(InstallError, match="does not match"):
            inst.install("dev/one")

    def test_a_required_signature_that_is_missing_is_refused(
        self, tmp_repo: Repo, tmp_path: Path
    ) -> None:
        tmp_repo.add("dev/one")
        tmp_repo.publish()  # not signed
        public = repo_index.generate_keypair()[1]
        inst = installer(tmp_path)
        add_repo(inst, tmp_repo, public_key=public, require_signature=True)
        with pytest.raises(InstallError, match="requires a signature"):
            inst.install("dev/one")

    def test_an_untrusted_repository_needs_an_explicit_confirmation(
        self, tmp_repo: Repo, tmp_path: Path
    ) -> None:
        tmp_repo.add("dev/one")
        tmp_repo.publish()
        inst = installer(tmp_path)
        add_repo(inst, tmp_repo, trusted=False)
        with pytest.raises(UntrustedRepository):
            inst.install("dev/one")
        assert not inst.install_dir("dev/one").exists()
        assert inst.install("dev/one", allow_untrusted=True).installed == [
            "dev/one 1.0.0"
        ]

    def test_installing_never_grants_and_grant_is_limited_to_the_declaration(
        self, tmp_repo: Repo, tmp_path: Path
    ) -> None:
        tmp_repo.add("dev/one", caps=("log", "storage"))
        tmp_repo.publish()
        inst = installer(tmp_path)
        add_repo(inst, tmp_repo)
        inst.install("dev/one")
        assert inst.requested_capabilities("dev/one") == ("log", "storage")
        with pytest.raises(InstallError, match="did not declare"):
            inst.grant("dev/one", ["log", "interfaces.register"])
        assert inst.grants.granted("dev/one") == frozenset()
        assert inst.grant("dev/one", ["log"]) == ["log"]
        assert inst.grants.granted("dev/one") == frozenset({"log"})
        assert inst.grant("dev/one") == ["log", "storage"]


# ---------------------------------------------------------- several repos


class TestSeveralRepositories:
    def test_priority_breaks_a_tie_and_a_real_difference_must_be_chosen(
        self, tmp_path: Path
    ) -> None:
        one, two = Repo(tmp_path / "r1", tmp_path), Repo(tmp_path / "r2", tmp_path)
        one.add("dev/x", extra_files={"w.txt": b"one"})
        two.add("dev/x", extra_files={"w.txt": b"two"})
        one.publish()
        two.publish()
        inst = installer(tmp_path)
        add_repo(inst, one, "one", priority=10)
        add_repo(inst, two, "two", priority=20)
        with pytest.raises(InstallError, match="differs between repositories"):
            inst.install("dev/x")
        inst.install("dev/x", repo="two")
        assert (inst.install_dir("dev/x") / "w.txt").read_bytes() == b"two"

    def test_the_same_package_in_two_repositories_takes_the_first_by_priority(
        self, tmp_path: Path
    ) -> None:
        one, two = Repo(tmp_path / "r1", tmp_path), Repo(tmp_path / "r2", tmp_path)
        archive = one.add("dev/x")
        (two.dir / "packages/dev").mkdir(parents=True)
        (two.dir / "packages/dev" / archive.name).write_bytes(archive.read_bytes())
        one.publish()
        two.publish()
        inst = installer(tmp_path)
        add_repo(inst, one, "one", priority=20)
        add_repo(inst, two, "two", priority=10)
        inst.install("dev/x")
        assert inst.state.load().extensions["dev/x"].repo == "two"

    def test_the_highest_compatible_version_wins_across_repositories(
        self, tmp_path: Path
    ) -> None:
        one, two = Repo(tmp_path / "r1", tmp_path), Repo(tmp_path / "r2", tmp_path)
        one.add("dev/x", "1.0.0")
        two.add("dev/x", "2.0.0")
        two.add("dev/x", "3.0.0", core_api=">=5")  # too new for this Core
        one.publish()
        two.publish()
        inst = installer(tmp_path)
        add_repo(inst, one, "one")
        add_repo(inst, two, "two")
        assert inst.install("dev/x").installed == ["dev/x 2.0.0"]

    def test_an_unreachable_repository_does_not_hide_the_others(
        self, tmp_repo: Repo, tmp_path: Path
    ) -> None:
        tmp_repo.add("dev/one")
        tmp_repo.publish()
        inst = installer(tmp_path)
        inst.repos.add(
            Repository(
                name="gone",
                location=str(tmp_path / "nowhere"),
                priority=1,
                trusted=True,
                require_signature=False,
            )
        )
        add_repo(inst, tmp_repo)
        candidates, problems = inst.available()
        assert [c.entry.id for c in candidates] == ["dev/one"]
        assert len(problems) == 1 and "gone" in problems[0]
        assert inst.install("dev/one").installed == ["dev/one 1.0.0"]


# ------------------------------------------------- update, rollback, remove


class TestLifecycle:
    def _two_versions(self, tmp_repo: Repo, tmp_path: Path) -> Installer:
        tmp_repo.add("dev/one", "1.0.0", extra_files={"marker.txt": b"v1"})
        tmp_repo.publish()
        inst = installer(tmp_path)
        add_repo(inst, tmp_repo)
        inst.install("dev/one")
        tmp_repo.add("dev/one", "1.1.0", extra_files={"marker.txt": b"v11"})
        tmp_repo.publish()
        return inst

    def test_an_update_replaces_atomically_and_keeps_the_previous_for_rollback(
        self, tmp_repo: Repo, tmp_path: Path
    ) -> None:
        inst = self._two_versions(tmp_repo, tmp_path)
        assert inst.updates() == [("dev/one", "1.0.0", "1.1.0")]
        inst.set_enabled("dev/one", False)
        assert inst.update("dev/one").installed == ["dev/one 1.1.0"]
        assert (inst.install_dir("dev/one") / "marker.txt").read_bytes() == b"v11"
        record = inst.state.load().extensions["dev/one"]
        assert (record.version, record.previous_version, record.enabled) == (
            "1.1.0",
            "1.0.0",
            False,
        )
        assert inst.updates() == []
        assert inst.rollback("dev/one") == "1.0.0"
        assert (inst.install_dir("dev/one") / "marker.txt").read_bytes() == b"v1"
        assert inst.rollback("dev/one") == "1.1.0"  # and forward again

    def test_rollback_says_when_there_is_nothing_to_go_back_to(
        self, tmp_repo: Repo, tmp_path: Path
    ) -> None:
        tmp_repo.add("dev/one")
        tmp_repo.publish()
        inst = installer(tmp_path)
        add_repo(inst, tmp_repo)
        inst.install("dev/one")
        with pytest.raises(InstallError, match="no previous version"):
            inst.rollback("dev/one")

    def test_remove_deletes_the_files_and_keeps_the_data_unless_purged(
        self, tmp_repo: Repo, tmp_path: Path
    ) -> None:
        tmp_repo.add("dev/one")
        tmp_repo.publish()
        inst = installer(tmp_path)
        add_repo(inst, tmp_repo)
        inst.install("dev/one")
        inst.grant("dev/one")
        inst.data_dir("dev/one").mkdir(parents=True)
        (inst.data_dir("dev/one") / "mine.db").write_text("data")
        inst.remove("dev/one")
        assert (
            not inst.install_dir("dev/one").exists()
            and not inst.previous_dir("dev/one").exists()
        )
        assert (inst.data_dir("dev/one") / "mine.db").exists()  # kept
        assert inst.grants.granted("dev/one") == frozenset()
        assert "dev/one" not in inst.state.load().extensions
        inst.install("dev/one")
        inst.remove("dev/one", purge=True)
        assert not inst.data_dir("dev/one").exists()
        with pytest.raises(InstallError, match="not installed"):
            inst.remove("dev/one")

    def test_an_extension_that_others_need_cannot_be_removed(
        self, tmp_repo: Repo, tmp_path: Path
    ) -> None:
        tmp_repo.add("dev/lib")
        tmp_repo.add("dev/app", requires=("dev/lib",))
        tmp_repo.publish()
        inst = installer(tmp_path)
        add_repo(inst, tmp_repo)
        inst.install("dev/app")
        with pytest.raises(InstallError, match="needed by dev/app"):
            inst.remove("dev/lib")
        inst.remove("dev/app")
        inst.remove("dev/lib")

    def test_on_and_off_are_independent_of_installed(
        self, tmp_repo: Repo, tmp_path: Path
    ) -> None:
        tmp_repo.add("dev/one")
        tmp_repo.publish()
        inst = installer(tmp_path)
        add_repo(inst, tmp_repo)
        inst.install("dev/one")
        assert inst.state.is_enabled("dev/one") is True
        inst.set_enabled("dev/one", False)
        assert (
            inst.state.is_enabled("dev/one") is False
            and inst.install_dir("dev/one").exists()
        )
        assert (
            inst.state.is_enabled("dev/not-recorded") is True
        )  # a development folder is on
        with pytest.raises(InstallError):
            inst.set_enabled("dev/ghost", True)

    def test_the_listing_says_what_is_installed_available_and_outdated(
        self, tmp_repo: Repo, tmp_path: Path
    ) -> None:
        inst = self._two_versions(tmp_repo, tmp_path)
        tmp_repo.add("dev/other", experimental=True)
        tmp_repo.publish()
        rows, problems = inst.rows()
        by_id = {r.id: r for r in rows}
        assert problems == []
        assert (
            by_id["dev/one"].installed,
            by_id["dev/one"].available,
            by_id["dev/one"].update,
        ) == ("1.0.0", "1.1.0", True)
        assert (by_id["dev/other"].installed, by_id["dev/other"].experimental) == (
            "",
            True,
        )


# ---------------------------------------------------------------- defaults


class TestDefaults:
    def _official(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """An official repository (a local stand-in with its own key) that offers one
        default and one extension that is not."""
        bundle = Repo(tmp_path / "official", tmp_path)
        bundle.sign_with_new_key()
        bundle.add("dev/core_ui")
        bundle.add("dev/extra")
        bundle.publish(name="official", defaults=("dev/core_ui",))
        stand_in = Repository(
            name="official",
            location=str(bundle.dir),
            kind="local",
            public_key=bundle.public,
            require_signature=True,
            trusted=True,
            builtin=True,
        )
        monkeypatch.setattr(
            "core.extensions.repo.config.builtin_repositories", lambda: [stand_in]
        )
        return bundle.dir

    def test_defaults_install_once_from_the_official_repository(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._official(tmp_path, monkeypatch)
        inst = Installer(tmp_path / "runtime", core_api_version="0.1.0")
        installed, failed = inst.ensure_defaults()
        assert installed == ["dev/core_ui"] and failed == []
        assert set(inst.state.load().extensions) == {"dev/core_ui"}
        assert inst.state.load().defaults_done is True
        assert inst.ensure_defaults() == ([], [])

    def test_a_default_of_the_official_repository_is_granted_what_it_declares(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._official(tmp_path, monkeypatch)
        inst = Installer(tmp_path / "runtime", core_api_version="0.1.0")
        inst.ensure_defaults()
        assert inst.grants.granted("dev/core_ui") == {"log", "storage"}
        record = inst.state.load().extensions["dev/core_ui"]
        assert record.enabled is True  # on as well as granted

    def test_a_default_another_repository_merely_flags_is_not_granted(
        self, tmp_path: Path
    ) -> None:
        other = Repo(tmp_path / "other", tmp_path)
        other.sign_with_new_key()
        other.add("dev/pushy")
        other.publish(name="other", defaults=("dev/pushy",))
        inst = Installer(tmp_path / "runtime", core_api_version="0.1.0")
        add_repo(inst, other, name="other")
        installed, failed = inst.ensure_defaults()
        assert installed == ["dev/pushy"] and failed == []
        assert inst.grants.granted("dev/pushy") == frozenset()

    def test_only_the_defaults_are_granted_not_the_rest_of_the_catalog(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._official(tmp_path, monkeypatch)
        inst = Installer(tmp_path / "runtime", core_api_version="0.1.0")
        inst.ensure_defaults()
        assert inst.grants.granted("dev/extra") == frozenset()

    def test_a_default_the_operator_removed_is_never_put_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._official(tmp_path, monkeypatch)
        inst = Installer(tmp_path / "runtime", core_api_version="0.1.0")
        inst.ensure_defaults()
        inst.remove("dev/core_ui")
        state = inst.state.load()
        state.defaults_done = False  # even if the step runs again
        inst.state.save(state)
        assert inst.ensure_defaults() == ([], [])
        assert "dev/core_ui" not in inst.state.load().extensions

    def test_a_failing_default_is_retried_next_time_and_does_not_stop_the_others(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bundle = Repo(tmp_path / "catalog", tmp_path)
        bundle.add("dev/good")
        bundle.add("dev/bad")
        bundle.publish(defaults=("dev/good", "dev/bad"))
        inst = Installer(tmp_path / "runtime", core_api_version="0.1.0")
        add_repo(inst, bundle)
        real = inst.install

        def picky(ext_id: str, **kw: Any) -> Any:
            if ext_id == "dev/bad":
                raise InstallError("boom")
            return real(ext_id, **kw)

        monkeypatch.setattr(inst, "install", picky)
        installed, failed = inst.ensure_defaults()
        assert installed == ["dev/good"] and len(failed) == 1 and "dev/bad" in failed[0]
        assert inst.state.load().defaults_done is False
        monkeypatch.setattr(inst, "install", real)
        assert inst.ensure_defaults()[0] == ["dev/bad"]
        assert inst.state.load().defaults_done is True

    def test_an_unreachable_official_repository_installs_nothing_and_is_tried_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No network at the first start: nothing is installed, the step is not marked
        done, and the next start tries again."""
        bundle = self._official(tmp_path, monkeypatch)
        (bundle / "synth-repo.json").rename(bundle / "gone.json")
        inst = Installer(tmp_path / "runtime", core_api_version="0.1.0")
        installed, failed = inst.ensure_defaults()
        assert installed == [] and len(failed) == 1 and "official" in failed[0]
        assert inst.state.load().defaults_done is False
        (bundle / "gone.json").rename(bundle / "synth-repo.json")
        assert inst.ensure_defaults()[0] == ["dev/core_ui"]


def test_the_public_key_helper_matches_what_keygen_prints() -> None:
    private, public = repo_index.generate_keypair()
    assert repo_index.public_key_of(private) == public
    assert len(base64.b64decode(public)) == 32


class TestOfficialRepository:
    @pytest.fixture(autouse=True)
    def _real_builtin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from core.extensions.repo import config

        monkeypatch.setattr(config, "builtin_repositories", _REAL_BUILTIN)

    def test_it_is_always_there_pinned_signed_and_trusted(self, tmp_path: Path) -> None:
        from core.extensions.repo.config import (
            OFFICIAL_CHANNEL,
            OFFICIAL_KEY,
            OFFICIAL_NAME,
            OFFICIAL_REPO,
        )

        inst = Installer(tmp_path / "runtime", core_api_version="0.1.0")
        [repo] = inst.repos.load()  # nothing was added: it is built in
        assert (repo.name, repo.kind, repo.location, repo.branch) == (
            OFFICIAL_NAME,
            "github",
            OFFICIAL_REPO,
            OFFICIAL_CHANNEL,
        )
        assert (
            repo.public_key == OFFICIAL_KEY and repo.require_signature and repo.trusted
        )
        assert repo.builtin is True

    def test_it_cannot_be_removed_or_edited(self, tmp_path: Path) -> None:
        inst = Installer(tmp_path / "runtime", core_api_version="0.1.0")
        with pytest.raises(ConfigError, match="cannot be removed"):
            inst.repos.remove("official")
        with pytest.raises(ConfigError, match="cannot be edited"):
            inst.repos.update("official", trusted=False)
        assert [r.name for r in inst.repos.load()] == ["official"]

    def test_it_is_never_written_to_the_repositories_file(self, tmp_path: Path) -> None:
        inst = Installer(tmp_path / "runtime", core_api_version="0.1.0")
        mine = Repository(name="mine", location=str(tmp_path), kind="local")
        inst.repos.add(mine)
        stored = json.loads(inst.repos.path.read_text())
        assert [r["name"] for r in stored["repositories"]] == ["mine"]
        assert sorted(r.name for r in inst.repos.load()) == ["mine", "official"]

    def test_a_stale_copy_from_an_older_version_is_ignored(
        self, tmp_path: Path
    ) -> None:
        inst = Installer(tmp_path / "runtime", core_api_version="0.1.0")
        inst.repos.path.parent.mkdir(parents=True, exist_ok=True)
        inst.repos.path.write_text(
            json.dumps(
                {
                    "repositories": [
                        {"name": "official", "location": "evil", "kind": "local"}
                    ]
                }
            )
        )
        [repo] = inst.repos.load()
        assert repo.kind == "github" and repo.builtin

    def test_a_repository_cannot_take_its_name(self, tmp_path: Path) -> None:
        inst = Installer(tmp_path / "runtime", core_api_version="0.1.0")
        with pytest.raises(ConfigError, match="already exists"):
            inst.repos.add(
                Repository(name="official", location=str(tmp_path), kind="local")
            )

    def test_the_release_channel_can_be_chosen_with_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SYNTH_OFFICIAL_CHANNEL", "latest")
        inst = Installer(tmp_path / "runtime", core_api_version="0.1.0")
        assert inst.repos.load()[0].branch == "latest"

    def test_the_pinned_key_is_a_valid_ed25519_public_key(self) -> None:
        from core.extensions.repo.config import OFFICIAL_KEY

        assert len(base64.b64decode(OFFICIAL_KEY)) == 32
        assert repo_index.key_fingerprint(OFFICIAL_KEY)
