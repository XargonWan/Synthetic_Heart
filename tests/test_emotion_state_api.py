from fastapi.testclient import TestClient
from core.webui import SynthWebUIInterface


def create_client():
    ui = SynthWebUIInterface(autostart=False)
    return TestClient(ui.app)


def test_get_emotion_state_exists_and_returns_object():
    client = create_client()
    r = client.get("/api/emotion_state")
    assert r.status_code == 200
    data = r.json()
    # Endpoint should always return an object with 'emotions' key (may be empty)
    assert isinstance(data, dict)
    assert "emotions" in data
    assert isinstance(data["emotions"], dict)
