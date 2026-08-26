"""Tests for the Rift Vessel diary compactor (no real DB / LLM)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from core import vessel_diary_compactor as vdc


def _buf(n: int, prefix: str = "moment") -> list[dict[str, Any]]:
    """Build a buffer of ``n`` experience items with distinct summaries."""
    return [{"event_type": "sighting", "summary": f"{prefix} {i}"} for i in range(n)]


class _FakeEngine:
    """Engine stub that echoes chunk/fold payloads back as JSON."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate_response(self, prompt: dict[str, Any]) -> str:
        self.calls.append(prompt)
        payload = prompt["input"]["payload"]
        if "moments" in payload:
            # One chunk → one partial. Echo the count so we can assert splitting.
            n = len(payload["moments"])
            return json.dumps({"partial": f"partial-of-{n}"})
        # Fold: join the fragments deterministically.
        frags = payload["fragments"]
        return json.dumps({"entry": "FOLDED:" + "|".join(frags)})


class _BrokenEngine:
    """Engine stub that always raises (LLM failure path)."""

    async def generate_response(self, prompt: dict[str, Any]) -> str:
        raise RuntimeError("boom")


def _patch_engine(monkeypatch: Any, engine: Any) -> None:
    async def _resolve() -> Any:
        return engine

    monkeypatch.setattr(vdc, "_resolve_engine", _resolve)
    monkeypatch.setattr(vdc, "_persona_hint", lambda: ("Rekku", ""))


def test_buffer_to_lines_skips_empty_and_prefixes_event_type() -> None:
    lines = vdc._buffer_to_lines(
        [
            {"event_type": "chat", "summary": "hi"},
            {"summary": ""},  # skipped
            {"summary": "no type"},
            "not a dict",  # skipped
        ]
    )
    assert lines == ["[chat] hi", "no type"]


def test_chunk_lines_respects_item_limit() -> None:
    lines = [f"l{i}" for i in range(10)]
    chunks = vdc._chunk_lines(lines, chunk_items=4, chunk_chars=100_000)
    assert [len(c) for c in chunks] == [4, 4, 2]


def test_chunk_lines_respects_char_budget() -> None:
    lines = ["x" * 50 for _ in range(5)]
    # Each line ~51 chars incl. newline; a 120-char budget fits 2 per chunk.
    chunks = vdc._chunk_lines(lines, chunk_items=100, chunk_chars=120)
    assert [len(c) for c in chunks] == [2, 2, 1]


@pytest.mark.asyncio
async def test_compact_empty_buffer_returns_none(monkeypatch: Any) -> None:
    _patch_engine(monkeypatch, _FakeEngine())
    result = await vdc.compact_session(
        session_id="s", environment="mc", interface_path=None, buffer=[], reason="x"
    )
    assert result is None


@pytest.mark.asyncio
async def test_compact_single_chunk(monkeypatch: Any) -> None:
    engine = _FakeEngine()
    _patch_engine(monkeypatch, engine)
    monkeypatch.setattr(vdc, "_resolve_chunk_config", lambda: (40, 6000))
    result = await vdc.compact_session(
        session_id="s",
        environment="mc",
        interface_path=None,
        buffer=_buf(3),
        reason="logout",
    )
    # Single chunk → single partial, no fold needed.
    assert result == "partial-of-3"
    assert len(engine.calls) == 1


@pytest.mark.asyncio
async def test_compact_multi_chunk_folds(monkeypatch: Any) -> None:
    engine = _FakeEngine()
    _patch_engine(monkeypatch, engine)
    # Force small chunks so a 10-item buffer splits into 3 chunks.
    monkeypatch.setattr(vdc, "_resolve_chunk_config", lambda: (4, 100_000))
    result = await vdc.compact_session(
        session_id="s",
        environment="mc",
        interface_path=None,
        buffer=_buf(10),
        reason="cooldown",
    )
    # 3 chunk calls + 1 fold call.
    assert result is not None
    assert result.startswith("FOLDED:")
    assert result == "FOLDED:partial-of-4|partial-of-4|partial-of-2"
    assert len(engine.calls) == 4


@pytest.mark.asyncio
async def test_compact_llm_failure_falls_back(monkeypatch: Any) -> None:
    _patch_engine(monkeypatch, _BrokenEngine())
    monkeypatch.setattr(vdc, "_resolve_chunk_config", lambda: (40, 6000))
    result = await vdc.compact_session(
        session_id="s",
        environment="mc",
        interface_path=None,
        buffer=_buf(2),
        reason="logout",
    )
    # Per-chunk failure degrades to raw chunk text, and the fold is skipped
    # (single chunk). The deterministic fallback join is returned.
    assert result is not None
    assert "moment 0" in result
    assert "moment 1" in result


@pytest.mark.asyncio
async def test_compact_no_engine_uses_fallback(monkeypatch: Any) -> None:
    async def _no_engine() -> Any:
        return None

    monkeypatch.setattr(vdc, "_resolve_engine", _no_engine)
    monkeypatch.setattr(vdc, "_persona_hint", lambda: ("Rekku", ""))
    result = await vdc.compact_session(
        session_id="s",
        environment="mc",
        interface_path=None,
        buffer=_buf(2),
        reason="logout",
    )
    assert result is not None
    assert "Lived experience in mc" in result
    assert "moment 0" in result


class _SaveCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.lastrowid = 42

    async def __aenter__(self) -> "_SaveCursor":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, query: str, params: Any = None) -> None:
        self.executed.append((query, params))

    async def fetchone(self) -> Any:
        # Postgres RETURNING id path: yield the new row id.
        return (42,)


class _SaveConn:
    def __init__(self, cursor: _SaveCursor) -> None:
        self._cursor = cursor
        self.committed = False

    async def __aenter__(self) -> "_SaveConn":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def cursor(self, *_a: Any, **_k: Any) -> _SaveCursor:
        return self._cursor

    async def commit(self) -> None:
        self.committed = True


@pytest.mark.asyncio
async def test_save_vessel_diary_inserts_and_returns_id(monkeypatch: Any) -> None:
    cursor = _SaveCursor()
    conn = _SaveConn(cursor)
    monkeypatch.setattr(vdc, "get_conn_ctx", lambda: conn)

    new_id = await vdc.save_vessel_diary(
        session_id="s-1",
        environment="mc",
        interface_path="vessel/minecraft",
        summary="I mined a block and felt alive.",
        moments_count=7,
        reason="logout",
    )

    assert new_id == 42
    assert conn.committed is True
    query, params = cursor.executed[0]
    assert "INSERT INTO vessel_diary" in query
    assert params[0] == "s-1"
    assert params[4] == 7


@pytest.mark.asyncio
async def test_save_vessel_diary_empty_summary_returns_none(monkeypatch: Any) -> None:
    called = False

    def _boom() -> Any:  # pragma: no cover - must not be reached
        nonlocal called
        called = True
        raise AssertionError("get_conn_ctx must not be called on empty summary")

    monkeypatch.setattr(vdc, "get_conn_ctx", _boom)
    result = await vdc.save_vessel_diary(
        session_id="s",
        environment="mc",
        interface_path=None,
        summary="",
        moments_count=0,
        reason="logout",
    )
    assert result is None
    assert called is False
