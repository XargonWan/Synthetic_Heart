# core/message_chain.py
"""Central message chain manager.

This module implements the message loop described by the user:

User -> Interface
Interface -> Message chain

Message chain receives messages (from interfaces or from LLM), tries to extract JSON
and send it to the action parser. If actions are executed the loop ends. If JSON-like
but invalid the message chain will call the corrector middleware (which queries the
active LLM plugin) until corrected JSON is returned or retries are exhausted.

The corrector never sends messages directly to interfaces; it only queries the LLM
via the registered plugin. The message chain marks LLM-origin messages so the
parser will only operate on model outputs.

Return codes:
- ACTIONS_EXECUTED -> actions parsed and executed
- BLOCKED -> message blocked (exhausted retries or explicit ignore)
- LLM_FAILED -> LLM produced invalid output and all corrector attempts were exhausted

Note: the LLM must always reply with valid JSON actions. Plain text from the LLM
is treated as a correctable error; the corrector will request valid JSON format.
"""

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, cast

from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.config_manager import config_registry
from core.beat_utils import is_outbound_beat

# Result constants
ACTIONS_EXECUTED = "ACTIONS_EXECUTED"
BLOCKED = "BLOCKED"
LLM_FAILED = "LLM_FAILED"

# Register FAILED_MESSAGE_TEXT configuration
FAILED_MESSAGE_TEXT = config_registry.get_var(
    "FAILED_MESSAGE_TEXT",
    "😵",
    label="Failed Message Text",
    description="Fallback message when LLM fails to respond or correct response.",
    group="core",
    component="core",
)

# Register RESPONSE_TIMEOUT configuration
RESPONSE_TIMEOUT = config_registry.get_var(
    "RESPONSE_TIMEOUT",
    2100,
    label="Response Timeout",
    description=(
        "Maximum time in seconds to wait for LLM responses before sending a "
        "fallback message. Must stay above LLM_GENERATION_TIMEOUT_SEC so a slow "
        "generation is not cut off by this outer guard."
    ),
    value_type=int,
    group="core",
    component="core",
)


def get_failed_message_text() -> str:
    """Get the fallback message when LLM fails."""
    fallback = FAILED_MESSAGE_TEXT
    # Ensure we return a string (ConfigVar might be returned)
    get_value = getattr(fallback, "get_value", None)
    if callable(get_value):
        fallback = get_value()
    return str(fallback)


# Map of interface prefixes to their correct message action types
_INTERFACE_TO_MESSAGE_ACTION: Dict[str, str] = {
    "telegram_bot": "message_telegram_bot",
    "discord_bot": "message_discord_bot",
    "synth_webui": "message_synth_webui",
    "matrix_chat": "message_matrix_chat",
    "ollama_serve": "message_ollama_serve",
    # radio_host: speaking on-air is the equivalent of "sending a message"
    "radio_host": "radio_speak",
}

_ACTION_TYPE_ALIASES: Dict[str, str] = {
    "diary": "create_personal_diary_entry",
    "diary_entry": "create_personal_diary_entry",
}

# Keys that are part of the action envelope and must NOT be swept into payload
# when gathering flat fields.
_ACTION_SYSTEM_KEYS = frozenset(
    {
        "type",
        "payload",
        "meta",
        # Alternative type/payload key names (already handled above, just exclude)
        "function",
        "name",
        "plugin",
        "action",
        "command",
        "method",
        "arguments",
        "parameters",
        "args",
        "schema",
        "input",
    }
)


def _resolve_message_action_for_path(interface_path: Optional[str]) -> Optional[str]:
    """Resolve the outbound message action type for an interface path.

    Standard interfaces map through ``_INTERFACE_TO_MESSAGE_ACTION``. A Vessel
    embodiment path (``vessel/<world>``) has no chat ``message_*`` action — its
    outbound reply is a spoken action ``vessel_<world>_say``. The world is taken
    structurally from the second path segment (no keyword matching). Returns
    ``None`` when the path carries no resolvable outbound action.
    """
    if not interface_path:
        return None
    parts = str(interface_path).split("/")
    prefix = parts[0] if parts else str(interface_path)
    if prefix == "vessel":
        if len(parts) >= 2 and parts[1].strip():
            return f"vessel_{parts[1].strip()}_say"
        return None
    return _INTERFACE_TO_MESSAGE_ACTION.get(prefix)


def _collect_message_texts(actions: list) -> list:
    """Collect the ordered, non-empty text bodies of every ``message_*`` action.

    A single LLM turn may emit several message actions (e.g. a multi-part
    reply).  When those messages are folded into one TTS voice note, every
    text must be preserved — historically only the first one survived the
    merge, silently dropping the rest.

    Args:
        actions: List of action dicts to scan.

    Returns:
        List of stripped message texts, in emission order.
    """
    if not actions:
        return []
    texts: list = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = action.get("type") or action.get("action")
        if not (isinstance(action_type, str) and action_type.startswith("message_")):
            continue
        payload = action.get("payload")
        if not isinstance(payload, dict):
            continue
        text = payload.get("text") or payload.get("content") or payload.get("message")
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    return texts


def _join_message_texts(texts: list) -> str:
    """Join several message bodies into a single voice-note text.

    Paragraph breaks (``\\n\\n``) give TTS engines a natural pause between the
    messages that were originally separate bubbles, and the same joined text
    becomes the audio caption.
    """
    return "\n\n".join(t for t in texts if isinstance(t, str) and t.strip())


def _normalize_message_payload_text(actions: list) -> list:
    """Promote legacy message payload text aliases to payload.text.

    LLMs occasionally emit message-like actions with keys such as ``body`` or
    ``content`` instead of the canonical ``text`` field required by validation.
    Normalize those aliases in-place before validation/correction so valid reply
    intents do not trigger an unnecessary corrective round-trip.

    Args:
        actions: List of action dicts to normalize.

    Returns:
        The same list with legacy text aliases copied into ``payload.text``.
    """
    if not actions:
        return actions

    for action in actions:
        if not isinstance(action, dict):
            continue

        action_type = action.get("type") or action.get("action")
        if not isinstance(action_type, str):
            continue
        if action_type not in (
            "message",
            "send_message",
        ) and not action_type.startswith("message_"):
            continue

        payload = action.get("payload")
        if not isinstance(payload, dict):
            continue

        existing_text = payload.get("text")
        if isinstance(existing_text, str) and existing_text.strip():
            continue

        for legacy_key in ("body", "content", "message", "value"):
            legacy_value = payload.get(legacy_key)
            if isinstance(legacy_value, str) and legacy_value.strip():
                payload["text"] = legacy_value
                log_debug(
                    f"[message_chain] Normalized payload.{legacy_key} -> payload.text for {action_type}"
                )
                break

    return actions


def _normalize_action_type_aliases(actions: list) -> list:
    """Rewrite legacy action aliases to their canonical registered names."""

    if not actions:
        return actions

    for action in actions:
        if not isinstance(action, dict):
            continue

        action_type = action.get("type") or action.get("action")
        if not isinstance(action_type, str):
            continue

        if action_type.startswith("default_api:"):
            action_type = action_type.split("default_api:", 1)[1]
            action["type"] = action_type
            if "action" in action:
                action["action"] = action_type

        canonical_type = _ACTION_TYPE_ALIASES.get(action_type)
        if not canonical_type or canonical_type == action_type:
            continue

        action["type"] = canonical_type
        log_debug(
            f"[message_chain] Normalized action type alias: {action_type} -> {canonical_type}"
        )

    return actions


def _normalize_diary_payload_fields(actions: list) -> list:
    """Promote legacy diary payload keys to canonical create_personal_diary_entry fields."""

    if not actions:
        return actions

    legacy_field_map = {
        "entry": "interaction_summary",
        "summary": "interaction_summary",
        "thought": "personal_thought",
    }

    diary_action: dict[str, Any] | None = None
    for action in actions:
        if not isinstance(action, dict):
            continue

        action_type = action.get("type") or action.get("action")
        if action_type != "create_personal_diary_entry":
            continue

        payload = action.get("payload")
        if isinstance(payload, dict):
            diary_action = action
            break

    normalized_actions = []

    for action in actions:
        if not isinstance(action, dict):
            normalized_actions.append(action)
            continue

        action_type = action.get("type") or action.get("action")
        if action_type == "thought":
            payload = action.get("payload")
            diary_payload = diary_action.get("payload") if diary_action else None
            if isinstance(payload, dict) and isinstance(diary_payload, dict):
                thought_value = payload.get("personal_thought") or payload.get(
                    "thought"
                )
                existing_thought = diary_payload.get("personal_thought")
                if isinstance(thought_value, str) and thought_value.strip():
                    if isinstance(existing_thought, str) and existing_thought.strip():
                        if thought_value.strip() not in existing_thought:
                            diary_payload["personal_thought"] = (
                                f"{existing_thought}\n\n{thought_value}"
                            )
                    else:
                        diary_payload["personal_thought"] = thought_value
                if not diary_payload.get("timestamp") and payload.get("timestamp"):
                    diary_payload["timestamp"] = payload["timestamp"]
                log_debug(
                    "[message_chain] Folded legacy thought action into create_personal_diary_entry"
                )
                continue

            normalized_actions.append(action)
            continue

        if action_type != "create_personal_diary_entry":
            normalized_actions.append(action)
            continue

        payload = action.get("payload")
        if not isinstance(payload, dict):
            normalized_actions.append(action)
            continue

        for legacy_field, canonical_field in legacy_field_map.items():
            canonical_value = payload.get(canonical_field)
            if isinstance(canonical_value, str) and canonical_value.strip():
                continue

            legacy_value = payload.get(legacy_field)
            if not isinstance(legacy_value, str) or not legacy_value.strip():
                continue

            payload[canonical_field] = legacy_value
            log_debug(
                "[message_chain] Normalized diary payload field: "
                f"{legacy_field} -> {canonical_field}"
            )

        normalized_actions.append(action)

    return normalized_actions


def _auto_inject_interface_path(actions: list, interface_path: Optional[str]) -> list:
    """Auto-inject missing interface_path into message actions.

    LLMs sometimes forget to include interface_path in message actions, causing
    validation failures. This function detects message actions missing interface_path
    and injects it from the context before validation runs.

    Args:
        actions: List of action dicts to process
        interface_path: The interface path from context (e.g., 'telegram_bot/5551234567')

    Returns:
        The same list with interface_path injected in-place where missing
    """
    if not actions or not interface_path:
        return actions

    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = (
            action.get("type")
            or action.get("action")
            or action.get("command")
            or action.get("method")
        )
        # Only process registered interface message action types (no hard-coded checks)
        if not action_type:
            continue
        try:
            # Import helper from action_parser to determine if this action_type
            # is a user-facing message action according to registered interfaces
            from core.action_parser import _is_interface_message_action

            if not _is_interface_message_action(action_type):
                continue
        except Exception:
            # Fallback: if helper unavailable, skip injection to avoid false positives
            continue

        payload = action.get("payload")
        if not isinstance(payload, dict):
            # Create payload if missing
            payload = {}
            action["payload"] = payload

        # Check if interface_path is missing
        if not payload.get("interface_path") and not payload.get("chat_name"):
            log_info(
                f"[message_chain] 🔧 Auto-injecting interface_path='{interface_path}' into {action_type} action (was missing)"
            )
            payload["interface_path"] = interface_path

    return actions


def _collect_beat_allowed_paths(ctx: Optional[dict]) -> Optional[set[str]]:
    """Collect the set of interface_paths a Grillo beat is allowed to route to.

    Grillo observer beats present the model with snippets from multiple
    conversations plus a list of eligible outreach targets. The model must reply
    to the conversation a snippet came from (or reach out to an eligible target).
    Because the beat runs with a placeholder context path (``grillo/-1``), a
    mis-chosen interface_path would otherwise be delivered verbatim to whatever
    chat the model named, silently landing the reply in the wrong conversation.

    This builds the union of every routable interface_path the beat actually
    offered the model: the ``chat:`` path embedded in each snippet plus every
    ``interface_path`` from the eligible-targets list. Returns ``None`` when the
    context is not a Grillo beat or carries no routable material (in which case
    no target validation should be applied).

    Args:
        ctx: The runtime context dict for the current turn.

    Returns:
        A set of allowed interface_path strings, or ``None`` when validation
        does not apply.
    """
    if not isinstance(ctx, dict) or not ctx.get("grillo_beat"):
        return None

    # Web-search delivery turns (search_orchestrator._deliver) are a second turn
    # whose ONLY job is to report the completed search results. They are
    # hard-scoped to their originating interface_path: the report belongs in the
    # chat where the search was prompted, never redirected elsewhere. Without
    # this, the model can deliver the results to an unrelated conversation —
    # observed: a Grillo self-initiated search (origin ``grillo/-1``, no human
    # requester) spammed Discord channels with unsolicited store/link messages
    # pulled from the observer snippets. Scoping to the origin means a real
    # human chat delivers only there, while an internal origin (``grillo/-1``)
    # allows no outward message_* routing at all (there is no grillo message
    # interface), so the delivery degrades to a no-op instead of spamming.
    if ctx.get("beat_type") == "web_search_result":
        origin = ctx.get("interface_path")
        if isinstance(origin, str) and origin.strip():
            return {origin.strip()}
        return None

    allowed: set[str] = set()

    snippets = ctx.get("grillo_snippets")
    if isinstance(snippets, (list, tuple)):
        for snippet in snippets:
            if not isinstance(snippet, str):
                continue
            # Snippets are rendered as "(chat:<path> | sender:... | ts) <text>".
            marker = "chat:"
            start = snippet.find(marker)
            if start == -1:
                continue
            start += len(marker)
            end = start
            # The path ends at the first field separator or closing paren.
            while end < len(snippet) and snippet[end] not in (" ", "|", ")"):
                end += 1
            path = snippet[start:end].strip()
            if path:
                allowed.add(path)

    targets = ctx.get("grillo_targets")
    if isinstance(targets, (list, tuple)):
        for target in targets:
            if isinstance(target, dict):
                path = target.get("interface_path")
                if isinstance(path, str) and path.strip():
                    allowed.add(path.strip())

    return allowed or None


def _drop_misrouted_beat_actions(actions: list, ctx: Optional[dict]) -> list:
    """Drop beat actions routed to a conversation the beat never offered.

    For Grillo beats (observer/outreach), any action that carries a concrete
    ``interface_path`` in its payload must target one of the interface_paths the
    beat actually presented to the model — the origin of a replied snippet or an
    eligible outreach target. An action routed to a path outside that set is a
    routing hallucination and is dropped: not sending is strictly safer than
    delivering the reply to the wrong conversation. Actions with no concrete
    ``interface_path`` are left untouched for downstream handling.

    Args:
        actions: List of action dicts to filter.
        ctx: The runtime context dict for the current turn.

    Returns:
        The filtered list of actions.
    """
    if not isinstance(actions, list):
        return actions

    allowed_paths = _collect_beat_allowed_paths(ctx)
    if not allowed_paths:
        return actions

    kept: list = []
    for action in actions:
        if not isinstance(action, dict):
            kept.append(action)
            continue

        payload = action.get("payload")
        target_path = (
            payload.get("interface_path") if isinstance(payload, dict) else None
        )
        # Only actions that carry a concrete routing target can be misrouted.
        # Anything without an interface_path (diary entries, non-message actions,
        # or messages left for downstream injection) is left untouched.
        if not isinstance(target_path, str) or not target_path.strip():
            kept.append(action)
            continue

        if target_path.strip() in allowed_paths:
            kept.append(action)
        else:
            action_type = (
                action.get("type")
                or action.get("action")
                or action.get("command")
                or action.get("method")
            )
            log_warning(
                f"[message_chain] 🚫 Dropping beat action '{action_type}' routed to "
                f"'{target_path}' — not among the beat's offered targets "
                f"{sorted(allowed_paths)}"
            )

    return kept


