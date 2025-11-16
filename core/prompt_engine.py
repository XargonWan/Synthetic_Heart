# core/prompt_engine.py

from core.synth_tagging import extract_tags, expand_tags
import aiomysql
from core.db import get_conn_ctx
from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.json_utils import dumps as json_dumps
from core.config_manager import config_registry
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
    context_memory : dict[int, deque]
        Dictionary storing last messages per chat.
    interface_name : str | None
        Identifier of the interface that delivered the message.
    image_data : dict | None
        Processed image data from image_processor, if present.
    max_chars : int | None
        Maximum characters for the JSON prompt. If provided, the prompt will be
        intelligently reduced by removing oldest memories. If None, no reduction is done.
    """
    chat_id = getattr(message, "chat_id", None)
    text = getattr(message, "text", "") or ""

    # === 1. Context messages (chat_history) ===
    # Use CHAT_HISTORY from config_registry
    chat_history = list(context_memory.get(chat_id, []))[-CHAT_HISTORY_LIMIT:]

    # === 2. Tags and memory lookup ===
    tags = extract_tags(text)
    expanded_tags = expand_tags(tags)
    memories = []
    if expanded_tags:
        # Reduced to 3 memories for lighter JSON payloads; each will be trimmed to max 400 chars during reduction if needed
        memories = await search_memories(tags=expanded_tags, limit=3)

    # === 3. Context base (chat_history has priority over diary) ===
    context_section = {
        "chat_history": chat_history,
        "memories": memories,
    }

    # === 3a. Static injections from plugins ===
    try:
        from core.action_parser import gather_static_injections

        log_debug(f"[json_prompt] 🔄 About to call gather_static_injections()")
        injections = await gather_static_injections(message, context_memory)
        log_debug(f"[json_prompt] 📥 gather_static_injections() returned: {list(injections.keys()) if injections else 'empty'}")
        if isinstance(injections, dict):
            context_section.update(injections)
            log_debug(f"[json_prompt] ✅ Updated context_section with injections. Keys now: {list(context_section.keys())}")
    except Exception as e:
        log_warning(f"[json_prompt] Failed to gather static injections: {e}")

    # === 3b. AI Diary injection (uses remaining space after chat_history) ===
    try:
        from plugins.ai_diary import get_recent_entries, format_diary_for_injection, is_plugin_enabled, get_max_diary_chars, should_include_diary
        
        if is_plugin_enabled():
            # Get max prompt chars from active LLM first
            max_prompt_chars = DEFAULT_MAX_PROMPT_CHARS  # Default fallback
            try:
                from core.config import get_active_llm
                active_llm = await get_active_llm()
                
                # Get limits directly from the active LLM engine
                try:
                    from core.llm_registry import get_llm_registry
                    registry = get_llm_registry()
                    engine = registry.get_engine(active_llm)
                    
                    if not engine:
                        engine = registry.load_engine(active_llm)
                    
                    if engine and hasattr(engine, 'get_interface_limits'):
                        limits = engine.get_interface_limits()
                        max_prompt_chars = limits.get("max_prompt_chars", DEFAULT_MAX_PROMPT_CHARS)
                    else:
                        max_prompt_chars = DEFAULT_MAX_PROMPT_CHARS  # Fallback
                except Exception:
                    max_prompt_chars = DEFAULT_MAX_PROMPT_CHARS  # Safe fallback
                    
                log_debug(f"[json_prompt] Active interface max prompt chars: {max_prompt_chars}")
            except Exception as e:
                log_debug(f"[json_prompt] Could not get interface limits: {e}")
                max_prompt_chars = DEFAULT_MAX_PROMPT_CHARS  # Safe fallback
            
            # Get interface name
            interface_name = interface_name or "manual"
            
            # Calculate current prompt length including chat_history (approximate)
            # Chat history has priority, so diary gets what's left
            current_length = len(json_dumps(context_section)) + len(text)
            
            # Check if we should include diary (considering space already used by chat_history)
            if should_include_diary(interface_name, current_length, max_prompt_chars):
                max_chars = get_max_diary_chars(interface_name, current_length)
                
                # Use DIARY_HISTORY_DAYS from config_registry - cast to int to
                # avoid passing a ConfigVar-like object into timedelta()
                try:
                    days_val = int(DIARY_HISTORY_DAYS)
                except Exception:
                    days_val = 2
                recent_entries = get_recent_entries(days=days_val, max_chars=max_chars)
                
                if recent_entries:
                    # Store entries for potential reduction, and also formatted content
                    context_section["diary_entries"] = recent_entries
                    diary_content = format_diary_for_injection(recent_entries)
                    context_section["diary"] = diary_content
                    log_debug(f"[json_prompt] Added diary content: {len(diary_content)} chars from {len(recent_entries)} entries ({DIARY_HISTORY_DAYS} days)")
                else:
                    log_debug(f"[json_prompt] No diary entries to include (space: {max_chars} chars)")
            else:
                log_debug(f"[json_prompt] Diary not included due to space constraints (current: {current_length}, max: {max_prompt_chars})")
        
    except ImportError:
        log_debug("[json_prompt] AI Diary plugin not available")
    except Exception as e:
        log_warning(f"[json_prompt] Failed to add diary content: {e}")

    # === 4. Input payload ===
    thread_id = getattr(message, "thread_id", None)
    # Handle legacy message_thread_id from Telegram (map to thread_id)
    if thread_id is None:
        thread_id = getattr(message, "message_thread_id", None)
    
    # Normalize thread_id: treat 0 as "no thread" (None), keep positive integers
    # This is important because Telegram uses 0 for messages outside threads
    if thread_id == 0:
        thread_id = None
    
    input_payload = {
        "text": text,
        "source": {
            "chat_id": chat_id,
            "message_id": message.message_id,
            "username": message.from_user.full_name,
            "usertag": f"@{message.from_user.username}" if message.from_user.username else "(no tag)",
            "thread_id": thread_id,
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
        reply_full_name = getattr(reply_from, "full_name", "Unknown") if reply_from else "Unknown"
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
    
    # Interface-specific instructions are provided via the available actions block
    # No hardcoded interface references - plugins define their own instructions

    prompt_with_instructions = {
        "context": context_section,
        "input": input_section,
        "instructions": json_instructions,
    }

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
        # Load instructions and minify them immediately to save space
        instructions = """
