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
        self.rowcount = 1

    async def execute(self, sql: str, params: tuple = ()) -> None:
        self._log.append((sql, params))

    async def fetchall(self) -> list[dict[str, Any]]:
        return self._rows

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
