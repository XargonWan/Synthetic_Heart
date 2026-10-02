from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any, cast

import pytest

from plugins.soul_plugin import SoulPlugin
from plugins.soul_plugin import _SessionState
from core.plugin_base import PluginBase
from core.soul.emotion_engine import EmotionalEngine
from core.soul.models import EmotionalProfile, EmotionalTag, MemCell, MemCellRecall
from core.soul.repository import InMemorySoulRepository, PostgresSoulRepository


@pytest.fixture(autouse=True)
def _default_soul_plugin_tests_to_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYNTH_PRIMARY_DB", raising=False)
    monkeypatch.delenv("SOUL_POSTGRES_DSN", raising=False)
    monkeypatch.setenv("SYNTH_DB_TYPE", "mariadb")


def test_soul_plugin_has_no_internal_enable_flag() -> None:
    """soul_plugin activation is gated only by the global plugin toggle
    (PLUGIN_ENABLED__soul_plugin). It must not expose an internal
    `_is_enabled` config gate that duplicates the toggle, and it must not
    override `is_enabled` (the PluginBase default already returns True)."""
    assert not hasattr(SoulPlugin, "_is_enabled")
    assert SoulPlugin.is_enabled is PluginBase.is_enabled


@pytest.mark.asyncio
async def test_postgres_repository_pool_flows_through_core_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = PostgresSoulRepository(
        dsn="postgresql://user:pass@db.example/synth",
        schema="public",
        min_pool_size=2,
        max_pool_size=7,
    )
    fake_pool = object()
    captured: dict[str, Any] = {}

    async def _fake_get_named_postgres_pool(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return fake_pool

    monkeypatch.setattr(
        "core.db.get_named_postgres_pool", _fake_get_named_postgres_pool
    )

    with patch.object(
        PostgresSoulRepository,
        "_ensure_schema",
        new=AsyncMock(),
    ) as ensure_schema:
        pool = await repo._get_pool()
        cached_pool = await repo._get_pool()

    assert pool is fake_pool
    assert cached_pool is fake_pool
    assert captured["dsn"] == repo.dsn
    assert captured["minsize"] == 2
    assert captured["maxsize"] == 7
    assert captured["server_settings"] == {"search_path": "public"}
    assert captured["pool_key"].startswith("soul:public:2:7:")
    ensure_schema.assert_awaited_once_with(fake_pool)


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
            "type": "static_inject",
            "payload": {},
        },
        {},
        None,
        message,
    )

    assert result is not None


