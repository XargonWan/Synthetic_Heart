"""The WebUI auth gate: every route, token sources, cookie sessions, throttling."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from core import webui_auth
from core.webui import SynthWebUIInterface

TOKEN = "unit-test-token"
PROBE = "/api/skins/not-a-real-skin/activate"  # 404 once past auth, 401 before


@pytest.fixture(autouse=True)
def _enable_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SYNTH_WEBUI_AUTH", raising=False)
    monkeypatch.setenv("SYNTH_WEBUI_API_TOKEN", TOKEN)
    monkeypatch.setenv("SYNTH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(webui_auth, "_cached_token", None)
    webui_auth._failures.clear()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(SynthWebUIInterface(autostart=False).app, follow_redirects=False)


def test_unauthenticated_api_is_rejected(client: TestClient) -> None:
    assert client.post(PROBE).status_code == 401
    assert client.get("/api/config").status_code == 401
    assert client.post("/api/agent/run", json={}).status_code == 401


def test_browser_navigation_redirects_to_login(client: TestClient) -> None:
    resp = client.get("/", headers={"accept": "text/html"})
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login?next=")


def test_bearer_and_query_token_accepted(client: TestClient) -> None:
    assert (
        client.post(PROBE, headers={"Authorization": f"Bearer {TOKEN}"}).status_code
        == 404
    )
    assert client.post(f"{PROBE}?token={TOKEN}").status_code == 404
    assert (
        client.post(PROBE, headers={"Authorization": "Bearer nope"}).status_code == 401
    )


def test_login_sets_session_cookie(client: TestClient) -> None:
    bad = client.post("/login", data={"token": "wrong"})
    assert bad.status_code == 401

    ok = client.post("/login", data={"token": TOKEN, "next": "/diary"})
    assert ok.status_code == 303
    assert ok.headers["location"] == "/diary"
    cookie = ok.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie

    # The cookie alone now authenticates (TestClient keeps the jar).
    assert client.post(PROBE).status_code == 404


def test_login_next_cannot_redirect_off_site(client: TestClient) -> None:
    resp = client.post("/login", data={"token": TOKEN, "next": "//evil.example"})
    assert resp.headers["location"] == "/"


def test_cookie_session_blocks_cross_origin_writes(client: TestClient) -> None:
    client.post("/login", json={"token": TOKEN})
    foreign = client.post(PROBE, headers={"Origin": "https://evil.example"})
    assert foreign.status_code == 403
    same = client.post(PROBE, headers={"Origin": "http://testserver"})
    assert same.status_code == 404


def test_bearer_requests_skip_origin_check(client: TestClient) -> None:
    resp = client.post(
        PROBE,
        headers={"Authorization": f"Bearer {TOKEN}", "Origin": "https://other.example"},
    )
    assert resp.status_code == 404


def test_public_paths_stay_reachable(client: TestClient) -> None:
    assert client.get("/login").status_code == 200
    assert client.get("/health").status_code != 401
    assert client.get("/api/plugins/minecraft_vessel/skin.png").status_code != 401


def test_websocket_requires_credentials(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/logs"):
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass


def test_repeated_failures_are_throttled(client: TestClient) -> None:
    for _ in range(webui_auth._FAIL_LIMIT):
        client.post(PROBE, headers={"Authorization": "Bearer nope"})
    resp = client.post(PROBE, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 429


def test_auto_generated_token_is_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SYNTH_WEBUI_API_TOKEN", raising=False)
    monkeypatch.setenv("SYNTH_WEBUI_AUTH", "enabled")
    token = webui_auth.configured_token()
    assert token and len(token) >= 32
    saved = tmp_path / webui_auth.TOKEN_FILE_NAME
    assert saved.read_text().strip() == token
    assert (saved.stat().st_mode & 0o777) == 0o600
    monkeypatch.setattr(webui_auth, "_cached_token", None)
    assert webui_auth.configured_token() == token


def test_auth_is_off_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SYNTH_WEBUI_API_TOKEN", raising=False)
    monkeypatch.delenv("SYNTH_WEBUI_AUTH", raising=False)
    assert webui_auth.configured_token() is None
    assert not (tmp_path / webui_auth.TOKEN_FILE_NAME).exists()
    open_client = TestClient(SynthWebUIInterface(autostart=False).app)
    assert open_client.post(PROBE).status_code == 404


def test_explicit_token_enables_the_gate_without_the_switch(
    client: TestClient,
) -> None:
    # _enable_auth sets only SYNTH_WEBUI_API_TOKEN; that alone must turn it on.
    assert client.post(PROBE).status_code == 401
