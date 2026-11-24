from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import plugins.bio_manager as bio_manager
from core import bio_people as people_admin


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]], log: list[tuple[str, tuple]]):
        self._rows = rows
        self._log = log
        self._last_sql = ""
        self.rowcount = 1

    async def execute(self, sql: str, params: tuple = ()) -> None:
        self._last_sql = sql
        self._log.append((sql, params))

    async def fetchall(self) -> list[dict[str, Any]]:
        # The chat-history lookup (names the chat has for people) is its own query.
        return [] if "chat_history_cache" in self._last_sql else self._rows

    async def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    async def __aenter__(self) -> "_Cursor":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Conn:
    def __init__(self, rows: list[dict[str, Any]]):
        self.log: list[tuple[str, tuple]] = []
        self._rows = rows

    def cursor(self, *_: object) -> _Cursor:
        return _Cursor(self._rows, self.log)

    async def __aenter__(self) -> "_Conn":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def test_ids_are_classified_so_people_can_be_told_from_plumbing() -> None:
    assert people_admin.classify_person_id("31321637") == "person"
    assert people_admin.classify_person_id("ollama-user-ollama") == "api"
    assert (
        people_admin.classify_person_id("9a1b2c3d-1111-2222-3333-444455556666")
        == "session"
    )
    assert (
        people_admin.classify_person_id("matrix:9a1b2c3d-1111-2222-3333-444455556666")
        == "session"
    )
    assert people_admin.classify_person_id("some-name") == "other"


@pytest.mark.asyncio
async def test_list_people_summarises_without_leaking_the_full_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _Conn(
        [
            {
                "id": "123",
                "user_name": "Ann",
                "known_as": '["Annie"]',
                "information": "x" * 400,
                "privacy": "private",
                "last_accessed": "2026-11-01",
                "last_update": "2026-10-01",
                "update_count": 2,
            }
        ]
    )
    monkeypatch.setattr(people_admin, "get_conn_ctx", lambda: conn)

    people = await people_admin.list_people()

    assert people[0]["id"] == "123"
    assert people[0]["known_as"] == ["Annie"]
    assert len(people[0]["information_preview"]) == 160
    assert people[0]["privacy"] == "private"
    assert "information" not in people[0]


@pytest.mark.asyncio
async def test_privacy_only_accepts_known_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _Conn([])
    monkeypatch.setattr(people_admin, "get_conn_ctx", lambda: conn)

    assert await people_admin.set_privacy("123", "private")
    assert conn.log[-1][1] == ("private", "123")
    with pytest.raises(ValueError):
        await people_admin.set_privacy("123", "public")


@pytest.mark.asyncio
async def test_delete_person_reports_whether_anyone_was_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _Conn([])
    monkeypatch.setattr(people_admin, "get_conn_ctx", lambda: conn)
    assert await people_admin.delete_person("nobody") is False
    assert not any("DELETE" in sql for sql, _ in conn.log)

    conn = _Conn([{"id": "123", "information": "hi", "privacy": "default"}])
    monkeypatch.setattr(people_admin, "get_conn_ctx", lambda: conn)
    assert await people_admin.delete_person("123") is True
    assert any("DELETE FROM bio" in sql for sql, _ in conn.log)


@pytest.mark.asyncio
async def test_a_private_profile_is_only_given_to_the_person_it_is_about(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = bio_manager.BioPlugin()

    async def _resolve(target: str) -> str:
        return "123"

    async def _full(user_id: str) -> dict[str, Any]:
        return {
            "id": user_id,
            "privacy": "private",
            "information": "a confidence",
            "contacts": {"phone": "000"},
        }

    monkeypatch.setattr(plugin, "_resolve_target_async", _resolve)
    monkeypatch.setattr(bio_manager, "_get_bio_full_async", _full)
    action = {"type": "bio_full_request", "payload": {"targets": ["Ann"]}}

    stranger = SimpleNamespace(from_user=SimpleNamespace(id=999))
    result = await plugin.execute_action(action, {}, None, stranger)
    assert result["data"][0]["information"] == ""
    assert "contacts" not in result["data"][0]

    owner = SimpleNamespace(from_user=SimpleNamespace(id=123))
    result = await plugin.execute_action(action, {}, None, owner)
    assert result["data"][0]["information"] == "a confidence"


class _TwoTableConn(_Conn):
    """``bio`` rows for the profile query, ``seen`` rows for the chat-history one."""

    def __init__(self, bio_rows: list[dict[str, Any]], seen: list[dict[str, Any]]):
        super().__init__(bio_rows)
        self._seen = seen

    def cursor(self, *_: object) -> _Cursor:
        outer = self

        class _C(_Cursor):
            async def fetchall(self) -> list[dict[str, Any]]:
                return (
                    outer._seen
                    if "chat_history_cache" in self._last_sql
                    else outer._rows
                )

        return _C(self._rows, self.log)


@pytest.mark.asyncio
async def test_a_nameless_profile_is_shown_under_the_name_the_chat_has_for_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All live profiles had no stored name, so the People tab showed nobody."""
    conn = _TwoTableConn(
        [
            {"id": "1", "user_name": "", "known_as": "[]", "privacy": "default"},
            {"id": "2", "user_name": "Ann", "known_as": "[]", "privacy": "default"},
            {"id": "3", "user_name": "", "known_as": '["Bee"]', "privacy": "default"},
            {"id": "4", "user_name": "", "known_as": "[]", "privacy": "default"},
        ],
        [{"sender_id": "1", "sender_name": "Jay Cheshire"}],
    )
    monkeypatch.setattr(people_admin, "get_conn_ctx", lambda: conn)

    people = {p["id"]: p for p in await people_admin.list_people()}

    assert people["1"]["display_name"] == "Jay Cheshire"
    assert people["1"]["user_name"] == "", "the stored name is not invented"
    assert people["2"]["display_name"] == "Ann"
    assert people["3"]["display_name"] == "Bee"
    assert people["4"]["display_name"] == ""
    # Only the nameless profiles without a nickname are looked up in the chat history.
    lookup = [params for sql, params in conn.log if "chat_history_cache" in sql]
    assert lookup == [("1", "4")]


@pytest.mark.asyncio
async def test_the_display_name_the_platform_reports_is_stored_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed: list[tuple[str, tuple]] = []

    async def _execute(sql: str, params: tuple = ()) -> None:
        executed.append((sql, params))

    monkeypatch.setattr(bio_manager, "_execute", _execute)

    await bio_manager._remember_display_name_async("31321637", "  Jay   Cheshire ")
    await bio_manager._remember_display_name_async("-1", "Web Search")
    await bio_manager._remember_display_name_async("5", "")

    assert len(executed) == 1
    sql, params = executed[0]
    assert params == ("Jay Cheshire", "31321637")
    assert "user_name IS NULL OR user_name = ''" in sql, (
        "a set name is never overwritten"
    )
