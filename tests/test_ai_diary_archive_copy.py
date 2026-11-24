from __future__ import annotations

from typing import Any

import pytest

import plugins.ai_diary as ai_diary

_ROW: dict[str, Any] = {
    "id": 7,
    "content": "c",
    "personal_thought": "t",
    "emotions": "[]",
    "interaction_summary": "s",
    "created_at": "2026-03-01",
    "interface": "telegram_bot",
    "chat_id": "1",
    "thread_id": None,
    "user_message": "m",
    "context_tags": "[]",
    "involved_users": '["Ann"]',
}


def _capture(monkeypatch: pytest.MonkeyPatch, fail_first: bool = False):
    calls: list[tuple[str, tuple]] = []

    async def _execute(sql: str, params: tuple = ()) -> None:
        calls.append((sql, params))
        if fail_first and len(calls) == 1:
            raise RuntimeError("column involved_users does not exist")

    monkeypatch.setattr(ai_diary, "_execute", _execute)
    monkeypatch.setattr(ai_diary, "_run", lambda coro: __import__("asyncio").run(coro))
    return calls


def test_archiving_a_row_keeps_who_it_was_about(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture(monkeypatch)

    ai_diary._copy_diary_row("ai_diary_archive", _ROW)

    assert len(calls) == 1
    sql, params = calls[0]
    assert sql.startswith("INSERT INTO ai_diary_archive")
    assert "involved_users" in sql
    assert params[-1] == '["Ann"]'


def test_a_schema_without_the_column_still_archives_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture(monkeypatch, fail_first=True)

    ai_diary._copy_diary_row("ai_diary_archive", _ROW)

    assert len(calls) == 2
    assert "involved_users" not in calls[1][0]
    assert calls[1][1][0] == 7
