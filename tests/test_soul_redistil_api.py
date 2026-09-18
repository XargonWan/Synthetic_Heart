"""The WebUI endpoints behind the Settings tab's "Re-distil memories" button."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from core.webui import SynthWebUIInterface


class _FakeSoulPlugin:
    """The slice of SoulPlugin the endpoints touch."""

    def __init__(self, *, started: bool = True) -> None:
        self.start_redistil = AsyncMock(
            return_value={"started": started, "reason": None, "running": started}
        )

    async def redistil_status(self) -> dict[str, Any]:
        return {
            "running": False,
            "pending": 4,
            "rewritten": 12,
            "skipped": 1,
            "failed": 0,
        }


def _client(plugin: Any, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr("core.core_initializer.PLUGIN_REGISTRY", {"soul_plugin": plugin})
    webui = SynthWebUIInterface(autostart=False)
    return TestClient(webui.app)


def test_status_reports_the_pass_and_what_is_left(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _FakeSoulPlugin()
    response = _client(plugin, monkeypatch).get("/api/soul/redistil")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["pending"] == 4
    assert payload["running"] is False


def test_posting_the_button_starts_the_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _FakeSoulPlugin()
    response = _client(plugin, monkeypatch).post("/api/soul/redistil", json={})

    assert response.status_code == 200
    assert response.json()["started"] is True
    plugin.start_redistil.assert_awaited_once_with(limit=None)


def test_an_explicit_limit_reaches_the_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _FakeSoulPlugin()
    response = _client(plugin, monkeypatch).post("/api/soul/redistil", json={"limit": 25})

    assert response.status_code == 200
    plugin.start_redistil.assert_awaited_once_with(limit=25)


def test_a_non_numeric_limit_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _FakeSoulPlugin()
    response = _client(plugin, monkeypatch).post(
        "/api/soul/redistil", json={"limit": "everything"}
    )

    assert response.status_code == 400
    plugin.start_redistil.assert_not_awaited()


def test_a_press_with_no_body_still_starts_the_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _FakeSoulPlugin()
    response = _client(plugin, monkeypatch).post("/api/soul/redistil")

    assert response.status_code == 200
    plugin.start_redistil.assert_awaited_once_with(limit=None)


def test_a_running_pass_is_reported_as_not_started(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _FakeSoulPlugin(started=False)
    response = _client(plugin, monkeypatch).post("/api/soul/redistil", json={})

    assert response.status_code == 200
    assert response.json()["started"] is False


def test_without_the_soul_plugin_the_endpoint_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.core_initializer.PLUGIN_REGISTRY", {})
    webui = SynthWebUIInterface(autostart=False)
    client = TestClient(webui.app)

    assert client.get("/api/soul/redistil").status_code == 503
    assert client.post("/api/soul/redistil", json={}).status_code == 503
