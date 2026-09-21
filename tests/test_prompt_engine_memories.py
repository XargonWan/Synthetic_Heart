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
    # 0: token-selectivity measurement, 1: memories, 2: ai_diary, 3: chat history.
    assert len(queries) == 4
    assert queries[0].startswith("SELECT SUM(CASE WHEN LOWER(message_text) LIKE %s")
    assert "FROM chat_history_cache" in queries[0]
    assert all("JSON_CONTAINS" not in sql for sql in queries)
    assert "COALESCE(NULLIF(BTRIM(tags), ''), '[]')::jsonb ? %s" in queries[1]
    assert "COALESCE(NULLIF(BTRIM(context_tags), ''), '[]')::jsonb ? %s" in queries[2]
    assert conn_instance.cursor_obj.queries[1][1] == ["food", 15]
    assert conn_instance.cursor_obj.queries[2][1] == ["food", 15]
    # The chat tier binds the where token, then the same token for the relevance
    # ordering, then the pool limit.
    assert conn_instance.cursor_obj.queries[3][1] == ["%food%", "%food%", 15]


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
            "telegram_bot/-5293915984",
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
async def test_search_memories_excludes_chat_history_of_current_chat(
    monkeypatch,
) -> None:
    """The chat-history tier must not re-inject the chat being answered.

    The message currently being processed is persisted to chat_history_cache
    before the prompt is built, so a keyword search extracted from that very
    message would echo the live conversation (incl. stale greetings) back as
    "memories". Rows from the excluded interface_path must never appear.
    """
    captured_queries: list[tuple[str, list[object] | None]] = []

    class DummyCursor:
        def __init__(self) -> None:
            self.queries: list[tuple[str, list[object] | None]] = []

        async def execute(self, sql: str, params=None) -> None:
            stored = list(params) if params is not None else None
            self.queries.append((sql, stored))
            captured_queries.append((sql, stored))

        async def fetchall(self) -> list[list[object]]:
            last_sql, last_params = self.queries[-1]
            if "FROM chat_history_cache" in last_sql:
                # Simulate the DB honouring the NOT IN exclusion: when the
                # current chat's path is excluded, drop the row belonging to it
                # (identified here by the id 1) and keep the other-chat row.
                excluded_path = next(
                    (str(p) for p in (last_params or []) if "/" in str(p)),
                    None,
                )
                rows = [
                    [
                        "chat_history",
                        1,
                        "2026-08-15 06:26:34",
                        "Basically once its elevated i can reset it...",
                        None,
                        "telegram_bot/5208932647",
                    ],
                    [
                        "chat_history",
                        2,
                        "2026-08-14 06:00:00",
                        "older morning greeting from another chat",
                        None,
                        "telegram_bot/-5293915984",
                    ],
                ]
                if excluded_path:
                    return [r for r in rows if r[1] != 1]
                return rows
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
        keywords=["elevated", "reset"],
        include_chat=True,
        limit=5,
        exclude_interface_paths=["telegram_bot/5208932647"],
    )

    # The chat-history query must carry the NOT IN exclusion for the current
    # chat, AND-ed onto a *grouped* keyword predicate. Asserting only that the
    # string "interface_path NOT IN" appears is not enough: the original bug put
    # that predicate in the same OR-chain as the keywords, which made the whole
    # clause true for every row outside the excluded chats — so the current chat
    # was not excluded at all and the keyword filter stopped filtering. That
    # shipped because this test's fake cursor emulated the intended filtering in
    # Python instead of exercising the SQL it asserted.
    chat_queries = [
        (sql, params)
        for sql, params in captured_queries
        if "FROM chat_history_cache" in sql
    ]
    assert chat_queries, "chat-history tier query was not issued"
    chat_sql, chat_params = chat_queries[-1]
    assert "interface_path NOT IN" in chat_sql
    assert "telegram_bot/5208932647" in [str(p) for p in (chat_params or [])]

    where = chat_sql.split("WHERE", 1)[1].rsplit("ORDER BY", 1)[0]
    assert " AND interface_path NOT IN" in where, (
        "the exclusion must be AND-ed onto the keyword group, not OR-ed with "
        f"it: {where}"
    )
    keyword_part = where.split(" AND interface_path NOT IN", 1)[0].strip()
    assert keyword_part.startswith("(") and keyword_part.endswith(")"), (
        f"the keyword predicates must be grouped in one parenthesised OR-chain: {where}"
    )
    assert " OR " in keyword_part

    # The row from the excluded current chat must not be returned; the
    # other-chat row may surface.
    snippets = [str(r.get("snippet") or "") for r in results]
    assert not any("elevated i can reset" in s for s in snippets), (
        f"current chat's own message was echoed back as a memory: {snippets}"
    )


