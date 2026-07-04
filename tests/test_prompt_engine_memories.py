from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import core.prompt_engine as pe
import core.synth_core_memory as scm
from core.prompt_engine import build_json_prompt, search_memories


@pytest.mark.asyncio
async def test_search_memories_includes_ai_diary(monkeypatch):
    # Dummy cursor that records executed queries and returns rows for ai_diary query
    class DummyCursor:
        def __init__(self):
            self.queries = []
            self.calls = 0

        async def execute(self, sql, params=None):
            self.calls += 1
            self.queries.append((sql, params))

        async def fetchall(self):
            # First call: memories query -> return empty
            if self.calls == 1:
                return []
            # Second call: ai_diary query -> return some rows
            return [["Diary memory A"], ["Diary memory B"]]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class DummyConn:
        def __init__(self):
            self.cursor_obj = DummyCursor()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return self.cursor_obj

    conn_instance = DummyConn()

    def mock_get_conn_ctx():
        return conn_instance

    import core.db as cdb

    monkeypatch.setattr(cdb, "get_conn_ctx", mock_get_conn_ctx)
    import core.prompt_engine as pe

    monkeypatch.setattr(pe, "get_conn_ctx", mock_get_conn_ctx)

    results = await search_memories(tags=["food"], limit=5)
    assert "Diary memory A" in results
    assert "Diary memory B" in results


@pytest.mark.asyncio
async def test_search_memories_uses_postgres_tag_predicates(monkeypatch) -> None:
    class DummyCursor:
        def __init__(self) -> None:
            self.queries: list[tuple[str, list[object] | None]] = []

        async def execute(self, sql: str, params=None) -> None:
            stored_params = list(params) if params is not None else None
            self.queries.append((sql, stored_params))

        async def fetchall(self) -> list[list[str]]:
            return []

        async def __aenter__(self) -> "DummyCursor":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    class DummyConn:
        def __init__(self) -> None:
            self.cursor_obj = DummyCursor()

        async def __aenter__(self) -> "DummyConn":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        def cursor(self) -> DummyCursor:
            return self.cursor_obj

    conn_instance = DummyConn()

    def mock_get_conn_ctx() -> DummyConn:
        return conn_instance

    monkeypatch.setattr(pe, "get_conn_ctx", mock_get_conn_ctx)
    monkeypatch.setattr(pe, "_get_db_type", lambda: "postgres")

    results = await pe.search_memories(tags=["food", "work"], limit=5)

    assert results == []
    queries = [sql for sql, _ in conn_instance.cursor_obj.queries]
    assert queries
    assert all("JSON_CONTAINS" not in sql for sql in queries)
    assert all("SELECT DISTINCT" not in sql for sql in queries)
    assert any("::jsonb ? %s" in sql for sql in queries)
    assert conn_instance.cursor_obj.queries[0][1] == ["food", "work", 5]


@pytest.mark.asyncio
async def test_synth_core_search_memories_uses_postgres_tag_predicates(
    monkeypatch,
) -> None:
    class DummyCursor:
        def __init__(self) -> None:
            self.queries: list[tuple[str, list[object] | None]] = []

        async def execute(self, sql: str, params=None) -> None:
            stored_params = list(params) if params is not None else None
            self.queries.append((sql, stored_params))

        async def fetchall(self) -> list[tuple[str, int, datetime, str, str | None]]:
            return []

        async def __aenter__(self) -> "DummyCursor":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    class DummyConn:
        def __init__(self) -> None:
            self.cursor_obj = DummyCursor()

        async def __aenter__(self) -> "DummyConn":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        def cursor(self) -> DummyCursor:
            return self.cursor_obj

    conn_instance = DummyConn()

    def mock_get_conn_ctx() -> DummyConn:
        return conn_instance

    monkeypatch.setattr(scm, "get_conn_ctx", mock_get_conn_ctx)
    monkeypatch.setattr(scm, "_get_db_type", lambda: "postgres")

    results = await scm.search_memories(tags=["food"], limit=5)

    assert results == []
    queries = [sql for sql, _ in conn_instance.cursor_obj.queries]
    assert len(queries) == 3
    assert all("JSON_CONTAINS" not in sql for sql in queries)
    assert "COALESCE(NULLIF(BTRIM(tags), ''), '[]')::jsonb ? %s" in queries[0]
    assert "COALESCE(NULLIF(BTRIM(context_tags), ''), '[]')::jsonb ? %s" in queries[1]
    assert conn_instance.cursor_obj.queries[0][1] == ["food", 15]
    assert conn_instance.cursor_obj.queries[1][1] == ["food", 15]
    assert conn_instance.cursor_obj.queries[2][1] == ["%food%", 15]