@pytest.mark.asyncio
async def test_static_injection_recalls_relevant_memories() -> None:
    plugin = SoulPlugin()
    interface_path = "telegram_bot/999"

    seed_message = SimpleNamespace(
        interface_path=interface_path,
        text="Alice loves jasmine tea and cozy rainy evenings.",
        caption=None,
    )
    await plugin.get_static_injection(seed_message, {"interface_path": interface_path})
    await plugin._compile_interface(interface_path)

    recall_message = SimpleNamespace(
        interface_path=interface_path,
        text="What tea does Alice love again?",
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
        episodic_trace="Alice mentioned jasmine tea.",
        atomic_facts=["Alice|likes|jasmine tea"],
        emotional_tag=emotional_tag,
        foresight_signals=[],
        event_timestamp=now,
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
        event_timestamp=now,
        session_id="diary_merge:-1",
    )
    normal_cell = MemCell(
        id="normal",
        episodic_trace="Alice loves jasmine tea on rainy evenings.",
        atomic_facts=["Alice|likes|jasmine tea"],
        emotional_tag=emotional_tag,
        foresight_signals=[],
        event_timestamp=now,
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
            text="What tea does Alice love?",
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
async def test_recall_excludes_roleplay_memories() -> None:
    """Explicit/roleplay mem-cells must not be recalled into prompts (Grillo
    reflection beats included). An explicit cell recalled into a
    tag_elaboration beat was elaborated into an ever-more-explicit diary entry
    (langfuse 36cb0aca). Structural detector from core.soul.roleplay."""
    plugin = SoulPlugin()
    now = datetime.now(timezone.utc)
    emotional_tag = EmotionalTag(
        state_snapshot={"joy": 0.2, "fear": 0.0, "sad": 0.0, "anger": 0.0},
        dominant_emotion="joy",
        intensity=0.2,
        valence=0.2,
    )
    roleplay_cell = MemCell(
        id="rp",
        episodic_trace=(
            "slide my hand under your big shirt, grabbing your breast, you moan softly"
        ),
        atomic_facts=["you|moan|softly"],
        emotional_tag=emotional_tag,
        foresight_signals=[],
        event_timestamp=now,
        session_id="telegram_bot_5208932647",
    )
    normal_cell = MemCell(
        id="normal",
        episodic_trace="Alice loves jasmine tea on rainy evenings.",
        atomic_facts=["Alice|likes|jasmine tea"],
        emotional_tag=emotional_tag,
        foresight_signals=[],
        event_timestamp=now,
        session_id="telegram_bot_5208932647",
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
                    cell=roleplay_cell,
                    similarity=0.99,
                    lexical_score=0.9,
                    score=0.99,
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

    # Grillo beat path (the leak scenario).
    payload = await plugin.get_static_injection(
        SimpleNamespace(
            interface_path="grillo/-1",
            text="[G.R.I.L.L.O. Tag Elaboration] Reflect on your recent conversations",
            caption=None,
        ),
        {"interface_path": "grillo/-1", "grillo_beat": True},
    )

    recalled = [str(e) for e in payload.get("soul_recalled_memories", [])]
    assert len(recalled) == 1
    assert "jasmine tea" in recalled[0].lower()
    assert all("breast" not in e.lower() and "moan" not in e.lower() for e in recalled)


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


@pytest.mark.asyncio
async def test_compile_interface_skips_roleplay_only_buffer() -> None:
    plugin = SoulPlugin()
    iface = "telegram_bot/99"
    compiler = SimpleNamespace(
        post_session_compile=AsyncMock(return_value=["cell-1"]),
        async_consolidate=AsyncMock(return_value=["scene-1"]),
    )
    plugin._compiler = compiler

    plugin._buffers[iface] = [
        "I'm fucking cumming in your tight little ass bitch CUM WITH ME YOU SLUT"
    ]
    created = await plugin._compile_interface(iface)

    assert created == 0
    assert plugin._buffers[iface] == []
    compiler.post_session_compile.assert_not_awaited()


@pytest.mark.asyncio
async def test_compile_interface_keeps_non_roleplay_lines() -> None:
    plugin = SoulPlugin()
    iface = "telegram_bot/100"
    compiler = SimpleNamespace(
        post_session_compile=AsyncMock(return_value=["cell-1"]),
        async_consolidate=AsyncMock(return_value=["scene-1"]),
    )
    plugin._compiler = compiler

    plugin._buffers[iface] = [
        "I work on SynthHeart for real",
        "I'm fucking cumming in your tight little ass bitch",
        "I live in Berlin",
    ]
    created = await plugin._compile_interface(iface)

    assert created == 1
    compiled_transcript = compiler.post_session_compile.await_args.kwargs["transcript"]
    assert "work on SynthHeart" in compiled_transcript
    assert "live in Berlin" in compiled_transcript
    assert "fucking cumming" not in compiled_transcript


def test_build_embedder_uses_runtime_repository_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = SoulPlugin.__new__(SoulPlugin)

    class _FakeEmbedder:
        def __init__(self, *, model_id: str) -> None:
            self.model_id = model_id

    monkeypatch.setattr(
        SoulPlugin, "_get_repository_backend", staticmethod(lambda: "postgres")
    )
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    monkeypatch.setattr("plugins.soul_plugin.FastEmbedder", _FakeEmbedder)

    embedder = SoulPlugin._build_embedder(plugin)

    assert isinstance(embedder, _FakeEmbedder)
    assert embedder.model_id == "BAAI/bge-base-en-v1.5"


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
    retrieval_count: int = 0,
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
        "event_timestamp": datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc),
        "retrieval_count": retrieval_count,
        "explicit_importance": 0.0,
        "consolidated": False,
        "scene_id": None,
        "distilled_at": None,
        "vector_similarity": vector_similarity,
    }


