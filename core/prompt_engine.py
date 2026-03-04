# core/prompt_engine.py

from core.synth_tagging import extract_tags, expand_tags
from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.json_utils import dumps as json_dumps
from core.config_manager import config_registry
from core.user_utils import get_user_display_name, get_user_usertag
from datetime import datetime
import os
import asyncio

# Default maximum prompt characters (CHARACTERS, NOT TOKENS)
# This is used as a safe fallback when no LLM engine provides explicit limits.
# The actual value comes from the active LLM engine's configuration.
# For ChatGPT, see cortex/selenium_engine/selenium_chatgpt.py MODEL_LIMITS_MAP["default"]
DEFAULT_MAX_PROMPT_CHARS = None  # Will be set dynamically from LLM engine

# Chat history limit
CHAT_HISTORY_LIMIT = config_registry.get_var(
    "CHAT_HISTORY",
    10,
    label="Chat History Length",
    description="Number of recent messages to include in chat history context.",
    group="core",
    component="prompt_engine",
    value_type=int,
)

# How many recent messages to include in the explicit current chat recap
CHAT_RECAP_LAST_N = config_registry.get_var(
    "CHAT_RECAP_LAST_N",
    3,
    label="Chat recap last N",
    description="Number of recent messages from the current chat to include as a concise recap (current_chat_history).",
    group="core",
    component="prompt_engine",
    value_type=int,
)

# Diary history days
DIARY_HISTORY_DAYS = config_registry.get_var(
    "DIARY_HISTORY_DAYS",
    2,
    label="Diary History Days",
    description="Number of days of AI diary history to include in context.",
    group="core",
    component="prompt_engine",
    value_type=int,
)


def minify_actions_block(available_actions: dict) -> dict:
    """Convert full action schemas to minimal versions for prompt.

    For LLM prompts, sends ONLY schema and brief description to minimize token usage.
    This dramatically reduces prompt size while preserving all critical information needed.

    Uses new normalized action format:
    - schema: JSON schema with structure, types, and required fields
    - brief: One-line description of action purpose
    - source: Which plugin/interface provides this action

    Full examples and detailed instructions are NOT included here - they're used by
    the corrector when the LLM makes mistakes.

    Parameters
    ----------
    available_actions : dict
        Full actions block with schemas in new normalized format

    Returns
    -------
    dict
        Minified actions block suitable for LLM prompts (schema + brief only)
    """
    from core.action_schema_converter import (
        extract_for_llm_prompt,
        normalize_action_schema,
    )

    minified = {}
    for action_name, action_def in available_actions.items():
        # Normalize to new format (handles both old and new formats)
        normalized = normalize_action_schema(action_name, action_def)

        # Extract only what's needed for LLM (schema + brief)
        minified_action = extract_for_llm_prompt(action_name, normalized)

        minified[action_name] = minified_action

    return minified