@pytest.mark.asyncio
async def test_build_json_prompt_merges_soul_recalled_memories(monkeypatch):
    soul_memory = (
        "[SOUL recalled memory | 2026-04-18 | same chat] Alice loves jasmine tea."
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
        # Accepts the optional route argument, mirroring the real signature:
        # build_prompt_request passes the derived instruction route.
        lambda *args, **kwargs: "RESPOND ONLY WITH VALID JSON",
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
        "Recalled memory from 2026-04-18 (same chat): Alice loves jasmine tea.",
    ]


def test_chat_history_exclusion_holds_under_real_sql() -> None:
    """Evaluate the generated clause with a real SQL engine.

    The cursor-level test above can only assert what the SQL *string* looks
    like, and its fake cursor emulated the intended filtering in Python — which
    is exactly how the OR-join bug shipped: the assertion covered the intent
    while the generated clause filtered nothing. Running the real clause
    against a real engine fails if the exclusion is OR-ed with the keywords.
    """
    import sqlite3

    current = "telegram_bot/5208932647"
    where, params = scm._build_chat_history_where(
        ["elevated", "reset"], excluded_paths=[current]
    )
    assert where, "a keyword tier must produce a WHERE clause"

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE chat_history_cache "
        "(id INTEGER, interface_path TEXT, message_text TEXT)"
    )
    conn.executemany(
        "INSERT INTO chat_history_cache VALUES (?, ?, ?)",
        [
            (1, current, "Basically once its elevated i can reset it"),
            (2, "telegram_bot/-5293915984", "an elevated core needs a reset"),
            (3, "telegram_bot/-5293915984", "a line with no matching keyword"),
        ],
    )
    rows = conn.execute(
        f"SELECT id FROM chat_history_cache WHERE {where.replace('%s', '?')} "
        "ORDER BY id",
        list(params),
    ).fetchall()
    conn.close()

    ids = [row[0] for row in rows]
    assert 1 not in ids, f"the current chat's own line survived the exclusion: {ids}"
    assert ids == [2], (
        "only the other-chat row that actually matches a keyword may surface, "
        f"got: {ids}"
    )


def test_chat_history_where_without_exclusion_still_groups_keywords() -> None:
    """With nothing excluded, the keyword OR-chain must still be one group."""
    where, params = scm._build_chat_history_where(["alpha", "beta"], None)

    assert where == "(LOWER(message_text) LIKE %s OR LOWER(message_text) LIKE %s)"
    assert params == ["%alpha%", "%beta%"]
    assert "NOT IN" not in where

    # No tokens and nothing excluded -> no clause at all, so the tier is skipped
    # rather than issuing an unfiltered scan of the whole cache.
    assert scm._build_chat_history_where([], None) == ("", [])


def test_stored_memory_entries_carry_their_source_and_date() -> None:
    """A dict hit from the memories/ai_diary/chat_history tiers keeps provenance.

    Rendering only the entry's text dropped its source and its timestamp, so a
    raw line lifted from another conversation reached the prompt looking like a
    remembered fact: undated, unattributed, and indistinguishable from the
    model's own recollection.
    """
    entry = {
        "source": "chat_history",
        "id": 14530,
        "timestamp": "2026-09-19T11:34:32.123456+00:00",
        "snippet": 'Well i did say "as long as you like it" did I not',
        "tags": [],
        "interface_path": "telegram_bot/-5293915984",
    }

    rendered = pe._humanize_context_entry(entry, kind="memories")

    assert rendered == (
        "Recalled memory from 2026-09-19 "
        "(telegram_bot/-5293915984, chat history): "
        'Well i did say "as long as you like it" did I not'
    )


def test_stored_memory_label_survives_a_missing_date_and_unknown_source() -> None:
    """A hit with no timestamp and a new source still names what it is."""
    assert (
        pe._humanize_context_entry(
            {"source": "some_new_tier", "snippet": "body text"}, kind="memories"
        )
        == "Recalled memory (some new tier): body text"
    )
    assert (
        pe._humanize_context_entry({"snippet": "body text"}, kind="memories")
        == "Recalled memory (stored memory): body text"
    )