@pytest.mark.asyncio
async def test_postgres_recall_uses_hnsw_friendly_vector_candidate_query() -> None:
    row = _build_recall_row(
        cell_id="cell-0",
        session_id="telegram_bot_999",
        episodic_trace="Alice mentioned jasmine tea and rainy nights.",
        atomic_facts=["Alice|likes|jasmine tea"],
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
        episodic_trace="Alice loves jasmine tea and cozy rainy evenings.",
        atomic_facts=["Alice|likes|jasmine tea"],
        vector_similarity=0.71,
    )
    conn = _FakeRecallConn(vector_rows=[row], text_rows=[row])
    repo = PostgresSoulRepository(dsn="postgresql://unused")
    repo._pool = _FakeRecallPool(conn)

    matches = await repo.recall_memories(
        query_text="What tea does Alice love again?",
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
        episodic_trace="Alice loves jasmine tea and cozy rainy evenings.",
        atomic_facts=["Alice|likes|jasmine tea"],
        vector_similarity=0.88,
    )
    conn = _FakeRecallConn(vector_rows=[row], text_rows=[row])
    repo = PostgresSoulRepository(dsn="postgresql://unused")
    repo._pool = _FakeRecallPool(conn)

    matches = await repo.recall_memories(
        query_text="What tea does Alice love again?",
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
        atomic_facts=["Alice loves jasmine tea"],
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
                "Alice",
                "5551234567",
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
    assert "WHERE created_at >= %s" in executed_sql
    assert isinstance(params[0], datetime)
    assert '[2026-05-05T11:37:00+00:00] Alice: "first"' in transcript
    # The persona's own cached lines are labelled as its own, never left as the
    # bare "self" the interface stores (a label that says nothing about whose
    # side of the conversation it is on).
    assert '(the persona): "second"' in transcript
    assert 'self: "second"' not in transcript


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


@pytest.mark.asyncio
async def test_recall_ranking_does_not_reward_past_recalls() -> None:
    """Recall must not reward its own history.

    Identical cells (same similarity, emotion, recency) used to be ranked by
    ``retrieval_count``, which saturates at ten retrievals; two live cells had
    reached 70 and 164, so they were pinned into every prompt. The over-recalled
    twin must now rank strictly BELOW the fresh one.
    """
    fresh = _build_recall_row(
        cell_id="fresh",
        session_id="telegram_bot_999",
        episodic_trace="Alice mentioned jasmine tea in the rain.",
        atomic_facts=["Alice|likes|jasmine tea"],
        vector_similarity=0.8,
        retrieval_count=0,
    )
    over_recalled = _build_recall_row(
        cell_id="over-recalled",
        session_id="telegram_bot_999",
        episodic_trace="Alice mentioned jasmine tea in the rain.",
        atomic_facts=["Alice|likes|jasmine tea"],
        vector_similarity=0.8,
        retrieval_count=100,
    )
    conn = _FakeRecallConn(vector_rows=[over_recalled, fresh], text_rows=[])
    repo = PostgresSoulRepository(dsn="postgresql://unused")
    repo._pool = _FakeRecallPool(conn)

    matches = await repo.recall_memories(
        query_text="jasmine tea",
        query_embedding=[0.1, 0.2],
        session_id="telegram_bot_999",
        candidate_limit=5,
    )

    scores = {match.cell.id: match.score for match in matches}
    assert set(scores) == {"fresh", "over-recalled"}
    assert scores["fresh"] > scores["over-recalled"]


@pytest.mark.asyncio
async def test_retrieval_count_is_bumped_once_per_window() -> None:
    """Several prompt builds in one turn must not inflate the count."""
    plugin = SoulPlugin()
    interface_path = "telegram_bot/777"

    seed = SimpleNamespace(
        interface_path=interface_path,
        text="Alice mentioned jasmine tea in the rain.",
        caption=None,
    )
    await plugin.get_static_injection(seed, {"interface_path": interface_path})
    await plugin._compile_interface(interface_path)

    recall = SimpleNamespace(
        interface_path=interface_path,
        text="What did Alice mention about tea?",
        caption=None,
    )
    await plugin.get_static_injection(recall, {"interface_path": interface_path})
    after_first = max(
        (cell.retrieval_count for cell in plugin._repo.memcells.values()), default=0
    )

    await plugin.get_static_injection(recall, {"interface_path": interface_path})
    after_second = max(
        (cell.retrieval_count for cell in plugin._repo.memcells.values()), default=0
    )

    assert after_first == 1
    assert after_second == 1, "a second prompt build re-bumped the same cell"


def test_recalled_memory_names_the_conversation_it_came_from() -> None:
    """Cross-chat recall must say which chat it came from, never silently blend in."""
    plugin = SoulPlugin()
    emotional_tag = EmotionalTag(
        state_snapshot={"joy": 0.2, "fear": 0.0, "sad": 0.0, "anger": 0.0},
        dominant_emotion="joy",
        intensity=0.2,
        valence=0.2,
    )
    cell = MemCell(
        id="memory-elsewhere",
        episodic_trace="Scar: Nah we socialising now.",
        atomic_facts=[],
        emotional_tag=emotional_tag,
        foresight_signals=[],
        event_timestamp=datetime.now(timezone.utc),
        session_id="telegram_bot_5208932647",
    )
    match = MemCellRecall(cell=cell, similarity=0.9, lexical_score=0.8, score=0.9)

    formatted = plugin._format_recalled_memory(
        match, active_session_id="telegram_bot_999"
    )

    assert "other chat: telegram_bot_5208932647" in formatted
    assert "same chat" not in formatted


def test_recalled_memory_marks_same_chat_without_a_source_path() -> None:
    plugin = SoulPlugin()
    emotional_tag = EmotionalTag(
        state_snapshot={"joy": 0.2, "fear": 0.0, "sad": 0.0, "anger": 0.0},
        dominant_emotion="joy",
        intensity=0.2,
        valence=0.2,
    )
    cell = MemCell(
        id="memory-here",
        episodic_trace="Scar: how you doing love?",
        atomic_facts=[],
        emotional_tag=emotional_tag,
        foresight_signals=[],
        event_timestamp=datetime.now(timezone.utc),
        session_id="telegram_bot_999",
    )
    match = MemCellRecall(cell=cell, similarity=0.9, lexical_score=0.8, score=0.9)

    formatted = plugin._format_recalled_memory(
        match, active_session_id="telegram_bot_999"
    )

    assert "same chat" in formatted
    assert "other chat" not in formatted


@pytest.mark.asyncio
async def test_buffer_lines_are_attributed_to_their_speaker() -> None:
    """A compiled cell must record who said the line it holds.

    The rule-based extractor stores the transcript verbatim as the cell's
    episodic trace, so an unattributed buffer made the human's own words come
    back later as an unattributed "recalled memory" (the synth reading the user's
    speech as its own recollection).
    """
    plugin = SoulPlugin()
    interface_path = "telegram_bot/778"

    message = SimpleNamespace(
        interface_path=interface_path,
        text="Alice loves jasmine tea and cozy rainy evenings.",
        caption=None,
        sender_name="Scar",
    )
    await plugin.get_static_injection(message, {"interface_path": interface_path})
    await plugin._compile_interface(interface_path)

    traces = [cell.episodic_trace for cell in plugin._repo.memcells.values()]
    assert traces, "compile produced no cells"
    assert all(trace.startswith("Scar: ") for trace in traces), traces
    assert any("jasmine tea" in trace for trace in traces)


@pytest.mark.asyncio
async def test_buffer_lines_fall_back_to_user_when_the_sender_is_unknown() -> None:
    plugin = SoulPlugin()
    interface_path = "telegram_bot/779"

    message = SimpleNamespace(
        interface_path=interface_path,
        text="Remember the event on 2026-04-20.",
        caption=None,
    )
    await plugin.get_static_injection(message, {"interface_path": interface_path})
    await plugin._compile_interface(interface_path)

    traces = [cell.episodic_trace for cell in plugin._repo.memcells.values()]
    assert traces
    assert all(trace.startswith("user: ") for trace in traces), traces


def test_format_recalled_memory_omits_a_fact_that_repeats_the_trace() -> None:
    """The compiler's only "fact" is the trace itself, so echoing it must stop.

    Every live cell carries ``Conversation|summary|<the same line>`` as its sole
    atomic fact, so the block printed the line twice and every prompt carried the
    duplicate.
    """
    plugin = SoulPlugin()
    now = datetime.now(timezone.utc)
    line = "Scar: Damn are you kidding me, the memcell issue showed up again just then"
    cell = MemCell(
        id="dup",
        episodic_trace=line,
        atomic_facts=[f"Conversation|summary|{line}"],
        emotional_tag=EmotionalTag(
            state_snapshot={"joy": 0.2, "fear": 0.0, "sad": 0.0, "anger": 0.0},
            dominant_emotion="joy",
            intensity=0.2,
            valence=0.2,
        ),
        foresight_signals=[],
        event_timestamp=now,
        session_id="telegram_bot_5208932647",
    )

    formatted = plugin._format_recalled_memory(
        MemCellRecall(cell=cell, similarity=0.9, lexical_score=0.8, score=0.9),
        active_session_id="telegram_bot_5208932647",
    )

    assert "Key facts:" not in formatted
    assert "same chat" in formatted
    assert formatted.count("memcell issue showed up") == 1


def test_format_recalled_memory_keeps_a_fact_that_adds_information() -> None:
    plugin = SoulPlugin()
    now = datetime.now(timezone.utc)
    cell = MemCell(
        id="real",
        episodic_trace="Alice mentioned her morning routine.",
        atomic_facts=["Alice|likes|jasmine tea"],
        emotional_tag=EmotionalTag(
            state_snapshot={"joy": 0.2, "fear": 0.0, "sad": 0.0, "anger": 0.0},
            dominant_emotion="joy",
            intensity=0.2,
            valence=0.2,
        ),
        foresight_signals=[],
        event_timestamp=now,
        session_id="telegram_bot_5208932647",
    )

    formatted = plugin._format_recalled_memory(
        MemCellRecall(cell=cell, similarity=0.9, lexical_score=0.8, score=0.9),
        active_session_id="telegram_bot_5208932647",
    )

    assert "Key facts: Alice likes jasmine tea" in formatted


def test_recall_fatigue_is_strong_enough_to_rotate_the_recalled_set() -> None:
    """A cell recalled hundreds of times must lose real ground to a fresh one.

    The first fatigue version moved the final recall score by 0.03, which left the
    same lines pinned in every prompt (one live cell reached 663 retrievals while
    396 of 423 cells were never recalled).
    """
    from core.soul.models import compute_recall_salience

    def salience(count: int) -> float:
        return compute_recall_salience(
            emotional_intensity=0.5,
            recency_score=0.8,
            explicit_importance=0.1,
            retrieval_count=count,
        )

    fresh = salience(0)
    assert fresh - salience(180) >= 0.1
    assert fresh - salience(663) >= 0.1
    assert salience(663) >= 0.0


@pytest.mark.asyncio
async def test_recall_injects_each_line_only_once() -> None:
    """Two cells holding the same line must not be injected twice."""
    plugin = SoulPlugin()
    interface_path = "telegram_bot/321"
    now = datetime.now(timezone.utc)
    tag = EmotionalTag(
        state_snapshot={"joy": 0.1, "fear": 0.0, "sad": 0.0, "anger": 0.0},
        dominant_emotion="joy",
        intensity=0.1,
        valence=0.1,
    )

    def _cell(cell_id: str, text: str) -> MemCell:
        return MemCell(
            id=cell_id,
            episodic_trace=text,
            atomic_facts=[],
            emotional_tag=tag,
            foresight_signals=[],
            event_timestamp=now,
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
                    cell=_cell("a", "Alice loves jasmine tea."),
                    similarity=0.95,
                    lexical_score=0.9,
                    score=0.95,
                ),
                MemCellRecall(
                    cell=_cell("b", "Alice loves jasmine   tea."),
                    similarity=0.94,
                    lexical_score=0.9,
                    score=0.94,
                ),
                MemCellRecall(
                    cell=_cell("c", "Alice also keeps a green teapot."),
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
            text="What tea does Alice love?",
            caption=None,
        ),
        {"interface_path": interface_path},
    )

    recalled = [str(entry) for entry in payload.get("soul_recalled_memories", [])]

    assert len(recalled) == 2
    assert sum("jasmine tea" in entry.lower() for entry in recalled) == 1
    assert any("green teapot" in entry.lower() for entry in recalled)


