"""Memory Search plugin

Provides `memory_search` action to search `memories` and `ai_diary` tables in two modes:
- mode: "tags" -> payload: {"tags": [...], "max_results": int}
- mode: "free" -> payload: {"keywords": ["..."], "max_results": int} (preferred) or {"query": "...", "max_results": int}

When executed the plugin returns results and requests an LLM delivery so the model
can see the found memories and continue its response.
"""

from __future__ import annotations

import json
import asyncio
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone
import re
import time

from core.auto_response import request_llm_delivery
from core.config_manager import config_registry
from core.core_initializer import register_plugin
from core.db import _get_db_type
from core.db import get_conn_ctx
from core.logging_utils import log_info, log_debug, log_error, log_warning
from core.variables_engine import register_exposed_var


# Helper: map human-friendly keywords to sensible durations (some include slack already)
_SPECIAL_TIME_MAP = {
    "yesterday": timedelta(hours=48),  # user expects a wider radius for 'yesterday'
    "last week": timedelta(days=10),  # expand 'last week' to ~10 days
    "last_week": timedelta(days=10),
}

# Synonym expansion for better recall on abstract/colloquial search terms.
# Maps normalized (lowercase) trigger words to additional search terms.
_SYNONYM_MAP: Dict[str, List[str]] = {
    # Affection / romance
    "romantic": ["love", "kiss", "heart", "embrace", "tender"],
    "affection": ["love", "kiss", "hug", "embrace", "care"],
    "intimate": ["close", "tender", "touch", "hold", "kiss", "embrace", "body", "skin"],
    "passion": ["desire", "heat", "fire", "intense", "want"],
    # Physical descriptors - expanded for explicit content recall
    "steamy": [
        "hot",
        "heat",
        "sweat",
        "breath",
        "touch",
        "skin",
        "body",
        "tongue",
        "lips",
    ],
    "sensual": ["touch", "skin", "soft", "warm", "body", "feel", "caress"],
    "erotic": ["desire", "body", "touch", "skin", "heat", "aroused", "excited"],
    "spicy": ["hot", "intense", "wild", "heat", "naughty", "sexy", "aroused"],
    "naughty": ["tease", "play", "mischief", "wink", "sexy", "dirty"],
    "sexy": ["hot", "aroused", "desire", "body", "touch", "skin"],
    # Roleplay / intimate scenarios - maps euphemisms to actual stored content
    "roleplay": ["fantasy", "scenario", "pretend", "play", "imagine", "scene"],
    "sexual": ["aroused", "desire", "body", "touch", "kiss", "tongue", "lips", "skin"],
    "intimacy": ["close", "touch", "body", "kiss", "hold", "embrace", "love"],
    # Body parts - bidirectional expansion for euphemism matching
    "breast": ["chest", "body", "touch"],
    "tongue": ["kiss", "mouth", "lips", "lick"],
    "lips": ["kiss", "mouth", "tongue"],
    # Emotional states
    "happy": ["joy", "smile", "laugh", "cheerful", "pleased"],
    "sad": ["cry", "tear", "sorrow", "upset", "down"],
    "angry": ["mad", "furious", "rage", "upset", "annoyed"],
    "excited": ["thrill", "eager", "buzz", "anticipation", "aroused"],
    "aroused": ["excited", "desire", "want", "turned on", "hot"],
    # Conversations
    "conversation": ["talk", "chat", "discuss", "said", "told", "spoke"],
    "discussion": ["talk", "debate", "spoke", "argument"],
    # Time references that might appear differently
    "yesterday": ["last night", "earlier", "before"],
    "recent": ["lately", "just", "earlier", "today", "yesterday"],
    "last night": ["yesterday", "earlier", "before bed", "tonight"],
}


