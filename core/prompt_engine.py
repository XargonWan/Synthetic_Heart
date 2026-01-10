# core/prompt_engine.py

from core.synth_tagging import extract_tags, expand_tags
import aiomysql
from core.db import get_conn_ctx
from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.json_utils import dumps as json_dumps
from core.config_manager import config_registry
from core.user_utils import get_user_display_name, get_user_usertag
import aiomysql
import os

# Default maximum prompt characters (CHARACTERS, NOT TOKENS)
# This is used as a safe fallback when no LLM engine provides explicit limits.
# The actual value comes from the active LLM engine's configuration.
# For ChatGPT, see llm_engines/selenium_chatgpt.py MODEL_LIMITS_MAP["default"]
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
    from core.action_schema_converter import extract_for_llm_prompt, normalize_action_schema
    
    minified = {}
    for action_name, action_def in available_actions.items():
        # Normalize to new format (handles both old and new formats)
        normalized = normalize_action_schema(action_name, action_def)
        
        # Extract only what's needed for LLM (schema + brief)
        minified_action = extract_for_llm_prompt(action_name, normalized)
        
        minified[action_name] = minified_action
    
    return minified


async def build_json_prompt(message, context_memory, interface_name: str | None = None, image_data: dict | None = None, max_chars: int | None = None) -> dict:
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
    """
    import time
    start_time = time.time()
    log_info(f"[json_prompt] ⏱️ BUILD PROMPT START for interface={interface_name}")
    
    interface_path = getattr(message, "interface_path", None)
    text = getattr(message, "text", "") or ""
    
    # Determine if context_memory is a chat history map or a context dict
    # Context dicts have keys like 'interface_path', 'system_message', etc.
    # Chat history maps have interface_path as keys
    is_context_dict = isinstance(context_memory, dict) and any(
        key in context_memory for key in ['interface_path', 'system_message', 'chat_id_context']
    )

    # History-like context is now produced by HistoryEngine (plugin-centric aggregation)

    # === 2. Tags and memory lookup ===
    tags = extract_tags(text)
    expanded_tags = expand_tags(tags)
    memories = []
    if expanded_tags:
        # Limit follows unified verbosity (HistoryEngine will also apply a hard cap)
        try:
            from core.history_engine import _get_int as _history_get_int
            mem_limit = int(_history_get_int('CONTEXT_VERBOSITY', 10))
        except Exception:
            mem_limit = 10
        memories = await search_memories(tags=expanded_tags, limit=max(1, mem_limit))
        log_debug(f"[json_prompt] ⏱️ Loaded {len(memories)} memories from tags in {time.time() - start_time:.2f}s")

    # === 3. Context base (history + optional plugin contributions) ===
    try:
        from core.history_engine import HistoryEngine

        history_engine = HistoryEngine()
        context_section = await history_engine.build_context(
            message=message,
            context_memory=context_memory,
            interface_name=interface_name,
            text=text,
            memories=memories,
        )
    except Exception as e:
        log_warning(f"[json_prompt] Failed to build history context via HistoryEngine: {e}")
        context_section = {"memories": memories}

    # === 3a. Static injections from plugins ===
    static_persona = None  # Extract persona separately for instructions
    try:
        from core.action_parser import gather_static_injections

        log_info(f"[json_prompt] 🔄 About to call gather_static_injections()")
        injections = await gather_static_injections(message, context_memory)
        log_info(f"[json_prompt] 📥 gather_static_injections() returned: {list(injections.keys()) if injections else 'empty'}")
        if isinstance(injections, dict):
            # Extract persona BEFORE adding to context - it will go to instructions instead
            if "persona" in injections:
                static_persona = injections.pop("persona")
                log_info(f"[json_prompt] 👤 Extracted persona for instructions ({len(static_persona) if static_persona else 0} chars)")
            
            # Add remaining injections to context (but drop deprecated legacy keys)
            context_section.update(injections)
            # Deprecated (migrated to HistoryEngine)
            for legacy_key in ("latest_diary_entries", "diary_entries", "diary", "chat_history", "current_chat_history"):
                if legacy_key in context_section:
                    context_section.pop(legacy_key, None)
            log_info(f"[json_prompt] ✅ Updated context_section with injections. Keys now: {list(context_section.keys())}")
    except Exception as e:
        log_warning(f"[json_prompt] Failed to gather static injections: {e}")

    # === 4. Input payload ===
    # interface_path was already extracted at the beginning
    # If still not found, check if context_memory is actually a context dict with interface_path
    if not interface_path and isinstance(context_memory, dict) and "interface_path" in context_memory:
        interface_path = context_memory.get("interface_path")
        log_debug(f"[json_prompt] Retrieved interface_path from context dict: {interface_path}")
    
    input_payload = {
        "text": text,
        "source": {
            "interface_path": interface_path,
            "message_id": message.message_id,
            "username": get_user_display_name(getattr(message, "from_user", None)),
            "usertag": get_user_usertag(getattr(message, 'from_user', None)),
            "interface": interface_name,
        },
        "timestamp": message.date.isoformat(),
        "privacy": "default",
        "scope": "local",
    }

    # Add image data if present
    if image_data:
        input_payload["image"] = image_data
        log_debug(f"[json_prompt] Including image data in prompt: {image_data.get('type', 'unknown')}")

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
        log_info(f"[json_prompt] 👤 Persona prepended to instructions ({len(static_persona)} chars)")

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

    # For chat-like interfaces (Telegram, Discord, WebUI) include an explicit
    # unminified instruction block that reminds the LLM this is a chat and
    # must be concise. This must be preserved verbatim and sent as a system
    # message by LLM wrappers. We intentionally do not minify this text.
    try:
        chat_ifaces = ["telegram", "discord", "webui", "synth_webui", "telegram_bot", "discord_bot"]
        if interface_name and any(k in (interface_name or "").lower() for k in chat_ifaces):
            prompt_with_instructions["instructions_verbose"] = load_unminified_chat_instruction(interface_name)
            log_info(f"[json_prompt] 🔒 Added instructions_verbose for chat interface: {interface_name}")
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
        full_actions = core_initializer.actions_block.get(
            "available_actions", {}
        )
        # Minify to reduce token usage
        prompt_with_instructions["actions"] = minify_actions_block(full_actions)
        log_debug(f"[json_prompt] Actions block minified: {len(json_dumps(full_actions))} -> {len(json_dumps(prompt_with_instructions['actions']))} chars")
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
                from core.config import get_active_llm
                from core.llm_registry import get_llm_registry

                active_llm = await get_active_llm()
                registry = get_llm_registry()
                engine = registry.get_engine(active_llm)

                if not engine:
                    engine = registry.load_engine(active_llm)

                if engine and hasattr(engine, 'get_interface_limits'):
                    limits = engine.get_interface_limits()
                    max_prompt_chars = limits.get("max_prompt_chars")
            except Exception as e:
                log_debug(f"[json_prompt] Could not get interface limits for reduction: {e}")
        
        # Apply reduction only if max_chars is available
        if max_prompt_chars:
            prompt_with_instructions = reduce_prompt_for_llm_limit(prompt_with_instructions, max_prompt_chars)
        
    except Exception as e:
        log_warning(f"[json_prompt] Failed to apply prompt reduction: {e}")

    elapsed = time.time() - start_time
    log_info(f"[json_prompt] ⏱️ BUILD PROMPT COMPLETE in {elapsed:.2f}s, final size: {len(json_dumps(prompt_with_instructions)) if isinstance(prompt_with_instructions, dict) else len(str(prompt_with_instructions))} chars")
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

                log_debug(f"[search_memories] Retrieved {len(memories)} memories, ~{sum(len(str(m)) for m in memories)} chars total")
                return memories
        except Exception as e:
            log_error(f"Query failed: {repr(e)}")
            return []

async def build_prompt(
    user_text: str,
    identity_prompt: str = "",
    extract_tags_fn=extract_tags,
    search_memories_fn=None,
    limit: int = 5,
    log_path: str = "logs/prompt_cycle.log"
) -> list:
    tags = extract_tags_fn(user_text) if extract_tags_fn else []
    expanded_tags = expand_tags(tags) if tags else []
    memories = await search_memories_fn(tags=expanded_tags, limit=limit) if search_memories_fn else []

    memory_block = "\n".join(f"- {mem}" for mem in memories) if memories else "No relevant memory found."

    messages = []

    if identity_prompt:
        messages.append({"role": "system", "content": identity_prompt})

    messages.append({
        "role": "system",
        "content": f"[MEMORIE RILEVANTI]\n{memory_block}"
    })

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
                "RESPONSE FORMAT: {\"actions\": [{\"type\": \"action_name\", \"payload\": { ... }}] }\n"
                "Key rules: ALWAYS use 'type' and 'payload', one action object per array entry. Do NOT add any text outside the JSON."
                "Do NOT embed emotion tags, annotations, or bracketed markers inside message text (e.g., '{happy 6.0}')."
                "If you need to indicate an emotional state, include it as structured data in the JSON (e.g., a 'feelings' object or an action payload) and never inside the plain message content."
        )
        
    # Minify: remove leading/trailing spaces from each line, collapse multiple spaces
        import re
        lines = instructions.split('\n')
        minified_lines = [line.strip() for line in lines if line.strip()]
        return ' '.join(minified_lines)


def load_unminified_chat_instruction(interface_name: str | None = None) -> str:
        """Return an unminified, explicit chat instruction for chat interfaces.

        This text is intentionally verbose and must NOT be minified: it explains
        that the LLM is operating inside a chat interface, must be concise, and
        must follow the exact JSON response format. It should be preserved verbatim
        and sent as a system message to the model for chat interfaces (Telegram,
        Discord, WebUI).
        """
        # Keep this human-readable and not minified — we'll inject it directly
        header = "You are participating in a live chat conversation (interface: %s).\n" % (interface_name or "unknown")

        base = """
    This means your replies must be short, concise, and suitable for a chat UI.
