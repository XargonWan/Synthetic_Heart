import shutil
from pathlib import Path

from core.webui import SynthWebUIInterface


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
