from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any, cast

import pytest

from plugins.soul_plugin import SoulPlugin
from plugins.soul_plugin import _SessionState
from core.soul.emotion_engine import EmotionalEngine
from core.soul.models import EmotionalProfile, EmotionalTag, MemCell, MemCellRecall
from core.soul.repository import InMemorySoulRepository, PostgresSoulRepository


def test_soul_plugin_is_enabled_uses_config_gate() -> None:
    plugin = SoulPlugin.__new__(SoulPlugin)

    with patch.object(SoulPlugin, "_is_enabled", return_value=False):
        assert plugin.is_enabled() is False

    with patch.object(SoulPlugin, "_is_enabled", return_value=True):
        assert plugin.is_enabled() is True


@pytest.mark.asyncio
async def test_static_injection_contains_soul_keys() -> None:
    plugin = SoulPlugin()

    message = SimpleNamespace(
        interface_path="telegram_bot/123",
        text="I feel anxious about 2026-04-24 but also happy you are here.",
        caption=None,
    )

    payload = await plugin.get_static_injection(
        message, {"interface_path": "telegram_bot/123"}
    )

    assert "soul_user_profile" in payload
    assert "soul_session_state" in payload
    assert "soul_turn_emotion_delta" in payload
    assert "soul_active_foresight" in payload


@pytest.mark.asyncio
async def test_static_injection_provides_passive_context_for_grillo_beat() -> None:
    plugin = SoulPlugin()
    plugin._repo = SimpleNamespace(
        get_active_dsp=AsyncMock(return_value=None),
        list_active_foresight_signals=AsyncMock(return_value=[]),
    )
    recalled = ["[SOUL recalled memory | 2026-05-07] recent memory text"]
    recall_memories_mock = AsyncMock(return_value=recalled)
    plugin._recall_memories = cast(Any, recall_memories_mock)

    message = SimpleNamespace(
        interface_path="grillo/-1",
        text="[G.R.I.L.L.O. Memory Consolidation]",
        caption=None,
    )

    payload = await plugin.get_static_injection(
        message, {"interface_path": "grillo/-1", "grillo_beat": True}
    )

    assert "soul_recalled_memories" in payload
    assert payload["soul_recalled_memories"] == recalled
    assert "soul_user_profile" in payload
    assert "soul_session_state" in payload
    assert "grillo/beat" in str(payload["soul_session_state"])
    assert payload["soul_active_foresight"] == []
    # DSP and foresight fetched passively; recall runs with beat prompt text
    plugin._repo.get_active_dsp.assert_awaited_once()
    plugin._repo.list_active_foresight_signals.assert_awaited_once()
    recall_memories_mock.assert_awaited_once()
    # Session tracking must NOT happen — no _sessions entry for grillo beats
    assert "grillo/-1" not in plugin._sessions
    assert "grillo/beat" not in plugin._sessions


@pytest.mark.asyncio
async def test_force_compile_clears_interface_buffer() -> None:
    plugin = SoulPlugin()

    message = SimpleNamespace(
        interface_path="telegram_bot/555",
        text="I need to remember the event on 2026-04-20",
        caption=None,
    )
    await plugin.get_static_injection(message, {"interface_path": "telegram_bot/555"})

    assert plugin._buffers["telegram_bot/555"]

    result = await plugin.execute_action(
        {
            "type": "soul_force_compile",
            "payload": {"interface_path": "telegram_bot/555"},
        },
        {},
        None,
        message,
    )

    assert result["compiled_memcells"] >= 1
    assert plugin._buffers["telegram_bot/555"] == []