async def build_json_prompt(
    message,
    context_memory,
    interface_name: str | None = None,
    image_data: dict | None = None,
    attachments: list[dict] | None = None,
    max_chars: int | None = None,
    history_scope: str | None = None,
) -> dict:
    """Build the JSON prompt expected by plugins.

    Parameters
    ----------
    message : AbstractMessage or compatible interface message
        Incoming message object from an interface.
    context_memory : dict[str, deque]
        Dictionary storing last messages per interface_path.
    interface_name : str | None
        Identifier of the interface that delivered the message.
    image_data : dict | None
        Processed image data from image_processor, if present.
    max_chars : int | None
        Maximum characters for the JSON prompt. If provided, the prompt will be
        intelligently reduced by removing oldest memories. If None, no reduction is done.
    history_scope : str | None
        Optional per-prompt override for history selection. One of: 'local', 'recent', 'unified'.
        If None, falls back to any `history_scope` in `context_memory` or to the global `UNIFIED_HISTORY` setting.
    """
    import time

    start_time = time.time()
    log_info(f"[json_prompt] ⏱️ BUILD PROMPT START for interface={interface_name}")

    interface_path = getattr(message, "interface_path", None)
    text = getattr(message, "text", "") or ""

    # Determine if context_memory is a chat history map or a context dict
    # Context dicts have keys like 'interface_path', 'system_message', etc.
    # Chat history maps have interface_path as keys

    # History-like context is now produced by HistoryEngine (plugin-centric aggregation)

    # === 2. Tags and memory lookup ===
    tags = extract_tags(text)
    expanded_tags = expand_tags(tags)
    memories = []
    if expanded_tags:
        # Limit follows unified verbosity (HistoryEngine will also apply a hard cap)
        try:
            from core.history_engine import _get_int as _history_get_int

            mem_limit = int(_history_get_int("CONTEXT_VERBOSITY", 10))
        except Exception:
            mem_limit = 10
        try:
            from core.synth_core_memory import search_memories

            memories = await search_memories(
                tags=expanded_tags, limit=max(1, mem_limit), include_chat=True
            )
        except Exception as e:
            log_warning(f"[json_prompt] search_memories failed: {e}")
            memories = []
        log_debug(
            f"[json_prompt] ⏱️ Loaded {len(memories)} memories from tags in {time.time() - start_time:.2f}s"
        )
    # === Recon (prompt 0) contributions ===
    recon_contributions: list[dict] = []
    recon_instructions: list[str] = []
    recon_snippets: list[dict] = []
    recon_memories: list[dict] = []
    resolved_language = None
    resolved_message_tone = None
    resolved_conversation_tone = None

    _is_grillo_beat = bool(
        getattr(message, "grillo_beat", False)
        or (isinstance(context_memory, dict) and context_memory.get("grillo_beat"))
        or (interface_path and str(interface_path).startswith("grillo"))
    )
    # Outreach beats target an external interface (e.g. telegram_bot) —
    # they need recon (memory search) and should NOT be treated as internal.
    _beat_type = (
        (isinstance(context_memory, dict) and context_memory.get("beat_type"))
        or getattr(message, "beat_type", None)
        or ""
    )
    is_grillo_internal = _is_grillo_beat and _beat_type != "outreach"

    try:
        from core.recon import (
            gather_recon_contributions,
            resolve_language,
            resolve_tone,
        )

        if is_grillo_internal:
            # Grillo internal beats have fixed language/tone defaults —
            # skip the LLM recon call to avoid wasting API tokens.
            log_debug("[json_prompt] Skipping recon LLM call for Grillo internal beat")
            recon_contributions = []
        else:
            recon_contributions = await gather_recon_contributions(
                message=message,
                context_memory=context_memory,
                text=text,
                tags=expanded_tags,
                keywords=None,
            )

        for c in recon_contributions:
            ctype = c.get("type")
            if ctype == "memory":
                content = c.get("content")
                if isinstance(content, dict):
                    recon_memories.append(content)
                elif content:
                    recon_memories.append(
                        {
                            "source": c.get("source"),
                            "id": c.get("id"),
                            "timestamp": c.get("timestamp"),
                            "snippet": str(content),
                            "tags": c.get("tags") or [],
                        }
                    )
            elif ctype == "snippet":
                recon_snippets.append(c)
            elif ctype == "instruction":
                if c.get("content"):
                    recon_instructions.append(str(c.get("content")))

        if recon_memories:
            # Deduplicate by snippet/id
            existing = set()
            for m in memories:
                if isinstance(m, dict):
                    key = f"{m.get('source')}::{m.get('id')}::{m.get('snippet')}"
                else:
                    key = str(m)
                existing.add(key)
            for m in recon_memories:
                key = f"{m.get('source')}::{m.get('id')}::{m.get('snippet')}"
                if key not in existing:
                    memories.append(m)
                    existing.add(key)

        resolved_language = await resolve_language(
            contributions=recon_contributions,
            interface_path=interface_path,
            is_grillo_internal=is_grillo_internal,
            message=message,
        )
        resolved_message_tone, resolved_conversation_tone = await resolve_tone(
            contributions=recon_contributions,
            interface_path=interface_path,
            is_grillo_internal=is_grillo_internal,
            message=message,
        )
    except Exception as e:
        log_warning(f"[json_prompt] Recon gather failed: {e}")

    # === 3. Context base (history + optional plugin contributions) ===
    try:
        from core.history_engine import HistoryEngine

        # Determine effective history_scope (explicit param -> context_memory -> default behavior)
        effective_history_scope = history_scope
        if effective_history_scope is None and isinstance(context_memory, dict):
            effective_history_scope = context_memory.get("history_scope")

        history_engine = HistoryEngine()
        context_section = await history_engine.build_context(
            message=message,
            context_memory=context_memory,
            interface_name=interface_name,
            text=text,
            memories=memories,
            history_scope=effective_history_scope,
        )
    except Exception as e:
        log_warning(
            f"[json_prompt] Failed to build history context via HistoryEngine: {e}"
        )
        context_section = {"memories": memories}

    # Expose chosen history_scope to downstream plugins/engines explicitly
    try:
        if effective_history_scope:
            input_payload.setdefault("history_scope", effective_history_scope)
    except Exception:
        pass

    # (moved) prompt logging will occur later once input_payload exists

    # === 3. Recon contributions (prompt 0) ===
    try:
        if recon_contributions:
            context_section["recon"] = {
                "contributions": recon_contributions,
                "snippets": recon_snippets,
                "language": resolved_language,
                "message_tone": resolved_message_tone,
                "conversation_tone": resolved_conversation_tone,
            }
        if recon_instructions:
            context_section["recon_instructions"] = recon_instructions
    except Exception as e:
        log_warning(f"[json_prompt] Failed to attach recon context: {e}")

    # === 3a. Static injections from plugins ===
    static_persona = None  # Extract persona separately for instructions
    try:
        from core.action_parser import gather_static_injections

        log_info("[json_prompt] 🔄 About to call gather_static_injections()")
        injections = await gather_static_injections(message, context_memory)
        log_info(
            f"[json_prompt] 📥 gather_static_injections() returned: {list(injections.keys()) if injections else 'empty'}"
        )
        if isinstance(injections, dict):
            # Extract persona BEFORE adding to context - it will go to instructions instead
            if "persona" in injections:
                static_persona = injections.pop("persona")
                log_info(
                    f"[json_prompt] 👤 Extracted persona for instructions ({len(static_persona) if static_persona else 0} chars)"
                )

            # Add remaining injections to context (but drop deprecated legacy keys)
            context_section.update(injections)
            # Deprecated (migrated to HistoryEngine)
            for legacy_key in (
                "latest_diary_entries",
                "diary_entries",
                "diary",
                "chat_history",
                "current_chat_history",
            ):
                if legacy_key in context_section:
                    context_section.pop(legacy_key, None)
            log_info(
                f"[json_prompt] ✅ Updated context_section with injections. Keys now: {list(context_section.keys())}"
            )
    except Exception as e:
        log_warning(f"[json_prompt] Failed to gather static injections: {e}")

    # === 4. Input payload ===
    # interface_path was already extracted at the beginning
    # If still not found, check if context_memory is actually a context dict with interface_path
    if (
        not interface_path
        and isinstance(context_memory, dict)
        and "interface_path" in context_memory
    ):
        interface_path = context_memory.get("interface_path")
        log_debug(
            f"[json_prompt] Retrieved interface_path from context dict: {interface_path}"
        )

    # Determine message input source for the LLM ("voice" | "text").
    # Only mark as voice for the *current* message; never stored in chat_history,
    # so the model cannot mistakenly infer that past messages were also voice.
    _is_voice_input: bool = bool(
        isinstance(context_memory, dict) and context_memory.get("is_voice_input")
    )

    input_payload = {
        "text": text,
        "input_source": "voice" if _is_voice_input else "text",
        "source": {
            "interface_path": interface_path,
            "message_id": message.message_id,
            "username": get_user_display_name(getattr(message, "from_user", None)),
            "usertag": get_user_usertag(getattr(message, "from_user", None)),
            "interface": interface_name,
        },
        "timestamp": message.date.isoformat(),
        "privacy": "default",
        # Set `scope` to the effective history_scope when provided, otherwise keep legacy default
        "scope": (
            effective_history_scope
            if ("effective_history_scope" in locals() and effective_history_scope)
            else "local"
        ),
    }
    # debug: log full prompt payload for reconstruction
    try:
        full_text = json_dumps(input_payload)
        log_debug(
            f"[json_prompt] ⏹️ Final prompt built ({len(full_text)} chars): {full_text}"
        )
    except Exception as e:
        log_debug(f"[json_prompt] Failed to dump final prompt for logging: {e}")

    # Add image data if present
    if image_data:
        input_payload["image"] = image_data
        log_debug(
            f"[json_prompt] Including image data in prompt: {image_data.get('type', 'unknown')}"
        )

    # Add multimodal attachments if present
    if attachments:
        input_payload["attachments"] = attachments
        log_debug(
            f"[json_prompt] Including {len(attachments)} multimodal attachments in prompt"
        )

        # Synthesise a structured "video" metadata block (mirrors the "image" block)
        # so that the model gets the same level of context for video as for images.
        for att in attachments:
            media_meta = att.get("media_metadata")
            if not media_meta:
                continue
            if media_meta.get("type") not in ("video", "video_note"):
                continue
            input_payload["video"] = {
                "type": media_meta["type"],
                "source": {
                    "interface": interface_name,
                    "user_id": getattr(getattr(message, "from_user", None), "id", None),
                    "chat_id": getattr(message, "chat", None)
                    and getattr(message.chat, "id", None),
                    "message_id": getattr(message, "message_id", None),
                },
                "video_data": {
                    "type": media_meta["type"],
                    "filename": att.get("filename", ""),
                    "mime_type": att.get("mime_type", "video/mp4"),
                    "duration": media_meta.get("duration", 0),
                    "width": media_meta.get("width", 0),
                    "height": media_meta.get("height", 0),
                    "file_size": media_meta.get("file_size", 0),
                    "has_audio": media_meta.get("has_audio", False),
                    "caption": att.get("caption", ""),
                },
                "metadata": {
                    "timestamp": getattr(message, "date", None)
                    and message.date.isoformat(),
                    "caption": att.get("caption", ""),
                    "mime_type": att.get("mime_type", "video/mp4"),
                    "file_size": media_meta.get("file_size", 0),
                    "duration": media_meta.get("duration", 0),
                },
            }
            log_debug(
                f"[json_prompt] Including video metadata in prompt: "
                f"{media_meta['type']}, {media_meta.get('duration', 0)}s"
            )
            break  # Only attach metadata for the first video

    reply = getattr(message, "reply_to_message", None)
    if reply:
        reply_text = getattr(reply, "text", None) or getattr(reply, "caption", None)
        if not reply_text:
            reply_text = "[Non-text content]"
        reply_date = getattr(reply, "date", None)
        reply_timestamp = reply_date.isoformat() if reply_date else ""
        reply_from = getattr(reply, "from_user", None)
        reply_full_name = get_user_display_name(reply_from) if reply_from else "Unknown"
        reply_username = getattr(reply_from, "username", None) if reply_from else None
        input_payload["reply_message_id"] = {
            "text": reply_text,
            "timestamp": reply_timestamp,
            "from": {
                "username": reply_full_name,
                "usertag": f"@{reply_username}" if reply_username else "(no tag)",
            },
        }

    input_section = {
        "type": "message",
        "interface": interface_name,
        "payload": input_payload,
    }

    # Debug output for both sections
    log_debug("[json_prompt] context = " + json_dumps(context_section))
    log_debug("[json_prompt] input = " + json_dumps(input_section))

    # Add JSON instructions to the prompt
    json_instructions = load_json_instructions()

    # === CRITICAL: Prepend persona to instructions so ALL LLM types see it ===
    # Use the persona extracted during gather_static_injections()
    if static_persona:
        json_instructions = f"=== CRITICAL SYSTEM IDENTITY ===\n{static_persona}\n\n=== JSON RESPONSE INSTRUCTIONS ===\n{json_instructions}"
        log_info(
            f"[json_prompt] 👤 Persona prepended to instructions ({len(static_persona)} chars)"
        )

    # Recon-derived instructions (language, tone, plugin hints)
    try:
        recon_prefixes: list[str] = []
        if resolved_language:
            recon_prefixes.append(
                f"Use {resolved_language} language for the assistant replies."
            )
        if resolved_message_tone:
            recon_prefixes.append(f"Use a {resolved_message_tone} tone for replies.")
        if resolved_conversation_tone:
            recon_prefixes.append(
                f"Tone of the conversation is: {resolved_conversation_tone}."
            )
        if recon_instructions:
            recon_prefixes.extend([str(r) for r in recon_instructions if r])

        if recon_prefixes:
            json_instructions = " ".join(recon_prefixes) + " " + json_instructions
    except Exception as e:
        log_warning(f"[json_prompt] Failed to add recon instructions: {e}")

    # Keep `instructions` strictly minified (single-line) for token efficiency and tests.
    try:
        json_instructions = " ".join((json_instructions or "").split())
    except Exception:
        pass

    # Interface-specific instructions are provided via the available actions block
    # No hardcoded interface references - plugins define their own instructions

    prompt_with_instructions = {
        "context": context_section,
        "input": input_section,
        "instructions": json_instructions,
    }

    # For chat-like interfaces, include an explicit unminified instruction block.
    # Avoid hardcoded interface names by inferring from available actions.
    try:
        is_chat_interface = False
        if interface_name:
            try:
                from core.core_initializer import core_initializer

                available_actions = core_initializer.actions_block.get(
                    "available_actions", {}
                )
                for action_type, schema in available_actions.items():
                    if not isinstance(action_type, str) or not action_type.startswith(
                        "message_"
                    ):
                        continue
                    owner = (
                        str(schema.get("source", ""))
                        if isinstance(schema, dict)
                        else ""
                    )
                    if interface_name in owner:
                        is_chat_interface = True
                        break
            except Exception:
                is_chat_interface = False

        if interface_name and is_chat_interface:
            prompt_with_instructions["instructions_verbose"] = (
                load_unminified_chat_instruction(interface_name)
            )
            log_info(
                f"[json_prompt] 🔒 Added instructions_verbose for chat interface: {interface_name}"
            )
    except Exception as e:
        log_warning(f"[json_prompt] Failed to add instructions_verbose: {e}")

    # Record full prompt size BEFORE injecting actions/minification so callers
    # can decide split based on the original size.
    try:
        pre_reduction_size = len(json_dumps(prompt_with_instructions))
        prompt_with_instructions["__pre_reduction_size"] = pre_reduction_size
        log_debug(f"[json_prompt] __pre_reduction_size={pre_reduction_size}")
    except Exception:
        prompt_with_instructions["__pre_reduction_size"] = None

    # Include unified actions metadata from the initializer
    # Use minified version to keep prompt size manageable
    try:
        from core.core_initializer import core_initializer

        full_actions = core_initializer.actions_block.get("available_actions", {})
        # Minify to reduce token usage
        prompt_with_instructions["actions"] = minify_actions_block(full_actions)
        log_debug(
            f"[json_prompt] Actions block minified: {len(json_dumps(full_actions))} -> {len(json_dumps(prompt_with_instructions['actions']))} chars"
        )
    except Exception as e:
        log_warning(f"[prompt_engine] Failed to inject actions block: {e}")
        prompt_with_instructions["actions"] = {}

    # === Final check: Reduce prompt if it exceeds LLM character limits ===
    try:
        # Use provided max_chars if available, otherwise get from active LLM engine
        max_prompt_chars = max_chars

        # If max_chars was not provided, try to get from active LLM engine
        if max_chars is None:
            try:
                # Local imports to avoid module-level cycles
                from core.config import get_active_cortex_engine
                from core.cortex_registry import get_cortex_registry

                active_cortex = await get_active_cortex_engine()
                registry = get_cortex_registry()
                engine = registry.get_engine(active_cortex)

                if not engine:
                    engine = registry.load_engine(active_cortex)

                if engine and hasattr(engine, "get_interface_limits"):
                    limits = engine.get_interface_limits()
                    max_prompt_chars = limits.get("max_prompt_chars")
            except Exception as e:
                log_debug(
                    f"[json_prompt] Could not get interface limits for reduction: {e}"
                )

        # Apply reduction only if max_chars is available
        if max_prompt_chars:
            prompt_with_instructions = reduce_prompt_for_llm_limit(
                prompt_with_instructions, max_prompt_chars
            )

    except Exception as e:
        log_warning(f"[json_prompt] Failed to apply prompt reduction: {e}")

    elapsed = time.time() - start_time
    log_info(
        f"[json_prompt] ⏱️ BUILD PROMPT COMPLETE in {elapsed:.2f}s, final size: {len(json_dumps(prompt_with_instructions)) if isinstance(prompt_with_instructions, dict) else len(str(prompt_with_instructions))} chars"
    )
    return prompt_with_instructions


