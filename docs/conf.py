import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(".."))

project = "Synthetic Heart"
author = "Xargon"
release = "0.1"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.graphviz",
    "myst_parser",
]

# Accept both reStructuredText and Markdown source files. Markdown is used for
# plugin-owned ``guide.md`` files collected at build time (see below).
source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

templates_path = ["_templates"]
exclude_patterns = []

html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]

# --- Plugin-owned guide collection ----------------------------------------
# Each plugin may ship a ``guide.md`` inside its own folder. That file is the
# single source of truth: the WebUI reads it at runtime and the docs build
# copies it here so it is rendered alongside the rest of the manual. The
# generated directory is gitignored and rebuilt on every Sphinx run.
_DOCS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DOCS_DIR.parent
_PLUGINS_DIR = _REPO_ROOT / "plugins"
_GENERATED_DIR = _DOCS_DIR / "plugins" / "generated"


def _collect_plugin_guides(app=None, config=None) -> None:
    """Copy every plugin guide into ``docs/plugins/generated/``.

    Two guide layouts are supported so both folder-plugins and single-file
    plugins can ship documentation:

    * ``plugins/<name>/guide.md`` — for plugins that own a folder.
    * ``plugins/<group>/<name>/guide.md`` — for plugins nested one level
      deeper inside a grouping package (e.g. every Grillo beat plugin lives
      in ``plugins/grillo/<beat>/`` and ships its own ``guide.md``).
    * ``plugins/<name>.guide.md`` — for single-file plugins (the guide sits
      next to the ``<name>.py`` module, at any depth — e.g.
      ``plugins/recon/recon_web_search.guide.md``).

    Runs on the Sphinx ``config-inited`` event so the generated files exist
    before the toctree is resolved. Safe to call with no plugins present.
    """
    if _GENERATED_DIR.exists():
        shutil.rmtree(_GENERATED_DIR)
    _GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    if not _PLUGINS_DIR.is_dir():
        return

    # Folder-owned guides: plugins/<name>/guide.md and grouped sub-plugin
    # guides one level deeper (plugins/<group>/<name>/guide.md). The output
    # file is always named after the owning folder, so a nested beat plugin
    # such as plugins/grillo/grillo_dream/guide.md becomes grillo_dream.md.
    seen_folder_guides: set = set()
    for pattern in ("*/guide.md", "*/*/guide.md"):
        for guide in sorted(_PLUGINS_DIR.glob(pattern)):
            plugin_name = guide.parent.name
            if plugin_name in seen_folder_guides:
                continue
            seen_folder_guides.add(plugin_name)
            shutil.copyfile(guide, _GENERATED_DIR / f"{plugin_name}.md")

    # Single-file plugin guides: plugins/<name>.guide.md (searched recursively
    # so guides nested in subpackages such as plugins/recon/ are collected too).
    for guide in sorted(_PLUGINS_DIR.rglob("*.guide.md")):
        plugin_name = guide.name[: -len(".guide.md")]
        shutil.copyfile(guide, _GENERATED_DIR / f"{plugin_name}.md")

    # Copy shared guide assets (e.g. the Grillo mascot icon) alongside the
    # generated guides so the Markdown can reference them with a flat relative
    # path (e.g. ``![Grillo](GRILLO.png)``).
    _shared_assets = _DOCS_DIR / "res"
    for asset in ("GRILLO.png",):
        src = _shared_assets / asset
        if src.is_file():
            shutil.copyfile(src, _GENERATED_DIR / asset)


def setup(app):
    app.connect("config-inited", _collect_plugin_guides)
    return {"parallel_read_safe": True, "parallel_write_safe": True}


# Collect immediately at import so the generated files exist even when Sphinx
# resolves the toctree before firing ``config-inited`` in some environments.
_collect_plugin_guides()
