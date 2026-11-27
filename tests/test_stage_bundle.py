"""The path a fresh image takes to serve Stage: pack, bundle, first-start install, run.

Everything is real except the Stage client itself (a two-file stand-in for the build of
``frontend/``): the packer and signer, the first-start default install from the bundled
repository, the extension process and the web router.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from core.extensions.memory_ports import memory_ports
from core.extensions.repo.installer import Installer
from core.extensions.runtime import ExtensionRuntime
from core.extensions.sandbox import SandboxPolicy
from core.extensions.supervisor import RestartPolicy
from core.extensions.webui_web import build_router

ROOT = Path(__file__).resolve().parent.parent
FAST = RestartPolicy(
    initial_delay=0.05,
    max_failures=2,
    hello_timeout=10.0,
    stop_grace=0.5,
    term_wait=0.3,
)


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location(
        "build_bundled_extensions", ROOT / "scripts" / "build_bundled_extensions.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_bundled_extensions"] = module
    spec.loader.exec_module(module)
    return module


STAGE_MANIFEST = """schema = 1
[extension]
id = "official/synth_stage"
name = "SyntH Stage"
version = "0.1.0"
[compat]
core_api = ">=0.1,<1"
[entry]
module = "ext_main"
function = "main"
[provides.web]
static = "web"
alias = "stage"
same_origin = true
[permissions]
capabilities = ["log", "web.static", "web.origin"]
"""

STAGE_MAIN = """async def main(ctx):
    await ctx.stopping.wait()
"""


@pytest.fixture
def sources(tmp_path: Path) -> Path:
    """A stand-in for the Stage package (manifest, entry, and a stand-in built client)."""
    target = tmp_path / "sources" / "synth_stage"
    (target / "web" / "assets").mkdir(parents=True)
    (target / "synth-extension.toml").write_text(STAGE_MANIFEST, encoding="utf-8")
    (target / "ext_main.py").write_text(STAGE_MAIN, encoding="utf-8")
    (target / "web" / "index.html").write_text("<h1>the stage</h1>")
    (target / "web" / "assets" / "app.js").write_text("console.log('stage')")
    return tmp_path / "sources"


def test_the_bundle_has_the_index_signature_key_and_the_defaults(
    tmp_path: Path, sources: Path
) -> None:
    script = load_script()
    out = script.build(tmp_path / "bundled", ("official/synth_stage",), sources=sources)
    assert (out / "synth-repo.json").is_file() and (
        out / "synth-repo.json.sig"
    ).is_file()
    assert (out / "public.key").read_text().strip()
    index = (out / "synth-repo.json").read_text()
    assert '"default_install": true' in index and "official/synth_stage" in index


def test_an_unknown_default_is_an_error_not_a_silent_skip(
    tmp_path: Path, sources: Path
) -> None:
    script = load_script()
    with pytest.raises(SystemExit, match="no extension source"):
        script.build(tmp_path / "bundled", ("official/ghost",), sources=sources)


@pytest.mark.asyncio
async def test_a_first_start_installs_and_serves_the_stage(
    tmp_path: Path, sources: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = load_script()
    bundled = script.build(
        tmp_path / "bundled", ("official/synth_stage",), sources=sources
    )
    root = tmp_path / "extensions"
    installer = Installer(root, bundled=bundled)
    installed, failed = installer.ensure_defaults()
    assert (installed, failed) == (["official/synth_stage"], [])
    # A default of the baked-in repository is installed granted, on and ready to run.
    assert installer.grants.granted("official/synth_stage") >= {
        "web.static",
        "web.origin",
    }
    runtime = ExtensionRuntime(
        root,
        memory_ports(),
        policy=SandboxPolicy(drop_privileges=False),
        restart=FAST,
    )
    app = FastAPI()
    app.include_router(build_router(runtime.gateway, runtime.layout.install_dir))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as http:
            await runtime.start()
            index = await http.get("/stage/")
            asset = await http.get("/x/official/synth_stage/assets/app.js")
            outside = await http.get("/stage/..%2f..%2fsynth-extension.toml")
    finally:
        await runtime.stop()
    assert index.status_code == 200 and "<h1>the stage</h1>" in index.text
    assert asset.status_code == 200 and "console.log" in asset.text
    assert outside.status_code == 404
