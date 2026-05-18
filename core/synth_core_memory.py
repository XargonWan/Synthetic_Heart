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
    environment: str | None = None,
) -> list[dict]:
    """Unified memory search across memories, diary, and chat history.

    When *environment* is set (e.g. ``"skyrim"``), an implicit
    ``environment:<env>`` tag is added to the search so only memories
    tagged with that game world are returned.

    Returns normalized hits:
    {"source", "id", "timestamp", "snippet", "tags"}
    """

    tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
    keywords = [str(k).strip() for k in (keywords or []) if str(k).strip()]

    if environment:
        env_tag = f"environment:{environment}"
        if env_tag not in tags:
            tags.append(env_tag)

    if not tags and not keywords:
        return []

    limit = max(1, int(limit or 5))
    pool_limit = max(limit * 3, limit)

    hits: list[dict] = []

    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                # --- Memories table ---
                mem_conditions = []
                mem_params: list = []
                if tags:
                    tag_conditions, tag_params = _build_json_tag_conditions(
                        "tags", tags
                    )
                    mem_conditions.append(f"({tag_conditions})")
                    mem_params.extend(tag_params)
                if keywords:
                    kw_conditions = " OR ".join(["content LIKE %s"] * len(keywords))
                    mem_conditions.append(f"({kw_conditions})")
                    mem_params.extend([f"%{kw}%" for kw in keywords])

                if mem_conditions:
                    mem_where = " AND ".join(mem_conditions)
                    mem_query = (
                        "SELECT 'memories' AS source, id, timestamp, content, tags "
                        "FROM memories WHERE "
                        + mem_where
                        + " ORDER BY timestamp DESC LIMIT %s"
                    )
                    mem_params.append(pool_limit)
                    await cur.execute(mem_query, mem_params)
                    rows = await cur.fetchall()
                    for r in rows:
                        src, _id, ts, content, row_tags = r
                        snippet = content if isinstance(content, str) else str(content)
                        if len(snippet) > 400:
                            snippet = snippet[:400] + "..."
                        try:
                            ts_iso = (
                                ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
                            )
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

                # --- AI diary table ---
                diary_conditions = []
                diary_params: list = []
                if tags:
                    tag_conditions, tag_params = _build_json_tag_conditions(
                        "context_tags", tags
                    )
                    diary_conditions.append(f"({tag_conditions})")
                    diary_params.extend(tag_params)
                if keywords:
                    kw_conditions = " OR ".join(
                        [
                            "content LIKE %s",
                            "personal_thought LIKE %s",
                            "interaction_summary LIKE %s",
                            "user_message LIKE %s",
                        ]
                        * len(keywords)
                    )
                    diary_conditions.append(f"({kw_conditions})")
                    for kw in keywords:
                        like = f"%{kw}%"
                        diary_params.extend([like, like, like, like])

                if diary_conditions:
                    diary_where = " AND ".join(diary_conditions)
                    diary_query = (
                        "SELECT 'ai_diary' AS source, id, timestamp, content, context_tags "
                        "FROM ai_diary WHERE "
                        + diary_where
                        + " ORDER BY timestamp DESC LIMIT %s"
                    )
                    diary_params.append(pool_limit)
                    await cur.execute(diary_query, diary_params)
                    rows = await cur.fetchall()
                    for r in rows:
                        src, _id, ts, content, row_tags = r
                        snippet = content if isinstance(content, str) else str(content)
                        if len(snippet) > 400:
                            snippet = snippet[:400] + "..."
                        try:
                            ts_iso = (
                                ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
                            )
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

                # --- Chat history cache ---
                if include_chat and (keywords or tags):
                    chat_tokens = keywords + tags
                    chat_params: list = []
                    chat_conditions = []
                    for tok in chat_tokens:
                        chat_conditions.append("message_text LIKE %s")
                        chat_params.append(f"%{tok}%")
                    if chat_conditions:
                        chat_where = " OR ".join(chat_conditions)
                        chat_query = (
                            "SELECT 'chat_history' AS source, id, timestamp, message_text, NULL AS context_tags "
                            "FROM chat_history_cache WHERE "
                            + chat_where
                            + " ORDER BY timestamp DESC LIMIT %s"
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

    deduped.sort(key=_sort_key, reverse=True)
    return deduped[:limit]