def _recall_only_plugin(cells: list[MemCell], *, distils: bool) -> SoulPlugin:
    """A plugin wired for recall only: no DB, no LLM, just a candidate list."""

    plugin = SoulPlugin()
    compiler_attrs: dict[str, Any] = {
        "embedder": SimpleNamespace(embed=AsyncMock(return_value=[0.25, 0.75])),
    }
    if distils:
        compiler_attrs["memcell_extractor"] = SimpleNamespace(distils_content=True)
    plugin._compiler = SimpleNamespace(**compiler_attrs)

    matches = [
        MemCellRecall(
            cell=cell,
            similarity=0.95 - i * 0.01,
            lexical_score=0.9,
            score=0.95 - i * 0.01,
        )
        for i, cell in enumerate(cells)
    ]
    plugin._repo = SimpleNamespace(
        get_active_dsp=AsyncMock(return_value=None),
        list_active_foresight_signals=AsyncMock(return_value=[]),
        recall_memories=AsyncMock(return_value=matches),
        upsert_memcell=AsyncMock(return_value=None),
    )
    return plugin


def _recall_cell(cell_id: str, text: str, *, distilled: bool) -> MemCell:
    now = datetime.now(timezone.utc)
    return MemCell(
        id=cell_id,
        episodic_trace=text,
        atomic_facts=[],
        emotional_tag=EmotionalTag(
            state_snapshot={"joy": 0.1, "fear": 0.0, "sad": 0.0, "anger": 0.0},
            dominant_emotion="joy",
            intensity=0.1,
            valence=0.1,
        ),
        foresight_signals=[],
        event_timestamp=now,
        session_id="telegram_bot:321",
        distilled_at=now if distilled else None,
    )