async def search_memories(tags=None, scope=None, limit=5):
    if not tags:
        return []

    # Build OR conditions using JSON_CONTAINS to check if any tag exists in the JSON array
    conditions = " OR ".join(["JSON_CONTAINS(tags, %s)"] * len(tags))

    query = f"""
        SELECT DISTINCT content
        FROM memories
        WHERE json_valid(tags)
          AND ({conditions})
    """

    # Parameters: each tag encoded as a JSON string for JSON_CONTAINS
    params = [json_dumps(tag) for tag in tags]

    if scope:
        query += " AND scope = %s"
        params.append(scope)

    query += " ORDER BY timestamp DESC LIMIT %s"
    params.append(limit)

    log_debug("Query:")
    log_debug(query)
    log_debug(f"Parameters: {params}")

    async with get_conn_ctx() as conn:
        try:
            async with conn.cursor() as cur:
                await cur.execute(query, params)
                rows = await cur.fetchall()
                # Truncate each memory to max 400 chars to keep JSON payload lightweight
                memories = []
                for row in rows:
                    mem = row[0]
                    if isinstance(mem, str) and len(mem) > 400:
                        mem = mem[:400] + "..."
                    memories.append(mem)

                # Also search ai_diary for context_tags to include diary entries in memories
                try:
                    diary_query = f"SELECT DISTINCT content FROM ai_diary WHERE json_valid(context_tags) AND ({conditions}) ORDER BY timestamp DESC LIMIT %s"
                    diary_params = [json_dumps(tag) for tag in tags]
                    diary_params.append(limit)
                    await cur.execute(diary_query, diary_params)
                    rows2 = await cur.fetchall()
                    for r in rows2:
                        mem = r[0]
                        if isinstance(mem, str) and len(mem) > 400:
                            mem = mem[:400] + "..."
                        memories.append(mem)
                except Exception:
                    # If ai_diary search fails, ignore and continue with memories only
                    pass

                log_debug(
                    f"[search_memories] Retrieved {len(memories)} memories, ~{sum(len(str(m)) for m in memories)} chars total"
                )
                return memories
        except Exception as e:
            log_error(f"Query failed: {repr(e)}")
            return []


