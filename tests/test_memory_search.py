import json
import re

import plugins.memory_search as memory_search_module
from plugins.memory_search import MemorySearchPlugin, _parse_time_window_spec


def test_memory_search_is_enabled_uses_config(monkeypatch) -> None:
    monkeypatch.setattr(
        memory_search_module.config_registry,
        "get_value",
        lambda key, default=None, **kwargs: (
            False if key == "ENABLE_MEMORY_SEARCH" else default
        ),
    )

    plugin = MemorySearchPlugin()
    assert plugin.is_enabled() is False

    monkeypatch.setattr(
        memory_search_module.config_registry,
        "get_value",
        lambda key, default=None, **kwargs: (
            True if key == "ENABLE_MEMORY_SEARCH" else default
        ),
    )
    assert plugin.is_enabled() is True


def test_build_query_tags(monkeypatch):
    monkeypatch.setattr(memory_search_module, "_get_db_type", lambda: "mariadb")
    plugin = MemorySearchPlugin()
    payload = {"mode": "tags", "tags": ["Mostro", "Austriaco"]}
    union_q, params = plugin._build_query_and_params(payload, 5)

    _GRILLO_EXC = (
        "(interaction_summary NOT LIKE '%%@grillo%%' "
        "AND interaction_summary NOT LIKE '%%grillo%%' "
        "AND interaction_summary NOT LIKE '%%self-reflection%%' "
        "AND interaction_summary NOT LIKE '%%self reflection%%' "
        "AND interaction_summary NOT LIKE '%%curiosity exploration%%' "
        "AND interaction_summary NOT LIKE '%%Internal reflection%%' "
        "AND interaction_summary NOT LIKE '%%sensory mapping%%' "
        "AND personal_thought NOT LIKE '%%@grillo%%')"
    )
    expected_q = (
        "(SELECT 'memories' AS source, MIN(id) AS id, MAX(created_at) AS created_at, content "
        "FROM memories WHERE (JSON_CONTAINS(tags, %s) OR JSON_CONTAINS(tags, %s)) "
        "GROUP BY content ORDER BY created_at DESC LIMIT %s) "
        "UNION ALL "
        f"(SELECT 'ai_diary' AS source, id, created_at, content FROM ai_diary "
        f"WHERE (JSON_CONTAINS(context_tags, %s) OR JSON_CONTAINS(context_tags, %s)) AND {_GRILLO_EXC} "
        "ORDER BY created_at DESC LIMIT %s) "
        "ORDER BY created_at DESC LIMIT %s"
    )

    assert union_q == expected_q
    assert params == [
        json.dumps("Mostro"),
        json.dumps("Austriaco"),
        5,  # memories inner limit
        json.dumps("Mostro"),
        json.dumps("Austriaco"),
        5,  # diary inner limit
        5,  # outer limit
    ]


def test_build_query_free_includes_chat(monkeypatch):
    monkeypatch.setattr(memory_search_module, "_get_db_type", lambda: "mariadb")
    plugin = MemorySearchPlugin()
    payload = {"mode": "free", "keywords": ["mostro", "austriaco"]}
    union_q, params = plugin._build_query_and_params(payload, 10)

    _GRILLO_EXC = (
        "(interaction_summary NOT LIKE '%%@grillo%%' "
        "AND interaction_summary NOT LIKE '%%grillo%%' "
        "AND interaction_summary NOT LIKE '%%self-reflection%%' "
        "AND interaction_summary NOT LIKE '%%self reflection%%' "
        "AND interaction_summary NOT LIKE '%%curiosity exploration%%' "
        "AND interaction_summary NOT LIKE '%%Internal reflection%%' "
        "AND interaction_summary NOT LIKE '%%sensory mapping%%' "
        "AND personal_thought NOT LIKE '%%@grillo%%')"
    )
    expected_q = (
        "(SELECT 'memories' AS source, MIN(id) AS id, MAX(created_at) AS created_at, content "
        "FROM memories WHERE (content LIKE %s OR content LIKE %s) "
        "GROUP BY content ORDER BY created_at DESC LIMIT %s) "
        "UNION ALL "
        f"(SELECT 'ai_diary' AS source, id, created_at, content FROM ai_diary "
        "WHERE (personal_thought LIKE %s OR interaction_summary LIKE %s OR user_message LIKE %s "
        "OR personal_thought LIKE %s OR interaction_summary LIKE %s OR user_message LIKE %s) "
        f"AND {_GRILLO_EXC} "
        "ORDER BY created_at DESC LIMIT %s) "
        "UNION ALL "
        "(SELECT 'chat' AS source, id, created_at, message_text AS content FROM chat_history_cache "
        "WHERE (message_text LIKE %s OR message_text LIKE %s) "
        "ORDER BY created_at DESC LIMIT %s) "
        "ORDER BY created_at DESC LIMIT %s"
    )

    assert union_q == expected_q
    assert params == [
        "%mostro%",  # memories content
        "%austriaco%",  # memories content
        10,  # memories inner limit
        "%mostro%",  # diary personal_thought
        "%mostro%",  # diary interaction_summary
        "%mostro%",  # diary user_message
        "%austriaco%",  # diary personal_thought
        "%austriaco%",  # diary interaction_summary
        "%austriaco%",  # diary user_message
        10,  # diary inner limit
        "%mostro%",  # chat
        "%austriaco%",  # chat
        10,  # chat inner limit
        10,  # outer limit
    ]


