from core.db import _get_db_type, get_conn_ctx, insert_memory
import logging
import json
import os
from core.logging_utils import log_debug, log_info, log_warning

# === Memory logging setup ===
os.makedirs("logs", exist_ok=True)  # Ensure log directory exists

memory_logger = logging.getLogger("synth.memory")
if not memory_logger.handlers:
    memory_logger.setLevel(logging.INFO)
    handler = logging.FileHandler("logs/memoria.log", encoding="utf-8")
    formatter = logging.Formatter("%(asctime)s - %(message)s")
    handler.setFormatter(formatter)
    memory_logger.addHandler(handler)


# Static internal configuration (expandable)
DEFAULT_TAGS = json.dumps(["auto", "interazione"])
DEFAULT_SCOPE = "general"
DEFAULT_SOURCE = "chat"

REMEMBER_KEYWORDS = []


def _build_json_tag_conditions(column: str, tags: list[str]) -> tuple[str, list[str]]:
    if not tags:
        return "", []

    if _get_db_type() == "postgres":
        conditions = " OR ".join(
            [f"COALESCE(NULLIF(BTRIM({column}), ''), '[]')::jsonb ? %s"] * len(tags)
        )
        return conditions, list(tags)

    conditions = " OR ".join([f"JSON_CONTAINS({column}, %s)"] * len(tags))
    return conditions, [json.dumps(tag) for tag in tags]


def should_remember(user_text: str, response_text: str) -> bool:
    """
    synth autonomously evaluates whether the interaction is memorable.
    This decision is entirely internal and not visible to the user.
    """
    text = (user_text + " " + response_text).lower()

    if any(k in text for k in REMEMBER_KEYWORDS):
        return True

    if "mi hai fatto sentire" in response_text.lower():
        return True

    return False


async def silently_record_memory(
    user_text: str,
    response_text: str,
    tags: str = DEFAULT_TAGS,
    scope: str = DEFAULT_SCOPE,
    source: str = DEFAULT_SOURCE,
):
    """
    synth internally stores what it decided to remember.
    No feedback is provided externally.
    """

    # If tags is a list, convert to JSON
    if isinstance(tags, list):
        tags = json.dumps(tags)

    await insert_memory(
        content=user_text,
        author="synth",
        source=source,
        tags=tags,
        scope=scope,
        emotion=None,
        intensity=None,
        emotion_state=None,
    )

    log_info("[synth_CORE] 🧠 Memory saved autonomously.")

    memory_logger.info(
        f"[MEMORY] Saved by synth\n"
        f"→ Input: {user_text}\n"
        f"→ Response: {response_text}\n"
        f"→ Tags: {tags} | Scope: {scope} | Source: {source}"
    )


# Injection priority for core memory
INJECTION_PRIORITY = 6  # Medium-low priority


def register_injection_priority():
    """Register this component's injection priority."""
    log_info(f"[synth_core_memory] Registered injection priority: {INJECTION_PRIORITY}")
    return INJECTION_PRIORITY


# Register priority when module is loaded
register_injection_priority()