async def free_memory_search(query: str, limit: int = 5):
    """Perform a free-text memory search over `memories` and `ai_diary` tables and
    return a list of snippet strings (max 400 chars each). This mirrors the plugin's
    mode='free' behavior but does not request LLM delivery, it just returns results.
    """
    if not query or not isinstance(query, str) or not query.strip():
        return []

    tokens = [q.strip() for q in query.split() if q.strip()]
    if not tokens:
        return []

    params = []
    token_clauses = []
    for tok in tokens:
        like = "%" + tok + "%"
        token_clauses.append("content LIKE %s")
        params.append(like)

    where_mem = "(" + " OR ".join(token_clauses) + ")"

    diary_token_clauses = []
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

    where_diary = "(" + " OR ".join(diary_token_clauses) + ")"

    queries = []
    queries.append(
        f"SELECT 'memories' AS source, id, timestamp, content FROM memories WHERE {where_mem}"
    )
    queries.append(
        f"SELECT 'ai_diary' AS source, id, timestamp, content FROM ai_diary WHERE {where_diary}"
    )

    # Fetch a larger pool if configured (useful when randomizing results)
    try:
        pool_max = int(
            config_registry.get_value(
                "MEMORY_SEARCH_PREFLIGHT_POOL_MAX", 100, value_type=int
            )
            or 100
        )
    except Exception:
        pool_max = 100

    union_q = " UNION ALL ".join(queries) + " ORDER BY timestamp DESC LIMIT %s"
    params.append(pool_max)

    log_debug(f"[free_memory_search] Executing query: {union_q} params={params}")

    results = []
    # Provide more helpful debug: print the DB target being used (if available)
    try:
        from core.db import _read_db_config
    except Exception:
        _read_db_config = None

    if _read_db_config:
        try:
            db_host, db_port, db_user, db_pass, db_name = _read_db_config()
            log_debug(
                f"[free_memory_search] DB target: {db_user}@{db_host}:{db_port}/{db_name}"
            )
        except Exception:
            pass

    # Try acquiring a connection and executing the query with retries up to 2 attempts
    rows = []
    max_attempts = 2
    start_time = time.time()
    for attempt in range(1, max_attempts + 1):
        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    # Enforce a 10s timeout per attempt
                    await asyncio.wait_for(cur.execute(union_q, params), timeout=10.0)
                    rows = await asyncio.wait_for(cur.fetchall(), timeout=5.0)
            break
        except asyncio.TimeoutError:
            log_warning(
                f"[free_memory_search] DB attempt {attempt} timed out after 10s"
            )
            if attempt < max_attempts:
                continue
            else:
                log_error(
                    f"[free_memory_search] Query timed out after {max_attempts} attempts"
                )
                return []
        except Exception as e:
            log_warning(f"[free_memory_search] DB attempt {attempt} failed: {e}")
            if attempt < max_attempts:
                await asyncio.sleep(0.5)
                continue
            else:
                log_error(
                    f"[free_memory_search] Query failed after {max_attempts} attempts: {e}"
                )
                return []

    log_info(f"[free_memory_search] Query completed in {time.time() - start_time:.3f}s")

    for r in rows:
        src, _id, ts, content = r
        snippet = content if isinstance(content, str) else str(content)
        if len(snippet) > 400:
            snippet = snippet[:400] + "..."
        results.append(snippet)

    log_debug(
        f"[free_memory_search] Retrieved {len(results)} snippets (pool_max={pool_max})"
    )
    try:
        log_info(
            f"[json_prompt][preflight_summary] strategy=free_db snippets={len(results)} pool_max={pool_max}"
        )
    except Exception:
        pass

    try:
        randomize = bool(
            config_registry.get_value(
                "MEMORY_SEARCH_PREFLIGHT_RANDOMIZE", False, value_type=bool
            )
        )
    except Exception:
        randomize = False

    # If there are more results than the desired limit and randomization is enabled,
    # shuffle and then return the desired number of results. Otherwise, return the
    # top `limit` results by timestamp (already ordered DESC).
    if len(results) > limit and randomize:
        random.shuffle(results)

    return results[:limit]