@pytest.mark.asyncio
async def test_static_injection_recalls_relevant_memories() -> None:
    plugin = SoulPlugin()
    interface_path = "telegram_bot/999"

    seed_message = SimpleNamespace(
        interface_path=interface_path,
        text="Scarlet loves jasmine tea and cozy rainy evenings.",
        caption=None,
    )
    await plugin.get_static_injection(seed_message, {"interface_path": interface_path})
    await plugin._compile_interface(interface_path)

    recall_message = SimpleNamespace(
        interface_path=interface_path,
        text="What tea does Scarlet love again?",
        caption=None,
    )
    payload = await plugin.get_static_injection(
        recall_message, {"interface_path": interface_path}
    )

    recalled = payload.get("soul_recalled_memories")

    assert isinstance(recalled, list)
    recalled_entries = [str(entry) for entry in recalled]
    assert any("jasmine tea" in entry.lower() for entry in recalled_entries)


def test_format_recalled_memory_marks_entry_as_recalled() -> None:
    plugin = SoulPlugin()
    now = datetime.now(timezone.utc)
    emotional_tag = EmotionalTag(
        state_snapshot={"joy": 0.2, "fear": 0.0, "sad": 0.0, "anger": 0.0},
        dominant_emotion="joy",
        intensity=0.2,
        valence=0.2,
    )
    cell = MemCell(
        id="memory-1",
        episodic_trace="Scarlet mentioned jasmine tea.",
        atomic_facts=["Scarlet|likes|jasmine tea"],
        emotional_tag=emotional_tag,
        foresight_signals=[],
        timestamp=now,
        session_id="telegram_bot/999",
    )
    match = MemCellRecall(
        cell=cell,
        similarity=0.9,
        lexical_score=0.8,
        score=0.9,
    )

    formatted = plugin._format_recalled_memory(
        match,
        active_session_id="telegram_bot/999",
    )

    assert formatted.startswith("[SOUL recalled memory | ")
    assert "same chat" in formatted
    assert "jasmine tea" in formatted.lower()


@pytest.mark.asyncio
async def test_static_injection_excludes_diary_merge_housekeeping_memories() -> None:
    plugin = SoulPlugin()
    interface_path = "telegram_bot/321"
    now = datetime.now(timezone.utc)
    emotional_tag = EmotionalTag(
        state_snapshot={"joy": 0.1, "fear": 0.0, "sad": 0.0, "anger": 0.0},
        dominant_emotion="joy",
        intensity=0.1,
        valence=0.1,
    )
    internal_cell = MemCell(
        id="internal",
        episodic_trace=(
            "[DIARY CONSOLIDATION - INTERNAL SYSTEM TASK] Rewrite merged diary entry"
        ),
        atomic_facts=[
            "Conversation summary|is|[DIARY CONSOLIDATION - INTERNAL SYSTEM TASK]"
        ],
        emotional_tag=emotional_tag,
        foresight_signals=[],
        timestamp=now,
        session_id="diary_merge:-1",
    )
    normal_cell = MemCell(
        id="normal",
        episodic_trace="Scarlet loves jasmine tea on rainy evenings.",
        atomic_facts=["Scarlet|likes|jasmine tea"],
        emotional_tag=emotional_tag,
        foresight_signals=[],
        timestamp=now,
        session_id="telegram_bot:321",
    )

    plugin._compiler = SimpleNamespace(
        embedder=SimpleNamespace(embed=AsyncMock(return_value=[0.25, 0.75]))
    )
    plugin._repo = SimpleNamespace(
        get_active_dsp=AsyncMock(return_value=None),
        list_active_foresight_signals=AsyncMock(return_value=[]),
        recall_memories=AsyncMock(
            return_value=[
                MemCellRecall(
                    cell=internal_cell,
                    similarity=0.95,
                    lexical_score=0.9,
                    score=0.95,
                ),
                MemCellRecall(
                    cell=normal_cell,
                    similarity=0.9,
                    lexical_score=0.8,
                    score=0.9,
                ),
            ]
        ),
        upsert_memcell=AsyncMock(return_value=None),
    )

    payload = await plugin.get_static_injection(
        SimpleNamespace(
            interface_path=interface_path,
            text="What tea does Scarlet love?",
            caption=None,
        ),
        {"interface_path": interface_path},
    )

    recalled_raw = payload.get("soul_recalled_memories", [])
    assert isinstance(recalled_raw, list)
    recalled = [str(entry) for entry in recalled_raw]

    assert len(recalled) == 1
    assert "jasmine tea" in recalled[0].lower()
    assert all("diary consolidation" not in entry.lower() for entry in recalled)


