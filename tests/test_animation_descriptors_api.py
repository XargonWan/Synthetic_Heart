from fastapi.testclient import TestClient
from core.webui import SynthWebUIInterface
from pathlib import Path


def create_client():
    ui = SynthWebUIInterface(autostart=False)
    return TestClient(ui.app)


def test_existing_descriptor_returns_file():
    client = create_client()
    # Look Around.fbx.json exists in repo; API should return its parsed contents
    r = client.get("/api/skins/Rei/animations/idle/Look%20Around.fbx.json")
    assert r.status_code == 200
    data = r.json()
    assert data.get("play_once") is True
    assert "intro" in data


def test_missing_descriptor_returns_implicit():
    client = create_client()
    # Idle2.fbx has no .fbx.json file; API must return an implicit descriptor
    r = client.get("/api/skins/Rei/animations/idle/Idle2.fbx.json")
    assert r.status_code == 200
    data = r.json()
    # Idle animations should default to looping (play_once = False)
    assert data.get("play_once") is False


def test_invalid_animation_type_returns_404():
    client = create_client()
    r = client.get("/api/skins/Rei/animations/doesnotexist/Thing.fbx.json")
    assert r.status_code == 404


def test_malformed_descriptor_falls_back_to_implicit(tmp_path):
    # Write a malformed JSON descriptor file for Idle2.fbx.json and ensure the API returns the implicit descriptor
    client = create_client()
    desc_path = (
        Path(__file__).parent.parent
        / "skins"
        / "Rei"
        / "animations"
        / "idle"
        / "Idle2.fbx.json"
    )
    # Backup if it exists
    existed = desc_path.exists()
    if existed:
        backup = desc_path.read_text(encoding="utf-8")
    try:
        desc_path.write_text("{ this is : not valid json }", encoding="utf-8")
        r = client.get("/api/skins/Rei/animations/idle/Idle2.fbx.json")
        assert r.status_code == 200
        data = r.json()
        # Idle animations should still default to looping (play_once = False)
        assert data.get("play_once") is False
    finally:
        if existed:
            desc_path.write_text(backup, encoding="utf-8")
        else:
            try:
                desc_path.unlink()
            except Exception:
                pass