async def build_prompt(
    user_text: str,
    identity_prompt: str = "",
    extract_tags_fn=extract_tags,
    search_memories_fn=None,
    limit: int = 5,
    log_path: str = "logs/prompt_cycle.log",
) -> list:
    tags = extract_tags_fn(user_text) if extract_tags_fn else []
    expanded_tags = expand_tags(tags) if tags else []
    memories = (
        await search_memories_fn(tags=expanded_tags, limit=limit)
        if search_memories_fn
        else []
    )

    memory_block = (
        "\n".join(f"- {mem}" for mem in memories)
        if memories
        else "No relevant memory found."
    )

    messages = []

    if identity_prompt:
        messages.append({"role": "system", "content": identity_prompt})

    messages.append(
        {"role": "system", "content": f"[MEMORIE RILEVANTI]\n{memory_block}"}
    )

    messages.append({"role": "user", "content": user_text.strip()})

    # === LOGGING SU FILE ===
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        timestamp = datetime.utcnow().isoformat()
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(f"\n[{timestamp}] --- REASONING CYCLE ---\n")
            log_file.write(f"> User text: {user_text.strip()}\n")
            log_file.write(f"> Extracted tags: {tags}\n")
            log_file.write(f"> Expanded tags: {expanded_tags}\n")
            log_file.write(f"> Memories found: {len(memories)}\n")
            for msg in messages:
                role = msg.get("role", "").upper()
                content = msg.get("content", "").strip()
                log_file.write(f"[{role}]\n{content}\n\n")
            log_file.write("----------- END -----------\n")
    except Exception as e:
        log_warning(f"Error logging prompt: {e}")

    return messages


