"""Tests for ``core.extensions.manager`` (M1)."""

from __future__ import annotations

import zipfile
from pathlib import Path


from core.extensions.manager import ExtensionManager
from core.extensions.manifest import (
    MANIFEST_FILENAME,
    Manifest,
    parse_manifest_text,
)
from core.extensions.resolver import resolve


def manifest_text(ext_id: str, *, requires: str = "", version: str = "1.0.0") -> str:
    return f"""
[extension]
id = "{ext_id}"
name = "{ext_id}"
version = "{version}"

[compat]
core_api = ">=0.1"
{requires}
"""


def make_dir_extension(root: Path, folder: str, ext_id: str, **kw: str) -> Path:
    path = root / folder
    path.mkdir()
    (path / MANIFEST_FILENAME).write_text(manifest_text(ext_id, **kw), encoding="utf-8")
    return path


def make_archive_extension(root: Path, filename: str, ext_id: str) -> Path:
    path = root / filename
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(MANIFEST_FILENAME, manifest_text(ext_id))
    return path


class TestManagerRegistration:
    def test_add_directory(self, tmp_path: Path) -> None:
        path = make_dir_extension(tmp_path, "one", "dev/one")
        manager = ExtensionManager()
        manifest = manager.add_directory(path)
        assert isinstance(manifest, Manifest)
        assert manager.source_of("dev/one") == str(path)
        assert manager.load_issues == ()

    def test_add_archive(self, tmp_path: Path) -> None:
        path = make_archive_extension(tmp_path, "one.zip", "dev/packed")
        manager = ExtensionManager()
        assert manager.add_archive(path) is not None
        assert manager.source_of("dev/packed") == str(path)

    def test_invalid_manifest_is_recorded_and_not_registered(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "bad"
        path.mkdir()
        (path / MANIFEST_FILENAME).write_text("[extension]\n", encoding="utf-8")
        manager = ExtensionManager()
        assert manager.add_directory(path) is None
        assert manager.manifests == {}
        assert any("extension.id" in i.message for i in manager.load_issues)
        assert all(i.source == str(path) for i in manager.load_issues)

    def test_first_registration_of_an_id_wins(self, tmp_path: Path) -> None:
        first = make_dir_extension(tmp_path, "a_first", "dev/same", version="1.0.0")
        second = make_dir_extension(tmp_path, "b_second", "dev/same", version="2.0.0")
        manager = ExtensionManager()
        assert manager.add_directory(first) is not None
        assert manager.add_directory(second) is None
        assert manager.manifests["dev/same"].version == "1.0.0"
        assert manager.source_of("dev/same") == str(first)
        assert any("already registered" in i.message for i in manager.load_issues)

    def test_manifests_property_returns_a_copy(self) -> None:
        manager = ExtensionManager()
        manager.add_manifest(parse_manifest_text(manifest_text("dev/a")), "test")
        manager.manifests.pop("dev/a")  # type: ignore[attr-defined]
        assert "dev/a" in manager.manifests


class TestScanDirectory:
    def test_finds_directories_and_archives_and_skips_the_rest(
        self, tmp_path: Path
    ) -> None:
        make_dir_extension(tmp_path, "dir_ext", "dev/dir")
        make_archive_extension(tmp_path, "packed.zip", "dev/packed")
        make_dir_extension(tmp_path, ".hidden", "dev/hidden")
        make_dir_extension(tmp_path, "_private", "dev/private")
        (tmp_path / "notes.txt").write_text("not an extension", encoding="utf-8")
        (tmp_path / "empty_dir").mkdir()
        manager = ExtensionManager()
        added = manager.scan_directory(tmp_path)
        assert sorted(m.id for m in added) == ["dev/dir", "dev/packed"]
        assert manager.load_issues == ()

    def test_one_bad_extension_does_not_stop_the_scan(self, tmp_path: Path) -> None:
        make_dir_extension(tmp_path, "a_good", "dev/good")
        bad = tmp_path / "b_bad"
        bad.mkdir()
        (bad / MANIFEST_FILENAME).write_text("not = = toml", encoding="utf-8")
        (tmp_path / "c_broken.zip").write_bytes(b"nope")
        make_dir_extension(tmp_path, "d_good", "dev/also_good")
        manager = ExtensionManager()
        added = manager.scan_directory(tmp_path)
        assert sorted(m.id for m in added) == ["dev/also_good", "dev/good"]
        sources = {i.source for i in manager.load_issues}
        assert str(bad) in sources and str(tmp_path / "c_broken.zip") in sources

    def test_archive_suffixes_are_a_parameter(self, tmp_path: Path) -> None:
        make_archive_extension(tmp_path, "thing.synthx", "dev/custom")
        manager = ExtensionManager()
        assert manager.scan_directory(tmp_path) == []
        added = manager.scan_directory(tmp_path, archive_suffixes=(".synthx",))
        assert [m.id for m in added] == ["dev/custom"]

    def test_unreadable_root_is_reported_not_raised(self, tmp_path: Path) -> None:
        manager = ExtensionManager()
        assert manager.scan_directory(tmp_path / "does_not_exist") == []
        assert "cannot read directory" in manager.load_issues[0].message


class TestManagerResolution:
    def test_resolve_orders_dependencies_first(self, tmp_path: Path) -> None:
        make_dir_extension(
            tmp_path,
            "app",
            "dev/app",
            requires='[[requires.extensions]]\nid = "dev/lib"\n',
        )
        make_dir_extension(tmp_path, "lib", "dev/lib")
        manager = ExtensionManager()
        manager.scan_directory(tmp_path)
        result = manager.resolve()
        assert result.order == ["dev/lib", "dev/app"]
        assert result.excluded == {}

    def test_resolution_order_does_not_depend_on_load_order(self) -> None:
        first, second = ExtensionManager(), ExtensionManager()
        for name in ("b", "a", "c"):
            first.add_manifest(
                parse_manifest_text(manifest_text(f"dev/{name}")), "test"
            )
        for name in ("c", "a", "b"):
            second.add_manifest(
                parse_manifest_text(manifest_text(f"dev/{name}")), "test"
            )
        assert first.resolve().order == second.resolve().order

    def test_check_install_uses_the_registered_extensions(self, tmp_path: Path) -> None:
        make_dir_extension(tmp_path, "lib", "dev/lib")
        manager = ExtensionManager()
        manager.scan_directory(tmp_path)

        def candidate_requiring(target: str) -> Manifest:
            return parse_manifest_text(
                manifest_text(
                    "dev/app", requires=f'[[requires.extensions]]\nid = "{target}"\n'
                )
            )

        assert manager.check_install(candidate_requiring("dev/lib")) == []
        problems = manager.check_install(candidate_requiring("dev/absent"))
        assert [(p.extension_id, p.code) for p in problems] == [
            ("dev/app", "missing_dependency")
        ]
        # Asking never registers the candidate.
        assert "dev/app" not in manager.manifests

    def test_resolution_matches_the_free_function(self) -> None:
        manager = ExtensionManager()
        manager.add_manifest(parse_manifest_text(manifest_text("dev/a")), "test")
        assert manager.resolve().order == resolve(manager.manifests).order