@pytest.mark.asyncio
async def test_synth_core_search_memories_or_fallback_recovers_keyword_only_row(
    monkeypatch,
) -> None:
    """Regression: a row whose stored tags do NOT match the query tags but whose
    *content* contains the searched keyword must still be recovered via the
    Tier-2 (tag OR keyword) fallback.

    This reproduces the intermittent-recall bug: a fact recorded yesterday
    (e.g. a song title) was present in the diary ``content`` but its
    auto-generated ``context_tags`` were generic, so the original
    tag-AND-keyword query returned nothing on the first ask.
    """

    matching_ts = datetime(2026, 7, 3, tzinfo=timezone.utc)

    class DummyCursor:
        def __init__(self) -> None:
            self.queries: list[tuple[str, list[object] | None]] = []

        async def execute(self, sql: str, params=None) -> None:
            self.queries.append((sql, list(params) if params is not None else None))

        async def fetchall(self):
            last_sql, last_params = self.queries[-1]
            # Tier 1 uses AND; Tier 2 uses OR. Only the OR fallback against the
            # ai_diary table should surface the keyword-only row.
            is_or = " OR (" in last_sql or ") OR (" in last_sql
            is_diary = "FROM ai_diary" in last_sql
            has_keyword = any(
                isinstance(p, str) and "monoteista" in p for p in (last_params or [])
            )
            if is_diary and is_or and has_keyword:
                return [
                    (
                        "ai_diary",
                        14245,
                        matching_ts,
                        "Oggi con Jay abbiamo creato Spada Soddisfare Monoteista.",
                        '["musica", "suno_jam"]',
                    )
                ]
            return []

        async def __aenter__(self) -> "DummyCursor":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    class DummyConn:
        def __init__(self) -> None:
            self.cursor_obj = DummyCursor()

        async def __aenter__(self) -> "DummyConn":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        def cursor(self) -> DummyCursor:
            return self.cursor_obj

    conn_instance = DummyConn()

    monkeypatch.setattr(scm, "get_conn_ctx", lambda: conn_instance)
    monkeypatch.setattr(scm, "_get_db_type", lambda: "postgres")

    # tags do NOT match the stored row; the keyword matches the content only.
    results = await scm.search_memories(
        tags=["cars"],
        keywords=["monoteista"],
        include_chat=False,
        limit=5,
    )

    assert len(results) == 1
    assert results[0]["source"] == "ai_diary"
    assert results[0]["id"] == 14245
    assert "Monoteista" in results[0]["snippet"]

    # Tier 1 (AND) must have run first and returned nothing, then Tier 2 (OR).
    or_queries = [
        sql
        for sql, _ in conn_instance.cursor_obj.queries
        if " OR (" in sql or ") OR (" in sql
    ]
    assert or_queries, "Tier-2 OR fallback query was not issued"


@pytest.mark.asyncio
async def test_synth_core_search_memories_keyword_match_is_case_insensitive(
    monkeypatch,
) -> None:
    """Regression: on Postgres, LIKE is case-sensitive, so a lowercase token
    (e.g. "alonza", as produced by extract_tags) would never match content
    stored with different casing (e.g. "Alonza"). The keyword predicates must
    fold case (LOWER(col) LIKE lowercased-pattern) so the match works on both
    Postgres and MariaDB.
    """

    matching_ts = datetime(2026, 7, 3, tzinfo=timezone.utc)

    class DummyCursor:
        def __init__(self) -> None:
            self.queries: list[tuple[str, list[object] | None]] = []

        async def execute(self, sql: str, params=None) -> None:
            self.queries.append((sql, list(params) if params is not None else None))

        async def fetchall(self):
            last_sql, last_params = self.queries[-1]
            if "FROM memories" in last_sql:
                return [
                    (
                        "memories",
                        19417,
                        matching_ts,
                        "chat:telegram_bot/-1 | sender:Alonza | auto preferita",
                        '["grillo", "passive"]',
                    )
                ]
            return []

        async def __aenter__(self) -> "DummyCursor":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    class DummyConn:
        def __init__(self) -> None:
            self.cursor_obj = DummyCursor()

        async def __aenter__(self) -> "DummyConn":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        def cursor(self) -> DummyCursor:
            return self.cursor_obj

    conn_instance = DummyConn()

    monkeypatch.setattr(scm, "get_conn_ctx", lambda: conn_instance)
    monkeypatch.setattr(scm, "_get_db_type", lambda: "postgres")

    results = await scm.search_memories(
        keywords=["alonza"],
        include_chat=False,
        limit=5,
    )

    assert len(results) == 1
    assert results[0]["source"] == "memories"
    assert "Alonza" in results[0]["snippet"]

    # The keyword predicate must fold case: LOWER(content) with a lowercased
    # pattern. No raw case-sensitive "content LIKE" should be emitted, and the
    # pattern parameter must be lowercase.
    mem_sql, mem_params = next(
        (sql, params)
        for sql, params in conn_instance.cursor_obj.queries
        if "FROM memories" in sql
    )
    assert "LOWER(content) LIKE %s" in mem_sql
    assert "%alonza%" in (mem_params or [])
    assert "%Alonza%" not in (mem_params or [])