async def _recall_once(
    plugin: SoulPlugin, *, interface_path: str = "telegram_bot/321"
) -> list[str]:
    return await plugin._recall_memories(
        interface_path=interface_path,
        incoming_text="what tea does alice love",
        session=_SessionState(),
    )


@pytest.mark.asyncio
async def test_recall_skips_a_cell_that_still_holds_raw_transcript() -> None:
    """An unstamped cell is a legacy raw-transcript row, so it is not recalled.

    ``distilled_at IS NULL`` means the cell was written before the distilling
    extractor existed, which makes its trace the verbatim session text. There is
    nothing to inject until the re-distil pass rewrites it.
    """

    legacy = _recall_cell(
        "legacy",
        "Scar: I slowly slide my dick deep in your ass and moan your name",
        distilled=False,
    )
    fresh = _recall_cell(
        "fresh", "Alice loves jasmine tea on rainy nights.", distilled=True
    )
    plugin = _recall_only_plugin([legacy, fresh], distils=True)

    recalled = await _recall_once(plugin)

    assert len(recalled) == 1
    assert "jasmine tea" in recalled[0].lower()
    assert not any("slide my dick" in entry.lower() for entry in recalled)


@pytest.mark.asyncio
async def test_recall_ignores_the_stamp_when_the_extractor_does_not_distil() -> None:
    """The rule-based extractor stamps nothing, so the stamp cannot gate recall.

    Gating on it unconditionally would empty the memory block for any deployment
    running that extractor, where every cell is unstamped.
    """

    cell = _recall_cell(
        "only", "Alice loves jasmine tea on rainy nights.", distilled=False
    )
    plugin = _recall_only_plugin([cell], distils=False)

    recalled = await _recall_once(plugin)

    assert len(recalled) == 1
    assert "jasmine tea" in recalled[0].lower()