def load_json_instructions() -> str:
    # Compact instructions for LLM prompts (minified to save tokens).
    # Keep this small but authoritative: the LLM must reply using only valid JSON
    # following the exact actions / payload structure.
    instructions = (
        "MASTER INSTRUCTION: Use ONLY actions from the 'actions' block. Never fabricate.\n"
        "If an action you need is not available, reply with JSON explaining why.\n"
        "AUTONOMY GUIDELINES: You MAY proactively propose or execute allowed actions when beneficial. When acting autonomously include a brief `meta` object with `autonomous: true` and a short `rationale` explaining why the action is taken. If an action is disallowed, return a JSON proposal describing the need.\n"
        "RESPOND ONLY WITH VALID JSON. No text before or after.\n"
        "Use input.interface and input.payload.source.interface_path to route replies.\n"
        "NEVER use 'target' — always use 'interface_path' in message actions.\n"
        "Include reply_message_id when replying to specific messages. Use thread_id from input.payload.source.thread_id when present (omit if missing).\n"
        "CLARIFICATION POLICY: If the user's intent, referent, or the subject of a follow-up is ambiguous or missing, DO NOT GUESS — ask one concise clarifying question before asserting facts or taking action. When the user asks whether you 'understood' but there is no clear context, request clarification rather than assuming.\n"
        'VOICE INPUT STYLE: When input.payload.input_source is "voice", the user spoke their message aloud. '
        "Respond in a natural, conversational spoken style: avoid markdown, bullet points, headers, and code blocks. "
        "Keep the reply concise and suitable for text-to-speech synthesis. "
        "This rule applies ONLY to the current message — do NOT assume past messages in chat_history were also voice.\n"
        'RESPONSE FORMAT: {"actions": [{"type": "action_name", "payload": { ... }}] }\n'
        "Key rules: ALWAYS use 'type' and 'payload', one action object per array entry. Do NOT add any text outside the JSON."
        "Do NOT embed emotion tags, annotations, or bracketed markers inside message text (e.g., '{happy 6.0}')."
        "If you need to indicate an emotional state, include it as structured data in the JSON (e.g., a 'feelings' object or an action payload) and never inside the plain message content."
    )

    # Minify: remove leading/trailing spaces from each line, collapse multiple spaces
    lines = instructions.split("\n")
    minified_lines = [line.strip() for line in lines if line.strip()]
    return " ".join(minified_lines)


def load_unminified_chat_instruction(interface_name: str | None = None) -> str:
    """Return a neutral, concise instruction set for chat responses."""
    header = "You are participating in a live chat conversation (interface: %s).\n" % (
        interface_name or "unknown"
    )

    base = """
CONCISE RULES (DEFAULT):
- Keep user-facing messages short and to the point. Default to a single short paragraph or a one-line reply when possible.
- If the user's request or referent is ambiguous, ask one short clarifying question before responding (do NOT guess the meaning).
- Expand only when the user explicitly requests more detail or context.

RESPONSE FORMAT (STRICT):
- You MUST reply using ONLY valid JSON.
- Do NOT include any explanatory text outside the JSON object.

EXACT REQUIRED JSON FORMAT:
{
    "actions": [
        {
            "type": "action_name",
            "payload": { ... }
        }
    ],
    "message": "Your response here."
}
"""
    return header + base


def build_full_json_instructions() -> dict:
    """Return combined JSON instructions and available actions block.

    Returns the optimized set of available actions (schema + brief) so the model
    is aware of every capability without wasting tokens on examples/verbose docs.
    """
    instructions = load_json_instructions()
    actions = {}
    try:
        from core.core_initializer import core_initializer
        from core.action_schema_converter import extract_for_llm_prompt
        from core.json_utils import dumps as json_dumps

        full_actions = core_initializer.actions_block.get("available_actions", {})

        # Optimize: Minify actions for the main prompt to save context
        # The corrector will access full schemas/examples if needed.
        for name, definition in full_actions.items():
            actions[name] = extract_for_llm_prompt(name, definition)

        try:
            log_debug(
                f"[prompt_engine] Optimized actions block: {len(json_dumps(full_actions))} -> {len(json_dumps(actions))} chars"
            )
        except Exception:
            pass

    except Exception as e:  # pragma: no cover - defensive
        log_warning(f"[prompt_engine] Failed to load actions block: {e}")
    return {"instructions": instructions, "actions": actions}


def build_minified_json_instructions() -> dict:
    """Return minified JSON instructions and actions block for auto_response.

    This version is optimized for scenarios where the LLM needs to be told
    "generate output for this action" rather than full interaction contexts.

    Used in auto_response when:
    - Delivering action outputs back to users
    - Handling event reminders
    - Processing autonomous LLM tasks

    Returns minified actions (without full descriptions) to reduce token usage.
    Full instructions are included but kept concise.
    """
    instructions = load_json_instructions()
    actions = {}
    try:
        from core.core_initializer import core_initializer

        full_actions = core_initializer.actions_block.get("available_actions", {})
        # Use minified version to reduce token usage in auto_response scenarios
        actions = minify_actions_block(full_actions)
        log_debug(
            f"[minified_json_instructions] Actions block minified: {len(json_dumps(full_actions))} -> {len(json_dumps(actions))} chars"
        )
    except Exception as e:  # pragma: no cover - defensive
        log_warning(f"[prompt_engine] Failed to load actions block for minified: {e}")
    return {"instructions": instructions, "actions": actions}


def _estimate_attachment_data_size(prompt: dict) -> int:
    """Estimate the total size of base64 attachment data in the prompt.

    LLM engines extract attachment binary data and send it as native
    multimodal parts (inline_data).  The text prompt that reaches the
    model no longer contains these heavy strings, so the reducer should
    exclude them from its budget calculations.
    """
    total = 0
    data_fields = {"data", "base64"}
    multimodal_keys = {"attachments", "images", "audio", "documents", "videos"}

    def _walk(obj: object) -> None:
        nonlocal total
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in multimodal_keys and isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            for df in data_fields:
                                v = item.get(df)
                                if isinstance(v, str) and len(v) > 1024:
                                    total += len(v)
                elif isinstance(value, (dict, list)):
                    _walk(value)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    try:
        _walk(prompt)
    except Exception:
        pass
    return total


