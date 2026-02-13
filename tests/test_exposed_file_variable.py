import os
from fastapi.testclient import TestClient
from core.webui import SynthWebUIInterface
from core.variables_engine import register_exposed_var


def create_client():
    ui = SynthWebUIInterface(autostart=False)
    return TestClient(ui.app)


def test_upload_and_download_exposed_file(tmp_path):
    client = create_client()
    # Configure storage root to a temp dir
    storage_root = tmp_path / "exposed_storage"
    os.environ["SYNTH_EXPOSED_STORAGE_ROOT"] = str(storage_root)

    # Register a file-backed exposed variable
    key = "TEST_FILE_VAR"
    register_exposed_var(key, label="Test File Var", default="", ui_type="file")

    files = {"file": ("original.txt", b"hello world", "text/plain")}
    res = client.post(f"/api/config/{key}/upload", files=files)
    assert res.status_code == 200
    payload = res.json()
    assert payload.get("status") == "ok"
    stored = payload.get("stored_path")
    assert stored
    assert os.path.exists(stored)

    # Download the file
    r2 = client.get(f"/api/config/{key}/file")
    assert r2.status_code == 200
    assert r2.content == b"hello world"
    # Content-Disposition should include original filename
    assert 'attachment; filename="original.txt"' in r2.headers.get(
        "content-disposition", ""
    )


def test_upload_to_readonly_refused(tmp_path):
    client = create_client()
    storage_root = tmp_path / "exposed_storage"
    os.environ["SYNTH_EXPOSED_STORAGE_ROOT"] = str(storage_root)

    key = "TEST_FILE_VAR_READONLY"
    register_exposed_var(
        key, label="ReadOnly File", default="", ui_type="file", readonly=True
    )

    files = {"file": ("nope.txt", b"data", "text/plain")}
    res = client.post(f"/api/config/{key}/upload", files=files)
    assert res.status_code == 403