def _build_json_array_membership(
    column: str, values: List[str]
) -> Tuple[List[str], List[Any]]:
    if _get_db_type() == "postgres":
        return (
            [f"COALESCE(NULLIF(BTRIM({column}), ''), '[]')::jsonb ? %s"] * len(values),
            list(values),
        )

    return (
        [f"JSON_CONTAINS({column}, %s)"] * len(values),
        [json.dumps(value) for value in values],
    )


def _expand_tokens_with_synonyms(tokens: List[str]) -> List[str]:
    """Expand search tokens with synonyms for better recall.

    Returns a deduplicated list containing original tokens plus any synonyms.
    """
    expanded: List[str] = list(tokens)  # Start with originals
    seen: set[str] = {t.lower() for t in tokens}

    for tok in tokens:
        tok_lower = tok.lower()
        if tok_lower in _SYNONYM_MAP:
            for syn in _SYNONYM_MAP[tok_lower]:
                if syn.lower() not in seen:
                    expanded.append(syn)
                    seen.add(syn.lower())

    return expanded


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
        # Check date-only YYYY-MM-DD BEFORE fromisoformat: Python ≥3.11 accepts
        # bare dates and returns midnight, which would silently ignore is_end.
        m = re.match(r"^(?P<d>\d{4}-\d{2}-\d{2})$", s2)
        if m:
            if is_end:
                return datetime.fromisoformat(m.group("d") + "T23:59:59.999999+00:00")
            else:
                return datetime.fromisoformat(m.group("d") + "T00:00:00+00:00")
        try:
            dt = datetime.fromisoformat(s2)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
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
    hidden=True,
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

    def is_enabled(self) -> bool:
        return bool(
            config_registry.get_value("ENABLE_MEMORY_SEARCH", True, value_type=bool)
        )

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
                        "time_window": {
                            "description": "Optional time window for the search. Can be a string like 'yesterday', 'last week', '48 hours' or an object with explicit 'start'/'end' ISO datetimes or a 'duration' object.",
                            "oneOf": [{"type": "string"}, {"type": "object"}],
                        },
                    },
                    "required": ["mode"],
                },
                "brief": "Search memories by tags or free text in ai_diary and memories tables",
                "examples": {
                    "tags": {"mode": "tags", "tags": ["monster", "austria"]},
                    "free": {
                        "mode": "free",
                        "keywords": ["austrian", "monster"],
                        "time_window": "yesterday",
                    },
                },
            }
        }

    def get_prompt_instructions(self, action_type: str) -> dict:
        if action_type != "memory_search":
            return {}
        return {
            "description": (
                "Use `memory_search` when you lack information to answer. "
                "Call it with mode='tags' and a list of tags, or mode='free' and "
                "a list of keywords (preferred) or a free-text query. "
                "The plugin will return a list of matching memory snippets."
            ),
        }

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
                errors.append(
                    "For mode 'free' provide a non-empty 'keywords' list (preferred), a 'query' string, or a 'time_window'"
                )
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
        # Option to randomize results instead of ordering by timestamp.
        randomize = bool(payload.get("random", False))
        random_order_by = "RANDOM()" if _get_db_type() == "postgres" else "RAND()"

        if mode == "tags":
            tags = payload.get("tags", [])
            tag_conditions, tag_params = _build_json_array_membership("tags", tags)
            params.extend(tag_params)
            if tag_conditions:
                where_clauses_mem.append("(" + " OR ".join(tag_conditions) + ")")

            diary_tag_conditions, diary_tag_params = _build_json_array_membership(
                "context_tags", tags
            )
            params.extend(diary_tag_params)
            if diary_tag_conditions:
                where_clauses_diary.append(
                    "(" + " OR ".join(diary_tag_conditions) + ")"
                )

        elif mode == "free":
            keywords = payload.get("keywords")
            if isinstance(keywords, list) and any(str(x).strip() for x in keywords):
                tokens = [str(x).strip() for x in keywords if str(x).strip()]
            else:
                query = payload.get("query", "")
                tokens = [q.strip() for q in str(query).split() if q.strip()]
            if not tokens:
                # If a time_window is set, a time-only search is still meaningful:
                # it will return all entries within the given time range across all sources.
                if not payload.get("time_window"):
                    return "", []
            # Expand tokens with synonyms for better recall on abstract terms
            original_count = len(tokens)
            tokens = _expand_tokens_with_synonyms(tokens)
            if len(tokens) > original_count:
                log_debug(
                    f"[memory_search] Expanded {original_count} tokens to {len(tokens)} with synonyms"
                )
            token_clauses: List[str] = []
            for tok in tokens:
                like = "%" + tok + "%"
                token_clauses.append("content LIKE %s")
                params.append(like)
            if token_clauses:
                where_clauses_mem.append("(" + " OR ".join(token_clauses) + ")")

            # For ai_diary search personal_thought/interaction_summary/user_message only.
            # NOTE: ai_diary.content holds the SyntH's own response text, NOT user memories —
            # including it causes self-referential "hallucinated recall" where the model
            # receives its previous responses as if they were genuine memory snippets.
            diary_token_clauses: List[str] = []
            for tok in tokens:
                like = "%" + tok + "%"
                diary_token_clauses.append("personal_thought LIKE %s")
                params.append(like)
                diary_token_clauses.append("interaction_summary LIKE %s")
                params.append(like)
                diary_token_clauses.append("user_message LIKE %s")
                params.append(like)
            if diary_token_clauses:
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
        time_range = (
            _parse_time_window_spec(time_spec) if time_spec is not None else None
        )
        time_clause_parts: List[str] = []
        time_params: List[Any] = []
        if time_range:
            start_dt, end_dt = time_range
            time_clause_parts.append("created_at >= %s")
            time_clause_parts.append("created_at <= %s")
            # Use ISOformat UTC strings so DB comparison is deterministic
            time_params.extend([start_dt.isoformat(), end_dt.isoformat()])

        # Compose union query - track params per table to maintain correct ordering
        queries: List[str] = []
        final_params: List[Any] = []

        def _maybe_add_table(
            table_content_params: List[Any],
            table_where_clauses: List[str],
            table_name: str,
            select_expr: str,
            group_by: Optional[str] = None,
        ) -> None:
            """Add a table sub-query wrapped in parentheses with its own ORDER+LIMIT.

            Wrapping each source in its own LIMIT prevents a single source from
            monopolising the outer LIMIT and displacing older-but-relevant entries
            from other sources (e.g. chat history of today pushing out memories
            from two weeks ago).
            Params order: content/keyword params → time params → per-source limit.
            """
            clauses = list(table_where_clauses)  # copy
            if time_clause_parts:
                clauses.extend(time_clause_parts)
            if clauses:
                where = " AND ".join(clauses)
                q = f"{select_expr} WHERE {where}"
                if group_by:
                    q += f" GROUP BY {group_by}"
                order_by = random_order_by if randomize else "created_at DESC"
                queries.append(f"({q} ORDER BY {order_by} LIMIT %s)")
                # Params must match placeholder order: content/keyword params FIRST, then time params, then per-source limit
                final_params.extend(table_content_params)
                if time_clause_parts:
                    final_params.extend(time_params)
                final_params.append(max_results)  # per-source inner limit

        # Build per-table content params lists
        # For mode=tags: params are for JSON_CONTAINS calls
        # For mode=free: params are for LIKE clauses
        # We need to track which params belong to which table

        # Recalculate per-table params (we already have params but need to split them by table)
        # The original params list has: [mem_params..., diary_params..., chat_params...]
        # We need to count how many params each table needs

        if mode == "tags":
            tags = payload.get("tags", [])
            # memories table: one param per tag
            mem_param_count = len(tags) if where_clauses_mem else 0
            # ai_diary table: one param per tag
            diary_param_count = len(tags) if where_clauses_diary else 0
            chat_param_count = 0  # tags mode doesn't search chat
        else:  # mode == "free"
            keywords = payload.get("keywords")
            if isinstance(keywords, list) and any(str(x).strip() for x in keywords):
                tokens = [str(x).strip() for x in keywords if str(x).strip()]
            else:
                query = payload.get("query", "")
                tokens = [q.strip() for q in str(query).split() if q.strip()]
            # Apply synonym expansion to match what we did above
            original_count = len(tokens)
            tokens = _expand_tokens_with_synonyms(tokens)
            num_tokens = len(tokens)
            # memories: 1 LIKE per token
            mem_param_count = num_tokens if where_clauses_mem else 0
            # ai_diary: 3 LIKEs per token (personal_thought, interaction_summary, user_message)
            # ai_diary.content is excluded — it holds the SyntH's own responses, not user memories.
            diary_param_count = num_tokens * 3 if where_clauses_diary else 0
            # chat_history_cache: 1 LIKE per token
            chat_param_count = num_tokens if where_clauses_chat else 0

        # Split params by table
        idx = 0
        mem_content_params = params[idx : idx + mem_param_count]
        idx += mem_param_count
        diary_content_params = params[idx : idx + diary_param_count]
        idx += diary_param_count
        chat_content_params = params[idx : idx + chat_param_count]

        if where_clauses_mem or time_clause_parts:
            # Use MIN(id)/MAX(timestamp) + GROUP BY content to collapse duplicate entries.
            # grillo_observer stores the same chat message once per beat-interval, producing
            # dozens of identical rows that pollute the LIMIT slots.
            _maybe_add_table(
                mem_content_params,
                where_clauses_mem,
                "memories",
                "SELECT 'memories' AS source, MIN(id) AS id, MAX(created_at) AS created_at, content FROM memories",
                group_by="content",
            )
        if where_clauses_diary or time_clause_parts:
            # Exclude Grillo-generated internal entries (self-reflection, curiosity, etc.)
            # These pollute search results when the user asks about actual conversations.
            # Filter: exclude entries where interaction_summary contains grillo markers or has no real user interaction.
            # NOTE: We use %% to escape the % character because the query uses Python's %-style parameterization.
            grillo_exclusion = (
                "(interaction_summary NOT LIKE '%%@grillo%%' "
                "AND interaction_summary NOT LIKE '%%grillo%%' "
                "AND interaction_summary NOT LIKE '%%self-reflection%%' "
                "AND interaction_summary NOT LIKE '%%self reflection%%' "
                "AND interaction_summary NOT LIKE '%%curiosity exploration%%' "
                "AND interaction_summary NOT LIKE '%%Internal reflection%%' "
                "AND interaction_summary NOT LIKE '%%sensory mapping%%' "
                "AND personal_thought NOT LIKE '%%@grillo%%')"
            )
            where_clauses_diary.append(grillo_exclusion)
            _maybe_add_table(
                diary_content_params,
                where_clauses_diary,
                "ai_diary",
                "SELECT 'ai_diary' AS source, id, created_at, content FROM ai_diary",
            )
        # Chat history is only searched in free mode; in tags mode a bare time
        # window would otherwise pull in every chat message of the window.
        if mode == "free" and (where_clauses_chat or time_clause_parts):
            _maybe_add_table(
                chat_content_params,
                where_clauses_chat,
                "chat_history_cache",
                "SELECT 'chat' AS source, id, created_at, message_text AS content FROM chat_history_cache",
            )

        if not queries:
            return "", []

        # If randomize: use the backend's random order function, otherwise order by created_at desc
        order_clause = (
            f" ORDER BY {random_order_by} LIMIT %s"
            if randomize
            else " ORDER BY created_at DESC LIMIT %s"
        )
        union_q = " UNION ALL ".join(queries) + order_clause
        # For the LIMIT param
        final_params.append(max_results)

        return union_q, final_params

    async def _live_search(
        self, payload: Dict[str, Any], guild_id: int, original_message
    ) -> None:
        """Perform a memory search and send formatted results to a live session.

        This helper mirrors much of :meth:`execute_action` but instead of
        issuing an LLM delivery it sends a plain text summary back into the
        live voice session via ``LiveSessionManager.send_text``.  It is
        intentionally fire-and-forget (used via ``asyncio.create_task``).
        """
        # mimic execute_action's result-building part but no LLM delivery
        try:
            default_max = int(
                config_registry.get_value(
                    "MEMORY_SEARCH_MAX_RESULTS", 10, value_type=int
                )
                or 10
            )
            max_results = int(payload.get("max_results") or default_max)

            union_q, params = self._build_query_and_params(payload, max_results)
            if not union_q:
                return

            rows = []
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(union_q, params)
                    rows = await cur.fetchall()

            results: List[Dict[str, Any]] = []
            for r in rows:
                src, _id, ts, content = r
                try:
                    ts_iso = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
                except Exception:
                    ts_iso = str(ts)
                snippet = content if isinstance(content, str) else str(content)
                if len(snippet) > 400:
                    snippet = snippet[:400] + "..."
                results.append(
                    {
                        "source": src,
                        "id": _id,
                        "timestamp": ts_iso,
                        "snippet": snippet,
                    }
                )

            # format results into a simple text blob
            if results:
                text_lines = [f"[{r['timestamp']}] {r['snippet']}" for r in results]
            else:
                text_lines = ["No matching memories found for this search query."]
            final_text = "\n".join(text_lines)

            try:
                from core.live_session_manager import LiveSessionManager

                mgr = LiveSessionManager.get_instance()
                await mgr.send_text(guild_id, final_text)
            except Exception as e:
                log_warning(f"[memory_search] live send_text failed: {e}")
        except Exception as e:
            log_error(f"[memory_search] live_search failed: {e}")

    async def execute_action(
        self, action: Dict[str, Any], context: Dict[str, Any], bot, original_message
    ) -> Dict[str, Any]:
        payload = action.get("payload") or {}

        # When running as a prompt preflight, do NOT request an extra LLM delivery.
        # The caller will inject the results into the main prompt context.
        is_preflight = bool((context or {}).get("preflight"))

        # If this memory_search is being executed while processing an existing
        # action result delivery, do not spawn another follow-up prompt.
        # This avoids preparing a new prompt when we are still evaluating the
        # response to the current LLM prompt (multi-part evaluation flows).
        system_message = (context or {}).get("system_message") or {}
        is_action_result_delivery = bool(
            system_message.get("is_action_result_delivery")
            or (context or {}).get("is_action_result_delivery")
        )

        # the new async live search path
        interface_path = getattr(original_message, "interface_path", "")
        if isinstance(interface_path, str) and interface_path.startswith(
            "discord_live_"
        ):
            # extract guild id from path
            try:
                guild_id = int(interface_path.split("_")[2])
            except Exception:
                guild_id = None
            if guild_id is not None:
                asyncio.create_task(
                    self._live_search(payload, guild_id, original_message)
                )
                return {"processed": True, "results": [], "async": True}

        # Check toggle
        enabled = bool(
            config_registry.get_value("ENABLE_MEMORY_SEARCH", True, value_type=bool)
        )
        if not enabled:
            log_info("[memory_search] Plugin disabled by config; skipping search")
            return {"processed": True, "results": []}

        # Determine max results
        default_max = int(
            config_registry.get_value("MEMORY_SEARCH_MAX_RESULTS", 10, value_type=int)
            or 10
        )
        max_results = int(payload.get("max_results") or default_max)

        results: List[Dict[str, Any]] = []

        try:
            # Build queries for both tables via helper to make testing easier
            union_q, params = self._build_query_and_params(payload, max_results)
            if not union_q:
                return {"processed": True, "results": []}

            log_debug(f"[memory_search] Executing query: {union_q} params={params}")
            query_start = time.time()
            rows = []
            try:
                async with get_conn_ctx() as conn:
                    async with conn.cursor() as cur:
                        # Enforce a client-side timeout since MariaDB session timeout might not be supported/working
                        await asyncio.wait_for(
                            cur.execute(union_q, params), timeout=15.0
                        )
                        rows = await asyncio.wait_for(cur.fetchall(), timeout=5.0)
                log_info(
                    f"[memory_search] Query executed in {time.time() - query_start:.3f}s"
                )
            except asyncio.TimeoutError:
                log_error(
                    f"[memory_search] ⏰ Query timed out after {time.time() - query_start:.1f}s"
                )
                return {"processed": True, "results": [], "error": "Query timed out"}
            except Exception as e:
                log_error(f"[memory_search] Query error: {e}")
                return {"processed": True, "results": [], "error": str(e)}

            for r in rows:
                src, _id, ts, content = r
                try:
                    ts_iso = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
                except Exception:
                    ts_iso = str(ts)
                snippet = content if isinstance(content, str) else str(content)
                if len(snippet) > 400:
                    snippet = snippet[:400] + "..."
                results.append(
                    {
                        "source": src,
                        "id": _id,
                        "timestamp": ts_iso,
                        "snippet": snippet,
                    }
                )

            log_info(f"[memory_search] Retrieved {len(results)} results")

            delivered = False
            # Send results back to LLM via auto_response so the model can continue
            # (skip in preflight or when already processing an action-result delivery).
            if not is_preflight and not is_action_result_delivery:
                original_context = {
                    "interface_name": context.get("interface"),
                    "interface_path": getattr(original_message, "interface_path", None),
                    "chat_id": getattr(original_message, "chat_id", None),
                    "message_id": getattr(original_message, "message_id", None),
                }
                # Wrap in action_outputs format
                if results:
                    action_outputs = [
                        {"type": "memory_search_result", "result": r} for r in results
                    ]
                else:
                    # When no results found, send a clear "no results" message so the LLM
                    # knows what to do (respond to the user that no memories were found)
                    action_outputs = [
                        {
                            "type": "memory_search_result",
                            "result": {
                                "found": 0,
                                "message": "No matching memories found for this search query. "
                                "You should tell the user you couldn't find any memories matching their request.",
                            },
                        }
                    ]
                try:
                    delivered = await request_llm_delivery(
                        action_outputs=action_outputs,
                        original_context=original_context,
                        action_type="memory_search",
                    )
                    log_info(
                        f"[memory_search] Requested LLM delivery; success={bool(delivered)}; results={len(results)}"
                    )
                except Exception as e:
                    log_warning(f"[memory_search] Failed to request LLM delivery: {e}")
            elif is_action_result_delivery:
                log_debug(
                    "[memory_search] Skipping LLM delivery because memory_search is part of an action-result delivery evaluation"
                )

            return {
                "processed": True,
                "results": results,
                "delivered_to_llm": bool(delivered),
            }

        except Exception as e:
            log_error(f"[memory_search] Query failed: {e}")
            return {"error": str(e)}


# Export plugin class for dynamic import patterns
#
# PHASE 2 — Active in-turn recall (memory recall reliability work):
# This plugin is currently dormant (PLUGIN_CLASS = None), so Synth cannot issue
# an in-turn `memory_search` action to actively re-search her own memory when a
# first passive lookup misses. Phase 1 improved *passive* recall (two-tier
# AND-then-OR matching in core/synth_core_memory.search_memories + a real
# language-agnostic tokenizer in core/synth_tagging.extract_tags routed as
# keywords). If intermittent recall still occurs, Phase 2 is to reactivate this
# action by setting `PLUGIN_CLASS = MemorySearchPlugin` below, giving the model
# an explicit tool to look things up mid-turn. The latent SQL bugs here were
# already fixed (2026-06-12), so reactivation is safe — but verify the action's
# security level / prompt catalog exposure before enabling.
PLUGIN_CLASS = None