@pytest.mark.asyncio
async def test_recall_rotates_the_recalled_set_within_a_session() -> None:
    """A cell just injected steps aside so the next-most-similar ones are shown.

    Semantic similarity is 58% of the recall score and it does not move while the
    same person keeps talking about the same things, so without rotation the
    identical handful of memories is injected turn after turn.
    """

    cells = [
        _recall_cell(
            f"cell-{i}", f"Alice keeps jasmine tea tin number {i}.", distilled=True
        )
        for i in range(8)
    ]
    plugin = _recall_only_plugin(cells, distils=True)

    first = await _recall_once(plugin)
    second = await _recall_once(plugin)

    assert len(first) == 5
    # Still a full block on the second turn: the held-back cells fall back in
    # rather than leaving the model with nothing.
    assert len(second) == 5
    newly_shown = [entry for entry in second if entry not in first]
    assert len(newly_shown) == 3, (
        "the recalled set did not rotate, every entry had already been injected: "
        f"{second}"
    )


@pytest.mark.asyncio
async def test_recall_cooldown_is_scoped_to_the_conversation() -> None:
    """Another conversation's recall must not spend this one's memories.

    The cooldown map was keyed by the cell id alone, so it was global to the
    process: a Grillo beat (every 30 minutes, same process) or a second chat
    recalling a cell held it back here for the full 900 s. Measured live on
    2026-09-22, no assembled prompt carried a memory from that day while the
    store held 18 of them, and their retrieval counts show other callers had
    been selecting them.
    """

    cells = [
        _recall_cell(
            f"cell-{i}", f"Alice keeps jasmine tea tin number {i}.", distilled=True
        )
        for i in range(8)
    ]
    plugin = _recall_only_plugin(cells, distils=True)

    here = await _recall_once(plugin)
    elsewhere = await _recall_once(plugin, interface_path="telegram_bot/999")

    assert len(here) == 5
    assert "number 0" in here[0]
    assert len(elsewhere) == 5
    assert "number 0" in elsewhere[0], (
        f"the best match was held back by another conversation's recall: {elsewhere}"
    )


@pytest.mark.asyncio
async def test_recall_passes_the_configured_linked_sessions_to_the_repository() -> None:
    """SOUL_RECALL_LINKED_SESSIONS reaches the repository as a scope, and only
    for a turn whose own chat is in the list."""

    cells = [_recall_cell("cell-0", "Alice keeps jasmine tea.", distilled=True)]

    def _config(linked: str) -> Any:
        def _get_value(key: str, default: Any = None, **_kwargs: Any) -> Any:
            if key == "SOUL_RECALL_LINKED_SESSIONS":
                return linked
            return default

        return patch("core.config_manager.config_registry", get_value=_get_value)

    plugin = _recall_only_plugin(cells, distils=True)
    with _config("telegram_bot/321,telegram_bot/999"):
        await _recall_once(plugin)
    kwargs = plugin._repo.recall_memories.await_args.kwargs
    assert kwargs["session_id"] == "telegram_bot_321"
    assert kwargs["linked_session_ids"] == frozenset(
        {"telegram_bot_321", "telegram_bot_999"}
    )

    # A turn in a chat the list does not name links nothing.
    plugin = _recall_only_plugin(cells, distils=True)
    with _config("telegram_bot/321,telegram_bot/999"):
        await _recall_once(plugin, interface_path="telegram_bot/777")
    assert plugin._repo.recall_memories.await_args.kwargs["linked_session_ids"] is None

    # An empty setting keeps the previous behaviour exactly.
    plugin = _recall_only_plugin(cells, distils=True)
    with _config(""):
        await _recall_once(plugin)
    assert plugin._repo.recall_memories.await_args.kwargs["linked_session_ids"] is None


# ---------------------------------------------------------------------------
# The persona's own lines say so in the DSP transcript
# ---------------------------------------------------------------------------


def _patch_daily_transcript_rows(
    monkeypatch: pytest.MonkeyPatch, rows: list[tuple[Any, Any, Any, Any]]
) -> AsyncMock:
    """Point `_build_daily_transcript` at a fixed set of cached rows."""
    mock_cursor = AsyncMock()
    mock_cursor.fetchall = AsyncMock(return_value=rows)
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
    return mock_cursor