- MASTER INSTRUCTION: Use ONLY actions from the 'actions' block. Never fabricate.
- If an action you need is not in 'actions', respond with a JSON explaining why.
- RESPOND ONLY WITH VALID JSON. No text before or after.
- Use input.interface to know where the message came from and respond there.
- NEVER lie. If you don't know something, say "I don't know".
- Target responses to input.payload.source.chat_id
- CRITICAL: Include thread_id ONLY if input.payload.source.thread_id is a positive integer (>0) - use that exact value! If thread_id is null/0/missing, OMIT the field from your payload.
- Include reply_message_id if replying to specific messages.
- ALWAYS include create_personal_diary_entry action to record interactions.
- Interaction_summary examples: "User asked about weather, provided forecast" or "Discussed coding, provided solutions"

RESPONSE FORMAT - Your response MUST be valid JSON in this exact structure:
{
  "actions": [
    {
      "type": "action_name_from_actions_block",
      "payload": {
        "field1": "value1",
        "field2": "value2"
      }
    },
    {
      "type": "another_action",
      "payload": {
        "required_field": "value",
        "optional_field": "value"
      }
    }
  ]
}

Key rules:
- ALWAYS use "type" (not "name", "action", or any other field)
- ALWAYS use "payload" to wrap your parameters (not "parameters", "args", or any other field)
- Each action MUST have exactly two fields: "type" and "payload"
- Do NOT add any text, explanation, or markdown outside the JSON
- Do NOT include "description" or "instructions" in your response
- The "type" must match exactly one from the 'actions' block
"""
        
        # Minify: remove leading/trailing spaces from each line, collapse multiple spaces
        import re
        lines = instructions.split('\n')
        minified_lines = [line.strip() for line in lines if line.strip()]
        return ' '.join(minified_lines)


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
    
    CRITICAL: Both instructions AND persona (Rekku profile) are NEVER removed - they are SACRED.
    
    Priority order (STEP BY STEP):
    1. Remove oldest diary entries (if >3 available)
    2. Remove oldest chat messages (if >3 available)
    3. Remove memories entirely if needed
    4. Remove other context sections (but KEEP persona)
    5. FINAL EMERGENCY: Remove entire context except persona (but KEEP instructions + persona)
    
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
    diary_entries = context.get("diary_entries", [])
    chat_history = context.get("chat_history", [])
    
    # Minimum thresholds
    MIN_DIARY_ENTRIES = 3
    MIN_CHAT_MESSAGES = 3
    
    # === STEP 1: Remove oldest diary entries if needed ===
    while current_size > max_chars and len(diary_entries) > MIN_DIARY_ENTRIES:
        removed_entry = diary_entries.pop()  # Remove oldest
        try:
            from plugins.ai_diary import format_diary_for_injection
            valid_entries = [e for e in diary_entries if isinstance(e, dict)]
            if valid_entries:
                new_diary_content = format_diary_for_injection(valid_entries)
                context["diary"] = new_diary_content
            else:
                if "diary" in context:
                    del context["diary"]
        except Exception as e:
            log_warning(f"[reduce_prompt] Error reformatting diary: {e}")
            if "diary" in context:
                del context["diary"]
        current_size = len(json_dumps(reduced_prompt))
        log_debug(f"[reduce_prompt] Removed diary entry, {len(diary_entries)} remaining, now {current_size} chars")
    
    # === STEP 2: Remove oldest chat messages if needed ===
    while current_size > max_chars and len(chat_history) > MIN_CHAT_MESSAGES:
        chat_history.pop()  # Remove oldest
        current_size = len(json_dumps(reduced_prompt))
        log_debug(f"[reduce_prompt] Removed chat message, {len(chat_history)} remaining, now {current_size} chars")
    
    # === STEP 3: Remove memories entirely if still needed ===
    if current_size > max_chars:
        memories = context.get("memories", [])
        if memories:
            log_warning(f"[reduce_prompt] Removing memories section ({len(memories)} entries, ~{len(json_dumps(memories))} chars)")
            del context["memories"]
            current_size = len(json_dumps(reduced_prompt))
            log_debug(f"[reduce_prompt] After removing memories: {current_size} chars")
    
    # === STEP 4: Remove other context sections (but KEEP persona which is CRITICAL!) ===
    if current_size > max_chars:
        # Save persona before any aggressive removal
        persona_backup = context.get("persona")
        log_debug(f"[reduce_prompt] 🛡️ PROTECTING persona: {bool(persona_backup)}")
        
        # Remove optional context fields, but NEVER remove persona
        removable_keys = [k for k in list(context.keys()) if k not in ["persona", "chat_history", "diary_entries", "diary"]]
        for key in removable_keys:
            if current_size <= max_chars:
                break
            if key in context:
                log_warning(f"[reduce_prompt] Removing context field: {key}")
                del context[key]
                current_size = len(json_dumps(reduced_prompt))
                log_debug(f"[reduce_prompt] After removing {key}: {current_size} chars")
    
    # === STEP 5: Emergency - remove entire context but RESTORE persona if still needed ===
    if current_size > max_chars and "context" in reduced_prompt:
        log_error(f"[reduce_prompt] 🚨 Emergency: removing entire context but KEEPING persona (CRITICAL)")
        # Save persona before removing context
        saved_persona = reduced_prompt["context"].get("persona")
        # Remove entire context
        del reduced_prompt["context"]
        # Recreate context with ONLY persona if it exists
        if saved_persona:
            reduced_prompt["context"] = {"persona": saved_persona}
            log_debug(f"[reduce_prompt] ✅ Context cleared but persona RESTORED (CRITICAL)")
        current_size = len(json_dumps(reduced_prompt))
        log_debug(f"[reduce_prompt] After emergency context removal: {current_size} chars")
    
    # === FINAL CHECK: Instructions AND Persona are ALWAYS kept ===
    # If we're still over, something is very wrong - log error but don't remove instructions or persona
    final_size = len(json_dumps(reduced_prompt))
    if final_size > max_chars:
        log_error(f"[reduce_prompt] CRITICAL: Could not reduce prompt below {max_chars} chars, final size: {final_size}")
        log_error(f"[reduce_prompt] Instructions AND Persona are PROTECTED and NOT removed. Check what's taking so much space!")
    else:
        log_debug(f"[reduce_prompt] ✅ Successfully reduced prompt to {final_size} chars (limit: {max_chars})")
    
    return reduced_prompt


def reduce_json_text_for_transmission(json_text: str, max_chars: int) -> str:
    """Reduce JSON text for transmission by removing oldest memories ONLY.
    
    This is an EMERGENCY reduction used when the JSON prompt is too large
    to send to the LLM. It conservatively removes only the oldest memories
    to bring the size down below max_chars.
    
    Strategy:
    1. Parse the JSON
    2. Remove oldest memories one by one from latest_diary_entries
    3. Reserialize and check size
    4. Repeat until size <= max_chars or no more memories to remove
    5. Principle: "meno tagli e meglio è" - minimize cuts
    
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
        
        # Strategy: Remove oldest diary entries (they are ordered DESC, so end = oldest)
        diary_entries = context.get("latest_diary_entries", [])
        if isinstance(diary_entries, list) and len(diary_entries) > 1:
            log_debug(f"[transmission_reduce] Found {len(diary_entries)} diary entries, will remove oldest first")
            
            # Remove entries from the END (oldest first, since ordered DESC by recency)
            entries_removed = 0
            while current_size > max_chars and len(diary_entries) > 1:
                removed_entry = diary_entries.pop()  # Remove oldest
                context["latest_diary_entries"] = diary_entries
                current_size = len(json_dumps(data))  # Use imported json_dumps
                entries_removed += 1
                log_debug(f"[transmission_reduce] Removed diary entry id={removed_entry.get('id')}, now {current_size} chars, {len(diary_entries)} entries remaining")
            
            if entries_removed > 0:
                log_info(f"[transmission_reduce] Removed {entries_removed} oldest diary entries")
        
        # If STILL too big, try reducing memories array
        if current_size > max_chars:
            memories = context.get("memories", [])
            if isinstance(memories, list) and len(memories) > 0:
                log_debug(f"[transmission_reduce] Diary reduction insufficient. Found {len(memories)} memories, attempting reduction...")
                
                memories_removed = 0
                while current_size > max_chars and len(memories) > 0:
                    removed_memory = memories.pop()  # Remove oldest
                    context["memories"] = memories
                    current_size = len(json_dumps(data))  # Use imported json_dumps
                    memories_removed += 1
                    log_debug(f"[transmission_reduce] Removed oldest memory, now {current_size} chars, {len(memories)} memories remaining")
                
                if memories_removed > 0:
                    log_info(f"[transmission_reduce] Also removed {memories_removed} oldest memories")
        
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




