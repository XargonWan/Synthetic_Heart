"""The structural summary of what drove a turn (it feeds the turn card)."""

import core.turn_reason as turn_reason


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