@pytest.mark.asyncio
async def test_build_daily_transcript_names_the_persona_own_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DSP extractor is told which speaker is the persona (live 2026-09-22).

    The 2D deployment's transcript carried exactly `self:`, `Scar:` and `2B:`;
    nothing said whose "self" was, the extractor read the persona's own lines as
    the human's, and the compiled profile described the PERSONA - a woman named
    Dee - as the person being talked to, which handed the persona's name and
    gender to the human. Naming the persona's lines removes the guess; every
    other speaker keeps the label the interface stored.
    """
    plugin = SoulPlugin()
    monkeypatch.setattr(SoulPlugin, "_persona_display_name", staticmethod(lambda: "2D"))
    _patch_daily_transcript_rows(
        monkeypatch,
        [
            (
                "self",
                "self",
                "mmwah, come back here",
                datetime(2026, 9, 22, 20, 49, tzinfo=timezone.utc),
            ),
            (
                "Scar",
                "5208932647",
                "come here wifey",
                datetime(2026, 9, 22, 20, 49, 45, tzinfo=timezone.utc),
            ),
            (
                "2D",
                "2D",
                "the cookie is mine",
                datetime(2026, 9, 22, 20, 50, tzinfo=timezone.utc),
            ),
            (
                "2B",
                "5208932648",
                "hello you two",
                datetime(2026, 9, 22, 20, 51, tzinfo=timezone.utc),
            ),
        ],
    )

    transcript = await plugin._build_daily_transcript()

    assert '2D (the persona): "mmwah, come back here"' in transcript
    # The persona's own name is the same person: relabelled, not duplicated.
    assert '2D (the persona): "the cookie is mine"' in transcript
    assert transcript.count("(the persona)") == 2
    # The human and the other synth keep their own labels verbatim.
    assert 'Scar: "come here wifey"' in transcript
    assert '2B: "hello you two"' in transcript


@pytest.mark.asyncio
async def test_build_daily_transcript_labels_the_persona_without_a_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no SYNTH_NAME the label still says whose line it is."""
    plugin = SoulPlugin()
    monkeypatch.setattr(SoulPlugin, "_persona_display_name", staticmethod(lambda: ""))
    _patch_daily_transcript_rows(
        monkeypatch,
        [("self", "self", "mmwah", datetime(2026, 9, 22, 20, 49, tzinfo=timezone.utc))],
    )

    transcript = await plugin._build_daily_transcript()

    assert 'the persona: "mmwah"' in transcript
    assert "self:" not in transcript


def test_speaker_identities_reach_the_dsp_builder() -> None:
    """SOUL_SPEAKER_IDENTITIES must reach the compiler, not only the extractors.

    The compiler is the stage that decides the name and the gender the standing
    profile carries. Live 2026-09-22: one pass' extraction swapped the two roles,
    that pass was the newest evidence, and the profile was rewritten with the
    persona's name and gender on the human.
    """
    declared = "Scar (also called Scarlet) - he/him, the human; 2D (called Dee) - she/her, the persona, me"

    def _get_value(key: str, default: Any = None, **_kwargs: Any) -> Any:
        if key == "SOUL_SPEAKER_IDENTITIES":
            return declared
        return default

    with patch("core.config_manager.config_registry", get_value=_get_value):
        builder = SoulPlugin._build_dsp_builder()

    assert getattr(builder, "speaker_identity", "") == declared


def test_no_speaker_identities_keeps_the_builder_undeclared() -> None:
    """An empty setting keeps the previous prompts exactly (no declaration)."""

    def _get_value(key: str, default: Any = None, **_kwargs: Any) -> Any:
        return default

    with patch("core.config_manager.config_registry", get_value=_get_value):
        builder = SoulPlugin._build_dsp_builder()

    assert getattr(builder, "speaker_identity", "") == ""
    assert builder._declaration_block() == ""


# ---------------------------------------------------------------------------
# A compile distils BOTH sides of the conversation
# ---------------------------------------------------------------------------


def _capture_compiled_transcript(
    monkeypatch: pytest.MonkeyPatch, plugin: SoulPlugin
) -> dict[str, Any]:
    """Capture the transcript handed to the memcell extractor.

    ``SoulCompiler`` is a slots dataclass, so its method cannot be patched on the
    instance; the extractor it calls with the transcript can.
    """
    captured: dict[str, Any] = {}
    extractor = plugin._compiler.memcell_extractor

    async def fake_extract(
        _self: Any, *, transcript: str, current_date: Any
    ) -> list[Any]:
        captured["transcript"] = transcript
        captured["current_date"] = current_date
        return []

    monkeypatch.setattr(type(extractor), "extract_memcells", fake_extract)
    return captured


