from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from core.llm_failure_log import infer_failure_code
from core.webui import SynthWebUIInterface


def create_client() -> TestClient:
    ui = SynthWebUIInterface(autostart=False)
    return TestClient(ui.app)


def test_infer_failure_code_uses_validation_errors() -> None:
    code = infer_failure_code(
        "Exhausted 2 correction attempts for invalid JSON",
        correction_context={
            "errors": [
                "Unsupported type 'message_unknown' - no plugin or interface found to handle it"
            ]
        },
    )
    assert code == "unsupported_action"


def test_log_failures_api_returns_entries() -> None:
    client = create_client()
    mocked_entries = {
        "entries": [
            {
                "id": 7,
                "failure_code": "malformed_json",
                "stage": "llm_fallback",
                "reason": "Exhausted 2 correction attempts for invalid JSON",
                "interface_path": "synth_webui/demo",
                "chat_id": "demo",
                "thread_id": None,
                "engine": "openrouter",
                "model": "grok-4",
                "message_id": "abc",
                "content_preview": '{"actions": [',
                "metadata": {"errors": ["Missing 'type'"]},
                "created_at": datetime(2026, 4, 22, 12, 30, tzinfo=timezone.utc),
            }
        ],
        "page": 1,
        "per_page": 20,
        "total_count": 1,
        "total_pages": 1,
    }

    with patch(
        "core.llm_failure_log.list_failure_entries",
        AsyncMock(return_value=mocked_entries),
    ):
        response = client.get("/api/log-failures")

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["entries"][0]["failure_code"] == "malformed_json"
    assert data["entries"][0]["created_at"] == "2026-04-22T12:30:00+00:00"


def test_delete_log_failure_returns_404_when_missing() -> None:
    client = create_client()

    with patch(
        "core.llm_failure_log.delete_failure_entry",
        AsyncMock(return_value=False),
    ):
        response = client.delete("/api/log-failures/99")

    assert response.status_code == 404
    assert response.json()["detail"] == "Failure entry not found"


def test_delete_log_failure_returns_success() -> None:
    client = create_client()

    with patch(
        "core.llm_failure_log.delete_failure_entry",
        AsyncMock(return_value=True),
    ):
        response = client.delete("/api/log-failures/8")

    assert response.status_code == 200
    assert response.json() == {"success": True, "deleted": True, "id": 8}