def test_memory_merge_key_ignores_the_row_id() -> None:
    """Two rows holding the same sentence are one memory, whatever their ids.

    The store writes pairs of identical rows (`memories` ids 1690/1691,
    1692/1693, 1694/1695 live), and a merge key built from the id kept both, so a
    single sentence used two of the limited memory slots in every prompt.
    """
    first = {"source": "memories", "id": 1690, "snippet": "The moonlight is tracing."}
    twin = {"source": "memories", "id": 1691, "snippet": "The  moonlight   is tracing."}

    assert pe._memory_merge_key(first) == pe._memory_merge_key(twin)

    merged = pe._merge_memory_entries([first], [twin])
    assert merged == [first]


class _AggregateCursor:
    """Cursor that answers the token-selectivity aggregate with fixed counts."""

    def __init__(self, row: list[int]) -> None:
        self.row = row
        self.queries: list[tuple[str, list[object] | None]] = []

    async def execute(self, sql: str, params=None) -> None:
        self.queries.append((sql, list(params) if params is not None else None))

    async def fetchall(self) -> list[list[int]]:
        return [self.row]


@pytest.mark.asyncio
async def test_selective_keywords_drop_tokens_that_match_most_of_the_store() -> None:
    """A token that is in most rows says nothing about which row is wanted.

    Live: 'you' was in 89% of rows and 'your' in 53%, so the keyword clause
    matched 93% of the cache and filtered nothing.
    """
    # 2700/3000 = 90% "you" (dropped); 600/3000 = 20% "dee" (kept).
    cursor = _AggregateCursor([2700, 600, 3000])

    assert await scm._selective_keywords(cursor, ["you", "dee"]) == ["dee"]


@pytest.mark.asyncio
async def test_selective_keywords_never_drop_a_rare_token() -> None:
    """The rare-token guarantee is what the tier is for, so it must survive."""
    cursor = _AggregateCursor([0, 3000])

    assert await scm._selective_keywords(cursor, ["alonza"]) == ["alonza"]


@pytest.mark.asyncio
async def test_selective_keywords_drop_tokens_too_short_to_discriminate() -> None:
    cursor = _AggregateCursor([0, 3000])

    assert await scm._selective_keywords(cursor, ["a", "of", "dee"]) == ["dee"]


@pytest.mark.asyncio
async def test_selective_keywords_fail_open_when_the_store_cannot_answer() -> None:
    """A store that cannot be measured keeps every token, exactly as before."""

    class _BrokenCursor:
        async def execute(self, sql: str, params=None) -> None:
            raise RuntimeError("no such table")

    assert await scm._selective_keywords(_BrokenCursor(), ["you", "dee"]) == [
        "you",
        "dee",
    ]


def test_chat_history_order_ranks_by_matched_tokens_then_recency() -> None:
    """The tier's fixed slot budget must go to the best match, not the newest row."""
    sql, params = scm._chat_history_order(["dee", "jasmine"])

    assert sql.count("CASE WHEN LOWER(message_text) LIKE %s") == 2
    assert sql.endswith(") DESC, created_at DESC")
    assert params == ["%dee%", "%jasmine%"]

    # No tokens: plain recency, so the tier keeps a stable ordering.
    assert scm._chat_history_order([]) == ("created_at DESC", [])


@pytest.mark.asyncio
async def test_identical_store_rows_occupy_one_slot(monkeypatch) -> None:
    """Live duplicate rows must collapse to a single memory entry.

    `memories` holds pairs of rows with identical content (1690/1691, 1692/1693,
    1694/1695 on 2026-09-19). The dedupe key carried the row id, so both copies
    survived and one sentence took two of the limited slots.
    """
    row = [
        "memories",
        1690,
        datetime(2026, 4, 16, 12, 0, tzinfo=timezone.utc),
        "(chat:telegram_bot/-5028544398 | sender:self) The moonlight is tracing sharp lines.",
        '["observer"]',
    ]
    twin = list(row)
    twin[1] = 1691

    class DummyCursor:
        def __init__(self) -> None:
            self.queries: list[tuple[str, list[object] | None]] = []

        async def execute(self, sql: str, params=None) -> None:
            self.queries.append((sql, list(params) if params is not None else None))

        async def fetchall(self):
            last_sql = self.queries[-1][0]
            if "FROM memories" in last_sql:
                return [row, twin]
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
        keywords=["moonlight"], include_chat=False, limit=5
    )

    assert len(results) == 1, f"the identical twin row was kept: {results}"
    assert results[0]["id"] == 1690