@pytest.mark.asyncio
async def test_scheduler_tick_compiles_idle_sessions() -> None:
    plugin = SoulPlugin()

    iface = "telegram_bot/77"
    plugin._buffers[iface] = ["hello", "event 2026-04-30"]
    session = _SessionState()
    plugin._sessions[iface] = session

    session.last_seen = datetime.now(timezone.utc) - timedelta(hours=1)

    await plugin._tick_scheduler()

    assert plugin._buffers[iface] == []


@pytest.mark.asyncio
async def test_compile_interface_throttles_async_consolidate() -> None:
    plugin = SoulPlugin()
    iface = "telegram_bot/77"
    compiler = SimpleNamespace(
        post_session_compile=AsyncMock(return_value=["cell-1"]),
        async_consolidate=AsyncMock(return_value=["scene-1"]),
    )
    plugin._compiler = compiler

    plugin._buffers[iface] = ["first memory"]
    await plugin._compile_interface(iface)

    plugin._buffers[iface] = ["second memory"]
    await plugin._compile_interface(iface)

    assert compiler.async_consolidate.await_count == 1


@pytest.mark.asyncio
async def test_force_compile_bypasses_consolidation_cooldown() -> None:
    plugin = SoulPlugin()
    iface = "telegram_bot/88"
    compiler = SimpleNamespace(
        post_session_compile=AsyncMock(return_value=["cell-1"]),
        async_consolidate=AsyncMock(return_value=["scene-1"]),
    )
    plugin._compiler = compiler

    plugin._buffers[iface] = ["first memory"]
    await plugin._compile_interface(iface)

    plugin._buffers[iface] = ["second memory"]
    await plugin._force_compile(interface_path=iface)

    assert compiler.async_consolidate.await_count == 2


def test_repository_backend_postgres_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        SoulPlugin, "_get_repository_backend", staticmethod(lambda: "postgres")
    )
    monkeypatch.setattr(
        SoulPlugin,
        "_get_postgres_dsn",
        staticmethod(lambda: "postgresql://soul:soul@localhost:5432/soul_memory"),
    )

    plugin = SoulPlugin()

    assert isinstance(plugin._repo, PostgresSoulRepository)


def test_repository_backend_postgres_falls_back_without_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        SoulPlugin, "_get_repository_backend", staticmethod(lambda: "postgres")
    )
    monkeypatch.setattr(SoulPlugin, "_get_postgres_dsn", staticmethod(lambda: ""))

    plugin = SoulPlugin()

    assert isinstance(plugin._repo, InMemorySoulRepository)


class _FakeAcquire:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    async def __aenter__(self) -> object:
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeRecallConn:
    def __init__(self, *, vector_rows: list[dict], text_rows: list[dict]) -> None:
        self.vector_rows = vector_rows
        self.text_rows = text_rows
        self.queries: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        self.queries.append((sql, args))
        if "ORDER BY v.embedding <=> $1::vector ASC" in sql:
            return list(self.vector_rows)
        if "ORDER BY GREATEST(" in sql:
            return list(self.text_rows)
        raise AssertionError(sql)


class _FakeRecallPool:
    def __init__(self, conn: _FakeRecallConn) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


