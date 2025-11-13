# core/prompt_engine.py

from core.synth_tagging import extract_tags, expand_tags
import aiomysql
from core.db import get_conn
from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.json_utils import dumps as json_dumps
from core.config_manager import config_registry
import aiomysql
import os

# Default maximum prompt characters to use as a safe fallback when no LLM
# engine provides explicit limits. Keep this high to match modern models
# (e.g. gpt-4o / 128k token-like context). Can be tuned if needed.
DEFAULT_MAX_PROMPT_CHARS = 128000

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
    
    Instead of sending complete schemas with examples, field_types, and nested instructions,
    this creates a lean version with just essential information:
    - action name
    - brief description
    - required_fields
    - optional_fields
    - source
    - instructions (ONLY if action description contains "REQUIRED" or "MUST" - critical for LLM behavior)
    
    This reduces token usage dramatically while preserving all critical information.
    Full instructions are still available in each action definition if needed by plugins.
    
    Parameters
    ----------
    available_actions : dict
        Full actions block with complete schemas and instructions
        
    Returns
    -------
    dict
        Minified actions block suitable for LLM prompts
    """
    minified = {}
    for action_name, action_def in available_actions.items():
        # Keep only essential fields
        minified_action = {
            "description": action_def.get("description", ""),
            "required_fields": action_def.get("required_fields", []),
            "optional_fields": action_def.get("optional_fields", []),
            "source": action_def.get("source", ""),
        }
        
        # Include instructions if action description contains "REQUIRED" or "MUST" - these are critical for LLM behavior
        description = action_def.get("description", "").upper()
        if "REQUIRED" in description or "MUST" in description:
            instructions = action_def.get("instructions", {})
            if instructions:
                minified_action["instructions"] = instructions
        
        minified[action_name] = minified_action
    return minified


async def build_json_prompt(message, context_memory, interface_name: str | None = None, image_data: dict | None = None) -> dict:
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
        memories = await search_memories(tags=expanded_tags, limit=5)

    # === 3. Context base (chat_history has priority over diary) ===
    context_section = {
        "chat_history": chat_history,
        "memories": memories,
    }

    # === 3a. Static injections from plugins ===
    try:
        from core.action_parser import gather_static_injections

        injections = await gather_static_injections(message, context_memory)
        if isinstance(injections, dict):
            context_section.update(injections)
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
        # Get max prompt chars from active LLM
        max_prompt_chars = DEFAULT_MAX_PROMPT_CHARS  # Default fallback
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
                max_prompt_chars = limits.get("max_prompt_chars", DEFAULT_MAX_PROMPT_CHARS)
        except Exception as e:
            log_debug(f"[json_prompt] Could not get interface limits for reduction: {e}")
            max_prompt_chars = DEFAULT_MAX_PROMPT_CHARS  # Safe fallback
        
        # Apply reduction if needed
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

    conn = await get_conn()
    try:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()
            return [row[0] for row in rows]
    except Exception as e:
        log_error(f"Query failed: {repr(e)}")
        return []
    finally:
        conn.close()

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
        return """
- MASTER INSTRUCTION: Use ONLY actions from the 'actions' block. Never fabricate.
- If an action you need is not in 'actions', respond with a JSON explaining why.
- RESPOND ONLY WITH VALID JSON. No text before or after.
- Use input.interface to know where the message came from and respond there.
- NEVER lie. If you don't know something, say "I don't know".
- Target responses to input.payload.source.chat_id
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