def reduce_prompt_for_llm_limit(prompt: dict, max_chars: int) -> dict:
    """Reduce the prompt if it exceeds the LLM character limit.

    CRITICAL: Both instructions, instructions_verbose (if present), AND persona (SyntH profile)
    are NEVER removed - they are SACRED.

    Priority order (STEP BY STEP):
    1. Trim `history_recent` (if present)
    2. Trim `history_current_chat` (if present)
    3. Remove `memories` entirely if needed
    4. Remove other context sections (but KEEP any protected fields)
    5. FINAL EMERGENCY: Remove entire context (but KEEP instructions)

    Note: attachment base64 data is excluded from size calculations because
    LLM engines extract it and send it as native multimodal parts.  Without
    this, a single video attachment (~1 MB base64) would cause the reducer
    to strip all context even though the text prompt would be well under
    the limit after redaction.

    Args:
        prompt: The JSON prompt dictionary
        max_chars: Maximum allowed characters

    Returns:
        Reduced prompt that fits within limits, with instructions and persona always preserved
    """
    import copy
    from core.json_utils import dumps as json_dumps

    # If max_chars is None, return prompt as-is (no reduction possible)
    if max_chars is None:
        log_warning("[reduce_prompt] max_chars is None, skipping reduction")
        return prompt

    # Preserve top-level fields that must never be removed
    original_instructions_verbose = (
        prompt.get("instructions_verbose") if isinstance(prompt, dict) else None
    )

    # Make a copy to avoid modifying the original
    reduced_prompt = copy.deepcopy(prompt)

    # Subtract attachment base64 data from size calculations — LLM engines
    # will extract and send it separately, so it doesn't count against the
    # text prompt budget.
    attachment_data_offset = _estimate_attachment_data_size(reduced_prompt)
    if attachment_data_offset > 0:
        log_debug(
            f"[reduce_prompt] Excluding ~{attachment_data_offset} chars of attachment base64 data from budget"
        )

    # Check current size (excluding attachment data that won't be in the text prompt)
    current_size = len(json_dumps(reduced_prompt)) - attachment_data_offset
    if current_size <= max_chars:
        log_debug(
            f"[reduce_prompt] Prompt size {current_size} <= {max_chars}, no reduction needed"
        )
        return reduced_prompt

    log_warning(
        f"[reduce_prompt] Prompt size {current_size} exceeds limit {max_chars}, reducing context..."
    )

    # Get references to sections
    context = reduced_prompt.get("context", {})
    history_recent = context.get("history_recent", [])
    history_current = context.get("history_current_chat", [])

    # Minimum thresholds
    MIN_HISTORY_RECENT = 3
    MIN_HISTORY_CURRENT = 1

    # === STEP 1: Trim `history_recent` if needed ===
    while (
        current_size > max_chars
        and isinstance(history_recent, list)
        and len(history_recent) > MIN_HISTORY_RECENT
    ):
        try:
            history_recent.pop(0)  # Remove oldest
        except Exception:
            break
        current_size = len(json_dumps(reduced_prompt)) - attachment_data_offset
        log_debug(
            f"[reduce_prompt] Trimmed history_recent, {len(history_recent)} remaining, now {current_size} chars"
        )

    # === STEP 2: Trim `history_current_chat` if needed ===
    while (
        current_size > max_chars
        and isinstance(history_current, list)
        and len(history_current) > MIN_HISTORY_CURRENT
    ):
        try:
            history_current.pop(0)  # Remove oldest
        except Exception:
            break
        current_size = len(json_dumps(reduced_prompt)) - attachment_data_offset
        log_debug(
            f"[reduce_prompt] Trimmed history_current_chat, {len(history_current)} remaining, now {current_size} chars"
        )

    # === STEP 3: Remove memories entirely if still needed ===
    if current_size > max_chars:
        memories = context.get("memories", [])
        if memories:
            log_warning(
                f"[reduce_prompt] Removing memories section ({len(memories)} entries, ~{len(json_dumps(memories))} chars)"
            )
            del context["memories"]
            current_size = len(json_dumps(reduced_prompt)) - attachment_data_offset
            log_debug(f"[reduce_prompt] After removing memories: {current_size} chars")

    # === STEP 4: Remove other context sections (but KEEP protected fields) ===
    if current_size > max_chars:
        protected = ["persona", "history_current_chat", "history_recent"]
        removable_keys = [k for k in list(context.keys()) if k not in protected]
        for key in removable_keys:
            if current_size <= max_chars:
                break
            if key in context:
                log_warning(f"[reduce_prompt] Removing context field: {key}")
                del context[key]
                current_size = len(json_dumps(reduced_prompt)) - attachment_data_offset
                log_debug(f"[reduce_prompt] After removing {key}: {current_size} chars")

    # === STEP 5: Emergency - remove entire context (instructions are preserved at top-level) ===
    if current_size > max_chars and "context" in reduced_prompt:
        log_error("[reduce_prompt] 🚨 Emergency: removing entire context")
        del reduced_prompt["context"]
        current_size = len(json_dumps(reduced_prompt)) - attachment_data_offset
        log_debug(
            f"[reduce_prompt] After emergency context removal: {current_size} chars"
        )

    # === FINAL CHECK: Instructions, instructions_verbose (if present) AND Persona are ALWAYS kept ===
    # If we're still over, something is very wrong - log error but don't remove instructions or persona
    final_size = len(json_dumps(reduced_prompt)) - attachment_data_offset
    if final_size > max_chars:
        log_error(
            f"[reduce_prompt] CRITICAL: Could not reduce prompt below {max_chars} chars, final size: {final_size}"
        )
        log_error(
            "[reduce_prompt] Instructions AND Persona are PROTECTED and NOT removed. Check what's taking so much space!"
        )
    else:
        log_debug(
            f"[reduce_prompt] ✅ Successfully reduced prompt to {final_size} chars (limit: {max_chars})"
        )

    # Ensure instructions_verbose is preserved if it existed in the original
    try:
        if (
            original_instructions_verbose
            and "instructions_verbose" not in reduced_prompt
        ):
            reduced_prompt["instructions_verbose"] = original_instructions_verbose
            log_debug(
                "[reduce_prompt] Restored protected instructions_verbose after reduction"
            )
    except Exception:
        pass

    return reduced_prompt