@pytest.mark.asyncio
async def test_synth_core_search_memories_reserves_slots_for_long_term_memories(
    monkeypatch,
) -> None:
    """Regression: a purely chronological truncation lets a high-volume source
    (recent chat_history / ai_diary turns) monopolize the limited result set and
    evict older-but-relevant rows from the `memories` table. A long-term fact
    (e.g. Alonza's favourite car recorded weeks ago) must still surface even when
    many fresher chat rows also match the query.
    """

    old_ts = datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)
    recent_base = datetime(2026, 7, 3, 5, 0, tzinfo=timezone.utc)

    long_term_row = (
        "memories",
        19417,
        old_ts,
        "sender:Alonza | Per le Supercar nessuna supererà le forme della 458/488",
        '["grillo", "passive"]',
    )
    # Many fresher chat rows that also match the keyword.
    chat_rows = [
        (
            "chat_history",
            70190 + i,
            recent_base.replace(minute=i),
            f"Chi è Alonza? (turno {i})",
            None,
        )
        for i in range(20)
    ]

    class DummyCursor:
        def __init__(self) -> None:
            self.queries: list[tuple[str, list[object] | None]] = []

        async def execute(self, sql: str, params=None) -> None:
            self.queries.append((sql, list(params) if params is not None else None))

        async def fetchall(self):
            last_sql, _ = self.queries[-1]
            if "FROM memories" in last_sql:
                return [long_term_row]
            if "FROM chat_history_cache" in last_sql:
                return chat_rows
            return []

        async def __aenter__(self) -> "DummyCursor":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    class DummyConn:
        def __init__(self) -> None:
            self.cursor_obj = DummyCursor()

        async def __aenter__(self) -> "DummyConn":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        def cursor(self) -> DummyCursor:
            return self.cursor_obj

    conn_instance = DummyConn()

    monkeypatch.setattr(scm, "get_conn_ctx", lambda: conn_instance)
    monkeypatch.setattr(scm, "_get_db_type", lambda: "postgres")

    results = await scm.search_memories(
        keywords=["alonza"],
        include_chat=True,
        limit=5,
    )

    assert len(results) == 5
    # The long-term memory must not be evicted by the fresher chat turns.
    mem_ids = [r["id"] for r in results if r["source"] == "memories"]
    assert 19417 in mem_ids, (
        "long-term Alonza memory was evicted by recent chat rows: "
        f"{[(r['source'], r['id']) for r in results]}"
    )