This means your replies must be short, concise, and suitable for a chat UI.

CONCISE RULES:
- Keep user-facing messages short and to the point.
- Prefer short paragraphs or single-line replies when possible.
- Avoid long essays or verbose explanations unless explicitly requested by the user.

WHEN REFERENCING RECENT MESSAGES:
- When you mention a recent message, refer to its author in a precise but generic way (for example: "the author of the message said...", "the user wrote..."). Do NOT insert or invent specific personal names in these references.
- Avoid vague or impersonal phrasings such as "I saw someone" or "someone said"; aim to be concise, natural, and informative without naming individuals.

RESPONSE FORMAT:
- You MUST reply using ONLY valid JSON, and follow the exact structure shown below.
- Do NOT include any explanatory text outside the JSON object.

EXACT REQUIRED JSON FORMAT (use this, verbatim):
{
    "actions": [
        {
            "type": "action_name_from_actions_block",
            "payload": { ... }
        }
    ]
}

KEY REMINDERS:
- Each action object MUST contain exactly two keys: "type" and "payload".
- The "type" value MUST match a name from the 'actions' block supplied in the prompt.
- Use the provided interface_path from input.payload.source.interface_path when addressing replies.

EMOTIONS & METADATA:
- Do NOT insert emotional tags, annotations, or bracketed markers inside message text (for example: `{happy 6.0}`).
- If you need to convey emotion metadata, include it only as structured JSON (e.g., a `feelings` field or an explicit action payload), not inside the user-facing text.
"""
        # Prepend header (with interface name) and return; do NOT minify this text
        return header + base


def build_full_json_instructions() -> dict:
    """Return combined JSON instructions and available actions block.

    Always returns the full set of available actions so the model is aware of
    every capability, preserving flexibility and avoiding accidental action
    masking.
    """
    instructions = load_json_instructions()
    actions = {}
    try:
        from core.core_initializer import core_initializer
        actions = core_initializer.actions_block.get("available_actions", {})
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
        log_debug(f"[minified_json_instructions] Actions block minified: {len(json_dumps(full_actions))} -> {len(json_dumps(actions))} chars")
    except Exception as e:  # pragma: no cover - defensive
        log_warning(f"[prompt_engine] Failed to load actions block for minified: {e}")
    return {"instructions": instructions, "actions": actions}

def reduce_prompt_for_llm_limit(prompt: dict, max_chars: int) -> dict:
    """Reduce the prompt if it exceeds the LLM character limit.
    
    CRITICAL: Both instructions, instructions_verbose (if present), AND persona (Rekku profile)
    are NEVER removed - they are SACRED.
    
    Priority order (STEP BY STEP):
    1. Trim `history_recent` (if present)
    2. Trim `history_current_chat` (if present)
    3. Remove `memories` entirely if needed
    4. Remove other context sections (but KEEP any protected fields)
    5. FINAL EMERGENCY: Remove entire context (but KEEP instructions)
    
    Args:
        prompt: The JSON prompt dictionary
        max_chars: Maximum allowed characters
        
    Returns:
        Reduced prompt that fits within limits, with instructions and persona always preserved
    """
    import copy
    import re
    from core.json_utils import dumps as json_dumps
    
    # If max_chars is None, return prompt as-is (no reduction possible)
    if max_chars is None:
        log_warning("[reduce_prompt] max_chars is None, skipping reduction")
        return prompt
    
    # Preserve top-level fields that must never be removed
    original_instructions_verbose = prompt.get("instructions_verbose") if isinstance(prompt, dict) else None

    # Make a copy to avoid modifying the original
    reduced_prompt = copy.deepcopy(prompt)
    
    # Check current size
    current_size = len(json_dumps(reduced_prompt))
    if current_size <= max_chars:
        log_debug(f"[reduce_prompt] Prompt size {current_size} <= {max_chars}, no reduction needed")
        return reduced_prompt
    
    log_warning(f"[reduce_prompt] Prompt size {current_size} exceeds limit {max_chars}, reducing context...")
    
    # Get references to sections
    context = reduced_prompt.get("context", {})
    history_recent = context.get("history_recent", [])
    history_current = context.get("history_current_chat", [])

    # Minimum thresholds
    MIN_HISTORY_RECENT = 3
    MIN_HISTORY_CURRENT = 1

    # === STEP 1: Trim `history_recent` if needed ===
    while current_size > max_chars and isinstance(history_recent, list) and len(history_recent) > MIN_HISTORY_RECENT:
        try:
            history_recent.pop(0)  # Remove oldest
        except Exception:
            break
        current_size = len(json_dumps(reduced_prompt))
        log_debug(f"[reduce_prompt] Trimmed history_recent, {len(history_recent)} remaining, now {current_size} chars")

    # === STEP 2: Trim `history_current_chat` if needed ===
    while current_size > max_chars and isinstance(history_current, list) and len(history_current) > MIN_HISTORY_CURRENT:
        try:
            history_current.pop(0)  # Remove oldest
        except Exception:
            break
        current_size = len(json_dumps(reduced_prompt))
        log_debug(f"[reduce_prompt] Trimmed history_current_chat, {len(history_current)} remaining, now {current_size} chars")
    
    # === STEP 3: Remove memories entirely if still needed ===
    if current_size > max_chars:
        memories = context.get("memories", [])
        if memories:
            log_warning(f"[reduce_prompt] Removing memories section ({len(memories)} entries, ~{len(json_dumps(memories))} chars)")
            del context["memories"]
            current_size = len(json_dumps(reduced_prompt))
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
                current_size = len(json_dumps(reduced_prompt))
                log_debug(f"[reduce_prompt] After removing {key}: {current_size} chars")
    
    # === STEP 5: Emergency - remove entire context (instructions are preserved at top-level) ===
    if current_size > max_chars and "context" in reduced_prompt:
        log_error(f"[reduce_prompt] 🚨 Emergency: removing entire context")
        del reduced_prompt["context"]
        current_size = len(json_dumps(reduced_prompt))
        log_debug(f"[reduce_prompt] After emergency context removal: {current_size} chars")
    
    # === FINAL CHECK: Instructions, instructions_verbose (if present) AND Persona are ALWAYS kept ===
    # If we're still over, something is very wrong - log error but don't remove instructions or persona
    final_size = len(json_dumps(reduced_prompt))
    if final_size > max_chars:
        log_error(f"[reduce_prompt] CRITICAL: Could not reduce prompt below {max_chars} chars, final size: {final_size}")
        log_error(f"[reduce_prompt] Instructions AND Persona are PROTECTED and NOT removed. Check what's taking so much space!")
    else:
        log_debug(f"[reduce_prompt] ✅ Successfully reduced prompt to {final_size} chars (limit: {max_chars})")

    # Ensure instructions_verbose is preserved if it existed in the original
    try:
        if original_instructions_verbose and "instructions_verbose" not in reduced_prompt:
            reduced_prompt["instructions_verbose"] = original_instructions_verbose
            log_debug("[reduce_prompt] Restored protected instructions_verbose after reduction")
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
        log_debug(f"[transmission_reduce] JSON size {current_size} <= {max_chars}, no reduction needed")
        return json_text
    
    log_warning(f"[transmission_reduce] JSON size {current_size} exceeds limit {max_chars}, reducing...")
    
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
                log_debug(f"[transmission_reduce] Found {len(memories)} memories, attempting reduction...")
                
                memories_removed = 0
                while current_size > max_chars and len(memories) > 0:
                    removed_memory = memories.pop()  # Remove oldest
                    context["memories"] = memories
                    current_size = len(json_dumps(data))  # Use imported json_dumps
                    memories_removed += 1
                    log_debug(f"[transmission_reduce] Removed oldest memory, now {current_size} chars, {len(memories)} memories remaining")
                
                if memories_removed > 0:
                    log_info(f"[transmission_reduce] Also removed {memories_removed} oldest memories")

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
                    log_info(f"[transmission_reduce] Also trimmed history_recent by {removed} items")

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
                    log_info(f"[transmission_reduce] Also trimmed history_current_chat by {removed} items")
        
        # Serialize back to JSON using imported json_dumps
        reduced_json = json_dumps(data)
        final_size = len(reduced_json)
        
        if final_size <= max_chars:
            log_info(f"[transmission_reduce] SUCCESS: {current_size} → {final_size} chars (limit: {max_chars})")
        else:
            log_warning(f"[transmission_reduce] Partial reduction: {current_size} → {final_size} chars (limit: {max_chars}, still over by {final_size - max_chars})")
        
        return reduced_json
        
    except Exception as e:
        log_error(f"[transmission_reduce] Failed to reduce JSON: {e}")
        return json_text