def _build_recall_row(
    *,
    cell_id: str,
    session_id: str,
    episodic_trace: str,
    atomic_facts: list[str],
    vector_similarity: float,
) -> dict:
    return {
        "id": cell_id,
        "session_id": session_id,
        "episodic_trace": episodic_trace,
        "atomic_facts": atomic_facts,
        "emotional_tag": {
            "state_snapshot": {"joy": 0.2, "fear": 0.0, "sad": 0.0, "anger": 0.0},
            "dominant_emotion": "joy",
            "intensity": 0.2,
            "valence": 0.2,
        },
        "foresight_signals": [],
        "timestamp": datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc),
        "retrieval_count": 0,
        "explicit_importance": 0.0,
        "consolidated": False,
        "scene_id": None,
        "vector_similarity": vector_similarity,
    }


@pytest.mark.asyncio
async def test_postgres_recall_uses_hnsw_friendly_vector_candidate_query() -> None:
    row = _build_recall_row(
        cell_id="cell-0",
        session_id="telegram_bot_999",
        episodic_trace="Scarlet mentioned jasmine tea and rainy nights.",
        atomic_facts=["Scarlet|likes|jasmine tea"],
        vector_similarity=0.82,
    )
    conn = _FakeRecallConn(vector_rows=[row], text_rows=[])
    repo = PostgresSoulRepository(dsn="postgresql://unused")
    repo._pool = _FakeRecallPool(conn)

    matches = await repo.recall_memories(
        query_text="jasmine tea",
        query_embedding=[0.1, 0.2],
        session_id="telegram_bot_999",
        candidate_limit=5,
    )

    assert matches
    vector_sql = next(
        sql
        for sql, _ in conn.queries
        if "ORDER BY v.embedding <=> $1::vector ASC" in sql
    )
    assert "WITH vector_candidates AS" in vector_sql
    assert "FROM mem_cell_vectors v" in vector_sql
    assert (
        "FROM mem_cells c\n                JOIN mem_cell_vectors v ON v.mem_cell_id = c.id"
        not in vector_sql
    )


@pytest.mark.asyncio
async def test_postgres_recall_uses_index_friendly_text_query() -> None:
    row = _build_recall_row(
        cell_id="cell-1",
        session_id="telegram_bot_999",
        episodic_trace="Scarlet loves jasmine tea and cozy rainy evenings.",
        atomic_facts=["Scarlet|likes|jasmine tea"],
        vector_similarity=0.71,
    )
    conn = _FakeRecallConn(vector_rows=[row], text_rows=[row])
    repo = PostgresSoulRepository(dsn="postgresql://unused")
    repo._pool = _FakeRecallPool(conn)

    matches = await repo.recall_memories(
        query_text="What tea does Scarlet love again?",
        query_embedding=[0.1, 0.2],
        session_id="telegram_bot_999",
        candidate_limit=5,
    )

    assert matches
    text_sql = next(sql for sql, _ in conn.queries if "ORDER BY GREATEST(" in sql)
    assert "COALESCE(c.atomic_facts::text, '') % $1" not in text_sql
    assert (
        "c.episodic_trace || ' ' || COALESCE(c.atomic_facts::text, '')" not in text_sql
    )
    assert "to_tsvector('simple', c.episodic_trace)" in text_sql


@pytest.mark.asyncio
async def test_postgres_recall_skips_text_query_when_vector_window_is_full() -> None:
    row = _build_recall_row(
        cell_id="cell-full",
        session_id="telegram_bot_999",
        episodic_trace="Scarlet loves jasmine tea and cozy rainy evenings.",
        atomic_facts=["Scarlet|likes|jasmine tea"],
        vector_similarity=0.88,
    )
    conn = _FakeRecallConn(vector_rows=[row], text_rows=[row])
    repo = PostgresSoulRepository(dsn="postgresql://unused")
    repo._pool = _FakeRecallPool(conn)

    matches = await repo.recall_memories(
        query_text="What tea does Scarlet love again?",
        query_embedding=[0.1, 0.2],
        session_id="telegram_bot_999",
        candidate_limit=1,
    )

    assert matches
    assert any(
        "ORDER BY v.embedding <=> $1::vector ASC" in sql for sql, _ in conn.queries
    )
    assert not any("ORDER BY GREATEST(" in sql for sql, _ in conn.queries)


