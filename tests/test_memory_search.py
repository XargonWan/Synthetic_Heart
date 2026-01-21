import json

from plugins.memory_search import MemorySearchPlugin


def test_build_query_tags():
    plugin = MemorySearchPlugin()
    payload = {"mode": "tags", "tags": ["Mostro", "Austriaco"]}
    union_q, params = plugin._build_query_and_params(payload, 5)

    expected_q = (
        "SELECT 'memories' AS source, id, timestamp, content FROM memories WHERE (JSON_CONTAINS(tags, %s) OR JSON_CONTAINS(tags, %s)) "
        "UNION ALL SELECT 'ai_diary' AS source, id, timestamp, content FROM ai_diary WHERE (JSON_CONTAINS(context_tags, %s) OR JSON_CONTAINS(context_tags, %s)) "
        "ORDER BY timestamp DESC LIMIT %s"
    )

    assert union_q == expected_q
    assert params == [json.dumps("Mostro"), json.dumps("Austriaco"), json.dumps("Mostro"), json.dumps("Austriaco"), 5]


def test_build_query_free_includes_chat():
    plugin = MemorySearchPlugin()
    payload = {"mode": "free", "keywords": ["mostro", "austriaco"]}
    union_q, params = plugin._build_query_and_params(payload, 10)

    expected_q = (
        "SELECT 'memories' AS source, id, timestamp, content FROM memories WHERE (content LIKE %s OR content LIKE %s) "
        "UNION ALL SELECT 'ai_diary' AS source, id, timestamp, content FROM ai_diary WHERE (content LIKE %s OR personal_thought LIKE %s OR interaction_summary LIKE %s OR user_message LIKE %s OR content LIKE %s OR personal_thought LIKE %s OR interaction_summary LIKE %s OR user_message LIKE %s) "
        "UNION ALL SELECT 'chat' AS source, id, timestamp, message_text AS content FROM chat_history_cache WHERE (message_text LIKE %s OR message_text LIKE %s) "
        "ORDER BY timestamp DESC LIMIT %s"
    )

    assert union_q == expected_q
    assert params == [
        '%mostro%', '%austriaco%',
        '%mostro%', '%mostro%', '%mostro%', '%mostro%',
        '%austriaco%', '%austriaco%', '%austriaco%', '%austriaco%',
        '%mostro%', '%austriaco%',
        10,
    ]


def test_build_query_random_order():
    plugin = MemorySearchPlugin()
    payload = {"mode": "free", "keywords": ["x"], "random": True}
    union_q, params = plugin._build_query_and_params(payload, 3)
    assert union_q.endswith("ORDER BY RAND() LIMIT %s")
    # ensure params length matches placeholders (1 mem + 4 diary + 1 chat + limit)
    assert len(params) == 1 + 4 + 1 + 1


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
    # Should include timestamp clause
    assert "timestamp" in q
    # Params should contain at least one ISO-like timestamp
    found_iso = any(re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", str(x)) for x in params)
    assert found_iso


def test_validate_payload_allows_time_only_free_mode():
    plugin = MemorySearchPlugin()
    payload = {"mode": "free", "time_window": "yesterday"}
    errors = plugin.validate_payload({"payload": payload})
    # Should not produce validation errors
    assert errors == []


def test_parse_iso_range_string():
    from datetime import timezone

    plugin = MemorySearchPlugin()
    # Use a fixed ISO range
    spec = "2026-01-10T00:00:00Z/2026-01-12T23:59:59Z"
    r = plugin._parse_time_window_spec(spec)
    assert r is not None
    start, end = r
    assert start.tzinfo is not None and end.tzinfo is not None
    assert start.year == 2026 and start.month == 1 and start.day == 10
    assert end.year == 2026 and end.month == 1 and end.day == 12


def test_parse_date_only_range_string():
    plugin = MemorySearchPlugin()
    spec = "2026-01-10/2026-01-12"
    r = plugin._parse_time_window_spec(spec)
    assert r is not None
    start, end = r
    # dates without times should produce start at 00:00 and end at end-of-day
    assert start.hour == 0 and start.minute == 0
    assert end.hour == 23 and end.minute == 59