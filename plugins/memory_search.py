"""Memory Search plugin

Provides `memory_search` action to search `memories` and `ai_diary` tables in two modes:
- mode: "tags" -> payload: {"tags": [...], "max_results": int}
- mode: "free" -> payload: {"keywords": ["..."], "max_results": int} (preferred) or {"query": "...", "max_results": int}

When executed the plugin returns results and requests an LLM delivery so the model
can see the found memories and continue its response.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone
import re


# Helper: map human-friendly keywords to sensible durations (some include slack already)
_SPECIAL_TIME_MAP = {
    "yesterday": timedelta(hours=48),  # user expects a wider radius for 'yesterday'
    "last week": timedelta(days=10),    # expand 'last week' to ~10 days
    "last_week": timedelta(days=10),
}


def _parse_time_window_spec(spec: Any) -> Optional[Tuple[datetime, datetime]]:
    """Parse a time_window specification and return (start_dt, end_dt) in UTC.

    Supported forms:
      - string: 'yesterday', 'last week', '48 hours', '3 days', '7d', '2h'
      - ISO interval string: '2026-01-10/2026-01-12' or '2026-01-10T00:00:00Z/2026-01-12T23:59:59Z'
      - object with 'start' and/or 'end' ISO datetimes
      - object with 'duration': {'days': n, 'hours': m}

    If only a duration or keyword is provided, `end` is now (UTC) and `start` is `now - duration*slack`.
    For keywords in _SPECIAL_TIME_MAP we use the mapped timedelta directly and do NOT apply extra slack.
    For parsed numeric durations we add a 20% slack (multiply duration by 1.2).
    Returns None if it cannot interpret the spec.
    """
    now = datetime.now(timezone.utc)

    def _parse_iso_component(s: str, is_end: bool = False) -> Optional[datetime]:
        """Parse a single ISO datetime or date component.

        - Accepts full ISO datetimes, optionally ending with 'Z'
        - Accepts date-only 'YYYY-MM-DD' and returns start-of-day (or end-of-day when is_end=True)
        """
        if not isinstance(s, str) or not s.strip():
            return None
        s2 = s.strip()
        # Normalize trailing Z to +00:00 for fromisoformat
        if s2.endswith("Z"):
            s2 = s2[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s2)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            # Try date-only YYYY-MM-DD
            m = re.match(r"^(?P<d>\d{4}-\d{2}-\d{2})$", s2)
            if m:
                if is_end:
                    return datetime.fromisoformat(m.group("d") + "T23:59:59.999999+00:00")
                else:
                    return datetime.fromisoformat(m.group("d") + "T00:00:00+00:00")
            return None

    # If dict-like with explicit start/end
    if isinstance(spec, dict):
        start = None
        end = None
        dur = spec.get("duration")
        if spec.get("start"):
            start = _parse_iso_component(str(spec.get("start")), is_end=False)
        if spec.get("end"):
            end = _parse_iso_component(str(spec.get("end")), is_end=True)
        if dur and isinstance(dur, dict):
            d = timedelta(days=int(dur.get("days", 0)), hours=int(dur.get("hours", 0)))
            # apply 20% slack
            d = timedelta(seconds=int(d.total_seconds() * 1.2))
            start = now - d
            end = now
        if start or end:
            if not start:
                start = now - timedelta(days=365 * 10)  # very old fallback
            if not end:
                end = now
            return (start, end)

    # If string form
    if isinstance(spec, str):
        s = spec.strip()
        s_lower = s.lower()

        # ISO interval e.g. '2026-01-10/2026-01-12' or with times
        if "/" in s:
            left, right = [p.strip() for p in s.split("/", 1)]
            start_dt = _parse_iso_component(left, is_end=False)
            end_dt = _parse_iso_component(right, is_end=True)
            if start_dt and end_dt:
                return (start_dt, end_dt)

        # special keywords
        if s_lower in _SPECIAL_TIME_MAP:
            d = _SPECIAL_TIME_MAP[s_lower]
            return (now - d, now)

        # patterns: '48 hours', '3 days', '7d', '2h'
        m = re.match(r"^(?P<num>\d+)\s*(?P<unit>d|days|h|hours)$", s_lower)
        if m:
            num = int(m.group("num"))
            unit = m.group("unit")
            if unit.startswith("d"):
                d = timedelta(days=num)
            else:
                d = timedelta(hours=num)
            # 20% slack
            d = timedelta(seconds=int(d.total_seconds() * 1.2))
            return (now - d, now)

    return None

from core.core_initializer import register_plugin
from core.logging_utils import log_info, log_debug, log_error, log_warning
from core.config_manager import config_registry
from core.db import get_conn_ctx
from core.variables_engine import register_exposed_var
from core.auto_response import request_llm_delivery

# Exposed variables
register_exposed_var(
    "ENABLE_MEMORY_SEARCH",
    label="Enable Memory Search",
    default=True,
    value_type=bool,
    ui_type="bool",
    description="Toggle memory_search plugin behavior (when off, searches are not performed)",
    scope="core",
    component="memory_search",
)

register_exposed_var(
    "MEMORY_SEARCH_MAX_RESULTS",
    label="Memory Search Max Results",
    default=10,
    value_type=int,
    ui_type="number",
    description="Maximum number of memory results returned by memory_search",
    scope="core",
    component="memory_search",
)


class MemorySearchPlugin:
    display_name = "Memory Search"

    def __init__(self):
        register_plugin("memory_search", self)

    def get_supported_actions(self):
        return {
            "memory_search": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string", "enum": ["tags", "free"]},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "keywords": {"type": "array", "items": {"type": "string"}},
                        "query": {"type": "string"},
                        "max_results": {"type": "integer"},
                        "time_window": {"description": "Optional time window for the search. Can be a string like 'yesterday', 'last week', '48 hours' or an object with explicit 'start'/'end' ISO datetimes or a 'duration' object.", "oneOf": [{"type": "string"}, {"type": "object"}]},
                    },
                    "required": ["mode"],
                },
                "brief": "Search memories by tags or free text in ai_diary and memories tables",
                "examples": {
                    "tags": {"mode": "tags", "tags": ["monster", "austria"]},
                    "free": {"mode": "free", "keywords": ["austrian", "monster"], "time_window": "yesterday"},
                },
            }
        }

    def get_prompt_instructions(self, action_type: str) -> Optional[str]:
        if action_type != "memory_search":
            return None
        return (
            "Use `memory_search` when you lack information to answer. "
            "Call it with mode='tags' and a list of tags, or mode='free' and a list of keywords (preferred) or a free-text query. "
            "The plugin will return a list of matching memory snippets."
        )

    def validate_payload(self, action: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        payload = action.get("payload") or {}
        mode = payload.get("mode")
        if not mode or mode not in ("tags", "free"):
            errors.append("'mode' must be one of 'tags' or 'free'")
            return errors
        if mode == "tags":
            tags = payload.get("tags")
            if not isinstance(tags, list) or not tags:
                errors.append("For mode 'tags' provide a non-empty 'tags' array")
        if mode == "free":
            kws = payload.get("keywords")
            q = payload.get("query")
            has_keywords = isinstance(kws, list) and any(str(x).strip() for x in kws)
            has_query = isinstance(q, str) and q.strip()
            has_time = bool(payload.get("time_window") is not None)
            if not has_keywords and not has_query and not has_time:
                errors.append("For mode 'free' provide a non-empty 'keywords' list (preferred), a 'query' string, or a 'time_window'")
        max_r = payload.get("max_results")
        if max_r is not None:
            try:
                if int(max_r) <= 0:
                    errors.append("'max_results' must be a positive integer")
            except Exception:
                errors.append("'max_results' must be an integer")
        return errors

    def _build_query_and_params(self, payload: Dict[str, Any], max_results: int):
        """Internal helper: build the UNION query and corresponding params without executing it."""
        params: List[Any] = []
        where_clauses_mem: List[str] = []
        where_clauses_diary: List[str] = []
        where_clauses_chat: List[str] = []

        mode = payload.get("mode")
        # Option to randomize results instead of ordering by timestamp (uses MySQL RAND()).
        randomize = bool(payload.get("random", False))

        if mode == "tags":
            tags = payload.get("tags", [])
            # JSON_CONTAINS for memories.tags and ai_diary.context_tags
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

            # For ai_diary search content/personal_thought/interaction_summary/user_message
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

            # Also search recent chat history (chat_history_cache.message_text) so
            # user-sent messages (like reporting a dream) are included in results.
            # Only applied for mode='free'.
            chat_token_clauses: List[str] = []
            for tok in tokens:
                like = "%" + tok + "%"
                chat_token_clauses.append("message_text LIKE %s")
                params.append(like)
            if chat_token_clauses:
                where_clauses_chat.append("(" + " OR ".join(chat_token_clauses) + ")")

        # Apply optional time window (may produce clauses even when no token/tag filters exist)
        time_spec = payload.get("time_window")
        time_range = _parse_time_window_spec(time_spec) if time_spec is not None else None
        time_clause_parts: List[str] = []
        time_params: List[Any] = []
        if time_range:
            start_dt, end_dt = time_range
            time_clause_parts.append("timestamp >= %s")
            time_clause_parts.append("timestamp <= %s")
            # Use ISOformat UTC strings so DB comparison is deterministic
            time_params.extend([start_dt.isoformat(), end_dt.isoformat()])

        # Compose union query
        queries: List[str] = []

        def _maybe_add_table(table_where_clauses: List[str], table_name: str, select_expr: str):
            clauses = list(table_where_clauses)  # copy
            if time_clause_parts:
                clauses.extend(time_clause_parts)
            if clauses:
                where = " AND ".join(clauses)
                queries.append(f"{select_expr} WHERE {where}")

        if where_clauses_mem or time_clause_parts:
            _maybe_add_table(where_clauses_mem, 'memories', "SELECT 'memories' AS source, id, timestamp, content FROM memories")
        if where_clauses_diary or time_clause_parts:
            _maybe_add_table(where_clauses_diary, 'ai_diary', "SELECT 'ai_diary' AS source, id, timestamp, content FROM ai_diary")
        if where_clauses_chat or time_clause_parts:
            _maybe_add_table(where_clauses_chat, 'chat_history_cache', "SELECT 'chat' AS source, id, timestamp, message_text AS content FROM chat_history_cache")

        if not queries:
            return "", []

        # Prepend time params consistently (for each table that has time clauses we must add start/end in the same order we added them above)
        # We will insert time params for each table that has time clauses (equal to number of queries that include time clauses)
        final_params: List[Any] = []
        for q in queries:
            if time_clause_parts:
                final_params.extend(time_params)
            # For content/token clauses the earlier 'params' list already contains them in the same order as we constructed clauses
        # The content-related params are in 'params' in order (tags/tokens). We'll append them after the time params per-table grouping.
        # For simplicity we append all content params next (since placeholders are positional, the DB adapter will map them correctly as long as ordering matches the query).
        final_params.extend(params)

        # If randomize: order by RAND(), otherwise order by timestamp desc
        order_clause = " ORDER BY RAND() LIMIT %s" if randomize else " ORDER BY timestamp DESC LIMIT %s"
        union_q = " UNION ALL ".join(queries) + order_clause
        # For the LIMIT param
        final_params.append(max_results)

        return union_q, final_params
        # If randomize flag is set, use RAND() ordering to return varied results.
        # Note: ORDER BY RAND() can be slow on large tables; acceptable for small limits but consider a sampling strategy if needed.
        order_clause = " ORDER BY RAND() LIMIT %s" if randomize else " ORDER BY timestamp DESC LIMIT %s"
        union_q = " UNION ALL ".join(queries) + order_clause
        params.append(max_results)
        return union_q, params

    async def execute_action(self, action: Dict[str, Any], context: Dict[str, Any], bot, original_message) -> Dict[str, Any]:
        payload = action.get("payload") or {}

        # When running as a prompt preflight, do NOT request an extra LLM delivery.
        # The caller will inject the results into the main prompt context.
        is_preflight = bool((context or {}).get("preflight"))

        # Check toggle
        enabled = bool(config_registry.get_value("ENABLE_MEMORY_SEARCH", True, value_type=bool))
        if not enabled:
            log_info("[memory_search] Plugin disabled by config; skipping search")
            return {"processed": True, "results": []}

        # Determine max results
        default_max = int(config_registry.get_value("MEMORY_SEARCH_MAX_RESULTS", 10, value_type=int) or 10)
        max_results = int(payload.get("max_results") or default_max)

        mode = payload.get("mode")
        results: List[Dict[str, Any]] = []

        try:
            # Build queries for both tables via helper to make testing easier
            union_q, params = self._build_query_and_params(payload, max_results)
            if not union_q:
                return {"processed": True, "results": []}

            log_debug(f"[memory_search] Executing query: {union_q} params={params}")

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(union_q, params)
                    rows = await cur.fetchall()

                    for r in rows:
                        src, _id, ts, content = r
                        try:
                            ts_iso = ts.isoformat() if hasattr(ts, 'isoformat') else str(ts)
                        except Exception:
                            ts_iso = str(ts)
                        snippet = content if isinstance(content, str) else str(content)
                        if len(snippet) > 400:
                            snippet = snippet[:400] + "..."
                        results.append({
                            "source": src,
                            "id": _id,
                            "timestamp": ts_iso,
                            "snippet": snippet,
                        })

            log_info(f"[memory_search] Retrieved {len(results)} results")

            delivered = False
            # Send results back to LLM via auto_response so the model can continue (skip in preflight)
            if not is_preflight:
                original_context = {
                    "interface_name": context.get("interface"),
                    "interface_path": getattr(original_message, 'interface_path', None),
                    "chat_id": getattr(original_message, 'chat_id', None),
                    "message_id": getattr(original_message, 'message_id', None),
                }
                # Wrap in action_outputs format
                action_outputs = [{"type": "memory_search_result", "result": r} for r in results]
                try:
                    delivered = await request_llm_delivery(action_outputs=action_outputs, original_context=original_context, action_type="memory_search")
                    log_info(f"[memory_search] Requested LLM delivery; success={bool(delivered)}; results={len(results)}")
                except Exception as e:
                    log_warning(f"[memory_search] Failed to request LLM delivery: {e}")

            return {"processed": True, "results": results, "delivered_to_llm": bool(delivered)}

        except Exception as e:
            log_error(f"[memory_search] Query failed: {e}")
            return {"error": str(e)}


# Export plugin class for dynamic import patterns
PLUGIN_CLASS = MemorySearchPlugin