@pytest.mark.asyncio
async def test_postgres_recall_scores_atomic_facts_in_python() -> None:
    row = _build_recall_row(
        cell_id="cell-2",
        session_id="telegram_bot_999",
        episodic_trace="We talked for a bit.",
        atomic_facts=["Scarlet loves jasmine tea"],
        vector_similarity=0.6,
    )
    conn = _FakeRecallConn(vector_rows=[row], text_rows=[])
    repo = PostgresSoulRepository(dsn="postgresql://unused")
    repo._pool = _FakeRecallPool(conn)

    matches = await repo.recall_memories(
        query_text="jasmine tea",
        query_embedding=[0.1, 0.2],
        session_id="telegram_bot_999",
        candidate_limit=5,
    )

    assert matches
    assert matches[0].lexical_score > 0.5


@pytest.mark.asyncio
async def test_build_daily_transcript_uses_parameterized_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = SoulPlugin()

    mock_cursor = AsyncMock()
    mock_cursor.fetchall = AsyncMock(
        return_value=[
            (
                "Scar",
                "5208932647",
                "first",
                datetime(2026, 5, 5, 11, 37, tzinfo=timezone.utc),
            ),
            (
                "self",
                "self",
                "second",
                datetime(2026, 5, 5, 11, 38, tzinfo=timezone.utc),
            ),
        ]
    )

    mock_conn = AsyncMock()
    mock_conn.cursor = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_cursor),
            __aexit__=AsyncMock(return_value=None),
        )
    )

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr("plugins.soul_plugin.get_conn_ctx", lambda: mock_ctx)

    transcript = await plugin._build_daily_transcript()

    executed_sql, params = mock_cursor.execute.await_args_list[0][0]
    assert "INTERVAL 1 DAY" not in executed_sql
    assert "WHERE timestamp >= %s" in executed_sql
    assert isinstance(params[0], datetime)
    assert '[2026-05-05T11:37:00+00:00] Scar: "first"' in transcript
    assert '[2026-05-05T11:38:00+00:00] self: "second"' in transcript


def test_build_emotion_engine_returns_emotional_engine() -> None:
    plugin = SoulPlugin()
    assert isinstance(plugin._emotion_engine, EmotionalEngine)


def test_load_emotional_profile_falls_back_when_no_skins_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    profile = SoulPlugin._load_emotional_profile()
    assert isinstance(profile, EmotionalProfile)
    assert profile.as_dict() == EmotionalProfile().as_dict()


def test_load_emotional_profile_reads_emotional_profile_section(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json as _json

    monkeypatch.chdir(tmp_path)
    skin_dir = tmp_path / "skins" / "TestSkin"
    skin_dir.mkdir(parents=True)
    (skin_dir / "persona.json").write_text(
        _json.dumps({"emotional_profile": {"anxiety": 0.99, "loneliness": 0.01}}),
        encoding="utf-8",
    )

    with patch("core.config_manager.config_registry") as mock_reg:
        mock_reg.get_value.return_value = "TestSkin"
        profile = SoulPlugin._load_emotional_profile()

    assert profile.anxiety == 0.99
    assert profile.loneliness == 0.01
    assert profile.concern_for_user == 0.90


def test_load_emotional_profile_falls_back_when_no_emotional_profile_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json as _json

    monkeypatch.chdir(tmp_path)
    skin_dir = tmp_path / "skins" / "Rei"
    skin_dir.mkdir(parents=True)
    (skin_dir / "persona.json").write_text(
        _json.dumps({"name": "Rei", "description": "A persona"}),
        encoding="utf-8",
    )

    with patch("core.config_manager.config_registry") as mock_reg:
        mock_reg.get_value.return_value = "Rei"
        profile = SoulPlugin._load_emotional_profile()

    assert profile.as_dict() == EmotionalProfile().as_dict()