@pytest.mark.asyncio
async def test_compile_transcript_carries_both_sides_of_the_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The buffer holds only incoming lines, so the compile must take both sides.

    Live 2026-09-23: the 12:12 compile distilled three ``Scar:`` lines while the
    persona's own 12:07/12:05/12:02 replies sat in ``chat_history_cache``. The
    extractor is asked to state who did or said what, and the cells that come out
    of a one-sided transcript file the persona's own acts as the user's.
    """
    plugin = SoulPlugin()
    interface_path = "telegram_bot/777"
    monkeypatch.setattr(SoulPlugin, "_persona_display_name", staticmethod(lambda: "2D"))
    plugin._buffers[interface_path] = ["Scar: are you awake"]
    plugin._buffer_started[interface_path] = datetime(
        2026, 9, 23, 10, 4, tzinfo=timezone.utc
    )
    _patch_daily_transcript_rows(
        monkeypatch,
        [
            # Present in the cache, but from BEFORE this buffer: already compiled.
            (
                "Scar",
                "5208932647",
                "did you sleep at all",
                datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc),
            ),
            (
                "Scar",
                "5208932647",
                "are you awake",
                datetime(2026, 9, 23, 10, 4, 30, tzinfo=timezone.utc),
            ),
            (
                "self",
                "self",
                "mmh, awake now",
                datetime(2026, 9, 23, 10, 5, tzinfo=timezone.utc),
            ),
            (
                "Scar",
                "5208932647",
                "there is tea on the counter",
                datetime(2026, 9, 23, 10, 6, tzinfo=timezone.utc),
            ),
        ],
    )
    captured = _capture_compiled_transcript(monkeypatch, plugin)

    assert await plugin._compile_interface(interface_path) == 0

    transcript = captured["transcript"]
    # Both sides, the persona's own line named as the persona's.
    assert "Scar: are you awake" in transcript
    assert "2D (the persona): mmh, awake now" in transcript
    assert "Scar: there is tea on the counter" in transcript
    # In order, and nothing from the previous compile window.
    assert (
        transcript.index("are you awake")
        < transcript.index("awake now")
        < transcript.index("tea on the counter")
    )
    assert "did you sleep at all" not in transcript
    # The buffer is cleared together with its window anchor.
    assert plugin._buffers[interface_path] == []
    assert interface_path not in plugin._buffer_started


@pytest.mark.asyncio
async def test_compile_transcript_keeps_the_buffer_when_the_cache_misses_a_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache that does not hold the buffered lines must not replace them."""
    plugin = SoulPlugin()
    interface_path = "telegram_bot/778"
    monkeypatch.setattr(SoulPlugin, "_persona_display_name", staticmethod(lambda: "2D"))
    plugin._buffers[interface_path] = [
        "Scar: are you awake",
        "Scar: I made tea",
    ]
    plugin._buffer_started[interface_path] = datetime(
        2026, 9, 23, 10, 4, tzinfo=timezone.utc
    )
    # The cache holds a DIFFERENT conversation on the same interface (a stale or
    # partially written window): the buffered lines are the authority.
    _patch_daily_transcript_rows(
        monkeypatch,
        [
            (
                "self",
                "self",
                "some older line nobody buffered",
                datetime(2026, 9, 23, 10, 5, tzinfo=timezone.utc),
            )
        ],
    )
    captured = _capture_compiled_transcript(monkeypatch, plugin)

    await plugin._compile_interface(interface_path)

    assert captured["transcript"] == "Scar: are you awake\nScar: I made tea"


@pytest.mark.asyncio
async def test_compile_transcript_falls_back_when_the_cache_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache read that raises must not cost the session its compile."""
    plugin = SoulPlugin()
    interface_path = "telegram_bot/779"
    plugin._buffers[interface_path] = ["Scar: are you awake"]
    plugin._buffer_started[interface_path] = datetime(
        2026, 9, 23, 10, 4, tzinfo=timezone.utc
    )

    def _boom() -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr("plugins.soul_plugin.get_conn_ctx", _boom)
    captured = _capture_compiled_transcript(monkeypatch, plugin)

    assert await plugin._compile_interface(interface_path) == 0

    assert captured["transcript"] == "Scar: are you awake"


def test_cache_covers_buffer_requires_every_buffered_line() -> None:
    """Coverage is what licenses the cached transcript over the buffer."""
    cached = ["Scar: are you awake", "2D (the persona): mmh, awake now"]

    assert SoulPlugin._cache_covers_buffer(cached, ["Scar: are you awake"]) is True
    assert (
        SoulPlugin._cache_covers_buffer(
            cached, ["Scar: are you awake", "2D (the persona): mmh, awake now"]
        )
        is True
    )
    assert (
        SoulPlugin._cache_covers_buffer(
            cached, ["Scar: are you awake", "Scar: I made tea"]
        )
        is False
    )