@pytest.mark.asyncio
async def test_synth_core_search_memories_rare_keyword_survives_generic_dilution(
    monkeypatch,
) -> None:
    """Regression (BUG 3 — keyword dilution): a request mixes a rare, discriminating
    token ("alonza") with generic tokens ("test", "prova") that match many more
    recent rows in the SAME `memories` source. With pure recency ordering the
    generic-token matches (higher ids / fresher) fill the per-source slots and
    the older Alonza fact is evicted before it can reach the prompt.

    The fix ranks rows by keyword rarity (inverse document frequency within the
    pool) so the row matching the rare token survives truncation. This is purely
    statistical — it never inspects the meaning of any word.
    """

    old_ts = datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)
    recent_base = datetime(2026, 7, 3, 5, 0, tzinfo=timezone.utc)

    # The discriminating fact: only this row contains "alonza".
    alonza_row = (
        "memories",
        19417,
        old_ts,
        "sender:Alonza | Per le Supercar nessuna supererà le forme della 458/488",
        '["grillo", "passive"]',
    )
    # Many fresher, higher-id memories that match ONLY the generic tokens.
    generic_rows = [
        (
            "memories",
            20000 + i,
            recent_base.replace(minute=i),
            f"Un altro test / prova numero {i} senza informazioni utili",
            '["misc"]',
        )
        for i in range(12)
    ]

    class DummyCursor:
        def __init__(self) -> None:
            self.queries: list[tuple[str, list[object] | None]] = []

        async def execute(self, sql: str, params=None) -> None:
            self.queries.append((sql, list(params) if params is not None else None))

        async def fetchall(self):
            last_sql, _ = self.queries[-1]
            if "FROM memories" in last_sql:
                # Postgres ORDER BY timestamp DESC would return fresh generic
                # rows first, then the old Alonza row.
                return generic_rows + [alonza_row]
            return []

        async def __aenter__(self) -> "DummyCursor":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    class DummyConn:
        def __init__(self) -> None:
            self.cursor_obj = DummyCursor()

        async def __aenter__(self) -> "DummyConn":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        def cursor(self) -> DummyCursor:
            return self.cursor_obj

    conn_instance = DummyConn()

    monkeypatch.setattr(scm, "get_conn_ctx", lambda: conn_instance)
    monkeypatch.setattr(scm, "_get_db_type", lambda: "postgres")

    results = await scm.search_memories(
        keywords=["test", "prova", "alonza"],
        include_chat=False,
        limit=5,
    )

    assert len(results) == 5
    mem_ids = [r["id"] for r in results if r["source"] == "memories"]
    assert 19417 in mem_ids, (
        "rare-keyword Alonza memory was diluted out by generic-token matches: "
        f"{[(r['source'], r['id']) for r in results]}"
    )
    # No internal scoring field must leak into the public result.
    assert all("_relevance" not in r for r in results)


@pytest.mark.asyncio
async def test_build_json_prompt_merges_soul_recalled_memories(monkeypatch):
    soul_memory = (
        "[SOUL recalled memory | 2026-04-18 | same chat] Scarlet loves jasmine tea."
    )

    async def fake_build_context(
        self,
        *,
        message,
        context_memory,
        interface_name,
        text,
        memories,
        history_scope=None,
    ):
        del self, message, context_memory, interface_name, text, memories, history_scope
        return {"memories": ["Legacy memory"]}

    async def fake_gather_static_injections(message, context_memory):
        del message, context_memory
        return {"soul_recalled_memories": [soul_memory]}

    async def fake_gather_recon_contributions(**kwargs):
        del kwargs
        return []

    async def fake_resolve_language(**kwargs):
        del kwargs
        return None

    async def fake_resolve_tone(**kwargs):
        del kwargs
        return None, None

    monkeypatch.setattr("core.prompt_engine.extract_tags", lambda _text: [])
    monkeypatch.setattr("core.prompt_engine.expand_tags", lambda tags: tags)
    monkeypatch.setattr(
        "core.history_engine.HistoryEngine.build_context", fake_build_context
    )
    monkeypatch.setattr(
        "core.action_parser.gather_static_injections", fake_gather_static_injections
    )
    monkeypatch.setattr(
        "core.recon.gather_recon_contributions", fake_gather_recon_contributions
    )
    monkeypatch.setattr("core.recon.resolve_language", fake_resolve_language)
    monkeypatch.setattr("core.recon.resolve_tone", fake_resolve_tone)
    monkeypatch.setattr(
        "core.prompt_engine.load_json_instructions",
        lambda: "RESPOND ONLY WITH VALID JSON",
    )

    message = SimpleNamespace(
        interface_path="telegram_bot/123",
        text="hello",
        caption=None,
        message_id=1,
        date=datetime.now(timezone.utc),
        from_user=None,
        reply_to_message=None,
    )

    result = await build_json_prompt(message, {}, interface_name="telegram_bot")

    assert result["context"]["memories"] == [
        "Legacy memory",
        "Recalled memory from 2026-04-18 (same chat): Scarlet loves jasmine tea.",
    ]