def _drop_leaked_recon_actions(actions: list) -> list:
    """Drop actions whose type is a Recon-schema key leaked into the main pass.

    The Recon pass ("prompt 0") runs its own LLM call on the *same* engine
    immediately before the main pass. State-retaining external engines (e.g. the
    browser-driven selenium endpoint, which cannot be reset from our side) can
    carry that priming forward, so the main pass sometimes echoes Recon-schema
    keys (``tone_hint``, ``agent_intent``, ``memory_search`` …) back inside its
    ``actions`` array. Those keys are *never* real actions — validation would
    reject every one of them, starving the turn of any deliverable action and
    driving the corrector into an exhausting loop that ends in the ``😵``
    fallback.

    This filter removes any action whose ``type`` matches a currently-registered
    Recon key *before* action-type validation runs, so a real deliverable action
    (e.g. ``vessel_minecraft_say``) present in the same array can still succeed.
    The key set is collected reflectively from the plugin registry — there is no
    hardcoded keyword list, so it stays correct in every language and as plugins
    change. Fully guarded: any failure leaves ``actions`` untouched.

    Args:
        actions: List of action dicts to filter.

    Returns:
        The filtered list of actions (leaked Recon-schema actions removed).
    """
    if not isinstance(actions, list) or not actions:
        return actions

    try:
        from core.recon import get_registered_recon_keys

        recon_keys = get_registered_recon_keys()
    except Exception as e:
        log_debug(f"[message_chain] Could not load Recon keys for drop-filter: {e}")
        return actions

    if not recon_keys:
        return actions

    kept: list = []
    dropped: list = []
    for action in actions:
        if not isinstance(action, dict):
            kept.append(action)
            continue
        atype = action.get("type") or action.get("action")
        if isinstance(atype, str) and atype.strip() in recon_keys:
            dropped.append(atype.strip())
        else:
            kept.append(action)

    if dropped:
        log_warning(
            f"[message_chain] Dropped {len(dropped)} leaked Recon-schema "
            f"action(s) before validation (engine state contamination): "
            f"{dropped}; {len(kept)} action(s) remain"
        )

    return kept


def _drop_out_of_scope_leaked_actions(actions: list, ctx: Optional[dict]) -> list:
    """Drop actions the current turn never offered (engine state contamination).

    The per-turn Fast-Lane prompt hides out-of-scope actions via the Hybrid-C
    scope gate (see ``core.prompt_engine._derive_default_prompt_action_types``):
    on a plain chat turn (e.g. ``ollama_serve``) the ``vessel_*`` / ``agent_*``
    actions are removed from the prompt, so a well-behaved model can only choose
    from the in-scope allowlist. A *state-retaining* external engine (e.g. the
    browser-driven selenium endpoint, which cannot be reset from our side) keeps
    the conversation history across turns, so on a core turn it sometimes echoes
    an action it was offered on an earlier *Vessel* turn — e.g.
    ``vessel_minecraft_collect_block`` — even though the current prompt never
    contained it. That leaked action is not deliverable on this interface: the
    corrector then demands a chat reply, the model re-emits the same off-scope
    action, and the turn loops into the ``😵`` fallback.

    This filter drops any action whose ``type`` is NOT in the scoped allowlist
    (``ctx['allowed_action_types']``) that ``plugin_instance`` recorded from the
    exact prompt generated for this turn. It is fully structural — the allowlist
    is the set of action names actually offered, there is no keyword list and no
    message-text inspection — so it stays correct in every language and as the
    action set changes. Guarded: if no scoped allowlist is available (the turn
    was not scope-gated, e.g. a beat with its own explicit scope) the list is
    returned untouched so this never narrows a legitimately-wide turn.

    Args:
        actions: List of action dicts to filter.
        ctx: The runtime context dict for the current turn.

    Returns:
        The filtered list of actions (out-of-scope leaked actions removed).
    """
    if not isinstance(actions, list) or not actions or not isinstance(ctx, dict):
        return actions

    scoped = ctx.get("allowed_action_types") or ctx.get("allowed_actions")
    if not isinstance(scoped, (list, set, tuple)) or not scoped:
        # No scoped allowlist recorded for this turn — do not narrow it.
        return actions

    allowlist = {str(a).strip() for a in scoped if a}
    if not allowlist:
        return actions

    kept: list = []
    dropped: list = []
    for action in actions:
        if not isinstance(action, dict):
            kept.append(action)
            continue
        atype = action.get("type") or action.get("action")
        if isinstance(atype, str) and atype.strip() and atype.strip() not in allowlist:
            dropped.append(atype.strip())
        else:
            kept.append(action)

    if dropped:
        log_warning(
            f"[message_chain] Dropped {len(dropped)} out-of-scope action(s) not "
            f"offered this turn (state-retaining engine leak): {dropped}; "
            f"{len(kept)} action(s) remain"
        )

    return kept


def _normalize_message_unknown(actions: list, interface_path: Optional[str]) -> list:
    """Normalize 'message_unknown' action types to the correct interface-specific type.

    Some LLMs fabricate action types like 'message_unknown' instead of using the
    correct interface-specific action (e.g., 'message_telegram_bot'). This function
    detects and corrects such actions based on the interface_path.

    Args:
        actions: List of action dicts to normalize
        interface_path: The interface path (e.g., 'telegram_bot/5551234567')

    Returns:
        The same list with any 'message_unknown' types corrected in-place
    """
    if not actions or not interface_path:
        return actions

    # Extract interface prefix from path (e.g., 'telegram_bot' from 'telegram_bot/5551234567')
    interface_prefix = (
        interface_path.split("/")[0] if "/" in interface_path else interface_path
    )
    correct_action_type = _INTERFACE_TO_MESSAGE_ACTION.get(interface_prefix)

    if not correct_action_type:
        return actions

    # Only normalize if the resolved interface-specific action type is actually supported
    try:
        # Import dynamically to avoid circular import at module load
        from core.action_parser import get_supported_action_types

        supported = get_supported_action_types()
    except Exception:
        supported = set()

    # Only proceed with normalization if the target action is registered in the system
    if correct_action_type not in supported:
        log_debug(
            f"[message_chain] Skipping normalization to '{correct_action_type}' because it is not in supported action types"
        )
        return actions

    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = action.get("type") or action.get("action")
        # Normalize 'message_unknown' and similar fabricated types
        if (
            action_type
            and isinstance(action_type, str)
            and action_type.startswith("message_")
            and action_type not in _INTERFACE_TO_MESSAGE_ACTION.values()
        ):
            old_type = action_type
            # Update whichever key was used
            if "type" in action:
                action["type"] = correct_action_type
            if "action" in action:
                action["action"] = correct_action_type
            log_info(
                f"[message_chain] 🔧 Normalized invalid action type '{old_type}' -> '{correct_action_type}' based on interface_path={interface_path}"
            )

    return actions


def _build_missing_reply_hint(
    interface_path: str,
    is_reactive_vessel_chat: bool,
    current_player_message: str | None = None,
    last_self_line: str | None = None,
) -> str:
    """Build the corrector hint shown when a user-facing turn produced no reply.

    For an embodied vessel turn the correct outward reply is a spoken action
    (``vessel_<world>_say``), not a chat ``message_*`` action, so the hint must
    name the right action family. The vessel world is the second segment of the
    interface path (e.g. ``vessel/minecraft`` -> ``vessel_minecraft_say``); if it
    is missing we fall back to the generic ``vessel_<world>_say`` placeholder.

    ``current_player_message`` and ``last_self_line`` steer the retry toward the
    player's *new* message and away from parroting Synth's own previous line
    (the weak-cortex self-echo loop). Both are passed through verbatim from the
    turn context — the hint never inspects their content for keywords, so it
    stays multi-language safe.
    """
    if is_reactive_vessel_chat:
        parts = (interface_path or "").split("/")
        world = parts[1] if len(parts) > 1 and parts[1] else "<world>"
        say_action = f"vessel_{world}_say"
        hint = (
            "CHAT REPLY REQUIRED: A player spoke to you in-world and is waiting for a reply. "
            f"You MUST include a speak action for this world (e.g., '{say_action}') so your words "
            "are voiced in the world. Internal actions like diary entries and emotion updates do "
            "NOT substitute for speaking back to the player."
        )
        if current_player_message and current_player_message.strip():
            hint += (
                " Answer THIS latest message from the player: "
                f'"{current_player_message.strip()}".'
            )
        if last_self_line and last_self_line.strip():
            hint += (
                " Do NOT repeat your own previous line "
                f'("{last_self_line.strip()}") — write a new, relevant reply.'
            )
        return hint
    return (
        "CHAT REPLY REQUIRED: The user is waiting for a reply in this active conversation turn. "
        f"You MUST include a message action targeting the originating interface '{(interface_path or '').split('/')[0]}' "
        "(e.g., 'message_telegram_bot') to reply to the user. Internal actions like diary entries "
        "and emotion updates do NOT substitute for replying."
    )


def _vessel_say_delivered(processed: Any) -> bool:
    """Return True if any successfully-processed action was an in-world speak verb.

    Detection is purely structural (``vessel_<world>_say`` prefix/suffix), never
    based on the message text, so it stays world-agnostic and keyword-free.
    """
    if not isinstance(processed, list):
        return False
    for item in processed:
        name: str | None = None
        if isinstance(item, dict):
            name = item.get("action") or item.get("type")
        elif isinstance(item, str):
            name = item
        if (
            isinstance(name, str)
            and name.startswith("vessel_")
            and name.endswith("_say")
        ):
            return True
    return False


def _contains_vessel_disconnect_action(actions: Any) -> bool:
    """Return True when an action collection contains ``vessel_disconnect``.

    ``vessel_disconnect`` is a terminal lifecycle action for an in-world chat
    turn.  Once it succeeds, the world-specific ``vessel_<world>_say`` action
    is intentionally removed from the live action set, so the generic
    user-facing missing-reply corrector must not ask the model to emit one.
    Detection is based on the registered action name, never on message text.
    """
    if not isinstance(actions, list):
        return False
    for item in actions:
        name: str | None = None
        if isinstance(item, dict):
            name = item.get("action") or item.get("type")
        elif isinstance(item, str):
            name = item
        if name == "vessel_disconnect":
            return True
    return False


def _last_self_vessel_utterance(ctx: dict[str, Any], interface_path: str) -> str | None:
    """Return the text of Synth's most recent own reply in this vessel chat.

    Reads the current-chat history the prompt was built from (the per-interface
    deque stored on ``ctx`` under ``interface_path``) and returns the text of the
    latest entry authored by Synth itself (``user_id``/``sender_id`` == ``self``).
    Used to detect and suppress a verbatim self-echo on a weak cortex, where the
    model parrots its own last spoken line instead of answering the player.

    Detection is purely structural (author identity + exact string equality),
    never based on the message text or any language-specific token, so it stays
    multi-language safe and keyword-free. Returns None when nothing is found.
    """
    if not interface_path:
        return None
    try:
        history = ctx.get(interface_path)
    except Exception:
        return None
    if not history:
        return None
    try:
        entries = list(history)
    except Exception:
        return None
    for entry in reversed(entries):
        if not isinstance(entry, dict):
            continue
        author = entry.get("user_id") or entry.get("sender_id")
        if isinstance(author, str) and author == "self":
            text = entry.get("text")
            if isinstance(text, str) and text.strip():
                return text
    return None


async def _deliver_vessel_fallback_reply(
    bot: Any,
    message: Any,
    ctx: dict[str, Any],
    reason: str,
) -> bool:
    """Speak a deterministic in-world reply when the LLM never produced a ``say``.

    The weak vessel cortex sometimes answers a reactive player chat with only an
    internal verb (e.g. ``vessel_<world>_observe``) and ignores the corrector's
    request for a speak action, leaving the player with no reply at all. As a
    last-resort safety net we synthesise a ``vessel_<world>_say`` ourselves and
    run it through the normal action dispatch so the connector voices it in-world.

    Purely structural: the world is the second segment of the vessel
    ``interface_path`` (``vessel/minecraft`` -> ``vessel_minecraft_say``). Returns
    True when a fallback speak action was dispatched.
    """
    interface_path = str(ctx.get("interface_path") or "")
    parts = interface_path.split("/")
    world = parts[1] if len(parts) > 1 and parts[1] else ""
    if not world:
        return False

    fallback_text = get_failed_message_text()
    say_action_type = f"vessel_{world}_say"
    say_action = {
        "type": say_action_type,
        "payload": {"text": fallback_text},
    }
    log_warning(
        f"[message_chain] 🌀 Vessel reactive turn produced no in-world reply "
        f"({reason}); dispatching deterministic '{say_action_type}' fallback so "
        "the player still hears back"
    )
    try:
        from core.action_parser import run_actions

        result = await run_actions([say_action], ctx, bot, message)
        return bool(result.get("processed"))
    except Exception as exc:  # pragma: no cover - defensive
        log_error(f"[message_chain] Failed to dispatch vessel fallback reply: {exc}")
        return False


