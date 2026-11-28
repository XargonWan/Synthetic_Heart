"""Skins shipped as extensions: a manifest, a copy into the skins directory, a removal."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.extensions.manifest import (
    ManifestError,
    parse_manifest_text as parse_manifest,
)
from core.extensions.repo import index as repo_index
from core.extensions.repo.config import Repository
from core.extensions.repo.installer import Installer
from core.extensions.repo.pack import build_index, pack_extension
from core.extensions.skins import MARKER, read_marker

SKIN_TOML = """
[extension]
id = "{ext_id}"
name = "{title}"
version = "{version}"
description = "A skin."
author = "Someone"

[compat]
core_api = ">=0.1,<1"

[[provides.skins]]
name = "{skin}"
type = "{type}"
path = "skin"
"""


def make_skin_source(
    base: Path,
    ext_id: str = "dev/skin_ada",
    skin: str = "Ada",
    version: str = "1.0.0",
    skin_type: str = "vrm",
) -> Path:
    src = base / f"src-{ext_id.replace('/', '-')}-{version}"
    (src / "skin").mkdir(parents=True, exist_ok=True)
    (src / "synth-extension.toml").write_text(
        SKIN_TOML.format(
            ext_id=ext_id, title=skin, version=version, skin=skin, type=skin_type
        ),
        encoding="utf-8",
    )
    (src / "skin" / "persona.json").write_text('{"name": "%s"}' % skin)
    (src / "skin" / "model.vrm").write_bytes(b"vrm-" + version.encode())
    return src


def publish(tmp_path: Path, **kw: Any) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    pack_extension(make_skin_source(tmp_path / "s", **kw), repo, bundle_libs=False)
    build_index(repo, name="test")
    return repo


def installer_for(tmp_path: Path, repo: Path, skins: Path) -> Installer:
    inst = Installer(tmp_path / "runtime", core_api_version="0.1.0", skins_dir=skins)
    inst.repos.add(
        Repository(
            name="test",
            location=str(repo),
            kind="local",
            require_signature=False,
            trusted=True,
        )
    )
    return inst


class TestManifest:
    def test_a_skin_extension_has_no_process_and_is_of_kind_skins(
        self, tmp_path: Path
    ) -> None:
        src = make_skin_source(tmp_path)
        manifest = parse_manifest((src / "synth-extension.toml").read_text())
        assert manifest.entry is None and manifest.kind == "skins"
        assert [(s.name, s.type, s.path) for s in manifest.provides.skins] == [
            ("Ada", "vrm", "skin")
        ]

    @pytest.mark.parametrize("skin_type", ["live2d", "inochi2d", "gif"])
    def test_only_vrm_is_a_skin_type_for_now(
        self, tmp_path: Path, skin_type: str
    ) -> None:
        text = (
            make_skin_source(tmp_path, skin_type=skin_type) / "synth-extension.toml"
        ).read_text()
        with pytest.raises(ManifestError, match="must be one of vrm"):
            parse_manifest(text)

    @pytest.mark.parametrize("name", ["temp", "../x", "a/b", ""])
    def test_a_skin_cannot_take_a_reserved_or_unsafe_name(
        self, tmp_path: Path, name: str
    ) -> None:
        text = (
            make_skin_source(tmp_path, skin=name or "x") / "synth-extension.toml"
        ).read_text()
        text = text.replace(f'name = "{name or "x"}"\ntype', f'name = "{name}"\ntype')
        with pytest.raises(ManifestError):
            parse_manifest(text)

    def test_a_plugin_is_not_of_kind_skins(self, tmp_path: Path) -> None:
        manifest = parse_manifest(
            '[extension]\nid = "dev/one"\nname = "One"\nversion = "1.0.0"\n'
            '[compat]\ncore_api = ">=0.1,<1"\n[entry]\nmodule = "ext_main"\n'
        )
        assert manifest.kind == "plugin"


class TestIndexKind:
    def test_the_index_says_which_packages_are_skins(self, tmp_path: Path) -> None:
        repo = publish(tmp_path)
        parsed = repo_index.parse_index((repo / "synth-repo.json").read_bytes())
        assert [p.kind for p in parsed.packages] == ["skins"]

    def test_a_catalog_row_of_a_skin_is_marked_before_and_after_install(
        self, tmp_path: Path
    ) -> None:
        inst = installer_for(tmp_path, publish(tmp_path), tmp_path / "skins")
        assert [r.kind for r in inst.rows()[0]] == ["skins"]
        inst.install("dev/skin_ada")
        assert [r.kind for r in inst.rows()[0]] == ["skins"]


def picture(size: int, mode: str = "RGBA") -> bytes:
    """A real PNG of ``size`` x ``size`` pixels with enough noise to be big."""
    import io

    from PIL import Image

    noise = Image.effect_noise((size, size), 60).convert(mode)
    out = io.BytesIO()
    noise.save(out, format="PNG")
    return out.getvalue()


def publish_with_preview(
    tmp_path: Path, data: bytes, name: str = "preview.png"
) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    src = make_skin_source(tmp_path / "s")
    (src / "skin" / name).write_bytes(data)
    pack_extension(src, repo, bundle_libs=False)
    build_index(repo, name="test")
    return repo


class TestPreview:
    """The catalog shows a skin with its own picture, like the Skins tab does."""

    def test_a_preview_is_found_by_its_usual_names_in_the_skins_folder(self) -> None:
        from core.extensions.repo.previews import preview_name

        names = ["skin/model.vrm", "skin/preview.jpg", "other/preview.png"]
        assert preview_name(names, "skin") == "skin/preview.jpg"
        assert preview_name(names, "skin/") == "skin/preview.jpg"
        assert preview_name(names, "nowhere") is None
        assert preview_name(["preview.png"], "") == "preview.png"

    def test_a_picture_that_fits_is_left_alone_and_a_big_one_is_shrunk(self) -> None:
        import io

        from PIL import Image

        from core.extensions.repo.previews import MAX_EDGE, fit

        small = picture(64)
        assert fit(small, ".png") == (small, ".png")
        big = picture(2600)
        assert len(big) > 2 * 1024 * 1024
        fitted = fit(big, ".png")
        assert fitted is not None
        data, suffix = fitted
        assert len(data) <= 2 * 1024 * 1024
        with Image.open(io.BytesIO(data)) as shrunk:
            assert max(shrunk.size) <= MAX_EDGE and shrunk.mode in ("RGBA", "RGB")
        assert fit(b"not a picture " * 300000, ".png") is None
        assert fit(b"", ".png") is None

    def test_the_index_lists_the_preview_of_a_skin_first(self, tmp_path: Path) -> None:
        repo = publish_with_preview(tmp_path, picture(64))
        parsed = repo_index.parse_index((repo / "synth-repo.json").read_bytes())
        (entry,) = parsed.packages
        assert [s.file.rsplit("--", 1)[1] for s in entry.screenshots] == [
            "Ada-preview.png"
        ]
        assert (repo / entry.screenshots[0].file).is_file()

    def test_a_preview_too_big_for_the_catalog_is_published_shrunk(
        self, tmp_path: Path
    ) -> None:
        # Miku's real preview is 4 MB; the catalog's limit is 2 MB per picture.
        repo = publish_with_preview(tmp_path, picture(2600))
        parsed = repo_index.parse_index((repo / "synth-repo.json").read_bytes())
        (shot,) = parsed.packages[0].screenshots
        assert 0 < shot.size <= 2 * 1024 * 1024
        assert (repo / shot.file).stat().st_size == shot.size

    def test_a_skin_without_a_preview_has_no_picture(self, tmp_path: Path) -> None:
        repo = publish(tmp_path)
        parsed = repo_index.parse_index((repo / "synth-repo.json").read_bytes())
        assert parsed.packages[0].screenshots == ()

    def test_the_picture_is_served_before_and_after_the_skin_is_installed(
        self, tmp_path: Path
    ) -> None:
        small = picture(64)
        repo = publish_with_preview(tmp_path, small)
        inst = installer_for(tmp_path, repo, tmp_path / "skins")
        assert inst.screenshot("dev/skin_ada", 0) == (small, "image/png")
        assert inst.rows()[0][0].screenshots == 1
        inst.install("dev/skin_ada")
        for shot in repo.rglob("*--*"):  # the repository no longer matters
            shot.unlink()
        assert inst.screenshot("dev/skin_ada", 0) == (small, "image/png")
        assert inst.screenshot("dev/skin_ada", 1) is None
        assert inst.rows()[0][0].screenshots == 1

    def test_an_installed_preview_that_is_too_big_is_served_shrunk(
        self, tmp_path: Path
    ) -> None:
        repo = publish_with_preview(tmp_path, picture(2600))
        inst = installer_for(tmp_path, repo, tmp_path / "skins")
        inst.install("dev/skin_ada")
        for shot in repo.rglob("*--*"):
            shot.unlink()
        served = inst.screenshot("dev/skin_ada", 0)
        assert served is not None and 0 < len(served[0]) <= 2 * 1024 * 1024

    def test_the_catalog_shows_a_skin_large_with_its_picture_as_the_icon(self) -> None:
        script = (
            Path(__file__).resolve().parent.parent
            / "res/synth_webui/js/extension_store.js"
        ).read_text(encoding="utf-8")
        assert "skinPreview" in script and "skin-preview-large" in script
        assert "x.kind === 'skins' && x.screenshots" in script


class TestPlacement:
    def test_install_puts_the_skin_in_the_skins_directory_with_a_marker(
        self, tmp_path: Path
    ) -> None:
        skins = tmp_path / "skins"
        installer_for(tmp_path, publish(tmp_path), skins).install("dev/skin_ada")
        assert (skins / "Ada" / "model.vrm").read_bytes() == b"vrm-1.0.0"
        assert read_marker(skins / "Ada") == {"id": "dev/skin_ada", "version": "1.0.0"}

    def test_uninstall_takes_exactly_that_skin_away(self, tmp_path: Path) -> None:
        skins = tmp_path / "skins"
        (skins / "Rei").mkdir(parents=True)
        (skins / "Rei" / "model.vrm").write_bytes(b"rei")
        inst = installer_for(tmp_path, publish(tmp_path), skins)
        inst.install("dev/skin_ada")
        inst.remove("dev/skin_ada")
        assert not (skins / "Ada").exists()
        assert (skins / "Rei" / "model.vrm").read_bytes() == b"rei"

    def test_turning_it_off_and_on_removes_and_restores_the_skin(
        self, tmp_path: Path
    ) -> None:
        skins = tmp_path / "skins"
        inst = installer_for(tmp_path, publish(tmp_path), skins)
        inst.install("dev/skin_ada")
        inst.set_enabled("dev/skin_ada", False)
        assert not (skins / "Ada").exists()
        inst.set_enabled("dev/skin_ada", True)
        assert (skins / "Ada" / "model.vrm").exists()

    def test_an_update_replaces_the_files(self, tmp_path: Path) -> None:
        skins = tmp_path / "skins"
        repo = publish(tmp_path)
        inst = installer_for(tmp_path, repo, skins)
        inst.install("dev/skin_ada")
        pack_extension(
            make_skin_source(tmp_path / "s", version="1.1.0"), repo, bundle_libs=False
        )
        build_index(repo, name="test")
        inst.update("dev/skin_ada")
        assert (skins / "Ada" / "model.vrm").read_bytes() == b"vrm-1.1.0"
        assert read_marker(skins / "Ada")["version"] == "1.1.0"  # type: ignore[index]

    def test_a_folder_that_is_not_an_extensions_is_never_overwritten(
        self, tmp_path: Path
    ) -> None:
        skins = tmp_path / "skins"
        (skins / "Ada").mkdir(parents=True)
        (skins / "Ada" / "model.vrm").write_bytes(b"mine")
        inst = installer_for(tmp_path, publish(tmp_path), skins)
        inst.install("dev/skin_ada")
        assert (skins / "Ada" / "model.vrm").read_bytes() == b"mine"
        assert "already exists" in " ".join(inst.sync_skins())
        # and removing the extension leaves the user's folder alone
        inst.remove("dev/skin_ada")
        assert (skins / "Ada" / "model.vrm").read_bytes() == b"mine"
        assert not (skins / "Ada" / MARKER).exists()

    def test_the_start_up_sync_restores_skins_into_an_empty_directory(
        self, tmp_path: Path
    ) -> None:
        skins = tmp_path / "skins"
        inst = installer_for(tmp_path, publish(tmp_path), skins)
        inst.install("dev/skin_ada")
        import shutil

        shutil.rmtree(skins)  # a fresh image: the volume of installed extensions stays
        assert inst.sync_skins() == []
        assert (skins / "Ada" / "model.vrm").exists()

    def test_without_a_skins_directory_nothing_is_placed(self, tmp_path: Path) -> None:
        inst = Installer(tmp_path / "runtime", core_api_version="0.1.0")
        assert inst.sync_skins() == []


class TestWebuiSkins:
    def test_bundled_skins_are_not_removable_and_extension_skins_say_whose_they_are(
        self, tmp_path: Path
    ) -> None:
        from core.webui import SynthWebUIInterface

        skins = tmp_path / "skins"
        installer_for(tmp_path, publish(tmp_path), skins).install("dev/skin_ada")
        (skins / "Rei").mkdir()
        (skins / "Zero").mkdir()
        (skins / "Mine").mkdir()
        origin = SynthWebUIInterface._skin_origin  # noqa: SLF001
        assert origin(skins / "Rei") == {
            "bundled": True,
            "extension_id": None,
            "removable": False,
        }
        assert origin(skins / "Zero")["removable"] is False
        assert origin(skins / "Ada") == {
            "bundled": False,
            "extension_id": "dev/skin_ada",
            "removable": True,
        }
        assert origin(skins / "Mine")["extension_id"] is None


class TestSkinsTabScripts:
    root = Path(__file__).resolve().parent.parent / "res/synth_webui"

    def test_the_skins_page_has_the_same_three_tabs_as_plugins(self) -> None:
        html = (
            Path(__file__).resolve().parent.parent
            / "core/webui_templates/sections/skins.html"
        ).read_text(encoding="utf-8")
        for tab in ("installed", "catalog", "repositories"):
            assert f'data-skins-subtab="{tab}"' in html
        # the catalog and the repositories are the Plugins store's own views
        assert 'id="skins-extension-catalog"' in html
        assert 'id="skins-extension-repositories"' in html

    def test_the_skins_tab_uninstalls_extension_skins_and_follows_the_catalog(
        self,
    ) -> None:
        script = (self.root / "js/skins-ui.js").read_text(encoding="utf-8")
        assert "'/remove'" in script and "s.extension_id" in script
        # installing and updating are the catalog's job; the grid redraws when it changes
        assert "synth-extensions-changed" in script and "'/install'" not in script
        # the bundled skins are never offered a delete button
        assert "folder !== 'Rei' && folder !== 'Zero'" in script

    def test_the_store_script_draws_a_skins_only_catalog_and_the_same_repositories(
        self,
    ) -> None:
        script = (self.root / "js/extension_store.js").read_text(encoding="utf-8")
        assert (
            "skins-extension-catalog" in script
            and "skins-extension-repositories" in script
        )
        assert "(x.kind === 'skins') === wantSkins" in script
        assert "synth-extensions-changed" in script

    def test_the_plugins_catalog_leaves_skins_to_the_skins_tab(self) -> None:
        script = (self.root / "js/extension_store.js").read_text(encoding="utf-8")
        assert "(x.kind === 'skins') === wantSkins" in script and "'plugins'" in script