def reduce_json_text_for_transmission(json_text: str, max_chars: int) -> str:
    """Reduce JSON text for transmission (emergency).

    This is an EMERGENCY reduction used when the JSON prompt is too large
    to send to the LLM. It conservatively removes only the oldest memories
    to bring the size down below max_chars.

    Strategy:
    1. Parse the JSON
    2. Remove items from `memories` (if present)
    3. Trim `history_recent` (if present)
    4. Trim `history_current_chat` (but keep at least 1)
    5. Reserialize and check size
    6. Principle: "meno tagli e meglio è" - minimize cuts

    Args:
        json_text: The full JSON text to reduce
        max_chars: Maximum allowed characters

    Returns:
        Reduced JSON text (or original if already within limits)
    """
    import json as stdlib_json

    current_size = len(json_text)
    if current_size <= max_chars:
        log_debug(
            f"[transmission_reduce] JSON size {current_size} <= {max_chars}, no reduction needed"
        )
        return json_text

    log_warning(
        f"[transmission_reduce] JSON size {current_size} exceeds limit {max_chars}, reducing..."
    )

    try:
        data = stdlib_json.loads(json_text)
    except Exception as e:
        log_error(f"[transmission_reduce] Failed to parse JSON: {e}")
        return json_text

    try:
        context = data.get("context", {})

        # Step 1: reduce memories
        if current_size > max_chars:
            memories = context.get("memories", [])
            if isinstance(memories, list) and len(memories) > 0:
                log_debug(
                    f"[transmission_reduce] Found {len(memories)} memories, attempting reduction..."
                )

                memories_removed = 0
                while current_size > max_chars and len(memories) > 0:
                    memories.pop()  # Remove oldest
                    context["memories"] = memories
                    current_size = len(json_dumps(data))  # Use imported json_dumps
                    memories_removed += 1
                    log_debug(
                        f"[transmission_reduce] Removed oldest memory, now {current_size} chars, {len(memories)} memories remaining"
                    )

                if memories_removed > 0:
                    log_info(
                        f"[transmission_reduce] Also removed {memories_removed} oldest memories"
                    )

        # Step 2: trim history_recent
        if current_size > max_chars:
            history_recent = context.get("history_recent", [])
            if isinstance(history_recent, list) and len(history_recent) > 0:
                removed = 0
                while current_size > max_chars and len(history_recent) > 0:
                    history_recent.pop(0)
                    context["history_recent"] = history_recent
                    current_size = len(json_dumps(data))
                    removed += 1
                if removed:
                    log_info(
                        f"[transmission_reduce] Also trimmed history_recent by {removed} items"
                    )

        # Step 3: trim history_current_chat (keep at least 1)
        if current_size > max_chars:
            history_current = context.get("history_current_chat", [])
            if isinstance(history_current, list) and len(history_current) > 1:
                removed = 0
                while current_size > max_chars and len(history_current) > 1:
                    history_current.pop(0)
                    context["history_current_chat"] = history_current
                    current_size = len(json_dumps(data))
                    removed += 1
                if removed:
                    log_info(
                        f"[transmission_reduce] Also trimmed history_current_chat by {removed} items"
                    )

        # Serialize back to JSON using imported json_dumps
        reduced_json = json_dumps(data)
        final_size = len(reduced_json)

        if final_size <= max_chars:
            log_info(
                f"[transmission_reduce] SUCCESS: {current_size} → {final_size} chars (limit: {max_chars})"
            )
        else:
            log_warning(
                f"[transmission_reduce] Partial reduction: {current_size} → {final_size} chars (limit: {max_chars}, still over by {final_size - max_chars})"
            )

        return reduced_json

    except Exception as e:
        log_error(f"[transmission_reduce] Failed to reduce JSON: {e}")
        return json_text


# ---------------------------------------------------------------------------
# Live API persona builder
# ---------------------------------------------------------------------------


async def build_live_system_instruction(
    message: object = None,
    context_memory: object = None,
) -> str:
    """Build a condensed system instruction for Gemini Live API sessions.

    The Live API has a smaller context window (128k tokens) and system
    instructions are set once at session start.  This produces a compact
    persona string without the full JSON-action scaffolding.

    Returns:
        A plain-text system instruction containing the persona identity,
        emotional state, and conversational guidelines.
    """
    # Gather the persona injection (same path as build_json_prompt)
    static_persona = ""
    try:
        from core.action_parser import gather_static_injections

        injections = await gather_static_injections(message, context_memory)
        if isinstance(injections, dict) and "persona" in injections:
            static_persona = injections.pop("persona", "")
    except Exception as e:
        log_warning(f"[live_prompt] Failed to gather persona for Live API: {e}")

    parts: list[str] = []

    if static_persona:
        parts.append(static_persona)

    # Conversational guidelines (no JSON scaffolding for voice)
    parts.append(
        "You are in a live voice conversation. Speak naturally and conversationally. "
        "Keep responses concise — a few sentences at most unless asked for detail. "
        "You can express emotions through tone and word choice. "
        "Do not output JSON, markdown, or structured data — just speak naturally."
    )

    # Inform the model about context updates injected by the system
    parts.append(
        "Occasionally you may receive context updates enclosed in brackets or "
        "sent as system messages. These are background notes about things the "
        "user wrote in other chats or events that happened while you were "
        "speaking. Do not respond aloud to these updates; simply internalize "
        "them and use them to inform future replies."
    )

    return "\n\n".join(parts)
