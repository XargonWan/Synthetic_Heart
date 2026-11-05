"""The WebUI endpoints behind the Settings tab's "Run nightly compaction now" button.

The panel exists so the nightly pass can be exercised without waiting a night for it:
these tests pin the shape it reads (running, the last summary, the preview) and the fact
that a press only ever starts a background pass.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from core.webui import SynthWebUIInterface


class _FakeCompactor:
    """The slice of the Grillo compactor the endpoints touch."""

    def __init__(self, *, started: bool = True) -> None:
        self.start_compaction_now = AsyncMock(
            return_value={"started": started, "reason": None, "running": started}
        )

    async def compaction_status(self) -> dict[str, Any]:
        return {
            "running": False,
            "dry_run": False,
            "started_at": "2026-09-27T05:00:03+00:00",
            "finished_at": "2026-09-27T05:02:22+00:00",
            "error": None,
            "summary": {"persisted": 5, "skipped_covered": 4, "model_calls": 5},
            "preview": {"days": 10, "covered": 39, "remaining": 0, "age_days": 2},
        }


def _client(plugin: Any, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(
        "core.core_initializer.PLUGIN_REGISTRY", {"grillo_compactor": plugin}
    )
    webui = SynthWebUIInterface(autostart=False)
    return TestClient(webui.app)


def test_status_reports_the_last_pass_and_what_is_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _FakeCompactor()
    response = _client(plugin, monkeypatch).get("/api/grillo/compaction")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["summary"]["persisted"] == 5
    assert payload["summary"]["skipped_covered"] == 4
    assert payload["preview"]["covered"] == 39
    assert payload["running"] is False


def test_posting_the_button_starts_the_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _FakeCompactor()
    response = _client(plugin, monkeypatch).post("/api/grillo/compaction", json={})

    assert response.status_code == 200
    assert response.json()["started"] is True
    plugin.start_compaction_now.assert_awaited_once_with(dry_run=False)


def test_a_dry_run_press_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _FakeCompactor()
    response = _client(plugin, monkeypatch).post(
        "/api/grillo/compaction", json={"dry_run": True}
    )

    assert response.status_code == 200
    plugin.start_compaction_now.assert_awaited_once_with(dry_run=True)


def test_a_second_press_is_reported_not_queued(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _FakeCompactor(started=False)
    response = _client(plugin, monkeypatch).post("/api/grillo/compaction", json={})

    assert response.status_code == 200
    assert response.json()["started"] is False


def test_without_the_plugin_the_panel_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.core_initializer.PLUGIN_REGISTRY", {})
    webui = SynthWebUIInterface(autostart=False)
    client = TestClient(webui.app)

    assert client.get("/api/grillo/compaction").status_code == 503
    assert client.post("/api/grillo/compaction", json={}).status_code == 503