async def search_memories(
    *,
    tags: list[str] | None = None,
    keywords: list[str] | None = None,
    include_chat: bool = True,
    limit: int = 5,
    exclude_interface_paths: list[str] | None = None,
) -> list[dict]:
    """Unified memory search across memories, diary, and chat history.

    Args:
        tags: Recon-derived tags to match against stored tag columns.
        keywords: Content keywords to match against stored text columns.
        include_chat: Also search raw ``chat_history_cache`` rows.
        limit: Maximum hits to return.
        exclude_interface_paths: Interface paths whose raw chat rows must be
            excluded from the chat-history tier. The message currently being
            answered is persisted to ``chat_history_cache`` *before* the prompt
            is built, so a keyword search extracted from that very message
            matches it (and its recent neighbours) verbatim and re-injects the
            current conversation as "memories" — the model then anchors on
            stale lines (e.g. a "good morning" greeting) instead of the actual
            question. Callers pass the current chat's path so its own raw
            lines are never duplicated; durable facts still come from the
            ``memories``/``ai_diary`` tiers.

    Returns:
        Normalized hits:
        {"source", "id", "timestamp", "snippet", "tags"}
    """

    tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
    keywords = [str(k).strip() for k in (keywords or []) if str(k).strip()]

    if not tags and not keywords:
        return []

    limit = max(1, int(limit or 5))
    pool_limit = max(limit * 3, limit)

    hits: list[dict] = []

    # Two-tier precision-then-recall search over the memories/ai_diary tables.
    #
    # Tier 1 (precision): tag_clause AND keyword_clause. Returns the highest-
    #   confidence rows where both the recon-derived tags AND the content
    #   keywords match.
    # Tier 2 (recall): tag_clause OR keyword_clause. Run WHENEVER both clauses
    #   are present (not only when Tier 1 is empty). This rescues rows whose
    #   stored context_tags are generic auto-tags (e.g. ["grillo", "observer",
    #   "passive"]) that don't match the query-derived tags, but whose content
    #   DOES contain the searched keyword. Running it only on an empty Tier 1
    #   was insufficient: a single unrelated ai_diary AND-match made Tier 1
    #   non-empty, so a stored fact (e.g. "sender:Alonza ... Supercar 458/488",
    #   tagged ["grillo","observer","passive"]) was never even queried and
    #   never reached the pool. Both tiers feed the same dedup + IDF-relevance
    #   ranking below, so the rarer keyword-only match still surfaces correctly.
    #
    # When only tags OR only keywords are present (not both), the AND and OR
    # joins are identical, so Tier 2 is skipped as it would be redundant.
    both_present = bool(tags) and bool(keywords)

    def _append_rows(rows) -> None:
        for r in rows:
            src, _id, ts, content, row_tags = r
            snippet = content if isinstance(content, str) else str(content)
            if len(snippet) > 400:
                snippet = snippet[:400] + "..."
            try:
                ts_iso = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            except Exception:
                ts_iso = str(ts)
            try:
                tags_list = json.loads(row_tags) if row_tags else []
            except Exception:
                tags_list = []
            hits.append(
                {
                    "source": src,
                    "id": _id,
                    "timestamp": ts_iso,
                    "snippet": snippet,
                    "tags": tags_list,
                }
            )

    def _mem_where(join_op: str) -> tuple[str, list]:
        conditions: list[str] = []
        params: list = []
        if tags:
            tag_conditions, tag_params = _build_json_tag_conditions("tags", tags)
            conditions.append(f"({tag_conditions})")
            params.extend(tag_params)
        if keywords:
            # LOWER(col) with a lowercased pattern keeps matching case-insensitive
            # across backends: Postgres LIKE is case-sensitive (MariaDB LIKE is
            # not), so a lowercase token like "alonza" would never match content
            # stored as "Alonza" without this normalization.
            kw_conditions = " OR ".join(["LOWER(content) LIKE %s"] * len(keywords))
            conditions.append(f"({kw_conditions})")
            params.extend([f"%{kw.lower()}%" for kw in keywords])
        return f" {join_op} ".join(conditions), params

    def _diary_where(join_op: str) -> tuple[str, list]:
        conditions: list[str] = []
        params: list = []
        if tags:
            tag_conditions, tag_params = _build_json_tag_conditions(
                "context_tags", tags
            )
            conditions.append(f"({tag_conditions})")
            params.extend(tag_params)
        if keywords:
            # Case-insensitive across backends (see _mem_where note).
            kw_conditions = " OR ".join(
                [
                    "LOWER(content) LIKE %s",
                    "LOWER(personal_thought) LIKE %s",
                    "LOWER(interaction_summary) LIKE %s",
                    "LOWER(user_message) LIKE %s",
                ]
                * len(keywords)
            )
            conditions.append(f"({kw_conditions})")
            for kw in keywords:
                like = f"%{kw.lower()}%"
                params.extend([like, like, like, like])
        return f" {join_op} ".join(conditions), params

    async def _run_tier(cur, join_op: str) -> int:
        """Run the memories + ai_diary queries with the given join operator.
        Appends matches to ``hits`` and returns how many rows were added."""
        added = 0

        mem_where, mem_params = _mem_where(join_op)
        if mem_where:
            mem_query = (
                "SELECT 'memories' AS source, id, created_at, content, tags "
                "FROM memories WHERE "
                + mem_where
                + " ORDER BY created_at DESC LIMIT %s"
            )
            mem_params.append(pool_limit)
            await cur.execute(mem_query, mem_params)
            rows = await cur.fetchall()
            added += len(rows)
            _append_rows(rows)

        diary_where, diary_params = _diary_where(join_op)
        if diary_where:
            diary_query = (
                "SELECT 'ai_diary' AS source, id, created_at, content, context_tags "
                "FROM ai_diary WHERE "
                + diary_where
                + " ORDER BY created_at DESC LIMIT %s"
            )
            diary_params.append(pool_limit)
            await cur.execute(diary_query, diary_params)
            rows = await cur.fetchall()
            added += len(rows)
            _append_rows(rows)

        return added

    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                # Tier 1: precision (tag AND keyword).
                await _run_tier(cur, "AND")

                # Tier 2: recall (tag OR keyword), run whenever both clauses are
                # present. Dedup below removes rows already found by Tier 1, and
                # IDF ranking decides final ordering, so this only ADDS the
                # keyword-only matches that Tier 1's AND join would have dropped.
                if both_present:
                    await _run_tier(cur, "OR")

                # --- Chat history cache ---
                if include_chat and (keywords or tags):
                    chat_tokens = keywords + tags
                    chat_params: list = []
                    chat_conditions = []
                    for tok in chat_tokens:
                        # Case-insensitive across backends (see _mem_where note).
                        chat_conditions.append("LOWER(message_text) LIKE %s")
                        chat_params.append(f"%{tok.lower()}%")
                    # Never re-inject the chat we are currently answering from.
                    # The current message is persisted to the cache before the
                    # prompt is built, so a keyword search extracted from it
                    # matches itself (and its recent neighbours) verbatim and
                    # echoes the live conversation back as "memories" — the
                    # model then keeps "responding to the good morning message"
                    # because that greeting is re-injected every turn.
                    if exclude_interface_paths:
                        excluded = [str(p) for p in exclude_interface_paths if p]
                        if excluded:
                            chat_conditions.append(
                                "interface_path NOT IN (%s)"
                                % ",".join(["%s"] * len(excluded))
                            )
                            chat_params.extend(excluded)
                    if chat_conditions:
                        chat_where = " OR ".join(chat_conditions)
                        chat_query = (
                            "SELECT 'chat_history' AS source, id, created_at, message_text, NULL AS context_tags "
                            "FROM chat_history_cache WHERE "
                            + chat_where
                            + " ORDER BY created_at DESC LIMIT %s"
                        )
                        chat_params.append(pool_limit)
                        try:
                            await cur.execute(chat_query, chat_params)
                            rows = await cur.fetchall()
                            for r in rows:
                                src, _id, ts, content, _ = r
                                snippet = (
                                    content
                                    if isinstance(content, str)
                                    else str(content)
                                )
                                if len(snippet) > 400:
                                    snippet = snippet[:400] + "..."
                                try:
                                    ts_iso = (
                                        ts.isoformat()
                                        if hasattr(ts, "isoformat")
                                        else str(ts)
                                    )
                                except Exception:
                                    ts_iso = str(ts)
                                hits.append(
                                    {
                                        "source": src,
                                        "id": _id,
                                        "timestamp": ts_iso,
                                        "snippet": snippet,
                                        "tags": [],
                                    }
                                )
                        except Exception as e:
                            log_debug(
                                f"[search_memories] chat_history_cache query failed: {e}"
                            )
    except Exception as e:
        log_warning(f"[search_memories] query failed: {e}")
        return []

    # Deduplicate and order by timestamp desc
    seen = set()
    deduped: list[dict] = []
    for h in hits:
        snippet_key = str(h.get("snippet") or "")[:80]
        key = f"{h.get('source')}::{h.get('id')}::{snippet_key}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(h)

    def _sort_key(h: dict) -> float:
        ts = h.get("timestamp")
        if isinstance(ts, str):
            try:
                from datetime import datetime

                return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except Exception:
                return 0.0
        return 0.0

    # Relevance scoring by keyword rarity (purely statistical — no word
    # semantics). Pure recency ordering lets common query tokens (which match
    # many recent rows) dilute a rare, discriminating token: e.g. a request
    # like "another test, try to remember Alonza" produces tokens where "test"
    # matches dozens of recent rows while "alonza" matches only a few older
    # ones. Ordered by timestamp alone, the recent "test" matches outrank and
    # evict the older "alonza" fact before it can reach the prompt.
    #
    # Fix: weight each matched keyword by its inverse document frequency within
    # THIS result pool (how many of the returned rows contain it). A token that
    # appears in few rows contributes a high weight; a token that appears in
    # many rows contributes little. Each row's relevance is the sum of the
    # weights of the keywords it contains. This is data-frequency based only —
    # it never inspects what a word means, so it works in any language.
    lowered_keywords = [kw.lower() for kw in keywords]
    if lowered_keywords:
        df: dict[str, int] = {kw: 0 for kw in lowered_keywords}
        for h in deduped:
            snippet_lc = str(h.get("snippet") or "").lower()
            for kw in lowered_keywords:
                if kw and kw in snippet_lc:
                    df[kw] += 1
        for h in deduped:
            snippet_lc = str(h.get("snippet") or "").lower()
            score = 0.0
            for kw in lowered_keywords:
                if kw and kw in snippet_lc and df[kw] > 0:
                    # Inverse document frequency within the pool.
                    score += 1.0 / df[kw]
            h["_relevance"] = score
    else:
        for h in deduped:
            h["_relevance"] = 0.0

    def _rank_key(h: dict) -> tuple[float, float]:
        # Primary: keyword-rarity relevance. Tie-breaker: recency.
        return (float(h.get("_relevance") or 0.0), _sort_key(h))

    deduped.sort(key=_rank_key, reverse=True)

    # Fair, source-aware truncation. A purely chronological cut lets a single
    # high-volume source (recent chat_history / ai_diary turns) monopolize the
    # limited result set and evict older-but-relevant long-term facts from the
    # `memories` table. That is exactly how a stored fact (e.g. Alonza's
    # favourite supercar, recorded weeks ago) could be found by the query yet
    # never reach the prompt. Reserve slots per source via round-robin so
    # long-term memories always get a chance to surface, while still preferring
    # more relevant (then more recent) rows within each source.
    def _strip_internal(rows: list[dict]) -> list[dict]:
        for r in rows:
            r.pop("_relevance", None)
        return rows

    if len(deduped) <= limit:
        return _strip_internal(deduped)

    by_source: dict[str, list[dict]] = {}
    for h in deduped:
        by_source.setdefault(str(h.get("source")), []).append(h)

    # Within each source, order by relevance first so the most discriminating
    # match is the one that survives round-robin truncation, not merely the
    # most recent.
    for src_rows in by_source.values():
        src_rows.sort(key=_rank_key, reverse=True)

    selected: list[dict] = []
    # Iterate round-robin across sources, always taking the most relevant
    # remaining row from each, until we hit the limit.
    while len(selected) < limit and any(by_source.values()):
        for src_rows in by_source.values():
            if not src_rows:
                continue
            selected.append(src_rows.pop(0))
            if len(selected) >= limit:
                break

    # Preserve relevance-then-recency ordering in the final output.
    selected.sort(key=_rank_key, reverse=True)
    return _strip_internal(selected)