def test_build_query_random_order(monkeypatch):
    monkeypatch.setattr(memory_search_module, "_get_db_type", lambda: "mariadb")
    plugin = MemorySearchPlugin()
    payload = {"mode": "free", "keywords": ["x"], "random": True}
    union_q, params = plugin._build_query_and_params(payload, 3)
    assert union_q.endswith("ORDER BY RAND() LIMIT %s")
    # 1 keyword -> mem(1+1) + diary(3+1) + chat(1+1) + outer(1) = 9
    assert len(params) == 9


def test_build_query_tags_postgres(monkeypatch):
    monkeypatch.setattr(memory_search_module, "_get_db_type", lambda: "postgres")
    plugin = MemorySearchPlugin()
    payload = {"mode": "tags", "tags": ["Mostro", "Austriaco"]}
    union_q, params = plugin._build_query_and_params(payload, 5)

    assert "JSON_CONTAINS" not in union_q
    assert "COALESCE(NULLIF(BTRIM(tags), ''), '[]')::jsonb ? %s" in union_q
    assert "COALESCE(NULLIF(BTRIM(context_tags), ''), '[]')::jsonb ? %s" in union_q
    assert params == ["Mostro", "Austriaco", 5, "Mostro", "Austriaco", 5, 5]


def test_build_query_random_order_postgres(monkeypatch):
    monkeypatch.setattr(memory_search_module, "_get_db_type", lambda: "postgres")
    plugin = MemorySearchPlugin()
    payload = {"mode": "free", "keywords": ["x"], "random": True}
    union_q, _params = plugin._build_query_and_params(payload, 3)

    assert union_q.endswith("ORDER BY RANDOM() LIMIT %s")


def test_parse_time_window_yesterday():
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    r = _parse_time_window_spec("yesterday")
    assert r is not None
    start, end = r
    # 'yesterday' mapped to 48 hours per spec
    diff = end - start
    assert abs(diff - timedelta(hours=48)) < timedelta(minutes=1)
    # end should be approximately now
    assert abs((now - end).total_seconds()) < 5


def test_build_query_includes_timestamp_for_time_only():
    plugin = MemorySearchPlugin()
    payload = {"mode": "free", "time_window": "yesterday"}
    q, params = plugin._build_query_and_params(payload, max_results=5)
    assert q
    # Should include created_at clause
    assert "created_at" in q
    # Params should contain at least one ISO-like timestamp
    found_iso = any(
        re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", str(x)) for x in params
    )
    assert found_iso


def test_validate_payload_allows_time_only_free_mode():
    plugin = MemorySearchPlugin()
    payload = {"mode": "free", "time_window": "yesterday"}
    errors = plugin.validate_payload({"payload": payload})
    # Should not produce validation errors
    assert errors == []


def test_parse_iso_range_string():
    # Use a fixed ISO range
    spec = "2026-01-10T00:00:00Z/2026-01-12T23:59:59Z"
    r = _parse_time_window_spec(spec)
    assert r is not None
    start, end = r
    assert start.tzinfo is not None and end.tzinfo is not None
    assert start.year == 2026 and start.month == 1 and start.day == 10
    assert end.year == 2026 and end.month == 1 and end.day == 12


def test_parse_date_only_range_string():
    spec = "2026-01-10/2026-01-12"
    r = _parse_time_window_spec(spec)
    assert r is not None
    start, end = r
    # dates without times should produce start at 00:00 and end at end-of-day
    assert start.hour == 0 and start.minute == 0
    assert end.hour == 23 and end.minute == 59
