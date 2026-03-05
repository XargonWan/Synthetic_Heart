import shutil
from pathlib import Path

from core.webui import SynthWebUIInterface
from fastapi.testclient import TestClient


def _ensure_temp_vrm_dir(tmp_path: Path, name: str = "user.vrm") -> Path:
    temp_dir = Path("skins") / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    f = temp_dir / name
    f.write_bytes(b"dummy-vrm")
    return f


def test_user_vrm_prevents_rei_default(tmp_path, monkeypatch):
    # Arrange: ensure a user VRM exists in skins/temp before interface init
    temp_dir = Path("skins") / "temp"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    user_vrm = temp_dir / "uploaded.vrm"
    user_vrm.write_bytes(b"vrm")

    # Act: create interface (autostart disabled for tests)
    ui = SynthWebUIInterface(autostart=False)

    # Assert: active_vrm should be the user-uploaded file (not Rei)
    assert ui.active_vrm == "uploaded.vrm"


def test_upload_vrm_endpoint_http(tmp_path):
    # verify the /api/vrm route accepts multipart uploads and sets active model
    temp_dir = Path("skins") / "temp"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    ui = SynthWebUIInterface(autostart=False)
    client = TestClient(ui.app)

    files = {"file": ("http.vrm", b"dummy", "application/octet-stream")}
    r = client.post("/api/vrm", files=files)
    assert r.status_code == 200 or r.status_code == 204

    # uploaded file should exist and be active
    assert ui.active_vrm == "model.vrm"
    assert (temp_dir / "model.vrm").exists()
    assert (temp_dir / "model.vrm").read_bytes() == b"dummy"


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


def test_rei_used_when_no_user_vrms_and_no_marker(tmp_path, monkeypatch):
    # Arrange: ensure skins/temp is empty and Rei model exists
    temp_dir = Path("skins") / "temp"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Ensure Rei model file exists for test (copy if not present)
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


import io
from starlette.datastructures import UploadFile
import pytest


@pytest.mark.asyncio
async def test_upload_vrm_becomes_active_and_replaces_old(tmp_path):
    # start with a pre-existing stale VRM in cache
    temp_dir = Path("skins") / "temp"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    leftover = temp_dir / "old_stale.vrm"
    leftover.write_bytes(b"legacy")

    # create interface and perform upload
    ui = SynthWebUIInterface(autostart=False)
    assert leftover.exists(), "precondition: stale file should exist"

    first_data = b"first"
    upload1 = UploadFile(filename="first.vrm", file=io.BytesIO(first_data))
    await ui.upload_vrm_model(upload1)

    # the uploaded file should become model.vrm and be active
    model_path = temp_dir / "model.vrm"
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
    vrms = list(temp_dir.glob("*.vrm"))
    assert vrms == [model_path], "only the single model.vrm file should remain in cache"

    # reinstantiate UI to ensure marker keeps the uploaded model active
    ui2 = SynthWebUIInterface(autostart=False)
    assert ui2.active_vrm == "model.vrm"
