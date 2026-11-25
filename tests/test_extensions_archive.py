"""Tests for ``core.extensions.archive`` (platform milestone M1)."""

from __future__ import annotations

import stat
import warnings
import zipfile
from pathlib import Path

import pytest

from core.extensions.archive import (
    MAX_MANIFEST_BYTES,
    archive_sha256,
    check_archive,
    read_archive_manifest,
)
from core.extensions.manifest import MANIFEST_FILENAME, ManifestError

MANIFEST = """
[extension]
id = "dev/packed"
name = "Packed"
version = "1.0.0"

[compat]
core_api = ">=0.1"
"""


def _pack(
    path: Path,
    members: dict[str, str | bytes],
    *,
    compression: int = zipfile.ZIP_STORED,
) -> Path:
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return path


def _good(tmp_path: Path, name: str = "packed.zip") -> Path:
    return _pack(
        tmp_path / name,
        {MANIFEST_FILENAME: MANIFEST, "pkg/main.py": "print('hi')\n"},
    )


class TestReadArchive:
    def test_reads_manifest_from_a_stored_archive(self, tmp_path: Path) -> None:
        manifest = read_archive_manifest(_good(tmp_path))
        assert manifest.id == "dev/packed"
        assert manifest.version == "1.0.0"

    def test_invalid_manifest_inside_archive_is_reported(self, tmp_path: Path) -> None:
        path = _pack(tmp_path / "bad.zip", {MANIFEST_FILENAME: "[extension]\n"})
        with pytest.raises(ManifestError) as info:
            read_archive_manifest(path)
        assert "bad.zip" in str(info.value)
        assert any("extension.id" in e for e in info.value.errors)

    def test_nonzip_file(self, tmp_path: Path) -> None:
        path = tmp_path / "x.zip"
        path.write_bytes(b"this is not a zip")
        with pytest.raises(ManifestError) as info:
            read_archive_manifest(path)
        assert "not a readable zip archive" in info.value.errors[0]

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ManifestError):
            read_archive_manifest(tmp_path / "nope.zip")

    def test_manifest_must_be_at_the_root(self, tmp_path: Path) -> None:
        path = _pack(tmp_path / "nested.zip", {f"sub/{MANIFEST_FILENAME}": MANIFEST})
        with pytest.raises(ManifestError) as info:
            read_archive_manifest(path)
        assert any("missing at the archive root" in e for e in info.value.errors)

    def test_oversized_manifest_is_refused_without_reading_it_all(
        self, tmp_path: Path
    ) -> None:
        big = MANIFEST + "#" * (MAX_MANIFEST_BYTES + 10)
        path = _pack(tmp_path / "big.zip", {MANIFEST_FILENAME: big})
        with pytest.raises(ManifestError) as info:
            read_archive_manifest(path)
        assert any("larger than" in e for e in info.value.errors)

    def test_non_utf8_manifest(self, tmp_path: Path) -> None:
        path = _pack(tmp_path / "enc.zip", {MANIFEST_FILENAME: b"\xff\xfe\x00bad"})
        with pytest.raises(ManifestError) as info:
            read_archive_manifest(path)
        assert "UTF-8" in info.value.errors[0]


class TestCompression:
    def test_compressed_archive_is_rejected_by_default(self, tmp_path: Path) -> None:
        path = _pack(
            tmp_path / "deflated.zip",
            {MANIFEST_FILENAME: MANIFEST},
            compression=zipfile.ZIP_DEFLATED,
        )
        problems = check_archive(path)
        assert any("must be uncompressed" in p for p in problems)
        with pytest.raises(ManifestError):
            read_archive_manifest(path)

    def test_compressed_archive_can_be_allowed(self, tmp_path: Path) -> None:
        path = _pack(
            tmp_path / "deflated.zip",
            {MANIFEST_FILENAME: MANIFEST},
            compression=zipfile.ZIP_DEFLATED,
        )
        assert check_archive(path, require_stored=False) == []
        assert read_archive_manifest(path, require_stored=False).id == "dev/packed"


class TestUnsafeMembers:
    @pytest.mark.parametrize(
        "name",
        ["../evil.py", "a/../../evil.py", "/abs/evil.py", "C:/evil.py", "a\\b.py"],
    )
    def test_unsafe_member_names(self, tmp_path: Path, name: str) -> None:
        path = _pack(tmp_path / "u.zip", {MANIFEST_FILENAME: MANIFEST, name: "x"})
        problems = check_archive(path)
        assert any("unsafe member name" in p for p in problems)
        with pytest.raises(ManifestError):
            read_archive_manifest(path)

    def test_symlink_member_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "link.zip"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(MANIFEST_FILENAME, MANIFEST)
            info = zipfile.ZipInfo("link")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "/etc/passwd")
        assert any("symbolic link" in p for p in check_archive(path))

    def test_duplicate_member_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "dup.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr(MANIFEST_FILENAME, MANIFEST)
                archive.writestr("a.py", "1")
                archive.writestr("a.py", "2")
        assert any("duplicate member" in p for p in check_archive(path))


class TestSha256:
    def test_hash_identifies_the_file(self, tmp_path: Path) -> None:
        one = _good(tmp_path, "one.zip")
        same = tmp_path / "same.zip"
        same.write_bytes(one.read_bytes())
        other = _pack(tmp_path / "other.zip", {MANIFEST_FILENAME: MANIFEST + "\n"})
        assert archive_sha256(one) == archive_sha256(same)
        assert archive_sha256(one) != archive_sha256(other)
        assert len(archive_sha256(one)) == 64
