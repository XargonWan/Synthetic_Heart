from fastapi.testclient import TestClient
from core.webui import SynthWebUIInterface


def create_client():
    ui = SynthWebUIInterface(autostart=False)
    return TestClient(ui.app)


def test_history_chat_returns_messages_and_interface_paths(monkeypatch):
    # Mock get_conn_ctx to avoid connecting to the live database during test execution
    from tests.test_grillo_beat_system import _create_mock_db_context

    mock_ctx, _ = _create_mock_db_context()
    monkeypatch.setattr("core.db.get_conn_ctx", lambda: mock_ctx)

    client = create_client()
    # Simpler: directly test the endpoint when no messages exist returns success and empty lists
    r = client.get("/api/history/chat")
    assert r.status_code == 200
    d = r.json()
    assert "messages" in d and isinstance(d["messages"], list)
    assert "interface_paths" in d and isinstance(d["interface_paths"], list)
