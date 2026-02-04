import json
from fastapi.testclient import TestClient
from core.webui import SynthWebUIInterface


def create_client():
    ui = SynthWebUIInterface(autostart=False)
    return TestClient(ui.app)


def test_existing_descriptor_returns_file():
    client = create_client()
    # Look Around.fbx.json exists in repo; API should return its parsed contents
    r = client.get('/api/skins/Rei/animations/idle/Look%20Around.fbx.json')
    assert r.status_code == 200
    data = r.json()
    assert data.get('play_once') is True
    assert 'intro' in data


def test_missing_descriptor_returns_implicit():
    client = create_client()
    # Idle2.fbx has no .fbx.json file; API must return an implicit descriptor
    r = client.get('/api/skins/Rei/animations/idle/Idle2.fbx.json')
    assert r.status_code == 200
    data = r.json()
    # Idle animations should default to looping (play_once = False)
    assert data.get('play_once') is False


def test_invalid_animation_type_returns_404():
    client = create_client()
    r = client.get('/api/skins/Rei/animations/doesnotexist/Thing.fbx.json')
    assert r.status_code == 404
