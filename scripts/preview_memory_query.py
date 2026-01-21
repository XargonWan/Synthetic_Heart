#!/usr/bin/env python3
"""Quick script to preview the SQL query and params produced for a given payload.
This avoids importing the full project (no DB or aiomysql required).
"""
import json
from typing import Any, Dict, List


def build_query_and_params(payload: Dict[str, Any], max_results: int):
    params: List[Any] = []
    where_clauses_mem: List[str] = []
    where_clauses_diary: List[str] = []

    mode = payload.get("mode")

    if mode == "tags":
        tags = payload.get("tags", [])
        tag_conditions: List[str] = []
        for t in tags:
            tag_conditions.append("JSON_CONTAINS(tags, %s)")
            params.append(json.dumps(t))
        if tag_conditions:
            where_clauses_mem.append("(" + " OR ".join(tag_conditions) + ")")

        diary_tag_conditions: List[str] = []
        for t in tags:
            diary_tag_conditions.append("JSON_CONTAINS(context_tags, %s)")
            params.append(json.dumps(t))
        if diary_tag_conditions:
            where_clauses_diary.append("(" + " OR ".join(diary_tag_conditions) + ")")

    elif mode == "free":
        keywords = payload.get("keywords")
        if isinstance(keywords, list) and any(str(x).strip() for x in keywords):
            tokens = [str(x).strip() for x in keywords if str(x).strip()]
        else:
            query = payload.get("query", "")
            tokens = [q.strip() for q in str(query).split() if q.strip()]
        if not tokens:
            return "", []
        token_clauses: List[str] = []
        for tok in tokens:
            like = "%" + tok + "%"
            token_clauses.append("content LIKE %s")
            params.append(like)
        where_clauses_mem.append("(" + " OR ".join(token_clauses) + ")")

        diary_token_clauses: List[str] = []
        for tok in tokens:
            like = "%" + tok + "%"
            diary_token_clauses.append("content LIKE %s")
            params.append(like)
            diary_token_clauses.append("personal_thought LIKE %s")
            params.append(like)
            diary_token_clauses.append("interaction_summary LIKE %s")
            params.append(like)
            diary_token_clauses.append("user_message LIKE %s")
            params.append(like)
        where_clauses_diary.append("(" + " OR ".join(diary_token_clauses) + ")")

    queries: List[str] = []
    if where_clauses_mem:
        mem_where = " AND ".join(where_clauses_mem)
        queries.append(f"SELECT 'memories' AS source, id, timestamp, content FROM memories WHERE {mem_where}")
    if where_clauses_diary:
        diary_where = " AND ".join(where_clauses_diary)
        queries.append(f"SELECT 'ai_diary' AS source, id, timestamp, content FROM ai_diary WHERE {diary_where}")

    if not queries:
        return "", []

    union_q = " UNION ALL ".join(queries) + " ORDER BY timestamp DESC LIMIT %s"
    params.append(max_results)
    return union_q, params


if __name__ == "__main__":
    payload = {"mode": "tags", "tags": ["Mostro", "Austriaco"]}
    q, params = build_query_and_params(payload, 10)
    print("UNION_Q:")
    print(q)
    print("\nPARAMS:")
    print(params)