def reduce_prompt_for_llm_limit(prompt: dict, max_chars: int) -> dict:
    """Reduce the prompt if it exceeds the LLM character limit by removing low-priority sections.
    
    New strategy: Alternate between removing oldest diary entries and oldest chat messages,
    maintaining minimum of 3 diary entries and 3 chat messages.
    
    Priority order (alternating):
    - Remove oldest diary entry (if >3 entries available)
    - Remove oldest chat message (if >3 messages available)
    - Repeat alternation until size is acceptable
    
    Args:
        prompt: The JSON prompt dictionary
        max_chars: Maximum allowed characters
        
    Returns:
        Reduced prompt that fits within limits
    """
    import copy
    from core.json_utils import dumps as json_dumps
    
    # Make a copy to avoid modifying the original
    reduced_prompt = copy.deepcopy(prompt)
    
    # Check current size
    current_size = len(json_dumps(reduced_prompt))
    if current_size <= max_chars:
        log_debug(f"[reduce_prompt] Prompt size {current_size} <= {max_chars}, no reduction needed")
        return reduced_prompt
    
    log_warning(f"[reduce_prompt] Prompt size {current_size} exceeds limit {max_chars}, reducing...")
    
    # Get references to sections
    context = reduced_prompt.get("context", {})
    diary_entries = context.get("diary_entries", [])
    chat_history = context.get("chat_history", [])
    
    # Minimum thresholds
    MIN_DIARY_ENTRIES = 3
    MIN_CHAT_MESSAGES = 3
    
    # Alternate removal: start with diary, then chat, then diary, etc.
    remove_diary_next = True
    
    while current_size > max_chars:
        removed_something = False
        
        if remove_diary_next and len(diary_entries) > MIN_DIARY_ENTRIES:
            # Remove oldest diary entry (pop from end since ordered DESC)
            if diary_entries:
                removed_entry = diary_entries.pop()
                # Reformat diary with remaining entries
                try:
                    from plugins.ai_diary import format_diary_for_injection
                    # Ensure all remaining entries are dicts (defensive check)
                    valid_entries = [e for e in diary_entries if isinstance(e, dict)]
                    if valid_entries:
                        new_diary_content = format_diary_for_injection(valid_entries)
                        context["diary"] = new_diary_content
                    else:
                        # No valid entries, remove diary
                        if "diary" in context:
                            del context["diary"]
                except Exception as e:
                    # Fallback: remove diary if formatting fails
                    log_warning(f"[reduce_prompt] Error reformatting diary after removal: {e}")
                    if "diary" in context:
                        del context["diary"]
                removed_something = True
                log_debug(f"[reduce_prompt] Removed oldest diary entry, {len(diary_entries)} remaining")
        
        elif not remove_diary_next and len(chat_history) > MIN_CHAT_MESSAGES:
            # Remove oldest chat message (pop from end)
            if chat_history:
                removed = chat_history.pop()
                removed_something = True
                log_debug(f"[reduce_prompt] Removed oldest chat message, {len(chat_history)} remaining")
        
        # Toggle for next iteration
        remove_diary_next = not remove_diary_next
        
        # Recalculate size
        current_size = len(json_dumps(reduced_prompt))
        
        # If we couldn't remove anything this iteration, break to avoid infinite loop
        if not removed_something:
            log_warning(f"[reduce_prompt] Cannot remove more items (diary: {len(diary_entries)}, chat: {len(chat_history)}), stopping reduction")
            break
    
    # If still too big after alternating removal, fall back to removing entire sections
    if current_size > max_chars:
        log_warning(f"[reduce_prompt] Alternating removal insufficient, removing entire sections...")
        
        # Remove entire diary if present and we have more than minimum entries
        if "diary" in context and len(diary_entries) > MIN_DIARY_ENTRIES:
            del context["diary"]
            del context["diary_entries"]
            current_size = len(json_dumps(reduced_prompt))
            log_debug(f"[reduce_prompt] Removed entire diary, now {current_size} chars")
        
        # If still too big, remove memories
        if current_size > max_chars and "memories" in context:
            del context["memories"]
            current_size = len(json_dumps(reduced_prompt))
            log_debug(f"[reduce_prompt] Removed memories, now {current_size} chars")
        
        # Last resort: remove chat_history entirely (but only if we have more than minimum)
        if current_size > max_chars and "chat_history" in context and len(chat_history) > MIN_CHAT_MESSAGES:
            del context["chat_history"]
            current_size = len(json_dumps(reduced_prompt))
            log_debug(f"[reduce_prompt] Removed entire chat_history, now {current_size} chars")
    
    # Final check and logging
    final_size = len(json_dumps(reduced_prompt))
    if final_size > max_chars:
        log_error(f"[reduce_prompt] Could not reduce prompt below {max_chars} chars, final size: {final_size}")
        
        # Emergency: remove entire context
        if "context" in reduced_prompt:
            del reduced_prompt["context"]
            final_size = len(json_dumps(reduced_prompt))
            log_warning(f"[reduce_prompt] Emergency: Removed entire context, final size: {final_size}")
        
        # Last resort: simplify instructions
        if final_size > max_chars and "instructions" in reduced_prompt:
            original_instructions = reduced_prompt["instructions"]
            simplified_instructions = {
                "format": "Generate valid JSON with 'actions' array. Each action has 'type' and 'payload' fields.",
                "rules": ["Always respond with valid JSON", "Use available actions only", "Be concise"]
            }
            reduced_prompt["instructions"] = simplified_instructions
            final_size = len(json_dumps(reduced_prompt))
            log_warning(f"[reduce_prompt] Simplified instructions, final size: {final_size}")
            log_debug(f"[reduce_prompt] Original instructions size: {len(json_dumps(original_instructions))}, new size: {len(json_dumps(simplified_instructions))}")
        
        # If STILL exceeding, log critical error
        if final_size > max_chars:
            log_error(f"[reduce_prompt] CRITICAL: Prompt still {final_size} chars after all reductions! Max is {max_chars}")
            log_error(f"[reduce_prompt] Remaining sections: {list(reduced_prompt.keys())}")
            for key, value in reduced_prompt.items():
                section_size = len(json_dumps({key: value}))
                log_error(f"[reduce_prompt]   - {key}: {section_size} chars")
    
    return reduced_prompt



