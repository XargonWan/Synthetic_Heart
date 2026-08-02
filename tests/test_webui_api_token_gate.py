"""POST /api/audio/upload and /api/skins/{name}/activate honor SYNTH_WEBUI_API_TOKEN.

These two endpoints live directly on the webui app (not karada_api's
rest_router) but act on the avatar / feed text into the chain, so they carry
the same optional token gate. A 404 (unknown skin) or 422 (missing multipart
file) proves the request got past auth; 401 proves it did not.
"""

import pytest
from fastapi.testclient import TestClient

from core.webui import SynthWebUIInterface


@pytest.fixture()
def client() -> TestClient:
    ui = SynthWebUIInterface(autostart=False)
    return TestClient(ui.app)


def test_endpoints_open_when_token_unset(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    monkeypatch.delenv("SYNTH_WEBUI_API_TOKEN", raising=False)
    assert client.post("/api/skins/not-a-real-skin/activate").status_code == 404
    assert client.post("/api/audio/upload").status_code == 422


def test_endpoints_reject_missing_or_wrong_token(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    monkeypatch.setenv("SYNTH_WEBUI_API_TOKEN", "sekrit")
    assert client.post("/api/skins/not-a-real-skin/activate").status_code == 401
    assert client.post("/api/audio/upload").status_code == 401
    assert (
        client.post("/api/skins/not-a-real-skin/activate?token=wrong").status_code
        == 401
    )
    assert client.post("/api/audio/upload?token=wrong").status_code == 401


def test_endpoints_accept_query_and_bearer_token(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    monkeypatch.setenv("SYNTH_WEBUI_API_TOKEN", "sekrit")
    assert (
        client.post("/api/skins/not-a-real-skin/activate?token=sekrit").status_code
        == 404
    )
    assert client.post("/api/audio/upload?token=sekrit").status_code == 422
    assert (
        client.post(
            "/api/skins/not-a-real-skin/activate",
            headers={"Authorization": "Bearer sekrit"},
        ).status_code
        == 404
    )
