import io
import shutil
from pathlib import Path

import pytest
from core.webui import SynthWebUIInterface
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile


def _safe_clean_dir(path: Path) -> None:
    """Remove files in *path*, skipping anything that can't be deleted (e.g.
    files owned by the Docker container user).  Tries to remove the directory
    itself only when it becomes empty."""
    if not path.exists():
        return
    for item in list(path.iterdir()):
        try:
            if item.is_file() or item.is_symlink():
                item.unlink()
            elif item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
        except PermissionError:
            pass
    try:
        path.rmdir()
    except OSError:
        pass  # dir not empty because some files couldn't be removed


@pytest.fixture(autouse=True)
def _isolated_vrm_dir(tmp_path, monkeypatch):
    """Redirect ``SYNTH_WEBUI_VRM_DIR`` to an isolated *tmp_path* subdirectory
    so that tests never touch the real ``skins/temp/`` which may be owned by
    the Docker container user."""
    vrm_temp = tmp_path / "vrm_temp"
    vrm_temp.mkdir()
    monkeypatch.setenv("SYNTH_WEBUI_VRM_DIR", str(vrm_temp))
    yield vrm_temp
    # tmp_path is cleaned up automatically by pytest


def _ensure_temp_vrm_dir(tmp_path: Path, name: str = "user.vrm") -> Path:
    temp_dir = tmp_path / "vrm_temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    f = temp_dir / name
    f.write_bytes(b"dummy-vrm")
    return f


def test_user_vrm_prevents_rei_default(_isolated_vrm_dir: Path):
    # Arrange: ensure a user VRM exists in the isolated vrm dir before interface init
    user_vrm = _isolated_vrm_dir / "uploaded.vrm"
    user_vrm.write_bytes(b"vrm")

    # Act: create interface (autostart disabled for tests)
    ui = SynthWebUIInterface(autostart=False)

    # Assert: active_vrm should be the user-uploaded file (not Rei)
    assert ui.active_vrm == "uploaded.vrm"


def test_upload_vrm_endpoint_http(_isolated_vrm_dir: Path):
    # verify the /api/vrm route accepts multipart uploads and sets active model
    ui = SynthWebUIInterface(autostart=False)
    client = TestClient(ui.app)

    files = {"file": ("http.vrm", b"dummy", "application/octet-stream")}
    r = client.post("/api/vrm", files=files)
    assert r.status_code == 200 or r.status_code == 204

    # uploaded file should exist and be active
    assert ui.active_vrm == "model.vrm"
    assert (_isolated_vrm_dir / "model.vrm").exists()
    assert (_isolated_vrm_dir / "model.vrm").read_bytes() == b"dummy"


def test_skins_ui_contains_upload_logic():
    # ensure the shipped JS file has the listener we expect
    path = Path("res") / "synth_webui" / "js" / "skins-ui.js"
    text = path.read_text(encoding="utf-8")
    assert "[skins-ui] upload input change event" in text
    assert "skin-vrm-upload" in text

    # also verify skins.js has the listener since that one executes on tab activation
    sk_text = Path("res") / "synth_webui" / "js" / "skins.js"
    sk_contents = sk_text.read_text(encoding="utf-8")
    assert "[skins] upload input change event" in sk_contents


def test_render_index_replaces_static_version():
    ui = SynthWebUIInterface(autostart=False)
    html = ui._render_index()
    assert "%%STATIC_VERSION%%" not in html, (
        "static version placeholder should be replaced"
    )


def test_js_assets_no_cache_header(tmp_path):
    ui = SynthWebUIInterface(autostart=False)
    client = TestClient(ui.app)
    # create a dummy JS file under res to exercise mount
    js_file = Path("res/synth_webui/js/test.js")
    js_file.write_text("console.log('hi');")
    response = client.get("/js/test.js")
    assert response.status_code == 200
    # header should be present
    assert response.headers.get("cache-control") == "no-cache"


def test_rei_used_when_no_user_vrms_and_no_marker():
    # Arrange: isolated vrm dir starts empty (via autouse fixture), Rei model must exist
    rei_vrm = Path(__file__).parent.parent / "skins" / "Rei" / "model.vrm"
    assert rei_vrm.exists(), "Rei model.vrm must exist for this test"

    # Act: create interface
    ui = SynthWebUIInterface(autostart=False)

    # Assert: active_vrm resolves to Rei web path (not a temp file)
    assert (
        ui.active_vrm is None
        or str(ui.active_vrm).startswith("/skins/Rei")
        or "Rei" in str(ui.active_vrm)
    )


@pytest.mark.asyncio
async def test_upload_vrm_becomes_active_and_replaces_old(_isolated_vrm_dir: Path):
    # start with a pre-existing stale VRM in the isolated dir
    leftover = _isolated_vrm_dir / "old_stale.vrm"
    leftover.write_bytes(b"legacy")

    # create interface and perform upload
    ui = SynthWebUIInterface(autostart=False)
    assert leftover.exists(), "precondition: stale file should exist"

    first_data = b"first"
    upload1 = UploadFile(filename="first.vrm", file=io.BytesIO(first_data))
    await ui.upload_vrm_model(upload1)

    # the uploaded file should become model.vrm and be active
    model_path = _isolated_vrm_dir / "model.vrm"
    assert ui.active_vrm == "model.vrm"
    assert model_path.exists()
    assert model_path.read_bytes() == first_data

    # stale file must have been removed
    assert not leftover.exists()

    # uploading again should overwrite the previous model and keep only one file
    second_data = b"second"
    upload2 = UploadFile(filename="second.vrm", file=io.BytesIO(second_data))
    await ui.upload_vrm_model(upload2)

    assert ui.active_vrm == "model.vrm"
    assert model_path.read_bytes() == second_data
    vrms = list(_isolated_vrm_dir.glob("*.vrm"))
    assert vrms == [model_path], "only the single model.vrm file should remain in cache"

    # reinstantiate UI to ensure marker keeps the uploaded model active
    ui2 = SynthWebUIInterface(autostart=False)
    assert ui2.active_vrm == "model.vrm"


def test_current_skin_default_and_changes(_isolated_vrm_dir: Path):
    """Exercise the /api/skins/current_skin endpoint in various scenarios."""
    ui = SynthWebUIInterface(autostart=False)
    client = TestClient(ui.app)

    # default environment (no user VRM) should return Rei
    r = client.get("/api/skins/current_skin")
    assert r.status_code == 200
    assert r.json().get("skin") == "Rei"

    # uploading a VRM clears the skin
    files = {"file": ("http.vrm", b"dummy", "application/octet-stream")}
    r2 = client.post("/api/vrm", files=files)
    assert r2.status_code in (200, 204)
    r3 = client.get("/api/skins/current_skin")
    assert r3.status_code == 200
    assert r3.json().get("skin") is None

    # activate a named skin and verify current_skin reflects it
    dummy_skin = "FooSkin"
    skin_dir = Path("skins") / dummy_skin
    skin_dir.mkdir(parents=True, exist_ok=True)
    vrm_file = skin_dir / "model.vrm"
    vrm_file.write_bytes(b"vrm")
    try:
        r4 = client.post(f"/api/skins/{dummy_skin}/activate")
        assert r4.status_code == 201
        r5 = client.get("/api/skins/current_skin")
        assert r5.json().get("skin") == dummy_skin
    finally:
        shutil.rmtree(skin_dir, ignore_errors=True)
