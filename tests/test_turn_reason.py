import pytest

import core.turn_reason as turn_reason


@pytest.fixture(autouse=True)
def _clear_in_memory_store() -> None:
    """Isolate each test from the module-level in-memory fallback list."""
    turn_reason._in_memory_reason_entries.clear()


@pytest.mark.asyncio
async def test_record_reason_noop_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(turn_reason, "_reason_trail_enabled", lambda: False)

    async def _fail_ensure() -> None:
        raise AssertionError("ensure_reason_trail_table must not be called")

    monkeypatch.setattr(turn_reason, "ensure_reason_trail_table", _fail_ensure)

    await turn_reason.record_reason(
        interface_path="telegram_bot/1",
        reply_preview="hello",
        memories=[{"source": "recon"}],
    )

    assert turn_reason._in_memory_reason_entries == []


@pytest.mark.asyncio
async def test_record_reason_in_memory_fallback_round_trip(monkeypatch) -> None:
    async def _fail_ensure() -> None:
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(turn_reason, "ensure_reason_trail_table", _fail_ensure)

    await turn_reason.record_reason(
        interface_path="telegram_bot/123",
        reply_preview="hello there",
        memories=[{"source": "recon", "id": 7, "snippet": "a memory"}],
        diary_sources=[{"id": 3, "interface": "telegram_bot"}],
        emotion="joy (7.0 - moderate)",
        goal={"description": "build a cottage", "target_name": "oak_log"},
        beat_type="will",
        history_scope="local",
    )

    entries = await turn_reason.list_reasons(limit=10)
    assert len(entries) == 1

    entry = entries[0]
    assert entry["interface_path"] == "telegram_bot/123"
    assert entry["reply_preview"] == "hello there"
    assert entry["emotion"] == "joy (7.0 - moderate)"
    assert entry["beat_type"] == "will"
    assert entry["history_scope"] == "local"
    assert entry["memories"] == [{"source": "recon", "id": 7, "snippet": "a memory"}]
    assert entry["diary_sources"] == [{"id": 3, "interface": "telegram_bot"}]
    assert entry["goal"]["description"] == "build a cottage"
    assert entry["id"] < 0  # in-memory fallback ids are negative


@pytest.mark.asyncio
async def test_list_reasons_caps_and_searches(monkeypatch) -> None:
    async def _fail_ensure() -> None:
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(turn_reason, "ensure_reason_trail_table", _fail_ensure)
    monkeypatch.setattr(turn_reason, "_reason_trail_max_rows", lambda: 3)

    for i in range(5):
        await turn_reason.record_reason(
            interface_path=f"grillo/{i}",
            reply_preview=f"beat {i}",
            beat_type="grillo",
        )

    capped = await turn_reason.list_reasons()
    assert len(capped) == 3

    matched = await turn_reason.list_reasons(search="beat 2")
    assert len(matched) >= 1
    assert all("beat 2" in (e.get("reply_preview") or "") for e in matched)


def test_build_reason_summary_structural() -> None:
    summary = turn_reason.build_reason_summary(
        memories=[
            {"source": "recon", "id": 9, "content": "x" * 300},
            {"snippet": "no source", "content": "ignored"},
        ],
        diary_entries=[
            {
                "id": 1,
                "created_at": "2026-08-14T00:00:00",
                "interface": "telegram_bot",
                "content": "private diary content",
            }
        ],
        emotion={"joy": 7.0, "sadness": 2.0},
        beat_type="grillo",
        history_scope="unified",
        goal={
            "id": 5,
            "description": "mine diamonds",
            "note": "for tools",
            "target_kind": "block",
            "target_name": "diamond_ore",
            "status": "active",
            "current_step": 1,
        },
    )

    assert summary["beat_type"] == "grillo"
    assert summary["history_scope"] == "unified"

    assert summary["memories"][0]["source"] == "recon"
    assert summary["memories"][0]["id"] == 9
    assert len(summary["memories"][0]["snippet"]) <= 200
    assert summary["memories"][1] == {"snippet": "no source"}

    assert summary["diary_sources"] == [
        {"id": 1, "created_at": "2026-08-14T00:00:00", "interface": "telegram_bot"}
    ]
    assert "content" not in summary["diary_sources"][0]

    assert summary["emotion"] == "joy:7.0, sadness:2.0"
    assert summary["goal"]["target_name"] == "diamond_ore"
    assert summary["goal"]["current_step"] == 1
    assert "note" in summary["goal"]


def test_build_reason_summary_empty_inputs() -> None:
    summary = turn_reason.build_reason_summary(
        memories=None,
        diary_entries=None,
        emotion=None,
        beat_type="",
        history_scope=None,
        goal=None,
    )

    assert summary["memories"] == []
    assert summary["diary_sources"] == []
    assert summary["emotion"] is None
    assert summary["goal"] is None
    assert summary["beat_type"] is None
    assert summary["history_scope"] is None
