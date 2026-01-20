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