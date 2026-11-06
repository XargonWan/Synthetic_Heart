"""The statistics wiring: the queue consumer's counters, GET /api/stats, and the Logs > Stats tab."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core import message_queue as mq
from core import stats_registry
from core.webui import SynthWebUIInterface

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def fresh_stats(monkeypatch: pytest.MonkeyPatch):
    stats_registry.reset_for_tests()
    # The queue's in-flight bookkeeping is module state: put it back as it was, or "the engine finished an item a
    # moment ago" leaks into the idle beat tests that rely on a queue that never ran anything.
    for name, value in (
        ("_in_flight_label", None),
        ("_in_flight_meta", {}),
        ("_in_flight_failed", False),
        ("_last_item_finished_at", None),
    ):
        monkeypatch.setattr(mq, name, value)
    yield
    stats_registry.reset_for_tests()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(SynthWebUIInterface(autostart=False).app)


def test_a_finished_item_counts_as_a_turn_by_interface_and_kind() -> None:
    mq._note_item_in_flight(
        {
            "chat_id": 1,
            "interface": "vessel",
            "interface_path": "vessel/retroxr/root",
            "timestamp": 0,
        }
    )
    mq._clear_item_in_flight()
    registry = stats_registry.get_stats()
    assert registry.count("turns.ok") == 1
    assert registry.count("turns.ok", interface="vessel", kind="vessel") == 1
    assert registry.timing("turn.seconds")["count"] == 1
    assert registry.timing("turn.wait_seconds")["count"] == 1


def test_a_failed_item_counts_as_failed_and_the_next_one_starts_clean() -> None:
    mq._note_item_in_flight(
        {"chat_id": 2, "interface": "telegram_bot", "vessel_player_chat": False}
    )
    mq._note_turn_failed()
    mq._clear_item_in_flight()
    mq._note_item_in_flight({"chat_id": 3, "interface": "telegram_bot"})
    mq._clear_item_in_flight()
    registry = stats_registry.get_stats()
    assert registry.count("turns.failed", interface="telegram_bot", kind="chat") == 1
    assert registry.count("turns.ok", interface="telegram_bot", kind="chat") == 1


def test_a_player_chat_in_a_world_is_its_own_kind() -> None:
    mq._note_item_in_flight(
        {
            "chat_id": 4,
            "interface": "vessel",
            "interface_path": "vessel/retroxr",
            "vessel_player_chat": True,
        }
    )
    mq._clear_item_in_flight()
    assert (
        stats_registry.get_stats().count(
            "turns.ok", interface="vessel", kind="vessel_chat"
        )
        == 1
    )


def test_clearing_with_nothing_in_flight_counts_nothing() -> None:
    mq._clear_item_in_flight()
    assert stats_registry.get_stats().count("turns.ok") == 0


def test_api_stats_shape(client: TestClient) -> None:
    stats_registry.incr("turns.ok", interface="vessel")
    stats_registry.observe("turn.seconds", 40.0, interface="vessel", kind="vessel")
    body = client.get("/api/stats?period=3d").json()
    assert body["success"] is True
    assert body["period"]["key"] == "3d"
    assert {p["key"] for p in body["periods"]} >= {"1h", "24h", "7d", "all"}
    turns = next(sec for sec in body["sections"] if sec["source"] == "core.turns")
    cards = next(b for b in turns["blocks"] if b["type"] == "cards")["items"]
    average = next(i for i in cards if i["label"] == "average turn")
    assert average["value"] == 40.0
    assert "config" not in body  # the pacing settings live in the vessel's settings now


def test_api_stats_survives_a_bad_period(client: TestClient) -> None:
    assert client.get("/api/stats?period=banana").json()["period"]["key"] == "7d"
    assert client.get("/api/stats?days=banana").json()["success"] is True
    assert client.get("/api/stats?days=3").json()["period"]["key"] == "3d"


def test_about_summary_reports_the_turns_of_today(client: TestClient) -> None:
    stats_registry.incr("turns.ok", 2)
    stats_registry.incr("turns.failed")
    assert client.get("/api/about").json()["messages_today"] == 3


def test_the_logs_page_has_a_stats_tab_and_its_script() -> None:
    html = (ROOT / "core/webui_templates/sections/logs.html").read_text(
        encoding="utf-8"
    )
    js = (ROOT / "res/synth_webui/js/main.js").read_text(encoding="utf-8")
    assert (
        'data-logs-subtab="stats"' in html
        and 'id="logs-panel-stats"' in html
        and 'id="logs-stats-output"' in html
    )
    assert (
        "function loadStats" in js
        and "nextSubtab === 'stats'" in js
        and "/api/stats?period=" in js
    )
    # The page draws whatever sections the server sends; it holds no plugin's pacing inputs any more.
    assert "logs-stats-gap" not in js and "saveStatsPacing" not in js
    assert 'value="all"' in html