async def send_llm_fallback_message(
    bot,
    message: SimpleNamespace,
    failure_reason: str,
    context: dict[str, Any] | None = None,
) -> str:
    """Send fallback message when LLM fails and log the failure reason.

    Args:
        bot: The bot instance
        message: The message object (may have interface_path attribute)
        failure_reason: Description of why the LLM failed
        context: Optional context dict that may contain interface_path
    """
    fallback_text = get_failed_message_text()
    # Ensure fallback_text is a string (ConfigVar might be returned)
    fallback_get_value = getattr(fallback_text, "get_value", None)
    if callable(fallback_get_value):
        fallback_text = fallback_get_value()
    fallback_text = str(fallback_text)
    chat_id = getattr(message, "chat_id", None)
    # Preserve thread_id when available so the fallback message is routed to the
    # same message thread and not defaulted to 0
    thread_id = getattr(message, "thread_id", None)
    if not thread_id and context:
        thread_id = context.get("thread_id")

    # Extract interface_path from message or context - CRITICAL for routing to correct interface
    interface_path = getattr(message, "interface_path", None)
    if not interface_path and context:
        interface_path = context.get("interface_path")

    # Debug: trace all available routing info
    log_debug(
        f"[message_chain] FALLBACK ROUTING DEBUG: message.interface_path={getattr(message, 'interface_path', None)}, "
        f"context.interface_path={context.get('interface_path') if context else None}, "
        f"resolved_interface_path={interface_path}, chat_id={chat_id}, thread_id={thread_id}"
    )

    # Log detailed error
    log_error(
        f"[message_chain] LLM FAILURE - Chat: {chat_id}, Interface: {interface_path}, Thread: {thread_id}, Reason: {failure_reason}"
    )
    log_error(f"[message_chain] Sending fallback message: '{fallback_text}'")

    async def _record_failure_event(
        stage: str,
        reason_text: str,
        *,
        failure_code: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> None:
        if stage == "llm_fallback" and getattr(message, "_llm_failure_logged", False):
            return

        try:
            from core.llm_failure_log import build_failure_entry, record_failure_entry

            correction_context = getattr(message, "correction_context", None)
            last_action_result = getattr(message, "last_action_result", None)
            metadata: dict[str, Any] = {}
            if isinstance(extra_metadata, dict):
                metadata.update(extra_metadata)
            if isinstance(last_action_result, dict) and last_action_result:
                metadata["last_action_result"] = last_action_result
            if context:
                for key in (
                    "source",
                    "interface_name",
                    "payload_thread_id",
                    "allowed_action_types",
                ):
                    if key in context:
                        metadata[key] = context.get(key)

            original_text = None
            if context:
                original_text = context.get("original_text")
            if not isinstance(original_text, str):
                original_text = getattr(message, "original_text", None)
            if not isinstance(original_text, str):
                original_text = None

            entry = build_failure_entry(
                reason=reason_text,
                stage=stage,
                interface_path=interface_path,
                chat_id=chat_id,
                thread_id=thread_id,
                engine=(context or {}).get("engine")
                or (context or {}).get("cortex_engine"),
                model=(context or {}).get("model")
                or (context or {}).get("cortex_model"),
                message_id=getattr(message, "event_id", None)
                or getattr(message, "message_id", None),
                content_preview=original_text[:500]
                if isinstance(original_text, str)
                else None,
                correction_context=correction_context
                if isinstance(correction_context, dict)
                else None,
                metadata=metadata,
                failure_code=failure_code,
            )
            await record_failure_entry(entry)
            if stage == "llm_fallback":
                message._llm_failure_logged = True
        except Exception as exc:
            log_warning(f"[message_chain] Failed to persist failure log entry: {exc}")

    await _record_failure_event("llm_fallback", failure_reason)

    # Autonomous Rift Vessel cognition turns (will-beats, sightings, reflection,
    # post-damage appraisal) are purely internal: no human is awaiting a reply.
    # When the weak vessel/base cortex exhausts the correction loop on such a
    # turn, speaking the "😵" fallback *in-world* pollutes the player's chat with
    # a failure emoji for a turn nobody addressed. The failure is still recorded
    # above for diagnostics; we simply must not voice it. A reactive player chat
    # (``vessel_player_chat``) is user-facing and keeps its audible fallback.
    # Structural detection via routing metadata only, never message text.
    try:
        from core.interface_path_utils import is_vessel_embodiment_context

        _is_reactive_vessel_chat = bool((context or {}).get("vessel_player_chat"))
        if is_vessel_embodiment_context(context or {}) and not _is_reactive_vessel_chat:
            log_warning(
                "[message_chain] Suppressing in-world '😵' fallback for autonomous "
                f"vessel turn (interface_path={interface_path}, reason={failure_reason}); "
                "failure recorded but not voiced to avoid polluting player chat"
            )
            return fallback_text
    except Exception as exc:  # pragma: no cover - defensive
        log_warning(
            f"[message_chain] Autonomous-vessel fallback suppression check failed: {exc}"
        )

    # Clear transient avatar face state so upstream outages do not leave the
    # persona stuck with stale failure-adjacent expressions on reconnect.
    try:
        from core.animation_handler import get_karada_state_server

        karada = get_karada_state_server()
        if karada is not None:
            await karada.push_face_expression(None, 0)
            await karada.clear_face_values()
    except Exception as face_exc:
        log_warning(
            f"[message_chain] Failed to clear face state on fallback: {face_exc}"
        )

    # Send fallback message through transport layer
    try:
        from core.transport_layer import universal_send

        # Get the send_message method from the bot (interface)
        if bot and hasattr(bot, "send_message"):
            try:
                # First attempt: prefer interface-friendly args (message_thread_id is commonly used)
                # skip_history=True: fallback messages must NOT be stored in chat history — they
                # would pollute the LLM context and make future responses progressively slower.
                await universal_send(
                    bot.send_message,
                    chat_id,
                    text=fallback_text,
                    interface_path=interface_path,
                    thread_id=thread_id,
                    is_llm_response=True,  # Mark as LLM response so interface handles normally
                    skip_history=True,  # Do NOT store fallback in chat history
                )
            except TypeError as te:
                # Some bots (or test fakes) don't accept 'message_thread_id'.
                # Retry without mapping thread id to message_thread_id.
                try:
                    log_warning(
                        f"[message_chain] send_message TypeError, retrying without message_thread_id: {te}"
                    )
                    await universal_send(
                        bot.send_message,
                        chat_id,
                        text=fallback_text,
                        interface_path=interface_path,
                        is_llm_response=True,
                        skip_history=True,  # Do NOT store fallback in chat history
                    )
                except Exception as e:
                    # If retry fails, surface the error but continue gracefully
                    log_error(
                        f"[message_chain] Failed to send fallback message after retry: {e} (original: {te})"
                    )
        else:
            await _record_failure_event(
                "delivery",
                "Fallback delivery skipped: bot does not have send_message method",
                failure_code="delivery_failed",
                extra_metadata={"fallback_text": fallback_text},
            )
            log_warning(
                "[message_chain] Bot does not have send_message method, cannot send fallback"
            )
        log_debug(
            f"[message_chain] Fallback message sent to chat {chat_id} via interface_path {interface_path} thread_id={thread_id}"
        )
        return fallback_text
    except Exception as e:
        log_error(f"[message_chain] Failed to send fallback message: {e}")
        return fallback_text


def _merge_correction_successes(
    previous: Any, current: Dict[str, Any]
) -> Dict[str, Any]:
    """Merge newly-executed actions into the accumulated correction context.

    ``message.correction_context`` is rebuilt on every correction pass. Naively
    replacing it with the *current* pass's successes loses the memory that a
    ``message_*`` action already delivered on an earlier pass — so the retry
    filter (``successful_types``) stops stripping it and the model's re-emitted
    reply gets delivered a second time (CHANGELOG 2026-06-26 duplicate
    Telegram messages). This merges the previous context's successful
    actions/types (deduplicated, order-stable) with the current pass's, so the
    "already delivered" knowledge accumulates across passes.
    """
    merged_actions: List[Dict[str, Any]] = []
    merged_types: List[str] = []
    seen_types: set = set()

    prev = previous if isinstance(previous, dict) else {}
    prev_actions = prev.get("successful_actions") or []
    prev_types = prev.get("successful_types") or []
    if isinstance(prev_actions, list):
        for a in prev_actions:
            if isinstance(a, dict):
                atype = a.get("type") or a.get("action")
                if atype not in seen_types:
                    seen_types.add(atype)
                    merged_actions.append(a)
                    merged_types.append(atype)
    if isinstance(prev_types, (list, tuple, set)):
        for atype in prev_types:
            if atype not in seen_types:
                seen_types.add(atype)
                merged_types.append(atype)

    for a in current.get("successful_actions") or []:
        if isinstance(a, dict):
            atype = a.get("type") or a.get("action")
            if atype not in seen_types:
                seen_types.add(atype)
                merged_actions.append(a)
                merged_types.append(atype)

    merged = dict(current)
    merged["successful_actions"] = merged_actions
    merged["successful_types"] = merged_types
    return merged


async def handle_incoming_message(
    bot,
    message: Optional[SimpleNamespace],
    text: str,
    *,
    source: str = "interface",
    context: Optional[Dict[str, Any]] = None,
    **kwargs,
):
    """Main entry point for the message chain.

    Parameters
    - bot: interface bot instance
    - message: SimpleNamespace-like message object (may be None)
    - text: incoming text to process
    - source: 'interface'|'user'|'llm' - origin of the text
    - context: optional context dict to pass to action parser
    - kwargs: additional metadata (e.g., thread_id)

    Returns one of the constants above.
    """
    # Local imports to avoid circular dependencies
    from core.transport_layer import extract_json_from_text, run_corrector_middleware
    from core.action_parser import run_actions, CORRECTOR_RETRIES
    from types import SimpleNamespace
    from datetime import datetime, timezone

    # Extract interface_path early for debug tracing
    _entry_interface_path = kwargs.get("interface_path") or (
        getattr(message, "interface_path", None) if message else None
    )
    _entry_chat_id = kwargs.get(
        "chat_id", getattr(message, "chat_id", "unknown") if message else "unknown"
    )
    _entry_thread_id = kwargs.get(
        "thread_id", getattr(message, "thread_id", None) if message else None
    )
    log_info(
        f"[message_chain] 🔄 ENTRY: source={source} text_len={len(text) if text else 0} chat_id={_entry_chat_id} interface_path={_entry_interface_path} thread_id={_entry_thread_id}"
    )
    log_debug(
        f"[message_chain] ENTRY CONTEXT: kwargs_keys={list(kwargs.keys())}, context_keys={list((context or {}).keys())}, message_type={type(message).__name__ if message else None}"
    )

    # Trace LLM→INTERFACE flow
    if source == "llm":
        log_info(
            "[message_chain] 📥 LLM→INTERFACE: Processing LLM response via message_chain (will apply llm_to_interface transport standards)"
        )

    if message is None:
        message = SimpleNamespace()
        message.chat_id = kwargs.get("chat_id")
        message.text = ""
        message.interface_path = kwargs.get("interface_path")
        message.date = datetime.now(timezone.utc)

    # Default context
    ctx = context or {}
    ctx["message"] = message
    ctx["original_text"] = (
        text  # Track original text in context, not on message (for consistency with immutable Telegram Message objects)
    )
    if not ctx.get("goal") and isinstance(text, str) and text.strip():
        ctx["goal"] = text.strip()
    if not ctx.get("original_user_message"):
        try:
            ctx["original_user_message"] = getattr(message, "text", "") or ""
        except Exception:
            ctx["original_user_message"] = ""

    # Mark cortex-origin in context (not on message object, as Telegram
    # Message objects are immutable). We normalise on "from_cortex" internally.
    is_cortex_origin = True if source == "llm" else ctx.get("from_cortex", False)
    # also honor explicit cortex/ai flags in context (legacy support)
    if not is_cortex_origin:
        is_cortex_origin = bool(ctx.get("from_cortex") or ctx.get("from_ai"))
    ctx["from_cortex"] = is_cortex_origin

    # Preserve raw LLM response text for Debrief/intent-recovery plugins
    if is_cortex_origin:
        try:
            ctx["llm_response_text"] = text or ""
        except Exception:
            ctx["llm_response_text"] = ""

        # Per-turn reason trail ("why did I say that"): the structural reason
        # summary built in ``build_prompt_request`` rides the context dict
        # (``plugin_instance`` stashes it under ``_reason_trail`` before the
        # engine call). Record it here once per LLM turn, now that the reply
        # text is known. Fail-open: any error must never affect the reply.
        try:
            _reason_trail = ctx.get("_reason_trail")
            if isinstance(_reason_trail, dict):
                from core.turn_reason import record_reason

                await record_reason(
                    interface_path=ctx.get("interface_path") or _entry_interface_path,
                    reply_preview=str(text or "")[:200],
                    memories=_reason_trail.get("memories"),
                    diary_sources=_reason_trail.get("diary_sources"),
                    emotion=_reason_trail.get("emotion"),
                    goal=_reason_trail.get("goal"),
                    beat_type=_reason_trail.get("beat_type"),
                    history_scope=_reason_trail.get("history_scope"),
                )
                # Never leave the internal key on a context dict that could be
                # reused by a later turn (a stale reason must not pair with a
                # newer reply).
                ctx.pop("_reason_trail", None)
        except Exception as _reason_exc:
            log_debug(f"[message_chain] Reason trail record skipped: {_reason_exc}")

    # Also set on message object if possible (for corrector_orchestrator and action_parser detection)
    try:
        if hasattr(message, "__dict__") or isinstance(message, type({})):
            # keep legacy attr for compatibility but core uses from_cortex
            message.from_cortex = is_cortex_origin
    except (AttributeError, TypeError):
        pass  # Message object is immutable (Telegram Message); use ctx instead

    # Preserve chat_id and interface_path in context to avoid losing them during processing
    # Check message attributes, kwargs, and original context (in that priority order)
    if hasattr(message, "chat_id") and message.chat_id:
        ctx["chat_id"] = message.chat_id
    elif kwargs.get("chat_id") and not ctx.get("chat_id"):
        ctx["chat_id"] = kwargs["chat_id"]

    # interface_path is CRITICAL for routing - check all sources
    if hasattr(message, "interface_path") and message.interface_path:
        ctx["interface_path"] = message.interface_path
    elif kwargs.get("interface_path") and not ctx.get("interface_path"):
        ctx["interface_path"] = kwargs["interface_path"]
    # If still not set, try to build it from interface + chat_id
    if not ctx.get("interface_path"):
        interface_name = ctx.get("interface") or kwargs.get("interface")
        chat_id = ctx.get("chat_id")
        if interface_name and chat_id:
            ctx["interface_path"] = f"{interface_name}/{chat_id}"
            log_debug(
                f"[message_chain] Built interface_path from interface+chat_id: {ctx['interface_path']}"
            )

    # Propagate is_voice_input from message attributes (set by WebUI when input
    # was transcribed from audio, so TTS auto-inject fires for voice-originated requests)
    if (
        hasattr(message, "is_voice_input")
        and message.is_voice_input
        and not ctx.get("is_voice_input")
    ):
        ctx["is_voice_input"] = True

    log_debug(
        f"[message_chain] Context preserved: interface_path={ctx.get('interface_path')}, chat_id={ctx.get('chat_id')}, interface={ctx.get('interface')}"
    )

    # Process LLM messages for emotional state updates
    if ctx.get("from_cortex", False) or source == "llm":
        log_info("[message_chain] 🎭 Starting emotion processing for LLM message...")
        try:
            from core.persona_manager import get_persona_manager

            persona_manager = get_persona_manager()
            if persona_manager:
                persona_manager.process_llm_message_for_emotions(text)
                log_info("[message_chain] ✅ Emotion processing completed successfully")

                # Check if there were invalid emotions - trigger corrector if so
                corrector_msg = persona_manager.get_emotion_validation_corrector()
                if corrector_msg:
                    log_warning(
                        "[message_chain] 🚨 Invalid emotions detected - triggering corrector"
                    )
                    # Trigger corrector with emotion validation message
                    from core.action_parser import run_action

                    try:
                        # Build corrector action
                        corrector_action = {
                            "type": "send_corrector_message",
                            "payload": {
                                "correction_type": "invalid_emotions",
                                "message": corrector_msg,
                                "interface_path": ctx.get("interface_path"),
                                "chat_id": ctx.get("chat_id"),
                            },
                        }

                        # Try to send corrector message
                        asyncio.create_task(
                            run_action(corrector_action, ctx, bot, message)
                        )
                        log_info(
                            "[message_chain] ✓ Corrector action scheduled for invalid emotions"
                        )
                    except Exception as ce:
                        log_warning(f"[message_chain] Could not send corrector: {ce}")
            else:
                log_warning("[message_chain] ⚠️ Persona manager not available")
        except Exception as e:
            log_error(f"[message_chain] ❌ Error processing LLM emotions: {e}")
            import traceback

            log_error(f"[message_chain] Traceback: {traceback.format_exc()}")

        # Remove internal emotion tags from the LLM text once they have been
        # processed so downstream actions and interfaces only see clean text.
        try:
            from plugins.emotion_manager import strip_emotion_tags

            cleaned_text = strip_emotion_tags(text)
            if cleaned_text != text:
                log_debug("[message_chain] Stripped emotion tags from LLM-origin text")
                text = cleaned_text
        except Exception:
            pass

    log_info("[message_chain] 📋 Starting action extraction loop...")
    # Retry/tried set to avoid loops
    tried_texts = set()
    attempt = 0
    max_retries = ctx.get("max_retries", int(CORRECTOR_RETRIES))

    # Check for action result delivery context - these responses should have minimal retries
    # to prevent cascading loops when processing action outputs (e.g., memory_search results)
    # (value read later if needed)
    try:
        system_message = ctx.get("system_message", {})
        if isinstance(system_message, dict):
            # note: we only read is_action_result when necessary later, ignore
            # for now to keep lint happy
            _ = system_message.get("is_action_result_delivery", False)
            custom_max = system_message.get("max_correction_attempts")
            if custom_max is not None and isinstance(custom_max, int):
                max_retries = min(max_retries, custom_max)
                log_info(
                    f"[message_chain] Action result delivery detected - limiting retries to {max_retries}"
                )
    except Exception:
        pass

    # Track whether any actions have successfully executed during the loop.
    # If at least one action ran, we consider the iteration partially successful and
    # avoid sending a generic fallback message later on. This matches the
    # requirement that "LLM failure" should only be emitted when the entire
    # iteration failed (or there was a technical error with no reply at all).
    actions_executed_during_loop = False

    # Track whether a ``vessel_<world>_say`` was actually voiced in-world at any
    # point during the loop (across correction retries). Used only for a reactive
    # in-world player chat: if the loop ends without one, we deterministically
    # speak a fallback so the player never gets total silence from a weak cortex.
    vessel_reply_delivered = False
    # A successful disconnect intentionally ends the in-world turn without a
    # spoken reply. Keep this across correction iterations because successful
    # actions are removed from retry payloads.
    vessel_disconnect_succeeded = False

    while True:
        log_info(
            f"[message_chain] 🔄 LOOP: attempt={attempt} source={source} chat={getattr(message, 'chat_id', None)} text_len={len(text) if text else 0}"
        )

        # Determine interface flags early
        user_facing_interfaces = [
            "discord_bot",
            "telegram_bot",
            "synth_webui",
            "matrix_chat",
            "ollama_serve",
        ]
        interface_path = ctx.get("interface_path") or ""
        chat_id = ctx.get("chat_id")

        # A reactive in-world player chat (structural ``vessel_player_chat`` flag,
        # set by the vessel interface from event kind + actor and propagated by the
        # queue) is a human speaking directly to Synth and therefore user-facing:
        # it must receive an outward reply just like an ordinary chat. Synth's own
        # autonomous vessel perceptions/will-beats leave the flag False, so they are
        # NOT treated as user-facing and never trigger the missing-reply corrector.
        is_reactive_vessel_chat = bool(ctx.get("vessel_player_chat"))

        is_user_facing = bool(
            interface_path
            and (
                any(
                    interface_path.startswith(f"{iface}/")
                    for iface in user_facing_interfaces
                )
                or is_reactive_vessel_chat
            )
        )

        is_internal_chat = chat_id == -1 or chat_id == "-1" or str(chat_id) == "-1"

        is_grillo_internal = ctx.get("grillo_beat", False) and not is_outbound_beat(
            ctx.get("beat_type")
        )

        # Prompt-scoped turns (e.g. vision_describe, delivery prompts) restrict the
        # LLM to specific action types; if no message_* type is allowed, the LLM
        # cannot reply to the user and we must not demand one.
        _allowed_types = ctx.get("allowed_action_types")
        is_scoped_non_message = (
            isinstance(_allowed_types, (list, tuple, set))
            and len(_allowed_types) > 0
            and not any(
                isinstance(t, str) and t.startswith("message_") for t in _allowed_types
            )
        )

        actions = None

        # Quick JSON extraction with metadata to detect corruption
        parsed = None
        metadata = {}
        try:
            log_info("[message_chain] Attempting to extract JSON from text...")
            parsed, metadata = extract_json_from_text(text, return_metadata=True)
            log_info(
                f"[message_chain] JSON extraction completed: parsed={parsed is not None} recovered={metadata.get('recovered')}"
            )
        except Exception as e:
            log_error(f"[message_chain] extract_json EXCEPTION: {e}")
            import traceback

            log_error(f"[message_chain] Traceback: {traceback.format_exc()}")

        # Check if JSON was recovered from corruption - may still have valid actions
        if parsed is not None and metadata.get("recovered"):
            log_warning(
                f"[message_chain] JSON recovered from corruption (errors: {metadata.get('error_count', 0)}, "
                f"unparsed: {len(metadata.get('unparsed_content', ''))} chars) - will execute valid actions and correct failures"
            )
            # Don't set parsed = None here - try to execute valid actions first

        if parsed is not None:
            # System messages are produced by the core/system and should NEVER be processed
            # This prevents loops caused by system messages being re-evaluated
            if isinstance(parsed, dict) and "system_message" in parsed:
                sm = parsed.get("system_message") or {}
                sm_type = sm.get("type") if isinstance(sm, dict) else None
                log_info(
                    f"[message_chain] Blocking system_message type={sm_type} (system-origin payload) - system messages must not enter the processing loop"
                )
                return BLOCKED

            # Build actions list

            # Pre-normalize: unwrap single-element arrays wrapping a
            # standard dict response — [{"actions": [...]}, ...],
            # [{"tool_calls": [...]}, ...], or [{"text": "...", ...}].
            # Collapse to the inner dict so the existing dict-branch
            # handles everything (meta, feelings, normalization, etc.).
            if (
                isinstance(parsed, list)
                and len(parsed) == 1
                and isinstance(parsed[0], dict)
                and (
                    "actions" in parsed[0]
                    or "tool_calls" in parsed[0]
                    or "text" in parsed[0]
                )
            ):
                _wrapper = parsed[0]
                _wkey = (
                    "actions"
                    if "actions" in _wrapper
                    else ("tool_calls" if "tool_calls" in _wrapper else "text")
                )
                log_info(
                    f'[message_chain] \U0001f504 Unwrapping [{{"{_wkey}": ...}}] '
                    "single-element list wrapper \u2192 dict"
                )
                parsed = _wrapper

            # Recognise 'tool_calls' as a synonym for 'actions'.  Gemini
            # sometimes outputs {"tool_calls": [{"name": ..., "arguments": ...}]}
            # instead of {"actions": [{"type": ..., "payload": ...}]}.
            if (
                isinstance(parsed, dict)
                and "tool_calls" in parsed
                and "actions" not in parsed
            ):
                tc = parsed["tool_calls"]
                if isinstance(tc, list):
                    log_info(
                        f"[message_chain] \U0001f504 Normalizing top-level 'tool_calls' "
                        f"({len(tc)} item(s)) \u2192 'actions'"
                    )
                    parsed["actions"] = tc
                    del parsed["tool_calls"]

            if isinstance(parsed, dict) and "actions" in parsed:
                actions = (
                    parsed["actions"] if isinstance(parsed["actions"], list) else None
                )
                if actions is None:
                    log_warning(
                        "[message_chain] actions field must be a list - triggering corrector"
                    )
                    # Don't return here - let corrector fix it
                    parsed = None  # Force correction path
                else:
                    # Recover a root-level type+payload action that the LLM placed
                    # outside the actions array, e.g.:
                    #   {"actions": [...], "type": "message_telegram_bot", "payload": {...}}
                    # gemma-uncensored and similar models occasionally emit this
                    # malformed structure where the last action leaks to root.
                    # Folding it in here avoids the corrector firing for every
                    # message that ends this way, which would produce duplicate
                    # user-visible messages via the deliver_to_llm chain.
                    _root_type = parsed.get("type")
                    _root_payload = parsed.get("payload")
                    if (
                        isinstance(_root_type, str)
                        and _root_type not in ("actions", "recovery_actions")
                        and isinstance(_root_payload, dict)
                    ):
                        actions.append({"type": _root_type, "payload": _root_payload})
                        log_info(
                            f"[message_chain] \U0001f504 Recovered root-level action "
                            f"'{_root_type}' outside actions array → appended"
                        )

                    # Normalize OpenAI tool calling format (name/parameters) to type/payload
                    for i, act in enumerate(actions):
                        if isinstance(act, dict):
                            act = cast(dict[str, Any], act)
                            # Normalize legacy single-key action objects inside the
                            # actions array, e.g. {"create_personal_diary_entry": {...}}
                            # to the canonical {"type": ..., "payload": ...} format.
                            if (
                                "type" not in act
                                and "action" not in act
                                and "name" not in act
                                and len(act) == 1
                            ):
                                action_name, action_payload = next(iter(act.items()))
                                if isinstance(action_name, str):
                                    normalized_payload = (
                                        action_payload
                                        if isinstance(action_payload, dict)
                                        else {"value": action_payload}
                                    )
                                    actions[i] = {
                                        "type": action_name,
                                        "payload": normalized_payload,
                                    }
                                    act = cast(dict[str, Any], actions[i])
                                    log_info(
                                        "[message_chain] 🔄 Normalized legacy single-key action object inside actions array: "
                                        f"{action_name}"
                                    )
                            # Normalize alternative keys for action type if 'type' is missing
                            if "type" not in act:
                                # Prioritize 'function' or 'name' (OpenAI/Gemini style) then 'plugin'
                                _t = (
                                    act.get("function")
                                    or act.get("name")
                                    or act.get("plugin")
                                    or act.get("action")
                                    or act.get("command")
                                    or act.get("method")
                                )
                                if _t:
                                    act["type"] = _t
                                    log_debug(
                                        f"[message_chain] Normalizing action type: mapped to '{_t}'"
                                    )

                            # Normalize alternative keys for payload if 'payload' is missing
                            if "payload" not in act:
                                # Prioritize 'arguments' (OpenAI) or 'parameters' (Gemini) then 'args'/'schema'/'input'
                                _p = (
                                    act.get("arguments")
                                    or act.get("parameters")
                                    or act.get("args")
                                    or act.get("schema")
                                    or act.get("input")
                                )
                                if _p:
                                    act["payload"] = _p
                                    log_debug(
                                        f"[message_chain] Normalizing action payload for {act.get('type')}"
                                    )

                            # Fallback: gather flat action fields into payload
                            if "payload" not in act and "type" in act:
                                _extra = {
                                    k: v
                                    for k, v in act.items()
                                    if k not in _ACTION_SYSTEM_KEYS
                                }
                                if _extra:
                                    for k in _extra:
                                        del act[k]
                                    act["payload"] = _extra
                                    log_debug(
                                        f"[message_chain] Gathered {len(_extra)} flat fields into payload for {act.get('type')}"
                                    )
                            elif "payload" in act and "type" in act:
                                # Merge any action-level keys that belong inside
                                # payload but were placed at the action level by
                                # the LLM (e.g. interface_path, chat_name,
                                # reply_to_message_id for message_* actions).
                                _orphaned = {
                                    k: v
                                    for k, v in act.items()
                                    if k not in _ACTION_SYSTEM_KEYS
                                }
                                if _orphaned:
                                    for k in _orphaned:
                                        del act[k]
                                    for k, v in _orphaned.items():
                                        if k not in act["payload"]:
                                            act["payload"][k] = v
                                    log_debug(
                                        f"[message_chain] Merged {len(_orphaned)} orphaned action-level"
                                        f" key(s) into payload for {act.get('type')}: {list(_orphaned)}"
                                    )
            elif isinstance(parsed, list):
                actions = parsed
                # Normalize OpenAI tool calling format for bare list responses
                for i, act in enumerate(actions):
                    if isinstance(act, dict):
                        act = cast(dict[str, Any], act)
                        # Normalize type
                        if "type" not in act:
                            _t = (
                                act.get("function")
                                or act.get("name")
                                or act.get("plugin")
                                or act.get("action")
                                or act.get("command")
                                or act.get("method")
                            )
                            if _t:
                                act["type"] = _t
                                log_debug(
                                    f"[message_chain] Normalizing bare list action type: mapped to '{_t}'"
                                )
                        # Normalize payload
                        if "payload" not in act:
                            _p = (
                                act.get("arguments")
                                or act.get("parameters")
                                or act.get("args")
                                or act.get("schema")
                                or act.get("input")
                            )
                            if _p:
                                act["payload"] = _p
                                log_debug(
                                    f"[message_chain] Normalizing bare list action payload for {act.get('type')}"
                                )

                        # Fallback: gather flat action fields into payload
                        if "payload" not in act and "type" in act:
                            _extra = {
                                k: v
                                for k, v in act.items()
                                if k not in _ACTION_SYSTEM_KEYS
                            }
                            if _extra:
                                for k in _extra:
                                    del act[k]
                                act["payload"] = _extra
                                log_debug(
                                    f"[message_chain] Gathered {len(_extra)} flat fields into payload for {act.get('type')}"
                                )
                        elif "payload" in act and "type" in act:
                            # Merge any action-level keys that belong inside
                            # payload but were placed at the action level by
                            # the LLM (e.g. interface_path, chat_name,
                            # reply_to_message_id for message_* actions).
                            _orphaned = {
                                k: v
                                for k, v in act.items()
                                if k not in _ACTION_SYSTEM_KEYS
                            }
                            if _orphaned:
                                for k in _orphaned:
                                    del act[k]
                                for k, v in _orphaned.items():
                                    if k not in act["payload"]:
                                        act["payload"][k] = v
                                log_debug(
                                    f"[message_chain] Merged {len(_orphaned)} orphaned action-level key(s) into"
                                    f" payload for {act.get('type')}: {list(_orphaned)}"
                                )
            elif isinstance(parsed, dict) and (
                "type" in parsed
                or "name" in parsed
                or "function" in parsed
                or "plugin" in parsed
                or "action" in parsed
                or "command" in parsed
                or "method" in parsed
            ):
                # Normalize singe-action dict (type/name/function/plugin/command)
                if "type" not in parsed:
                    parsed["type"] = (
                        parsed.get("function")
                        or parsed.get("name")
                        or parsed.get("plugin")
                        or parsed.get("action")
                        or parsed.get("command")
                        or parsed.get("method")
                    )
                if "payload" not in parsed:
                    parsed["payload"] = (
                        parsed.get("arguments")
                        or parsed.get("parameters")
                        or parsed.get("args")
                        or parsed.get("schema")
                        or parsed.get("input")
                    )
                # Fallback: gather flat action fields into payload
                if not parsed.get("payload") and "type" in parsed:
                    _extra = {
                        k: v for k, v in parsed.items() if k not in _ACTION_SYSTEM_KEYS
                    }
                    if _extra:
                        for k in _extra:
                            del parsed[k]
                        parsed["payload"] = _extra
                        log_debug(
                            f"[message_chain] Gathered {len(_extra)} flat fields into payload for {parsed.get('type')}"
                        )
                elif (
                    isinstance((_pl := parsed.get("payload")), dict)
                    and "type" in parsed
                ):
                    _orphaned = {
                        k: v for k, v in parsed.items() if k not in _ACTION_SYSTEM_KEYS
                    }
                    if _orphaned:
                        for k in _orphaned:
                            del parsed[k]
                        for k, v in _orphaned.items():
                            if k not in _pl:
                                _pl[k] = v
                        log_debug(
                            f"[message_chain] Merged {len(_orphaned)} orphaned action-level key(s) into"
                            f" payload for {parsed.get('type')}: {list(_orphaned)}"
                        )
                if "payload" not in parsed:
                    parsed["payload"] = {}
                actions = [parsed]
                log_debug(
                    f"[message_chain] Normalized single-action dict: {parsed.get('type')}"
                )
            elif isinstance(parsed, dict) and "recovery_actions" in parsed:
                # Debrief plugins sometimes return {"recovery_actions": [{"action_type": ..., "payload": ...}]}
                # Normalize this to the standard {"actions": [{"type": ..., "payload": ...}]} format.
                raw_recovery = parsed.get("recovery_actions")
                if isinstance(raw_recovery, list):
                    normalized_recovery = []
                    for item in raw_recovery:
                        if not isinstance(item, dict):
                            continue
                        # action_type → type (debrief uses action_type, core uses type)
                        atype = (
                            item.get("action_type")
                            or item.get("type")
                            or item.get("action")
                        )
                        if not atype:
                            continue
                        normalized_recovery.append(
                            {"type": atype, "payload": item.get("payload", {})}
                        )
                    if normalized_recovery:
                        log_info(
                            f"[message_chain] 🔄 Normalizing recovery_actions → actions "
                            f"({len(normalized_recovery)} item(s)): "
                            f"{[a['type'] for a in normalized_recovery]}"
                        )
                        actions = normalized_recovery
                    else:
                        log_warning(
                            "[message_chain] recovery_actions list was empty or malformed - triggering corrector"
                        )
                        parsed = None
                else:
                    log_warning(
                        "[message_chain] recovery_actions field is not a list - triggering corrector"
                    )
                    parsed = None
            elif (
                isinstance(parsed, dict)
                and "action" in parsed
                and isinstance(parsed["action"], str)
            ):
                # Normalize Gemini-style {"action": "...", "action_input"|"content": "..."} to standard format
                # This handles LLMs that output the older single-action format
                # Gemini sometimes uses "action_input", sometimes "content", sometimes "text"
                action_type = parsed.get("action")
                action_input = (
                    parsed.get("action_input")
                    or parsed.get("content")
                    or parsed.get("text")
                    or parsed.get("message")
                )
                log_info(
                    f"[message_chain] 🔄 Normalizing Gemini-style action format: {action_type}"
                )
                # Convert action_input to payload with 'text' key if it's a string
                if isinstance(action_input, str):
                    normalized_action = {
                        "type": action_type,
                        "payload": {"text": action_input},
                    }
                elif isinstance(action_input, dict):
                    normalized_action = {"type": action_type, "payload": action_input}
                else:
                    # Fallback: collect any remaining keys as payload (excluding 'action')
                    fallback_payload = {
                        k: v for k, v in parsed.items() if k != "action"
                    }
                    normalized_action = {
                        "type": action_type,
                        "payload": fallback_payload,
                    }
                    log_warning(
                        f"[message_chain] ⚠️ No recognized text field in Gemini action, using fallback payload: {list(fallback_payload.keys())}"
                    )
                actions = [normalized_action]
            elif (
                isinstance(parsed, dict)
                and "text" in parsed
                and "interface_path" in parsed
            ):
                # Bare message payload recovered from corrupted JSON — the
                # LLM produced the correct payload but the actions wrapper
                # was lost during JSON extraction.  Infer the action type
                # from the interface_path prefix.
                _ipath = str(parsed.get("interface_path", ""))
                _iface = _ipath.split("/")[0] if "/" in _ipath else _ipath
                _inferred_type = f"message_{_iface}" if _iface else None
                if _inferred_type:
                    log_info(
                        f"[message_chain] 🔄 Wrapping bare message payload "
                        f"as {_inferred_type} (recovered from corrupted JSON)"
                    )
                    actions = [{"type": _inferred_type, "payload": parsed}]
                else:
                    log_warning(
                        f"[message_chain] Bare payload has interface_path "
                        f"but no inferrable action type: {_ipath}"
                    )
                    parsed = None
            elif isinstance(parsed, dict) and "text" in parsed:
                # Text-only response without actions — e.g.
                # {"text": "...", "meta": {"autonomous": true}}
                # The LLM returned a plain message with optional metadata
                # but omitted the actions wrapper entirely.  Infer the
                # correct message action from the context interface_path.
                _ctx_ipath = ctx.get("interface_path", "") if ctx else ""
                _inferred_type = _resolve_message_action_for_path(_ctx_ipath)
                if _inferred_type:
                    _text_content = parsed.get("text", "")
                    # A vessel say action takes only `text`; a chat message
                    # action also carries interface_path for routing.
                    _payload: dict[str, Any] = {"text": _text_content}
                    if not _inferred_type.startswith("vessel_"):
                        _payload["interface_path"] = _ctx_ipath
                    _msg_action: dict[str, Any] = {
                        "type": _inferred_type,
                        "payload": _payload,
                    }
                    # Propagate top-level meta (e.g. autonomous flag) to the action
                    _top_meta = parsed.get("meta")
                    if isinstance(_top_meta, dict):
                        _msg_action["meta"] = _top_meta
                    actions = [_msg_action]
                    log_info(
                        f"[message_chain] 🔄 Wrapping text-only response "
                        f"as {_inferred_type} (no actions array, inferred "
                        f"from context interface_path={_ctx_ipath})"
                    )
                else:
                    log_warning(
                        f"[message_chain] Text-only response but cannot "
                        f"infer message action from interface_path: "
                        f"{_ctx_ipath!r}"
                    )
                    parsed = None
            elif (
                isinstance(parsed, dict)
                and not any(
                    k in ["actions", "recovery_actions", "type", "text"]
                    for k in parsed.keys()
                )
                and not isinstance(parsed.get("action"), str)
            ):
                # The LLM may have returned a dictionary mapping action types to payloads
                # e.g., {"message_telegram_bot": {...}, "create_personal_diary_entry": {...}}
                # Or it may have returned {"action": {"command": "...", ...}}
                log_info("[message_chain] 🔄 Normalizing action-key dictionary format")
                actions = []
                for k, v in parsed.items():
                    if isinstance(v, dict):
                        # Handle {"action": {"command": "update_emotion_from_tags", ...}}
                        if k == "action" and ("command" in v or "type" in v):
                            atype = v.get("command") or v.get("type")
                            actions.append({"type": str(atype), "payload": v})
                        else:
                            actions.append({"type": k, "payload": v})
                    else:
                        actions.append({"type": k, "payload": {}})
            else:
                log_warning(
                    f"[message_chain] Unrecognized JSON structure: {parsed} - triggering corrector"
                )
                # Don't return here - let corrector fix it
                parsed = None  # Force correction path

            # Normalize any 'message_unknown' or other fabricated message types
            # to the correct interface-specific action type before execution
            if actions:
                # --- New: treat unregistered top-level JSON keys as invalid actions (registry-driven) ---
                # Some LLMs return a response object like:
                # {"actions": [...], "message": "...", "feelings": {...}}
                # We already allow certain metadata keys (e.g. "feelings") via the validation registry.
                # Any other top-level key is treated as an invalid action type so the corrector
                # can regenerate the response using only registered actions.
                is_from_cortex = source == "llm" or getattr(
                    message, "from_cortex", False
                )
                if is_from_cortex and isinstance(parsed, dict):
                    try:
                        from core.validation_registry import get_validation_registry

                        allowed_metadata = (
                            get_validation_registry().get_response_metadata_keys()
                            or set()
                        )
                        extra_keys = [
                            k
                            for k in parsed.keys()
                            if k != "actions"
                            and k not in allowed_metadata
                            and not k.startswith("meta.")
                        ]
                        if extra_keys:
                            synthetic_actions = []
                            for key in extra_keys:
                                value = parsed.get(key)
                                # When a string value is stored under a message-like
                                # root key (e.g. {"message": "hello"}) the LLM
                                # probably intended a text reply.  Map it to
                                # {"text": ...} so that message_synth_webui and
                                # other send_message implementations can find it.
                                if isinstance(value, dict):
                                    payload = value
                                elif key in (
                                    "message",
                                    "text",
                                    "reply",
                                    "response",
                                ) and isinstance(value, str):
                                    payload = {"text": value}
                                else:
                                    payload = {"value": value}
                                # Skip synthetic message actions when the same text is
                                # already present in an explicit message action — the LLM
                                # commonly echoes the reply in a top-level "message" key in
                                # addition to the proper actions list, which creates a
                                # duplicate that fails validation (no interface_path) and
                                # triggers an unnecessary correction loop.
                                if key in (
                                    "message",
                                    "text",
                                    "reply",
                                    "response",
                                ) and isinstance(value, str):

                                    def _matches_existing_message(action: Any) -> bool:
                                        if not isinstance(action, dict):
                                            return False
                                        action_type = action.get("type")
                                        if not isinstance(action_type, str):
                                            return False
                                        if not action_type.startswith("message_"):
                                            return False
                                        payload = action.get("payload")
                                        if not isinstance(payload, dict):
                                            return False
                                        return payload.get("text") == value

                                    already_present = any(
                                        _matches_existing_message(a) for a in actions
                                    )
                                    if already_present:
                                        log_debug(
                                            f"[message_chain] Skipping synthetic '{key}' action — text already present in explicit message action"
                                        )
                                        continue
                                # Resolve the correct action type and inject
                                # interface_path for message-like synthetic actions
                                # so they pass validation without a correction loop.
                                synthetic_type = key
                                ctx_ipath = ctx.get("interface_path") if ctx else None
                                if key in (
                                    "message",
                                    "text",
                                    "reply",
                                    "response",
                                ) and isinstance(value, str):
                                    if ctx_ipath:
                                        resolved = _resolve_message_action_for_path(
                                            str(ctx_ipath)
                                        )
                                        if resolved:
                                            synthetic_type = resolved
                                        # A vessel say action carries only text;
                                        # only chat message actions need routing
                                        # via interface_path.
                                        if (
                                            not str(synthetic_type).startswith(
                                                "vessel_"
                                            )
                                            and "interface_path" not in payload
                                        ):
                                            payload["interface_path"] = str(ctx_ipath)
                                synthetic_actions.append(
                                    {"type": synthetic_type, "payload": payload}
                                )

                            actions.extend(synthetic_actions)
                            log_info(
                                f"[message_chain] Added {len(synthetic_actions)} synthetic action(s) for unregistered top-level key(s): {', '.join(extra_keys)}"
                            )
                    except Exception as e:
                        log_debug(
                            f"[message_chain] Failed to process top-level metadata keys for correction: {e}"
                        )

                ctx_interface_path = ctx.get("interface_path") if ctx else None
                actions = _normalize_message_unknown(actions, ctx_interface_path)
                actions = _normalize_action_type_aliases(actions)
                actions = _normalize_diary_payload_fields(actions)
                actions = _normalize_message_payload_text(actions)
                # Auto-inject interface_path into message actions that are missing it
                # This prevents validation failures and avoids costly LLM correction calls
                actions = _auto_inject_interface_path(actions, ctx_interface_path)
                # For Grillo beats, drop any message action routed to a conversation
                # the beat never offered (snippet origin or eligible target). This
                # prevents an observer reply from landing in the wrong chat.
                actions = _drop_misrouted_beat_actions(actions, ctx)
                # Drop Recon-schema keys that a state-retaining engine leaked into
                # the main pass (see _drop_leaked_recon_actions). Doing this here —
                # before action-type validation — stops the leaked keys from
                # starving the turn and triggering the corrector's 😵 loop, while
                # preserving any real deliverable action in the same array.
                actions = _drop_leaked_recon_actions(actions)
                # Drop actions the current turn never offered (e.g. a leaked
                # vessel_* action echoed by a state-retaining engine on a plain
                # chat turn). See _drop_out_of_scope_leaked_actions: this uses the
                # per-turn scoped allowlist recorded from the exact prompt, so an
                # off-scope leak is removed before it can drive the corrector into
                # the 😵 loop, while every in-scope deliverable action survives.
                if is_from_cortex:
                    actions = _drop_out_of_scope_leaked_actions(actions, ctx)

                # --- New: Validate action types early and trigger corrector for unsupported types ---
                try:
                    from core.action_parser import get_supported_action_types

                    supported_action_types = get_supported_action_types() or set()
                except Exception as e:
                    log_warning(
                        f"[message_chain] Could not load supported action types: {e}"
                    )
                    supported_action_types = set()

                # Only enforce this for LLM-originated responses
                if is_from_cortex and isinstance(actions, list):
                    try:
                        scoped_actions = ctx.get("allowed_action_types") or ctx.get(
                            "allowed_actions"
                        )
                        if isinstance(scoped_actions, (list, set, tuple)):
                            supported_action_types.update(
                                str(action_type)
                                for action_type in scoped_actions
                                if action_type
                            )
                    except Exception as e:
                        log_debug(
                            "[message_chain] Failed to merge scoped allowed action "
                            f"types into runtime supported set: {e}"
                        )

                    # quick mapping: some models may output a generic "message"
                    # or plain "text" / "reply" / "response" action when they
                    # really intend to send text to the current interface.
                    # Convert it to a concrete type based on the context path
                    # to avoid unnecessary corrector loops.
                    _GENERIC_MSG_TYPES = (
                        "message",
                        "send_message",
                        "message_send",  # some LLMs swap the word order
                        "text",
                        "reply",
                        "response",
                    )
                    if ctx_interface_path:
                        interface_prefix = (
                            ctx_interface_path.split("/")[0]
                            if "/" in ctx_interface_path
                            else ctx_interface_path
                        )
                        # A generic message action emitted during a Vessel
                        # embodiment turn must be spoken IN-WORLD, not routed to
                        # the WebUI fallback. The resolver returns
                        # vessel_<world>_say for a vessel path (world taken
                        # structurally from interface_path, no keyword matching).
                        # Without this, a generic "message_send" is misrouted to
                        # message_synth_webui and the in-world player never hears
                        # the reply.
                        resolved_message_type = (
                            _resolve_message_action_for_path(ctx_interface_path)
                            or "message_synth_webui"
                        )
                        rewrote_generic_message_action = False
                        for act in actions:
                            if (
                                isinstance(act, dict)
                                and act.get("type") in _GENERIC_MSG_TYPES
                            ):
                                act["type"] = resolved_message_type
                                rewrote_generic_message_action = True

                                payload = act.get("payload")
                                if not isinstance(payload, dict):
                                    payload = {}
                                    act["payload"] = payload

                                if (
                                    not isinstance(payload.get("text"), str)
                                    or not payload.get("text", "").strip()
                                ):
                                    for legacy_key in (
                                        "text",
                                        "body",
                                        "content",
                                        "message",
                                        "value",
                                    ):
                                        legacy_value = act.get(legacy_key)
                                        if (
                                            isinstance(legacy_value, str)
                                            and legacy_value.strip()
                                        ):
                                            payload["text"] = legacy_value
                                            break

                        if rewrote_generic_message_action:
                            actions = _normalize_message_payload_text(actions)
                            actions = _auto_inject_interface_path(
                                actions, ctx_interface_path
                            )
                            actions = _drop_misrouted_beat_actions(actions, ctx)

                    unsupported = []
                    for idx, act in enumerate(actions):
                        if not isinstance(act, dict):
                            continue
                        atype = act.get("type") or act.get("action")
                        if not atype or atype not in supported_action_types:
                            unsupported.append(
                                {
                                    "index": idx,
                                    "action": act,
                                    "errors": [
                                        f"Unsupported type '{atype}' - no plugin or interface found to handle it"
                                    ],
                                }
                            )

                    if unsupported:
                        bad_indices = frozenset(u["index"] for u in unsupported)
                        remaining_valid = [
                            act
                            for idx, act in enumerate(actions)
                            if idx not in bad_indices
                        ]

                        if remaining_valid:
                            # Some valid actions remain — drop only the unrecognised
                            # ones so the response can still be delivered without
                            # triggering (and likely exhausting) the corrector.
                            log_warning(
                                f"[message_chain] 🚨 Dropping {len(unsupported)} unsupported action "
                                f"type(s): {[u['action'].get('type') or u['action'].get('action') for u in unsupported]} "
                                "— continuing with valid actions"
                            )
                            actions = remaining_valid
                        else:
                            # All actions are unsupported — request correction
                            log_warning(
                                f"[message_chain] 🚨 Detected unsupported action types from LLM: {[u['action'].get('type') or u['action'].get('action') for u in unsupported]} - requesting correction"
                            )
                            # Attach correction context and force correction path
                            correction_context = {
                                "successful_actions": [],
                                "successful_types": [],
                                "failed_actions": unsupported,
                                "had_json_errors": False,
                                "original_text": text,
                            }
                            try:
                                if hasattr(message, "__dict__"):
                                    message.correction_context = correction_context
                            except Exception:
                                pass

                            parsed = None  # Trigger the corrector loop below

            # Synthera Emotion Forwarding: copy dominant feeling into tts_speak payload
            if (
                parsed is not None
                and isinstance(parsed, dict)
                and "feelings" in parsed
                and actions
            ):
                try:
                    feelings = parsed.get("feelings")
                    if isinstance(feelings, dict) and feelings:
                        valid_feelings = {
                            k: float(v)
                            for k, v in feelings.items()
                            if isinstance(v, (int, float))
                        }
                        if valid_feelings:
                            dominant_emotion = max(
                                valid_feelings.keys(),
                                key=lambda emotion_name: valid_feelings[emotion_name],
                            )
                            if valid_feelings[dominant_emotion] > 0:
                                log_debug(
                                    f"[message_chain] 🎭 Found dominant emotion in metadata: {dominant_emotion} ({valid_feelings[dominant_emotion]})"
                                )
                                for action in actions:
                                    atype = action.get("type") or action.get("action")
                                    if atype == "tts_speak":
                                        payload = action.get("payload")
                                        if isinstance(
                                            payload, dict
                                        ) and not payload.get("emotion"):
                                            payload["emotion"] = dominant_emotion
                                            log_info(
                                                f"[message_chain] 💉 Auto-injected emotion '{dominant_emotion}' into tts_speak payload"
                                            )
                except Exception as e:
                    log_warning(f"[message_chain] Failed to auto-forward emotions: {e}")

            # Only execute actions if we have valid ones
            if parsed is not None:
                # Note: LLM decides freely whether to respond to user or not
                # If no message_telegram_bot action is included, user simply receives nothing
                # Log for debugging purposes
                if source == "llm" or getattr(message, "from_cortex", False):
                    has_user_response = False
                    has_tts = False
                    # Set when the LLM emits an embodiment speak verb
                    # (``vessel_<world>_say``) — the ONLY channel that reaches the
                    # player inside the world. Tracked separately from
                    # ``has_user_response`` so that on a reactive in-world player chat
                    # a stray ``message_*`` action toward some other connected chat
                    # (e.g. the WebUI) does NOT satisfy the "did the player get an
                    # in-world reply?" check. Detection is purely structural.
                    has_inworld_reply = False
                    # Set when the LLM emits an action that delivers user-visible
                    # output on its own (a self-replying plugin action). Tracked
                    # separately from has_user_response so it suppresses the
                    # missing-reply corrector without affecting message/TTS handling.
                    has_user_output_action = False
                    user_message_action = None
                    # Determine current set of message action types from config (dynamic)
                    current_message_action_types = []
                    try:
                        MESSAGE_ACTION_TYPES = config_registry.get_var(
                            "MESSAGE_ACTION_TYPES",
                            [],
                            label="Message action types",
                            description=(
                                "Action types treated as outbound user-visible messages (used for response detection and logging)."
                            ),
                            group="core",
                            component="message_chain",
                        )
                        current_message_action_types = (
                            list(MESSAGE_ACTION_TYPES.value)
                            if hasattr(MESSAGE_ACTION_TYPES, "value")
                            else list(MESSAGE_ACTION_TYPES)
                        )
                    except Exception:
                        current_message_action_types = []

                    # If not configured, infer from available action schemas
                    if not current_message_action_types:
                        try:
                            from core.core_initializer import core_initializer

                            available_actions = core_initializer.actions_block.get(
                                "available_actions", {}
                            )
                            current_message_action_types = [
                                action_type
                                for action_type in available_actions.keys()
                                if isinstance(action_type, str)
                                and action_type.startswith("message_")
                            ]
                        except Exception:
                            current_message_action_types = []

                    # Action types that deliver user-visible output on their own
                    # (self-replying plugin actions, e.g. a plugin that calls
                    # bot.send_message inside execute_action). They satisfy the
                    # "did the user get a reply this turn?" check so the missing-reply
                    # corrector does not fire when the LLM responds with only such an
                    # action. Engine-agnostic and unrelated to any endpoint grammar —
                    # purely about whether the user receives output. Contributors add
                    # their own self-replying actions here via config; fetch-only
                    # actions that need a follow-up reply (e.g. recall_last_dream) must
                    # NOT be listed.
                    current_user_output_action_types = []
                    try:
                        USER_OUTPUT_ACTION_TYPES = config_registry.get_var(
                            "USER_OUTPUT_ACTION_TYPES",
                            ["get_recent_chats"],
                            label="User-output action types",
                            description=(
                                "Action types that deliver user-visible output on their own "
                                "(self-replying plugin actions). Counted as a user reply so the "
                                "missing-reply corrector does not fire when the LLM responds with "
                                "only such an action."
                            ),
                            group="core",
                            component="message_chain",
                        )
                        current_user_output_action_types = (
                            list(USER_OUTPUT_ACTION_TYPES.value)
                            if hasattr(USER_OUTPUT_ACTION_TYPES, "value")
                            else list(USER_OUTPUT_ACTION_TYPES)
                        )
                    except Exception:
                        current_user_output_action_types = []

                    if isinstance(actions, list):
                        actions = cast(list[dict[str, Any]], actions)
                        # ========================================
                        # STRIP TTS FROM AUTONOMOUS MESSAGES
                        # ========================================
                        # Check if any message action is autonomous (Grillo outreach, dreams, etc.)
                        # If so, remove any tts_speak actions - they shouldn't be spoken
                        has_autonomous_message = False
                        for action in actions:
                            if isinstance(action, dict):
                                action_meta = action.get("meta")
                                if not isinstance(action_meta, dict):
                                    action_meta = {}
                                action_name = action.get("action") or action.get("type")
                                # Check if this is an autonomous message action
                                if action_meta.get("autonomous", False) is True:
                                    if (
                                        action_name
                                        and isinstance(action_name, str)
                                        and action_name.startswith("message_")
                                    ):
                                        has_autonomous_message = True
                                        break

                        if has_autonomous_message:
                            # Remove any tts_speak actions from autonomous message responses
                            tts_to_remove = []
                            for i, action in enumerate(actions):
                                if isinstance(action, dict):
                                    action_name = action.get("action") or action.get(
                                        "type"
                                    )
                                    if action_name == "tts_speak":
                                        tts_to_remove.append(i)

                            if tts_to_remove:
                                for i in reversed(tts_to_remove):
                                    removed = actions.pop(i)
                                    removed_payload = removed.get("payload")
                                    if not isinstance(removed_payload, dict):
                                        removed_payload = {}
                                    log_debug(
                                        f"[message_chain] 🔇 Stripped TTS from autonomous message: {removed_payload.get('text', '')[:40]}..."
                                    )

                        # Anti-echo guard for a reactive in-world player chat: on a
                        # weak vessel cortex the model sometimes parrots its own last
                        # spoken line (present in the current-chat history it was
                        # prompted with) instead of answering the player's new
                        # message. Compute Synth's most recent own utterance in this
                        # vessel chat once, up front, so any ``vessel_<world>_say``
                        # whose text is byte-identical to it can be dropped below —
                        # leaving ``has_inworld_reply`` False so the missing-reply
                        # corrector re-runs the turn and produces a fresh reply.
                        # Structural (author identity + exact string equality),
                        # keyword-free, multi-language safe.
                        last_self_vessel_say: str | None = None
                        if is_reactive_vessel_chat:
                            last_self_vessel_say = _last_self_vessel_utterance(
                                ctx, str(ctx.get("interface_path") or "")
                            )
                        vessel_echo_indices: list[int] = []

                        for _action_idx, action in enumerate(actions):
                            # Support both 'action' and 'type' keys
                            action_name = None
                            if isinstance(action, dict):
                                action_name = action.get("action") or action.get("type")
                            if action_name == "tts_speak":
                                has_tts = True
                            # An embodiment speak verb (structurally a
                            # ``vessel_<world>_say`` action, namespaced per connected
                            # world by the vessel plugin) delivers the reply in-world,
                            # so it counts as the outward user reply just like a
                            # ``message_*`` action. Detection is purely structural
                            # (prefix + speak-verb suffix), never based on the message
                            # text, so the missing-reply corrector does not fire and
                            # try to force a non-existent ``message_*`` action on a
                            # vessel turn.
                            is_vessel_speak = (
                                isinstance(action_name, str)
                                and action_name.startswith("vessel_")
                                and action_name.endswith("_say")
                            )
                            # Suppress a verbatim self-echo (see anti-echo note above).
                            if (
                                is_vessel_speak
                                and last_self_vessel_say is not None
                                and isinstance(action, dict)
                            ):
                                say_payload = action.get("payload")
                                say_text = (
                                    say_payload.get("text")
                                    if isinstance(say_payload, dict)
                                    else None
                                )
                                if (
                                    isinstance(say_text, str)
                                    and say_text.strip() == last_self_vessel_say.strip()
                                ):
                                    vessel_echo_indices.append(_action_idx)
                                    log_warning(
                                        "[message_chain] 🌀 Suppressing verbatim vessel "
                                        f"self-echo ('{say_text[:40]}') — model repeated its "
                                        "own last line; forcing a fresh reply to the player"
                                    )
                                    # Do NOT count this as an in-world reply so the
                                    # corrector re-runs and produces a real answer.
                                    continue
                            if is_vessel_speak:
                                has_inworld_reply = True
                            if (
                                action_name in current_message_action_types
                                or (
                                    isinstance(action_name, str)
                                    and action_name.startswith("message_")
                                )
                                or is_vessel_speak
                            ):
                                has_user_response = True
                                if not user_message_action:
                                    user_message_action = action
                                # break
                            if action_name in current_user_output_action_types:
                                has_user_output_action = True

                        # Drop the echoed say actions so they are never dispatched.
                        if vessel_echo_indices and isinstance(actions, list):
                            for _idx in reversed(vessel_echo_indices):
                                try:
                                    actions.pop(_idx)
                                except Exception:
                                    pass

                    # ------------------------------------------------------------------
                    # LLM-CHOSEN VOICE REPLY (text turn): merge into a single voice note
                    # ------------------------------------------------------------------
                    # When the LLM itself decided to answer with `tts_speak` AND also
                    # emitted a text-only message_* action for the same reply, we don't
                    # want the user to receive both a text bubble and a separate audio
                    # note. Mirror the voice-input behaviour: drop the standalone text
                    # message and fold its text into the tts_speak as the caption.
                    #
                    # This lets the synth reply with voice on a *typed* request (e.g.
                    # "answer me with a voice message") or whenever it chooses to speak,
                    # driven purely by the LLM's action choice (no keyword detection).
                    # Skipped for: WebUI (keeps its text bubble for the tts-play handler),
                    # autonomous/internal turns (already stripped above), and voice-input
                    # turns (handled by the auto-inject path below).
                    if (
                        has_tts
                        and has_user_response
                        and user_message_action
                        and isinstance(actions, list)
                    ):
                        _merge_iface_prefix = (ctx.get("interface_path") or "").split(
                            "/"
                        )[0]
                        _merge_is_voice_input = bool(ctx.get("is_voice_input", False))
                        _merge_request_tts = bool(
                            context
                            and isinstance(context, dict)
                            and context.get("request_tts")
                        )
                        # Only merge for LLM-driven voice replies on non-voice turns.
                        # Voice-originated turns are merged by the auto-inject path, and
                        # WebUI intentionally keeps the separate text bubble.
                        if (
                            _merge_iface_prefix != "synth_webui"
                            and not _merge_is_voice_input
                            and not _merge_request_tts
                        ):
                            # Find the LLM-emitted tts_speak that is NOT auto-injected.
                            _llm_tts_action = None
                            for _a in actions:
                                if not isinstance(_a, dict):
                                    continue
                                if (_a.get("action") or _a.get("type")) != "tts_speak":
                                    continue
                                _a_payload = _a.get("payload")
                                if not isinstance(_a_payload, dict):
                                    _a_payload = {}
                                if _a_payload.get("__auto_injected"):
                                    continue
                                _llm_tts_action = _a
                                break

                            if _llm_tts_action is not None:
                                _tts_payload = _llm_tts_action.get("payload")
                                if not isinstance(_tts_payload, dict):
                                    _tts_payload = {}
                                    _llm_tts_action["payload"] = _tts_payload
                                _spoken_text = str(
                                    _tts_payload.get("text") or ""
                                ).strip()
                                # Prefer the spoken text as caption; fall back to the
                                # text-only message bodies if tts_speak carried no
                                # text. When the LLM emitted several message actions,
                                # join ALL of their texts so no message is dropped.
                                _msg_texts = _collect_message_texts(actions)
                                _msg_text = _join_message_texts(_msg_texts)
                                _caption = _spoken_text or _msg_text
                                if _caption:
                                    if not _tts_payload.get("text"):
                                        _tts_payload["text"] = _caption
                                    _tts_payload["__merged_text"] = _caption
                                    log_info(
                                        "[message_chain] \U0001f3a4 LLM voice reply on text turn: "
                                        f"merging text+audio into a single voice message for: {_caption[:30]}..."
                                    )
                                    # Remove standalone message_* actions; the voice
                                    # message (audio + caption) replaces them.
                                    actions[:] = [
                                        a
                                        for a in actions
                                        if isinstance(a, dict)
                                        and (a.get("action") or a.get("type"))
                                        not in current_message_action_types
                                    ]

                    # Auto-inject TTS if there's a user response but no tts_speak
                    # Only for actual user-facing interfaces (not internal like grillo)
                    if (
                        has_user_response
                        and not has_tts
                        and user_message_action
                        and isinstance(actions, list)
                    ):
                        # Check if this is for a user-facing interface
                        user_facing_interfaces = [
                            "discord_bot",
                            "telegram_bot",
                            "synth_webui",
                            "matrix_chat",
                            "ollama_serve",
                        ]
                        interface_path = ctx.get("interface_path") or ""
                        chat_id = ctx.get("chat_id")

                        # interface_path must have a chat_id suffix to be user-facing
                        # e.g., "telegram_bot/5551234567" not just "telegram_bot".
                        # A reactive in-world player chat is user-facing too (see the
                        # earlier ``is_reactive_vessel_chat`` note); autonomous vessel
                        # perceptions leave the structural flag False and stay excluded.
                        is_reactive_vessel_chat = bool(ctx.get("vessel_player_chat"))
                        # Outbound Grillo beats (observer, scheduled_reminder,
                        # web_search_result) target a real interface and are
                        # user-facing: their ``message_*`` actions must ride the same
                        # TTS auto-inject path as ordinary replies. Internal beats
                        # (self_reflection, curiosity, ...) stay excluded.
                        is_grillo_outbound = bool(
                            ctx.get("grillo_beat", False)
                        ) and is_outbound_beat(ctx.get("beat_type"))
                        is_user_facing = (
                            bool(
                                interface_path
                                and (
                                    any(
                                        interface_path.startswith(f"{iface}/")
                                        for iface in user_facing_interfaces
                                    )
                                    or is_reactive_vessel_chat
                                )
                            )
                            or is_grillo_outbound
                        )

                        # Check if this is an internal/system message
                        # chat_id == -1 indicates internal system messages (Grillo beats, etc.)
                        is_internal_chat = (
                            chat_id == -1 or chat_id == "-1" or str(chat_id) == "-1"
                        )

                        # Check if this is a Grillo internal beat (not outbound)
                        # Only internal Grillo beats (self_reflection, curiosity, etc.) skip TTS
                        # Outbound beats (observer) ARE user-facing and SHOULD get TTS
                        is_grillo_internal = ctx.get(
                            "grillo_beat", False
                        ) and not is_outbound_beat(ctx.get("beat_type"))

                        # Check for autonomous messages (Grillo outreach, dreams, etc.)
                        # These are system-initiated, not user-response, so they shouldn't get TTS
                        action_meta = (
                            user_message_action.get("meta")
                            if isinstance(user_message_action, dict)
                            else {}
                        )
                        if not isinstance(action_meta, dict):
                            action_meta = {}
                        is_autonomous = action_meta.get("autonomous", False) is True

                        # Check for system startup/internal message patterns
                        payload = (
                            user_message_action.get("payload")
                            if isinstance(user_message_action, dict)
                            else {}
                        )
                        if not isinstance(payload, dict):
                            payload = {}
                        text_to_speak = (
                            payload.get("text")
                            or payload.get("content")
                            or payload.get("message")
                            or ""
                        )
                        # If the LLM returned plain text rather than structured JSON,
                        # there will be no user_message_action and text_to_speak will
                        # be empty.  In cases where the interface explicitly asked for
                        # speech (e.g. voice note, request_tts flag) we fall back to
                        # speaking the raw text output so the user still hears audio.
                        if not text_to_speak and ctx.get("request_tts") and text:
                            text_to_speak = text
                        # Patterns that indicate internal/system messages that shouldn't get TTS
                        internal_message_patterns = [
                            "Synthetic Heart AI online",
                            "Action schema and system instructions",
                            "Analysis of recent memory logs",
                            "I have completed the analysis of memory logs",
                            "I have successfully processed",
                            "operational instruction",
                            "memory consolidation",
                            "tag elaboration",
                            "system initialization",
                            "configuration updated",
                        ]
                        is_system_message = any(
                            pattern.lower() in text_to_speak.lower()
                            for pattern in internal_message_patterns
                        )

                        # Check if TTS was already executed in a previous correction attempt
                        # This prevents double-TTS when correction flow runs
                        tts_already_executed = False
                        correction_ctx = getattr(message, "correction_context", None)
                        if correction_ctx:
                            # Only treat TTS as already-executed when the SAME text
                            # was already spoken. A correction often returns a NEW
                            # message (e.g. after a failed action); suppressing TTS
                            # then leaves the corrected reply as silent text
                            # (Langfuse 11feca6f: the corrector's new reply was
                            # sent as plain text with no voice note).
                            spoken_texts: set[str] = set()
                            successful_actions = correction_ctx.get(
                                "successful_actions", []
                            )
                            if isinstance(successful_actions, list):
                                for action in successful_actions:
                                    if (
                                        isinstance(action, dict)
                                        and action.get("type") == "tts_speak"
                                    ):
                                        spoken_payload = action.get("payload", {})
                                        if isinstance(spoken_payload, dict):
                                            spoken_text = spoken_payload.get("text")
                                            if (
                                                isinstance(spoken_text, str)
                                                and spoken_text.strip()
                                            ):
                                                spoken_texts.add(spoken_text.strip())
                            if not spoken_texts:
                                # Fallback: keep the legacy type-only check when
                                # no spoken text is recorded.
                                successful_types = correction_ctx.get(
                                    "successful_types", []
                                )
                                if (
                                    isinstance(successful_types, list)
                                    and "tts_speak" in successful_types
                                ):
                                    tts_already_executed = True
                            elif (
                                isinstance(text_to_speak, str)
                                and text_to_speak.strip() in spoken_texts
                            ):
                                tts_already_executed = True

                        # Determine if we should skip TTS
                        # When the LLM explicitly set send_as_voice=true on the
                        # message_* action, the audio is routed centrally in
                        # action_parser (VoxPlugin.speak) and the plain text is
                        # suppressed there. Auto-injecting tts_speak here as well
                        # would produce a second spoken message, so skip it.
                        _explicit_send_as_voice = bool(payload.get("send_as_voice"))

                        # Skip for: internal grillo beats, internal chats, system messages, autonomous messages, already-executed TTS,
                        # an explicit send_as_voice request (handled in action_parser),
                        # or if the request_tts flag/feature is off.
                        # Outbound Grillo beats are exempt from the internal-chat
                        # skip so their user-facing messages get TTS.
                        should_skip_tts = (
                            is_grillo_internal
                            or (is_internal_chat and not is_grillo_outbound)
                            or is_system_message
                            or is_autonomous
                            or tts_already_executed
                            or _explicit_send_as_voice
                        )
                        # honor explicit request flag passed via context or wrapped message
                        _explicit_tts_request = bool(
                            context
                            and isinstance(context, dict)
                            and context.get("request_tts")
                        )
                        if context and isinstance(context, dict):
                            if context.get("request_tts") is False:
                                should_skip_tts = True

                        # Determine whether Vox/TTS is effectively enabled.  The new
                        # canonical switch is the active engine name: if the selected
                        # engine is non-empty and not "disabled", we consider TTS on.
                        # For backwards compatibility we also honour the old boolean
                        # flags/endpoints, but they no longer drive the feature state.
                        tts_raw = ""
                        try:
                            active = str(
                                config_registry.get_value(
                                    "ACTIVE_VOX_ENGINE",
                                    "",
                                    value_type=str,
                                    group="plugins",
                                    component="vox_plugin",
                                )
                            )
                            vox_enabled = bool(active and active != "disabled")

                            # legacy fallback
                            if not vox_enabled:
                                tts_raw = str(
                                    config_registry.get_value(
                                        "TTS_ENDPOINTS",
                                        "",
                                        value_type=str,
                                        group="plugins",
                                        component="tts_lipsync",
                                    )
                                    or ""
                                )
                                vox_enabled = bool(
                                    config_registry.get_value(
                                        "TTS_ENABLED",
                                        False,
                                        value_type=bool,
                                        group="plugins",
                                        component="tts_lipsync",
                                    )
                                    or tts_raw
                                )

                            if not vox_enabled and not _explicit_tts_request:
                                should_skip_tts = True
                                log_debug(
                                    "[message_chain] Skipping TTS auto-inject because Vox engine is disabled"
                                )
                            elif not vox_enabled and _explicit_tts_request:
                                log_debug(
                                    "[message_chain] Vox engine disabled but request_tts=True — "
                                    "allowing TTS attempt (VoxPlugin will fall back to text if needed)"
                                )

                        except Exception as e:
                            log_debug(
                                f"[message_chain] Error checking Vox/TTS config: {e}"
                            )

                        # For Telegram/Discord, only auto-inject TTS for voice-originated
                        # messages. WebUI, Matrix and Ollama always get TTS when VOX is active.
                        # VOX_SPEAK_TEXT_REPLIES (opt-in toggle, Engines tab) lifts the
                        # voice-input requirement so typed text replies also get a clip.
                        _is_voice_input = bool(ctx.get("is_voice_input", False))
                        _iface_tts_prefix = (ctx.get("interface_path") or "").split(
                            "/"
                        )[0]
                        _voice_only_tts_ifaces = {"telegram_bot", "discord_bot"}
                        tts_allowed = (
                            _iface_tts_prefix not in _voice_only_tts_ifaces
                        ) or _is_voice_input
                        _speak_text_replies = False
                        try:
                            _speak_text_replies = bool(
                                config_registry.get_value(
                                    "VOX_SPEAK_TEXT_REPLIES",
                                    False,
                                    value_type=bool,
                                    group="plugins",
                                    component="vox_plugin",
                                )
                            )
                        except Exception:
                            pass

                        if should_skip_tts:
                            skip_reason = []
                            if is_grillo_internal:
                                skip_reason.append(
                                    f"grillo_beat={ctx.get('beat_type')}"
                                )
                            if is_internal_chat:
                                skip_reason.append(f"internal_chat_id={chat_id}")
                            if is_system_message:
                                skip_reason.append("system_message_pattern")
                            if is_autonomous:
                                skip_reason.append("autonomous_message")
                            if tts_already_executed:
                                skip_reason.append("tts_already_executed_in_correction")
                            if not tts_raw:
                                skip_reason.append("tts_not_configured")
                            log_debug(
                                f"[message_chain] Skipping TTS auto-inject: {', '.join(skip_reason)}"
                            )
                        # auto-inject only for voice-originated messages or when
                        # the LLM explicitly asked for TTS.  previously the
                        # ``tts_allowed`` flag would permit any text response from
                        # WebUI/Matrix/other non-voice interfaces which caused the
                        # synth to speak even though the incoming message was not
                        # audio.  the requirement is that non-audio inputs should not
                        # trigger spoken replies — unless the operator opted in via
                        # the VOX_SPEAK_TEXT_REPLIES toggle, which attaches a clip
                        # to text replies on every user-facing interface.
                        elif (
                            is_user_facing
                            and (
                                (tts_allowed and _is_voice_input) or _speak_text_replies
                            )
                        ) or (context and context.get("request_tts")):
                            # With the new strategy we honor explicit TTS requests even when
                            # they come from non-WebUI interfaces (voice note, etc.).
                            if (
                                text_to_speak
                                and isinstance(text_to_speak, str)
                                and len(text_to_speak.strip()) > 0
                            ):
                                # Determine whether this is a voice-originated request.
                                # For voice inputs we want to send a SINGLE audio+caption
                                # message instead of separate text then audio.
                                # Exception: synth_webui always keeps the text bubble so the
                                # tts-play handler has a bubble to annotate; audio is overlaid.
                                is_voice_response = (
                                    _is_voice_input
                                    or bool(context and context.get("request_tts"))
                                ) and _iface_tts_prefix != "synth_webui"

                                # Carry the target interface_path into the injected
                                # tts_speak so audio dispatch works even when the
                                # current chat context has none (e.g. outbound Grillo
                                # beats that address another chat via the message
                                # action's payload).
                                _tts_target_path = payload.get(
                                    "interface_path"
                                ) or payload.get("chat_name")

                                if is_voice_response:
                                    # Voice response strategy:
                                    #   • Remove the standalone message_* actions so text
                                    #     is NOT sent as a separate message.
                                    #   • Inject tts_speak with __merged_text so the text
                                    #     becomes the audio caption (Telegram) or is sent
                                    #     immediately after the audio (other interfaces).
                                    #   • Do NOT set __auto_injected — if TTS fails the
                                    #     fallback will send text so the user is not left
                                    #     with zero response.
                                    # When the LLM emitted several message actions, join
                                    # ALL of their texts so no message is dropped: the
                                    # voice note speaks (and captions) the full reply.
                                    _voice_msg_texts = _collect_message_texts(actions)
                                    if len(_voice_msg_texts) > 1:
                                        _joined_voice_text = _join_message_texts(
                                            _voice_msg_texts
                                        )
                                        log_info(
                                            "[message_chain] 🎤 Voice response with "
                                            f"{len(_voice_msg_texts)} message actions — "
                                            "joining all texts into the voice note"
                                        )
                                        text_to_speak = _joined_voice_text
                                    log_info(
                                        f"[message_chain] 🎤 Voice response: replacing text-only message with audio+caption for: {text_to_speak[:30]}..."
                                    )
                                    # Remove all message_* actions; audio+caption replaces them
                                    actions[:] = [
                                        a
                                        for a in actions
                                        if isinstance(a, dict)
                                        and (a.get("action") or a.get("type"))
                                        not in current_message_action_types
                                    ]
                                    tts_action = {
                                        "type": "tts_speak",
                                        "payload": {
                                            "text": text_to_speak,
                                            # __merged_text becomes the caption on Telegram
                                            # and the follow-up text on other interfaces.
                                            "__merged_text": text_to_speak,
                                            "emotion": payload.get("emotion")
                                            if isinstance(payload, dict)
                                            else None,
                                        },
                                    }
                                else:
                                    # Non-voice: inject TTS alongside the existing message_*
                                    # action.  Set __auto_injected so VoxPlugin suppresses
                                    # the text fallback — text was already sent by message_*.
                                    log_info(
                                        f"[message_chain] 🗣️ Auto-injecting 'tts_speak' action for message: {text_to_speak[:30]}..."
                                    )
                                    tts_action = {
                                        "type": "tts_speak",
                                        "payload": {
                                            "text": text_to_speak,
                                            "emotion": payload.get("emotion")
                                            if isinstance(payload, dict)
                                            else None,
                                            # Flag: auto-injected by message_chain (not emitted
                                            # by the LLM). VoxPlugin uses this to suppress the
                                            # text fallback when TTS fails — text was already
                                            # sent by the message_*_bot action.
                                            "__auto_injected": True,
                                        },
                                    }
                                if _tts_target_path:
                                    tts_action["payload"]["interface_path"] = (
                                        _tts_target_path
                                    )
                                actions.append(tts_action)
                                # Update has_tts flag since we just added it
                                has_tts = True
                                # clear request flag so we don't duplicate later
                                if context and isinstance(context, dict):
                                    context.pop("request_tts", None)
                        else:
                            log_debug(
                                f"[message_chain] Skipping TTS auto-inject for non-user-facing interface: {interface_path}"
                            )

                    # at this point we may not have computed the
                    # various interface flags (they're defined only in
                    # the branch above). ensure they exist so the
                    # warning logic doesn't crash when has_user_response
                    # is False.
                    if "interface_path" not in locals():
                        interface_path = ctx.get("interface_path") or ""
                    if "is_user_facing" not in locals():
                        is_user_facing = False
                    if "is_reactive_vessel_chat" not in locals():
                        is_reactive_vessel_chat = bool(ctx.get("vessel_player_chat"))
                    if "is_grillo_internal" not in locals():
                        is_grillo_internal = False
                    if "is_internal_chat" not in locals():
                        is_internal_chat = False
                    if "has_user_response" not in locals():
                        has_user_response = False
                    if "has_inworld_reply" not in locals():
                        has_inworld_reply = False

                    if not has_user_response:
                        if (
                            is_user_facing
                            and not is_grillo_internal
                            and not is_internal_chat
                            and not _contains_vessel_disconnect_action(actions)
                        ):
                            log_warning(
                                f"[message_chain] ⚠️ LLM generated no outbound message action for user-facing interface '{interface_path}' — user will receive no reply"
                            )
                        else:
                            log_debug(
                                "[message_chain] LLM chose not to send user message (diary/internal only)"
                            )
                    else:
                        # Check if the originating interface has a corresponding action
                        iface_prefix = (interface_path or "").split("/")[0]
                        expected_action = _INTERFACE_TO_MESSAGE_ACTION.get(iface_prefix)
                        action_types_in_response = {
                            a.get("type") or a.get("action")
                            for a in (actions or [])
                            if isinstance(a, dict)
                        }
                        if (
                            expected_action
                            and expected_action not in action_types_in_response
                            and is_user_facing
                            and not is_grillo_internal
                            and not is_internal_chat
                        ):
                            log_warning(
                                f"[message_chain] ⚠️ LLM replied but not to the originating interface '{iface_prefix}' "
                                f"(expected '{expected_action}', got {action_types_in_response & set(_INTERFACE_TO_MESSAGE_ACTION.values())}) "
                                f"— user may not receive reply on {iface_prefix}"
                            )
                        else:
                            log_debug("[message_chain] LLM will send message to user")

                        # CRITICAL FIX: Merge text into TTS when both are present
                        # This ensures text+audio are sent in the SAME message and
                        # prevents a separate duplicate text reply.  We no longer
                        # gate on plugin availability since the payload can still
                        # fall back to merged_text if TTS fails.
                        if has_user_response and has_tts and isinstance(actions, list):
                            # Find all message actions carrying text and any TTS actions
                            message_actions_to_remove = []
                            tts_actions = []

                            for idx, action in enumerate(actions):
                                if not isinstance(action, dict):
                                    continue
                                action_type = action.get("action") or action.get("type")

                                if action_type == "tts_speak":
                                    tts_actions.append(action)
                                elif action_type in current_message_action_types:
                                    msg_payload = action.get("payload", {})
                                    msg_text = msg_payload.get("text")
                                    # record interface_path or chat_name for diagnostics
                                    msg_ipath = msg_payload.get(
                                        "interface_path"
                                    ) or msg_payload.get("chat_name")

                                    if msg_text:
                                        message_actions_to_remove.append(
                                            (idx, msg_text, msg_ipath)
                                        )

                            # Merge text into each TTS payload and then drop the
                            # standalone message actions.
                            if message_actions_to_remove and tts_actions:
                                log_info(
                                    f"[message_chain] 🔗 Merging {len(message_actions_to_remove)} message action(s) into TTS to send text+audio together"
                                )

                                # Join ALL message texts (not just the first) so a
                                # multi-message reply survives the merge verbatim —
                                # the voice note speaks and captions the full reply.
                                _all_msg_texts = [
                                    msg_text
                                    for (_, msg_text, _) in message_actions_to_remove
                                ]
                                _joined_msg_text = _join_message_texts(_all_msg_texts)

                                for tts_action in tts_actions:
                                    tts_payload = tts_action.get("payload", {})
                                    if not isinstance(tts_payload, dict):
                                        continue

                                    tts_payload["__merged_text"] = _joined_msg_text

                                    # When the spoken text was derived from the first
                                    # message action (auto-injected TTS), speak the full
                                    # merged reply instead of only the first message. An
                                    # LLM-chosen tts_speak carries its own deliberate
                                    # spoken text and is left untouched.
                                    _cur_text = str(
                                        tts_payload.get("text") or ""
                                    ).strip()
                                    if _cur_text and _cur_text in _all_msg_texts:
                                        tts_payload["text"] = _joined_msg_text
                                    log_info(
                                        f"[message_chain] ✅ Merged {len(_all_msg_texts)} message text(s) into tts_speak: '{_joined_msg_text[:50]}...'"
                                    )

                                # Remove the standalone message actions
                                for idx, _, _ in sorted(
                                    message_actions_to_remove, reverse=True
                                ):
                                    removed_action = actions.pop(idx)
                                    log_info(
                                        f"[message_chain] 🗑️ Removed duplicate message action (will be sent with TTS): {removed_action.get('type')}"
                                    )
                            else:
                                log_info(
                                    "[message_chain] ⚠️ TTS plugin not available - keeping separate message and TTS actions (text sent separately)"
                                )

                # Execute actions regardless of whether response is included
                if parsed is not None:
                    # has_user_response / has_user_output_action are only
                    # initialised in the source=="llm" branch above; on a
                    # corrector retry we re-enter this block with parsed
                    # re-populated but without passing through that init, so
                    # ensure they exist before the reads below.
                    if "has_user_response" not in locals():
                        has_user_response = False
                    if "has_inworld_reply" not in locals():
                        has_inworld_reply = False
                    if "has_user_output_action" not in locals():
                        has_user_output_action = False
                    if "interface_path" not in locals():
                        interface_path = ctx.get("interface_path") or ""
                    if "is_reactive_vessel_chat" not in locals():
                        is_reactive_vessel_chat = bool(ctx.get("vessel_player_chat"))
                    try:
                        log_debug(
                            f"[message_chain] EXECUTING ACTIONS: count={len(actions) if actions else 0}, interface_path={ctx.get('interface_path')}, chat_id={ctx.get('chat_id')}, action_types={[a.get('type') or a.get('action') for a in (actions or []) if isinstance(a, dict)]}"
                        )
                        # filter out previously-successful types when retrying
                        if attempt > 0:
                            cc = getattr(message, "correction_context", None) or {}
                            successful_types = cc.get("successful_types", [])
                            if isinstance(successful_types, (list, tuple, set)):
                                successful_types = list(successful_types)
                            if successful_types and isinstance(actions, list):
                                # If a message action already delivered on an
                                # earlier pass, suppress ALL message types on
                                # the retry — a duplicate delivery to the user
                                # is the worst failure mode, and the model
                                # re-emitting the reply is the CHANGELOG
                                # 2026-06-26 duplicate. Structural type-prefix
                                # match; never keyword logic.
                                delivered_message = any(
                                    str(t).startswith("message_")
                                    for t in successful_types
                                )
                                filtered = []
                                for act in actions:
                                    atype = None
                                    if isinstance(act, dict):
                                        atype = act.get("type") or act.get("action")
                                    if atype in successful_types:
                                        log_debug(
                                            f"[message_chain] Removing previously successful action type '{atype}' from retry payload"
                                        )
                                    elif delivered_message and str(
                                        atype or ""
                                    ).startswith("message_"):
                                        log_debug(
                                            f"[message_chain] Suppressing re-emitted message action '{atype}' on retry (already delivered)"
                                        )
                                    else:
                                        filtered.append(act)
                                actions = filtered
                        # Agentic Runtime 2.0: deterministic router. The router
                        # itself gates on AGENT_ENABLED (the single authoritative
                        # agent toggle); when the agent is off it returns FAST and
                        # the Fast Lane runs exactly as before.
                        from core.agent_router import (
                            classify as _agent_classify,
                            route as _agent_route,
                        )

                        action_list = actions if isinstance(actions, list) else []
                        lane = _agent_classify(action_list, context=ctx)
                        if lane == "agent":
                            log_info(
                                "[message_chain] 🤖 Agent Lane engaged for this turn"
                            )
                            result = await _agent_route(
                                action_list,
                                context=ctx,
                                bot=bot,
                                message=message,
                            )
                            # The agent lane returns its own result shape;
                            # normalize to what the loop expects downstream.
                            if not isinstance(result, dict):
                                result = {"processed": [], "failed_actions": []}
                            result.setdefault("processed", [])
                            result.setdefault("failed_actions", [])
                            result.setdefault("errors", [])
                            result.setdefault("action_outputs", [])
                            actions_executed_during_loop = True
                            # Skip the rest of the Fast-Lane correction logic.
                            delivered_to_llm = False
                            fixable_failures: list = []
                            unfixable_failures: list = []
                        else:
                            result = await run_actions(actions, ctx, bot, message)
                        processed = result.get("processed", [])
                        failed = result.get("failed_actions", [])
                        errors = result.get("errors", [])
                        if _contains_vessel_disconnect_action(processed):
                            vessel_disconnect_succeeded = True
                        # A non-empty action_outputs means run_actions already
                        # enqueued an LLM delivery follow-up (terminal output, or a
                        # deliver_to_llm fetch action like recall_last_dream). That
                        # follow-up *is* the user's reply, so the missing-reply
                        # corrector must not also fire — otherwise both produce a
                        # message and the user gets a double reply.
                        delivered_to_llm = bool(result.get("action_outputs"))

                        # remember if we actually ran anything
                        if processed:
                            actions_executed_during_loop = True
                        # remember if an in-world speak verb was voiced this turn
                        # (structural detection, keyword-free) so a reactive player
                        # chat that never produced a ``say`` can be backfilled with a
                        # deterministic fallback reply at loop exit
                        if _vessel_say_delivered(processed):
                            vessel_reply_delivered = True

                        log_info(
                            f"[message_chain] Actions result: {len(processed)} successful, {len(failed)} failed"
                        )

                        # If we had corruption recovery or validation failures, check if correction is needed
                        # But SKIP correction for "unfixable" errors (policy restrictions like whitelist/suggest mode)
                        # These can't be fixed by the LLM - they're system configuration issues
                        fixable_failures = [
                            f for f in failed if not f.get("unfixable", False)
                        ]
                        unfixable_failures = [
                            f for f in failed if f.get("unfixable", False)
                        ]

                        if unfixable_failures:
                            unfixable_types = [
                                f.get("action", {}).get("type", "?")
                                for f in unfixable_failures
                            ]
                            log_info(
                                f"[message_chain] Skipping correction for {len(unfixable_failures)} unfixable policy errors: {unfixable_types}"
                            )

                        recovered_with_extra_text = bool(
                            metadata.get("recovered", False)
                            and (
                                metadata.get("had_extra_text", False)
                                or metadata.get("prefix_length", 0)
                                or metadata.get("suffix_length", 0)
                                or metadata.get("unparsed_content", "")
                                or metadata.get("recovery_attempts", 0)
                            )
                        )

                        needs_correction = len(fixable_failures) > 0 or metadata.get(
                            "recovered", False
                        )

                        # Attach last action result to message/context so downstream hooks
                        # (e.g. Grillo action checker) can inspect what happened.
                        last_action_result = {
                            "processed": processed,
                            "failed": failed,
                            "errors": errors,
                        }
                        try:
                            ctx["last_action_result"] = last_action_result
                            if hasattr(message, "__dict__"):
                                message.last_action_result = last_action_result
                        except Exception:
                            pass

                        if needs_correction and (
                            source == "llm" or getattr(message, "from_cortex", False)
                        ):
                            # Some actions failed or JSON was corrupted - request selective correction.
                            # If recovery left extra trailing content, we may have silently dropped
                            # additional actions after executing the ones we could salvage.
                            if len(failed) > 0:
                                log_warning(
                                    f"[message_chain] {len(failed)} actions failed, requesting correction for missing/invalid actions"
                                )
                            elif recovered_with_extra_text:
                                extra_chars = int(
                                    metadata.get("prefix_length", 0)
                                ) + int(metadata.get("suffix_length", 0))
                                log_warning(
                                    "[message_chain] Recovered LLM JSON still had extra trailing content "
                                    f"({extra_chars} chars, {metadata.get('error_count', 0)} parse errors); "
                                    "requesting correction for dropped actions"
                                )
                            else:
                                log_warning(
                                    "[message_chain] JSON recovery requires correction despite no execution failures"
                                )

                            # Check if user response is required but missing.
                            # On a reactive in-world player chat the ONLY reply that
                            # reaches the player is an embodiment speak verb
                            # (``vessel_<world>_say``); a ``message_*`` action toward a
                            # different connected chat (e.g. the WebUI) does NOT count,
                            # so require ``has_inworld_reply`` in that case. Structural,
                            # no message-text inspection.
                            reply_present = (
                                has_inworld_reply
                                if is_reactive_vessel_chat
                                else has_user_response
                            )
                            missing_user_reply = False
                            if (
                                is_user_facing
                                and not is_grillo_internal
                                and not is_internal_chat
                                and not is_scoped_non_message
                                and not reply_present
                                and not has_user_output_action
                                and not delivered_to_llm
                                and not vessel_disconnect_succeeded
                            ):
                                missing_user_reply = True

                            errors_list = list(errors)
                            if missing_user_reply:
                                _hint_player_msg = (
                                    ctx.get("original_user_message") or text
                                    if is_reactive_vessel_chat
                                    else None
                                )
                                _hint_last_self = (
                                    _last_self_vessel_utterance(
                                        ctx, str(interface_path or "")
                                    )
                                    if is_reactive_vessel_chat
                                    else None
                                )
                                errors_list.append(
                                    _build_missing_reply_hint(
                                        interface_path,
                                        is_reactive_vessel_chat,
                                        current_player_message=_hint_player_msg,
                                        last_self_line=_hint_last_self,
                                    )
                                )

                            # Autonomous Rift Vessel turns (will/action/goal beats,
                            # reflections, perceptions) have no human awaiting a reply.
                            # When an action fails on such a turn, immediately re-invoking
                            # the LLM to "correct" is wasteful — it burns a full LLM call
                            # per retry and lets a weak model re-emit stray in-world `say`
                            # spam (the reported duplicate-message flood). The failure is
                            # already recorded above; the next (rate-limited) beat retries
                            # on its own cadence. Reactive player chat (vessel_player_chat)
                            # still gets correction so a person is always answered.
                            _is_autonomous_vessel_turn = False
                            try:
                                from core.interface_path_utils import (
                                    is_vessel_embodiment_context,
                                )

                                _is_autonomous_vessel_turn = (
                                    is_vessel_embodiment_context(ctx or {})
                                    and not is_reactive_vessel_chat
                                )
                            except Exception:  # pragma: no cover - defensive
                                _is_autonomous_vessel_turn = False
                            if (
                                _is_autonomous_vessel_turn
                                and len(failed) > 0
                                and not missing_user_reply
                                and not has_user_output_action
                                and not delivered_to_llm
                            ):
                                log_warning(
                                    "[message_chain] Skipping LLM correction for "
                                    f"autonomous vessel turn ({len(failed)} failed "
                                    "action(s)); next beat will retry"
                                )
                                return ACTIONS_EXECUTED

                            # Build correction context with info about what succeeded and what failed
                            correction_context = _merge_correction_successes(
                                getattr(message, "correction_context", None),
                                {
                                    "successful_actions": processed,
                                    "successful_types": [
                                        (a.get("type") or a.get("action"))
                                        for a in processed
                                        if isinstance(a, dict)
                                    ],
                                    "failed_actions": failed,
                                    "errors": errors_list,
                                    "had_json_errors": metadata.get("recovered", False),
                                    "original_text": text,
                                },
                            )

                            # Store this in the message for the corrector to use
                            if hasattr(message, "__dict__"):
                                message.correction_context = correction_context

                            # Set parsed = None to trigger correction path
                            # But keep the successful actions already executed
                            if (
                                len(failed) > 0
                                or recovered_with_extra_text
                                or missing_user_reply
                            ):
                                parsed = (
                                    None  # This will trigger the correction loop below
                                )
                            else:
                                # All actions succeeded despite recovery - we're done
                                log_info(
                                    "[message_chain] All actions executed successfully despite JSON recovery"
                                )
                                return ACTIONS_EXECUTED
                        else:
                            # All actions succeeded, but check if user response is
                            # required and missing. Same in-world reply rule as above:
                            # a reactive vessel player chat needs a ``vessel_*_say``,
                            # not just any ``message_*``.
                            reply_present = (
                                has_inworld_reply
                                if is_reactive_vessel_chat
                                else has_user_response
                            )
                            if (
                                is_user_facing
                                and not is_grillo_internal
                                and not is_internal_chat
                                and not is_scoped_non_message
                                and not reply_present
                                and not has_user_output_action
                                and not delivered_to_llm
                                and not vessel_disconnect_succeeded
                            ):
                                log_warning(
                                    f"[message_chain] ⚠️ LLM generated no outbound message action for user-facing interface '{interface_path}' — triggering corrector for missing reply"
                                )
                                correction_context = _merge_correction_successes(
                                    getattr(message, "correction_context", None),
                                    {
                                        "successful_actions": processed,
                                        "successful_types": [
                                            (a.get("type") or a.get("action"))
                                            for a in processed
                                            if isinstance(a, dict)
                                        ],
                                        "failed_actions": [],
                                        "errors": [
                                            _build_missing_reply_hint(
                                                interface_path,
                                                is_reactive_vessel_chat,
                                                current_player_message=(
                                                    ctx.get("original_user_message")
                                                    or text
                                                    if is_reactive_vessel_chat
                                                    else None
                                                ),
                                                last_self_line=(
                                                    _last_self_vessel_utterance(
                                                        ctx, str(interface_path or "")
                                                    )
                                                    if is_reactive_vessel_chat
                                                    else None
                                                ),
                                            )
                                        ],
                                        "had_json_errors": False,
                                        "original_text": text,
                                    },
                                )
                                if hasattr(message, "__dict__"):
                                    message.correction_context = correction_context
                                parsed = None
                            else:
                                log_info(
                                    "[message_chain] Actions executed successfully - loop interrupted"
                                )
                                return ACTIONS_EXECUTED

                    except Exception as e:
                        # Log with the full traceback so the crash site is
                        # visible in the logs (a bare str(e) hides WHERE the
                        # exception was raised, which made UnboundLocalErrors in
                        # this block very hard to locate).
                        log_error(
                            f"[message_chain] Failed to run actions: {type(e).__name__}: {e}",
                            e,
                        )
                        # On a hard exception during action execution we treat it as a
                        # technical failure. If nothing has run yet, propagate an LLM
                        # failure so the interface can show a fallback message. If some
                        # actions already succeeded we simply report ACTIONS_EXECUTED so
                        # the user isn't spammed with unrelated error texts.
                        if not actions_executed_during_loop:
                            failure_reason = (
                                f"Action execution exception: {type(e).__name__}: {e}"
                            )
                            try:
                                await send_llm_fallback_message(
                                    bot, message, failure_reason, context=ctx
                                )
                            except Exception:
                                pass
                            return LLM_FAILED
                        else:
                            return ACTIONS_EXECUTED

        # Not parsed. Only attempt correction for LLM-origin messages.
        # Non-LLM messages (interface/system source) are blocked without correction.
        # Plain text from the LLM is treated as a correctable error: the corrector
        # will request a valid JSON response from the model.
        if source == "llm":
            log_debug(
                "[message_chain] LLM returned non-JSON output; activating corrector to request JSON format"
            )
        if source != "llm" and not getattr(message, "from_cortex", False):
            log_debug("[message_chain] Non-LLM source; no correction needed")
            return BLOCKED

        # Additional check: if this is already a system error message from corrector, don't re-correct
        if "system_message" in (text or "") and "error" in (text or ""):
            log_debug(
                "[message_chain] Detected system error message from corrector; preventing re-correction loop"
            )
            return BLOCKED

        # Check if the LLM returned a clear server-side error (not fixable by correction).
        # In these cases the corrector would also fail since it hits the same engine, so
        # skip directly to the fallback to avoid wasting minutes of retry time.
        _SERVER_ERROR_MARKERS = (
            "logprobs not supported",
            "internal server error",
            "service unavailable",
            "bad gateway",
            "gateway timeout",
            "503 service unavailable",
            "502 bad gateway",
            "504 gateway timeout",
        )
        if any(marker in (text or "").lower() for marker in _SERVER_ERROR_MARKERS):
            log_warning(
                "[message_chain] LLM returned server-side error; skipping correction loop "
                f"(error: {(text or '')[:200]})"
            )
            if not actions_executed_during_loop:
                await send_llm_fallback_message(
                    bot, message, f"LLM engine error: {(text or '')[:200]}", context=ctx
                )
                return LLM_FAILED
            return ACTIONS_EXECUTED

        attempt += 1
        if attempt > max_retries:
            failure_reason = (
                f"Exhausted {max_retries} correction attempts for invalid JSON"
            )
            if not actions_executed_during_loop:
                log_warning(
                    f"[message_chain] {failure_reason}; sending fallback message"
                )
                await send_llm_fallback_message(
                    bot, message, failure_reason, context=ctx
                )
                return LLM_FAILED
            else:
                log_warning(
                    f"[message_chain] {failure_reason} but {len(actions or [])} action(s) already executed; skipping fallback"
                )
                if is_reactive_vessel_chat and not vessel_reply_delivered:
                    await _deliver_vessel_fallback_reply(
                        bot, message, ctx, failure_reason
                    )
                return ACTIONS_EXECUTED

        if text in tried_texts:
            failure_reason = "Correction loop detected - same text repeated"
            if not actions_executed_during_loop:
                log_warning(
                    f"[message_chain] {failure_reason}; sending fallback message"
                )
                await send_llm_fallback_message(
                    bot, message, failure_reason, context=ctx
                )
                return LLM_FAILED
            else:
                log_warning(
                    f"[message_chain] {failure_reason} but some actions already executed; skipping fallback"
                )
                if is_reactive_vessel_chat and not vessel_reply_delivered:
                    await _deliver_vessel_fallback_reply(
                        bot, message, ctx, failure_reason
                    )
                return ACTIONS_EXECUTED

        tried_texts.add(text)

        # Request correction from LLM via transport-layer middleware
        try:
            log_info(
                f"[message_chain] Calling corrector middleware for attempt={attempt}..."
            )
            log_debug(
                f"[message_chain] CORRECTOR CONTEXT: interface_path={ctx.get('interface_path')}, chat_id={getattr(message, 'chat_id', None)}, text_preview={text[:200] if text else ''}"
            )
            corrected = await run_corrector_middleware(
                text,
                bot=bot,
                context=ctx,
                chat_id=getattr(message, "chat_id", None),
                thread_id=getattr(message, "thread_id", None),
            )
            log_info(
                f"[message_chain] Corrector returned: corrected={corrected is not None} len={len(corrected) if corrected else 0}"
            )
        except Exception as e:
            failure_reason = f"Corrector middleware exception: {str(e)}"
            log_error(f"[message_chain] {failure_reason}")
            import traceback

            log_error(f"[message_chain] Traceback: {traceback.format_exc()}")
            # Only send fallback if no actions have succeeded; otherwise suppress
            # in accordance with the new policy that at least one delivered
            # message prevents an LLM failure notification.
            if not actions_executed_during_loop:
                await send_llm_fallback_message(
                    bot, message, failure_reason, context=ctx
                )
                return LLM_FAILED
            else:
                log_warning(
                    "[message_chain] Corrector exception but actions already executed; skipping fallback"
                )
                if is_reactive_vessel_chat and not vessel_reply_delivered:
                    await _deliver_vessel_fallback_reply(
                        bot, message, ctx, failure_reason
                    )
                return ACTIONS_EXECUTED

        if not corrected:
            log_debug("[message_chain] Corrector returned no correction this attempt")
            # Check if we're approaching max retries to avoid infinite waiting
            if attempt >= max_retries - 1:
                failure_reason = (
                    f"Corrector returned no correction after {attempt} attempts"
                )
                if not actions_executed_during_loop:
                    log_warning(
                        f"[message_chain] {failure_reason}; sending fallback message"
                    )
                    await send_llm_fallback_message(
                        bot, message, failure_reason, context=ctx
                    )
                    return LLM_FAILED
                else:
                    log_warning(
                        f"[message_chain] {failure_reason} but some actions already executed; skipping fallback"
                    )
                    if is_reactive_vessel_chat and not vessel_reply_delivered:
                        await _deliver_vessel_fallback_reply(
                            bot, message, ctx, failure_reason
                        )
                    return ACTIONS_EXECUTED
            # On no-correction, loop and let retry counter enforce blocking
            await asyncio.sleep(0.5)
            continue

        # Accept corrected text and treat it as LLM-origin for next iteration
        log_debug("[message_chain] Received corrected text from LLM; retrying parse")
        text = corrected
        source = "llm"
        ctx["original_text"] = text  # Track in context instead of on message object
        ctx["from_cortex"] = True  # Track in context instead of on message object
        # loop continues


# Backwards-compatible alias
handle_message = handle_incoming_message
