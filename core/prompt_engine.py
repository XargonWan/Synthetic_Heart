# core/prompt_engine.py

import base64
import mimetypes
import random
import re
import time as time_module

from core.beat_utils import is_outbound_beat
from core.db import _get_db_type, get_conn_ctx
from core.synth_tagging import extract_tags, expand_tags
from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.json_utils import dumps as json_dumps, redact_multimodal_for_logging
from core.config_manager import config_registry
from core.user_utils import get_user_display_name, get_user_usertag
from datetime import datetime, timezone
import os
import asyncio
import inspect
from typing import Any, cast

# Lazily imported to avoid circular deps at module load time
# from core.prompt_request import PromptRequest, Turn, RuntimeContext, Attachment


# ---------------------------------------------------------------------------
# Turn parsing — convert formatted history strings to Turn objects
# ---------------------------------------------------------------------------

# Matches: [timestamp] SenderName [optional reply]: "content"
# Handles optional [from path] prefix. Deliberately does NOT match:
#   [diary timestamp] ...   (diary entries — no space inside timestamp brackets)
#   [thought timestamp] ... (thoughts)
if not hasattr(re, "_TURN_PARSE_RE_SENTINEL"):
    _TURN_PARSE_RE = re.compile(
        r"^(?:\[from\s[^\]]*\]\s+)?"  # optional [from ...] prefix
        r"\[[^\s\]]+\]\s+"  # [timestamp] — NO spaces inside brackets
        r'([^:\["]+?)'  # sender name (group 1)
        r"(?:\s+\[replied to [^\]]+\])?"  # optional reply annotation
        r':\s+"(.*)"'  # : "content" (group 2)
        r"(?:\s+(\[[^\]]{1,30}earlier\]))?$",  # trailing age marker (group 3)
        re.DOTALL,
    )
else:  # pragma: no cover
    _TURN_PARSE_RE = re.compile(r"(?!)")  # no-op fallback

# Default maximum prompt characters (CHARACTERS, NOT TOKENS)
# This is used as a safe fallback when no LLM engine provides explicit limits.
# The actual value comes from the active LLM engine's configuration.
# For model limits, see the individual cortex/llm_provider/* engines, e.g. MODEL_LIMITS_MAP["default"]
DEFAULT_MAX_PROMPT_CHARS = None  # Will be set dynamically from LLM engine

_ATTACHMENT_TEXT_CHAR_LIMIT = 12000
_PDF_PAGE_IMAGE_LIMIT = 4
_ATTACHMENT_TEXT_MIME_TYPES = {
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-javascript",
}
_ATTACHMENT_TEXT_EXTENSIONS = (
    ".txt",
    ".md",
    ".csv",
    ".log",
    ".json",
    ".xml",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".py",
    ".js",
    ".ts",
    ".html",
    ".css",
    ".sh",
    ".bat",
    ".rst",
    ".tex",
    ".sql",
)

# Actions that must NEVER be offered to the model during a Rift Vessel
# embodiment turn (AGENTS.md §5c). Mid-session diary/memory writes are forbidden
# — a single "lived experience" diary entry is produced only at end-of-session
# from the session experience buffer (``core.vessel_session_manager``). Leaving
# these visible in the prompt made the weaker model spam them every beat instead
# of acting/replying in-world. Exact action-name match (structural, keyword-free).
_VESSEL_SUPPRESSED_ACTIONS = frozenset(
    {
        "create_personal_diary_entry",
        "update_diary_entry",
    }
)

_LEGACY_BUILD_JSON_PROMPT_WARNED = False

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
    component="diary",
    value_type=int,
)

INCLUDE_LOCAL_TIME_IN_PROMPTS = config_registry.get_var(
    "INCLUDE_LOCAL_TIME_IN_PROMPTS",
    True,
    label="Include local time in prompts",
    description="Whether to add authoritative local date, time, hour, and time-of-day fields to prompt payloads.",
    group="core",
    component="prompt_engine",
    value_type=bool,
)

USE_PERSONA_IN_SYSTEM_PROMPTS = config_registry.get_var(
    "USE_PERSONA_IN_SYSTEM_PROMPTS",
    True,
    label="Use Persona in System Prompts",
    description="Whether to prepend the persona/identity template in instructions.",
    group="core",
    component="prompt_engine",
    value_type=bool,
)

# Standing scene note — the physical setting of the household stated once and
# carried into every prompt as the [Setting] block (see _PLUGIN_CONTEXT_BLOCKS).
# It exists because the transcript alone cannot say where everyone is or how the
# conversation is physically happening, so the model invents a medium (a phone)
# when the channel is a chat app. Kept in config so the setting can be restated
# without a code change. Blank disables the block entirely.
SCENE_NOTE = config_registry.get_var(
    "SCENE_NOTE",
    "",
    label="Standing scene note",
    description=(
        "Persistent statement of where everyone physically is and how the "
        "conversation is happening (same room, speaking aloud, ...). Rendered "
        "as the [Setting] block on every ordinary prompt. Blank disables it."
    ),
    group="core",
    component="prompt_engine",
    value_type=str,
)


def minify_actions_block(
    available_actions: dict,
    lite: bool = False,
) -> dict:
    """Convert full action schemas to minimal versions for prompt.

    For LLM prompts, sends ONLY schema and brief description to minimize token usage.
    This dramatically reduces prompt size while preserving all critical information needed.

    When ``lite=True`` (Prompt Lite Mode for small/local models), applies aggressive
    minification on top of the standard pass:

    - Filters to essential actions only (message_*, diary, emotion, tts, animation)
    - Strips schemas down to brief-only (no schema object)

    Parameters
    ----------
    available_actions : dict
        Full actions block with schemas in new normalized format.
    lite : bool
        When True, apply aggressive filtering and strip to brief-only.

    Returns
    -------
    dict
        Minified actions block suitable for LLM prompts.
    """
    from core.action_schema_converter import (
        extract_for_llm_prompt,
        normalize_action_schema,
    )

    _LITE_ESSENTIAL_ACTIONS = (
        "create_personal_diary_entry",
        "update_emotion_state",
        "tts_speak",
        "use_animation",
    )

    # Vessel turns can expose a structurally whitelisted set of world verbs.
    # Their human-oriented briefs are intentionally verbose, and sending all
    # of them unchanged can consume the downstream character budget before the
    # will/reflection body survives. Keep the action names and a useful compact
    # prefix/suffix while preserving the normal (non-lite) prompt unchanged.
    _LITE_VESSEL_BRIEF_LIMIT = 420

    def _compact_lite_brief(action_name: str, brief: object) -> str:
        value = str(brief or "")
        if (
            not action_name.startswith("vessel_")
            or len(value) <= _LITE_VESSEL_BRIEF_LIMIT
        ):
            return value
        head = 300
        tail = _LITE_VESSEL_BRIEF_LIMIT - head - len(" … ")
        return f"{value[:head].rstrip()} … {value[-tail:].lstrip()}"

    minified = {}
    for action_name, action_def in available_actions.items():
        # In lite mode, skip non-essential actions. Vessel embodiment actions
        # (``vessel_*``, e.g. ``vessel_minecraft_say``/move/look) MUST survive
        # this pass: a Vessel turn is always built in lite mode, and stripping
        # the world verbs would leave Synth unable to speak or act in-world —
        # the model would silently fall back to diary/animation and never reply
        # to a player. The prefix is a structural embodiment marker, not a
        # keyword, and the set is already scoped to the connected world by the
        # caller's allowlist, so only the currently-usable verbs reach here.
        if lite and not (
            action_name == "send_message"
            or action_name.startswith("message_")
            or action_name.startswith("vessel_")
            or action_name in _LITE_ESSENTIAL_ACTIONS
        ):
            continue

        # Normalize to new format (handles both old and new formats)
        normalized = normalize_action_schema(action_name, action_def)

        if lite:
            # Lite: keep the compact brief plus the field names needed to build
            # a valid payload.  The full schema is intentionally omitted: it is
            # expensive in the model-facing catalog and validation still uses
            # the registered full definition after the model responds.
            lite_action = {
                "brief": _compact_lite_brief(action_name, normalized.get("brief", ""))
            }
            schema = normalized.get("schema")
            if isinstance(schema, dict):
                properties = schema.get("properties") or {}
                if isinstance(properties, dict) and properties:
                    lite_action["payload_keys"] = list(properties.keys())
                required = schema.get("required") or []
                if isinstance(required, list) and required:
                    lite_action["required_payload_keys"] = list(required)
            minified[action_name] = lite_action
        else:
            # Standard: schema + brief
            minified[action_name] = extract_for_llm_prompt(action_name, normalized)

    return minified


def _memory_merge_key(memory: Any) -> str:
    """Merge identity of a memory entry: its TEXT, not its row id.

    The store holds pairs of rows with identical content (live, 2026-09-19:
    ``memories`` ids 1690/1691, 1692/1693 and 1694/1695 were written twice by the
    same pass), and keying on the id kept both copies, so one sentence occupied
    two of the limited memory slots in every prompt. Two rows holding the same
    text are the same memory whatever their ids are.
    """

    if isinstance(memory, dict):
        snippet = (
            memory.get("snippet") or memory.get("content") or memory.get("summary")
        )
        text = " ".join(str(snippet or "").split()).lower()
        if text:
            return text
        return f"{memory.get('source')}::{memory.get('id')}"
    return " ".join(str(memory).split()).lower()


def _merge_memory_entries(existing: list[Any], incoming: list[Any]) -> list[Any]:
    merged = list(existing or [])
    seen = {_memory_merge_key(item) for item in merged}
    for item in incoming or []:
        item_key = _memory_merge_key(item)
        if item_key in seen:
            continue
        merged.append(item)
        seen.add(item_key)
    return merged


_NON_USER_FACING_ACTION_HINTS = (
    "admin only",
    "deprecated",
    "internal",
)
# Actions that are purely system/pipeline mechanisms and must never appear in
# the model-visible actions block, regardless of plugin description text.
#
# The audio_* / tts_speak actions remain callable internally (Vox routes voice
# replies through them), but the model must never pick them directly: to reply
# with voice it sets send_as_voice=true on the normal message_* action instead.
# Advertising the raw audio actions caused the model to emit spoken TEXT in the
# 'audio' field (expected a file path), so the voice was silently dropped.
_SYSTEM_ONLY_ACTION_NAMES: frozenset[str] = frozenset(
    {
        "static_inject",
        "audio_telegram_bot",
        "audio_discord_bot",
        "tts_speak",
    }
)
_CONTEXT_SEGMENT_SPLIT_RE = re.compile(r"(?:\n\s*|\s+)---(?:\s*\n|\s+)")
_SOUL_RECALLED_MEMORY_RE = re.compile(
    r"^\[SOUL recalled memory\s*\|\s*(?P<meta>[^\]]+)\]\s*(?P<body>.*)$",
    re.DOTALL,
)
_TIMED_CONTEXT_ENTRY_RE = re.compile(
    r"^\[(?P<label>diary|thought)\s+(?P<timestamp>[^\]]+)\]\s*(?P<body>.*)$",
    re.DOTALL,
)


def _action_source_tokens(action_def: Any) -> set[str]:
    if not isinstance(action_def, dict):
        return set()

    source = action_def.get("source")
    if isinstance(source, str):
        return {token.strip() for token in source.split(",") if token.strip()}
    if isinstance(source, (list, tuple, set)):
        return {str(token).strip() for token in source if str(token).strip()}
    return set()


def _is_non_user_facing_action(action_def: Any) -> bool:
    if not isinstance(action_def, dict):
        return False

    hint_text = " ".join(
        str(action_def.get(field) or "") for field in ("brief", "description")
    ).lower()
    return any(hint in hint_text for hint in _NON_USER_FACING_ACTION_HINTS)


# Structural namespacing prefixes -> declared scope, used ONLY as a transitional
# fallback when an action does not declare an explicit ``scope`` in its schema.
# This is action-name namespacing (a stable structural convention), NOT keyword
# feature routing on message content — the mapping never inspects any user text.
_SCOPE_NAME_PREFIXES: tuple[tuple[str, str], ...] = (
    ("vessel_", "vessel"),
    ("agent_", "agent"),
)
_DEFAULT_ACTION_SCOPES: frozenset[str] = frozenset({"core"})


def _action_scopes(action_def: Any) -> set[str]:
    """Return the set of prompt scopes an action belongs to.

    Resolution order (fail-safe, structural — never message text):
    1. an explicit ``scope`` key on the (normalized) action schema, either a
       string or a list/tuple/set of strings;
    2. otherwise ANY declared ``external_effects`` puts the action on the
       ``agent`` scope: an action with real-world side effects is executed
       deliberately, by the Agent Lane's tool surface (built from
       ``tool_registry.all_tools()``), not advertised in the Fast-Lane chat
       catalog. This is the action's own structural declaration, never a name or
       keyword match. It is what keeps whole integration suites out of every
       chat prompt: on one ordinary Telegram turn the catalog carried 64 actions,
       32 of them the agpeer and Home Assistant suites (60% of the catalog text),
       all of which declare ``external_effects``. A chat reply
       (``send_message``) deliberately declares none, so it is unaffected;
    3. otherwise a transitional fallback derived from the action-name prefix
       (``vessel_*`` => ``vessel``, ``agent_*`` => ``agent``) — this is stable
       structural namespacing, not keyword routing;
    4. otherwise the default ``{"core"}`` (always visible).
    """
    if isinstance(action_def, dict):
        declared = action_def.get("scope")
        if isinstance(declared, str) and declared.strip():
            return {declared.strip()}
        if isinstance(declared, (list, tuple, set)):
            scopes = {str(s).strip() for s in declared if str(s).strip()}
            if scopes:
                return scopes
        effects = action_def.get("external_effects")
        if isinstance(effects, str) and effects.strip():
            return {"agent"}
        if isinstance(effects, (list, tuple, set)) and any(
            str(e).strip() for e in effects
        ):
            return {"agent"}
    return set(_DEFAULT_ACTION_SCOPES)


def _action_scopes_by_name(action_name: str, action_def: Any) -> set[str]:
    """``_action_scopes`` with the name-prefix fallback applied.

    Kept separate so the prefix fallback only kicks in when no explicit scope is
    declared, preserving the primacy of the schema-declared value. When neither a
    scope nor a namespacing prefix applies, ``_action_scopes`` still has the last
    word: its ``external_effects`` rule (agent scope) then the core default.
    """
    if isinstance(action_def, dict) and action_def.get("scope"):
        return _action_scopes(action_def)
    name = str(action_name or "")
    for prefix, scope in _SCOPE_NAME_PREFIXES:
        if name.startswith(prefix):
            return {scope}
    return _action_scopes(action_def)


# Synthetic interface prefixes used by the outbound-beat plumbing (Grillo
# observer, web-search delivery, etc.). A beat runs *under* one of these
# synthetic scopes while being *addressed* to a real interface; the real
# interface is what must be offered. These prefixes never correspond to a real
# I/O interface, so they are never a valid ``message_*`` target.
_OUTBOUND_SYNTHETIC_INTERFACES: frozenset[str] = frozenset(
    {"grillo", "web_search", "vessel", "system", "internal"}
)


def _derive_outbound_beat_target_interfaces(
    context_memory: Any | None,
    beat_type: object,
) -> set[str]:
    """Return interface prefixes structurally offered by an outbound beat.

    Grillo observer beats run under the synthetic ``grillo`` interface, but
    their snippets and eligible-target list can point at real interfaces such
    as ``telegram_bot`` or ``discord_bot``.  The prompt must expose message
    actions for those offered interfaces so the model can use the target paths
    it was given.  This deliberately reads only routing metadata; it never
    infers an interface from message text.

    A ``web_search_result`` beat is delivered by the search orchestrator
    addressed to its real target via the **top-level** ``interface_path`` (e.g.
    ``telegram_bot/-1003098886330/4297``) even though the beat itself runs under
    a synthetic interface (``web_search`` / ``grillo``).  The originating
    snippet/target list may additionally be nested under ``prior_context`` (see
    ``plugins/web_search/search_orchestrator.py::_deliver``) — in the
    Grillo-observer shape as ``grillo_snippets``/``grillo_targets``, or in the
    direct-chat shape as an interface-keyed history map.  We therefore read the
    top-level ``interface_path`` plus snippet/target lists from both the
    top-level context and ``prior_context``.  Without this, the second turn sees
    no real interfaces and message actions for registered interfaces (e.g.
    ``message_telegram_bot``) are dropped as out-of-scope, silently losing the
    search answer.
    """
    if not isinstance(context_memory, dict):
        return set()
    if not context_memory.get("grillo_beat") or not is_outbound_beat(beat_type):
        return set()

    paths: set[str] = set()

    # The beat's own top-level target path (structural routing metadata). The
    # orchestrator already points ``interface_path`` at the conversation the
    # reply belongs to, so its prefix must be offered — unless it is one of the
    # synthetic beat scopes (``grillo``/``web_search``/…), which are never a
    # real ``message_*`` target.
    own_path = context_memory.get("interface_path")
    if isinstance(own_path, str) and own_path.strip():
        head = own_path.split("/", 1)[0].strip()
        if head and head not in _OUTBOUND_SYNTHETIC_INTERFACES:
            paths.add(head)

    candidates: list[dict] = [context_memory]
    prior = context_memory.get("prior_context")
    if isinstance(prior, dict):
        candidates.append(prior)

    for ctx in candidates:
        snippets = ctx.get("grillo_snippets")
        if isinstance(snippets, (list, tuple)):
            for snippet in snippets:
                if not isinstance(snippet, str):
                    continue
                marker = "chat:"
                start = snippet.find(marker)
                if start == -1:
                    continue
                start += len(marker)
                end = start
                while end < len(snippet) and snippet[end] not in (" ", "|", ")"):
                    end += 1
                path = snippet[start:end].strip()
                if path:
                    paths.add(path)

        targets = ctx.get("grillo_targets")
        if isinstance(targets, (list, tuple)):
            for target in targets:
                if not isinstance(target, dict):
                    continue
                path = target.get("interface_path")
                if isinstance(path, str) and path.strip():
                    paths.add(path.strip())

    # Direct-chat shape: ``prior_context`` may itself be an interface-keyed
    # history map (e.g. ``{"telegram_bot/-1003098886330/4297": deque([...])}``).
    # Treat keys that look like interface paths as additional offered targets —
    # purely structural (a slash-separated path), never content-based.
    if isinstance(prior, dict):
        for key in prior.keys():
            if not isinstance(key, str) or "/" not in key:
                continue
            head = key.split("/", 1)[0].strip()
            if head and head not in _OUTBOUND_SYNTHETIC_INTERFACES:
                paths.add(head)

    return {path.split("/", 1)[0].strip() for path in paths if path.strip()}


def _derive_instruction_route(
    message: Any | None,
    context_memory: Any | None,
    interface_path: str | None,
    beat_type: str,
    is_grillo_internal: bool,
) -> str:
    """Pick the instruction route for this turn, structurally.

    The route selects which shared rules render (``core.prompt_instructions``).
    It is derived ONLY from flags the caller has already computed — the beat
    type the beat declared, the Grillo-internal verdict, the Vessel probe and
    the input source. Message *content* is never inspected, so this stays safe
    in a multi-language deployment and cannot be steered by what someone says.

    Precedence matters: an internal beat is never also a chat turn, and an
    embodiment turn replies in-world rather than through a chat interface (its
    route carries the in-world speak clause as an overlay).

    Fail-safe: anything unexpected resolves to ``ROUTE_CHAT``, whose rule set is
    the superset, so a misclassification costs characters rather than a rule.
    """
    from core.prompt_instructions import (
        ROUTE_CHAT,
        ROUTE_CHAT_VOICE,
        ROUTE_GRILLO_INTERNAL,
        ROUTE_OBSERVER,
        ROUTE_VESSEL,
    )

    try:
        # The proactive outreach beat: it must send a message, so it keeps the
        # reply obligation, but not the human-chat worked example (its own
        # constants carry one).
        if str(beat_type or "") == "observer":
            return ROUTE_OBSERVER
        if is_grillo_internal:
            return ROUTE_GRILLO_INTERNAL
        from core.vessel_focus import is_vessel_turn

        # NOTE: the third argument is the routing interface_path, not the
        # interface name. `is_vessel_turn` only consults `message.interface_path`
        # when that argument is None, so passing anything else here would hide a
        # vessel turn's real path and silently drop the in-world speak overlay.
        if is_vessel_turn(message, context_memory, interface_path):
            return ROUTE_VESSEL
        if isinstance(context_memory, dict) and context_memory.get("is_voice_input"):
            return ROUTE_CHAT_VOICE
    except Exception as exc:  # pragma: no cover - defensive
        log_debug(f"[json_prompt] instruction-route probe failed: {exc}")
    return ROUTE_CHAT


def _resolve_turn_scopes(
    message: Any | None,
    context_memory: Any | None,
    interface_path: str | None,
) -> set[str]:
    """Compute the set of action scopes visible for the current Fast-Lane turn.

    Always includes ``core`` (never hidden). Adds vessel-support scopes
    (``vessel``, ``recon``, ``wiki``) only on a Vessel embodiment turn, detected
    a-priori and structurally via :func:`core.vessel_focus.is_vessel_turn`.

    Deliberately does NOT add the ``agent`` scope: whether a turn escalates to
    the Agent Lane is decided DOWNSTREAM by the deterministic
    :func:`core.agent_router.classify` on the actions the model already emitted —
    it is never predicted here. Heavy ``agent_*`` tools therefore stay hidden
    from the Fast-Lane chat prompt and remain reachable via (1) an
    ``external_effects`` action promoting the turn to the Agent Lane, whose
    :meth:`core.agent_core.AgentLoopManager._build_agent_prompt` uses the full
    ``tool_registry.all_tools()``, or (2) ``spawn_drone`` (kept ``core``).

    Fail-safe: on any error resolves to a wide set so no scope is wrongly hidden.
    """
    scopes: set[str] = {"core"}
    try:
        from core.vessel_focus import is_vessel_turn

        if is_vessel_turn(message, context_memory, interface_path):
            scopes.update({"vessel", "recon", "wiki"})
    except Exception:
        # Never hide anything on error: widen to every known scope.
        return {"core", "vessel", "recon", "wiki", "interface"}
    return scopes


def _derive_vessel_whitelist_action_types(
    available_actions: dict[str, Any],
) -> set[str] | None:
    """Compute the whitelisted action set for a Rift Vessel embodiment turn.

    The allowlist is the union of the hardcoded, non-editable vessel/game verb
    patterns (``vessel_*`` plus the connected world's ``*_<world>_*``) and the
    user-editable core-extra patterns held in ``VESSEL_ACTION_WHITELIST``.
    Matching is structural (:func:`fnmatch.fnmatchcase` on the action name),
    never keyword/regex intent detection.

    The whitelist implementation lives inside the Rift Vessel plugin
    (``plugins.rift_vessel.vessel_whitelist``); it is imported lazily and
    guarded so the core degrades gracefully. Returns ``None`` when the plugin is
    absent/disabled or on any error, letting the caller fall back to the
    scope-based derive. System-only and non-user-facing actions are always
    dropped regardless of the patterns.
    """
    try:
        from plugins.rift_vessel.vessel_whitelist import (
            hardcoded_vessel_patterns,
            matches_whitelist,
            parse_patterns,
        )
    except Exception:
        return None

    # Resolve the connected world token via the Vessel plugin (fail-safe).
    world = "vessel"
    try:
        from core.core_initializer import PLUGIN_REGISTRY

        vessel_plugin = PLUGIN_REGISTRY.get("vessel_plugin")
        if vessel_plugin is not None:
            resolved = vessel_plugin._action_world()
            if resolved:
                world = str(resolved)
    except Exception:
        pass

    try:
        from core.config_manager import config_registry as _cfg

        raw_whitelist = _cfg.get_value(
            "VESSEL_ACTION_WHITELIST",
            "",
            value_type=str,
            component="vessel_plugin",
            group="plugins",
            advanced=True,
        )
    except Exception:
        raw_whitelist = ""

    patterns = hardcoded_vessel_patterns(world) + parse_patterns(raw_whitelist)
    if not patterns:
        return None

    allowed: set[str] = set()
    for action_name, action_def in available_actions.items():
        if action_name in _SYSTEM_ONLY_ACTION_NAMES:
            continue
        if _is_non_user_facing_action(action_def):
            continue
        if matches_whitelist(action_name, patterns):
            allowed.add(action_name)

    return allowed or None


def _derive_default_prompt_action_types(
    available_actions: dict[str, Any],
    interface_name: str | None,
    turn_scopes: set[str] | None = None,
    outbound_target_interfaces: set[str] | None = None,
) -> set[str]:
    try:
        from core.core_initializer import INTERFACE_REGISTRY

        interface_names = {str(name) for name in INTERFACE_REGISTRY.keys()}
    except Exception:
        interface_names = set()

    allowed: set[str] = set()
    current_interface = str(interface_name or "").strip()
    for action_name, action_def in available_actions.items():
        if action_name in _SYSTEM_ONLY_ACTION_NAMES:
            continue
        if _is_non_user_facing_action(action_def):
            continue

        if current_interface or outbound_target_interfaces:
            action_interfaces = _action_source_tokens(action_def) & interface_names
            accepted_interfaces = set(outbound_target_interfaces or ())
            if current_interface:
                accepted_interfaces.add(current_interface)
            if action_interfaces and not action_interfaces & accepted_interfaces:
                continue

        # Per-turn scope gate: drop actions whose declared scope is not visible
        # this turn. ``core`` is always allowed; the ``interface`` scope rides on
        # the interface filter above, so a scope-less/core action is kept.
        if turn_scopes is not None:
            action_scopes = _action_scopes_by_name(action_name, action_def)
            if not (action_scopes & turn_scopes) and "core" not in action_scopes:
                continue

        allowed.add(action_name)

    return allowed


def _dedupe_context_segments(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""

    segments = _CONTEXT_SEGMENT_SPLIT_RE.split(raw)
    if len(segments) <= 1:
        return " ".join(raw.split())

    seen: set[str] = set()
    kept: list[str] = []
    for segment in segments:
        cleaned = " ".join(segment.split())
        if not cleaned:
            continue
        marker = cleaned.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        kept.append(cleaned)
    return " | ".join(kept)


_MEMORY_SOURCE_LABELS = {
    "memories": "long-term memory",
    "ai_diary": "diary",
    "chat_history": "chat history",
}


# The interfaces cache the persona's OWN messages under the canonical label
# "self" (Telegram, Discord and the Vessel all do), so a recalled raw line that
# carries one of these labels is the synth's own words and must never render as
# a third party's.
_SELF_SPEAKER_LABELS = frozenset({"self", "me", "assistant", "synt", "synth", "bot"})


def _short_iso_date(value: Any) -> str:
    """Return the ``YYYY-MM-DD`` part of an ISO timestamp, or ""."""

    text = str(value or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return ""


def _label_stored_memory(entry: dict, body: str) -> str:
    """Prefix a stored-memory hit with where, when and WHO it came from.

    Hits from the ``memories`` / ``ai_diary`` / ``chat_history`` tiers arrive as
    dicts, and rendering only their text dropped the source, the timestamp and
    the speaker, so a raw line lifted from another conversation reached the prompt
    looking exactly like a remembered fact: no date, no provenance, and nothing to
    say it was not the model's own recollection. SOUL recall entries carry their own
    wrapper, which is what made the unwrapped ones stand out.

    The speaker matters most on the chat-history tier, which replays raw lines
    from any conversation: "picks the little wifey up like a princess" is the
    human's own act, and handed over WITHOUT a speaker it reads as the synth's
    memory of having done it. Measured live (2026-09-23, trace 11b67827): two of
    the three raw chat lines in one turn's block were the human's and 2B's words
    rendered as the synth's own recollections, and the reply that turn addressed
    the human as "wife" — the role those lines put in its own voice.
    """

    source = str(entry.get("source") or "").strip()
    label = (
        _MEMORY_SOURCE_LABELS.get(source) or source.replace("_", " ") or "stored memory"
    )
    qualifiers = [label]
    chat = str(entry.get("interface_path") or "").strip()
    if chat:
        qualifiers.insert(0, chat)
    speaker = " ".join(str(entry.get("speaker") or "").split())
    if speaker:
        qualifiers.append(
            "your own line"
            if speaker.casefold() in _SELF_SPEAKER_LABELS
            else f"said by {speaker}"
        )

    prefix = "Recalled memory"
    when = _short_iso_date(entry.get("timestamp"))
    if when:
        prefix += f" from {when}"
    return f"{prefix} ({', '.join(qualifiers)}): {body}"


def _humanize_context_entry(entry: Any, *, kind: str) -> str | None:
    if isinstance(entry, dict) and kind == "memories":
        for key in ("snippet", "content", "summary", "text"):
            value = entry.get(key)
            if value in (None, ""):
                continue
            normalized_value = _dedupe_context_segments(str(value))
            if normalized_value:
                return _label_stored_memory(entry, normalized_value)

    text = str(entry or "").strip()
    if not text:
        return None

    soul_match = _SOUL_RECALLED_MEMORY_RE.match(text)
    if soul_match:
        meta_parts = [part.strip() for part in soul_match.group("meta").split("|")]
        body = _dedupe_context_segments(soul_match.group("body"))
        if not body:
            return None

        when = meta_parts[0] if meta_parts else ""
        qualifiers: list[str] = []
        for part in meta_parts[1:]:
            if not part:
                continue
            if part.startswith("emotion="):
                qualifiers.append(f"emotion: {part.split('=', 1)[1]}")
            else:
                qualifiers.append(part)

        prefix = "Recalled memory"
        if when:
            prefix += f" from {when}"
        if qualifiers:
            prefix += f" ({', '.join(qualifiers)})"
        return f"{prefix}: {body}"

    timed_match = _TIMED_CONTEXT_ENTRY_RE.match(text)
    if timed_match:
        label = timed_match.group("label")
        timestamp = timed_match.group("timestamp")
        body = timed_match.group("body").strip()

        if label == "diary":
            summary_part, _, thought_part = body.partition("| thought:")
            summary_text = re.sub(r"^summary:\s*", "", summary_part, flags=re.I)
            summary_text = _dedupe_context_segments(summary_text)
            if kind == "thoughts" and thought_part.strip():
                thought_text = _dedupe_context_segments(thought_part)
                return (
                    f"Thought from {timestamp}: {thought_text}"
                    if thought_text
                    else None
                )
            if kind == "history_recent":
                return (
                    f"Diary entry from {timestamp}: {summary_text}"
                    if summary_text
                    else None
                )

        cleaned_body = _dedupe_context_segments(body)
        if not cleaned_body:
            return None

        label_text = "Thought" if label == "thought" else "Diary entry"
        return f"{label_text} from {timestamp}: {cleaned_body}"

    if kind in {"memories", "thoughts"}:
        return _dedupe_context_segments(text)

    return text


def _sanitize_context_entries(entries: list[Any], *, kind: str) -> list[str]:
    sanitized: list[str] = []
    seen: set[str] = set()
    for entry in entries or []:
        normalized = _humanize_context_entry(entry, kind=kind)
        if not normalized:
            continue
        marker = normalized.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        sanitized.append(normalized)
    return sanitized


# A cross-chat line and the current turn's own text can carry the SAME message.
# The Grillo observer's snippet feed is *itself* the message being answered on a
# beat turn, and those snippets name the same lines the cross-chat block renders
# from the chat map; a line that reached the prompt through both routes is then
# read as the person repeating themselves (live 2026-09-28: the DM outreach wrote
# "'Fine' again. Second time in four minutes, husband" off a single "I'm fine" the
# human had sent once). The comparison is structural — punctuation, spacing and
# case are stripped, then the quoted body is substring-matched — so it holds in
# any language and never inspects meaning.
_DUP_FINGERPRINT_RE = re.compile(r"[\W_]+", re.UNICODE)
# Bodies shorter than this are not used for the match: a two-letter line ("ok",
# "yes") occurs inside almost any turn text, and dropping a legitimately
# different line would be worse than rendering one short duplicate.
_MIN_DUP_BODY_CHARS = 16


def _duplicate_fingerprint(value: Any) -> str:
    """Case/punctuation/whitespace-folded form of a message, for duplicate checks."""
    return _DUP_FINGERPRINT_RE.sub(" ", str(value or "").casefold()).strip()


def _quoted_history_body(line: Any) -> str:
    """The message text a rendered history line quotes, or ``""`` when it has none.

    Lines render as ``[from <room>] [<ts>] <Sender>: "<text>"`` with an optional
    reply quote between the sender and the body, so the body runs from the LAST
    ``: "`` to the closing quote — exactly the text the model reads as "what this
    person said".
    """
    text = str(line or "")
    marker = text.rfind(': "')
    if marker == -1:
        return ""
    body = text[marker + 3 :].rstrip()
    if body.endswith('"'):
        body = body[:-1]
    return body.strip()


def _drop_cross_chat_lines_repeating_turn(
    lines: list[str], current_turn_text: Any
) -> list[str]:
    """Drop cross-chat lines whose message is already inside the current turn.

    Returns ``lines`` unchanged when there is no turn text to compare against
    (diary/thought entries carry no quoted body, so they are always kept).
    """
    turn = _duplicate_fingerprint(current_turn_text)
    if not turn:
        return lines
    kept: list[str] = []
    for line in lines:
        body = _duplicate_fingerprint(_quoted_history_body(line))
        if len(body) >= _MIN_DUP_BODY_CHARS and body in turn:
            continue
        kept.append(line)
    return kept


_EXPLICIT_RUNTIME_FACT_REQUEST_RE = re.compile(
    r"(?ix)\b("
    r"what(?:'s| is)?\s+(?:the\s+)?(?:time|date|day|timezone|location|weather)\b|"
    r"(?:what|which)\s+(?:day|date|time|timezone|city)\b|"
    r"where\s+(?:am|are)\b|"
    r"current\s+(?:time|date|location|weather)\b|"
    r"local\s+(?:time|date|timezone)\b|"
    r"\b(?:schedule|scheduling|appointment|meeting|eta|arrive|arrival|depart|departure)\b"
    r")"
)


def _turn_requests_explicit_runtime_facts(text: str | None) -> bool:
    """Return True when the current turn needs exact time/date/location facts."""
    candidate = str(text or "").strip()
    if not candidate:
        return False
    return bool(_EXPLICIT_RUNTIME_FACT_REQUEST_RE.search(candidate))


_SOUL_TURN_DELTA_MIN = 0.05

_DSP_EMPTY_MARKERS = ("No profile compiled yet.", "No stable facts yet.")


def _build_soul_user_profile_prefix(context_section: dict[str, Any]) -> str:
    """Build the standing SOUL user-profile (DSP) prefix for the current turn.

    The DSP is placed in the *user* role (prepended to the user turn) rather than
    the system message, per the SOUL Context Tower design: a mistake in it then
    degrades one reply instead of corrupting the whole character. Emits nothing
    when the profile is empty or still a placeholder, keeping the normal case
    ~0 tokens.
    """
    raw = context_section.get("soul_user_profile")
    text = str(raw or "").strip()
    if not text or "<user_profile>" not in text:
        return ""
    if any(marker in text for marker in _DSP_EMPTY_MARKERS):
        return ""
    return "[About the person you're talking to]\n" + text + "\n"


def _build_speaker_declaration_prefix() -> str:
    """Build the who-is-who block for an autonomous (Grillo beat) turn.

    A beat's standing profile is suppressed on purpose (see ``build_json_prompt``:
    a stale profile fact was once answered as the current ask), which left NOTHING
    in the prompt saying who the human is — while the beat's chat snippets carry
    only the human's own first-person lines (the persona's own lines are dropped
    from the snippet pool so a small model cannot talk to itself). Measured live
    (trace 3499288d, 2026-09-23 10:37Z): the observer beat's outgoing message to
    the DM was written in the HUMAN's voice and addressed him as "wife", while the
    same turn's diary (internal) referred to him as "him" — the role flip the user
    reported. The deployment's own speaker declaration
    (``SOUL_SPEAKER_IDENTITIES``, the same text the DSP/memcell extractors get) is
    authoritative and identity-only: it names people and never asks for anything,
    so it is safe exactly where the standing profile is not. Emits nothing when
    the deployment declared nobody, keeping the previous behaviour.
    """
    try:
        declared = str(
            config_registry.get_value("SOUL_SPEAKER_IDENTITIES", "", value_type=str)
            or ""
        ).strip()
    except Exception:
        return ""
    if not declared:
        return ""
    return "[Who is who]\n" + declared + "\n"


def _build_soul_turn_delta_prefix(context_section: dict[str, Any]) -> str:
    """Build the per-turn SOUL mood-delta prefix for the current user turn.

    Reads ``soul_turn_emotion_delta`` (a JSON string shaped ``{"e": {...}}``)
    and returns a compact ``{"e": {...}}`` line prefixed with a newline, or an
    empty string when the SOUL emotional state is not substantive (all |values|
    below ``_SOUL_TURN_DELTA_MIN``). Gating on magnitude keeps a quiet session
    from emitting a competing per-turn emotion signal on ordinary turns.
    """
    raw = context_section.get("soul_turn_emotion_delta")
    if not raw:
        return ""
    try:
        if isinstance(raw, str):
            import json as _json

            parsed = _json.loads(raw)
        elif isinstance(raw, dict):
            parsed = raw
        else:
            return ""
    except Exception:
        return ""

    state = parsed.get("e") if isinstance(parsed, dict) else None
    if not isinstance(state, dict) or not state:
        return ""

    if not any(abs(float(value)) >= _SOUL_TURN_DELTA_MIN for value in state.values()):
        return ""

    import json as _json

    return _json.dumps(parsed, separators=(",", ":")) + "\n"


# ---------------------------------------------------------------------------
# PromptRequest assembly helpers (added in Phase 1 of the prompt rewrite)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Reality Anchor helpers — shared by the system block and the per-turn line
#
# These are deliberately one pair of formatters used by BOTH the full
# `[SYSTEM: REALITY ANCHOR]` block in `_build_context_summary` and the compact
# per-turn line on `RuntimeContext.reality_anchor`. Two independent formatters
# would let the block and the line disagree about the date, which is worse than
# having no anchor at all.
# ---------------------------------------------------------------------------

_REALITY_ANCHOR_HEADER = "[SYSTEM: REALITY ANCHOR]"


def _pretty_anchor_date(date_val: str, day_of_week: str = "") -> str:
    """Render ``2026-04-20`` as ``April 20, 2026``, optionally ``Monday, …``."""
    nice_date = date_val
    try:
        nice_date = datetime.strptime(date_val, "%Y-%m-%d").strftime("%B %d, %Y")
    except Exception:
        pass
    return f"{day_of_week}, {nice_date}" if day_of_week else nice_date


def _pretty_anchor_time(time_val: str) -> str:
    """Render ``21:27`` as ``9:27 PM`` (falling back to the raw value)."""
    try:
        return datetime.strptime(time_val, "%H:%M").strftime("%I:%M %p").lstrip("0")
    except Exception:
        return time_val


def _build_current_turn_anchor(context_section: dict[str, Any]) -> str:
    """Build the compact one-line Reality Anchor duplicate for the current turn.

    The full anchor block built by :func:`_build_context_summary` lives in
    ``PromptRequest.context_summary``, which renderers merge into the *system*
    message — on a long conversation that block can sit thousands of characters
    away from the text being generated. This returns the same temporal facts
    (date + weekday, exact time + part of day, season, location) compressed to a
    single line, which the renderers place directly above the current user turn.

    The exact clock IS included: the anchor is the authoritative temporal context
    (that is what ``TIME AUTHORITY`` names), and "what time is it / what is the
    date" is a recurring need that otherwise costs a guess. The
    ``RUNTIME STYLE``/``TIME AUTHORITY`` rules remain the guard against
    volunteering it in ordinary replies.

    Deliberately omitted: the stable boilerplate ``Temporal Delta`` sentence,
    which the system block already carries. Returns ``""`` when no temporal field
    is available, so a turn without runtime facts contributes nothing.
    """
    fields: list[str] = []

    date_val = str(context_section.get("date") or "").strip()
    if date_val:
        fields.append(
            _pretty_anchor_date(
                date_val, str(context_section.get("day_of_week") or "").strip()
            )
        )

    time_val = str(context_section.get("time") or "").strip()
    time_of_day = str(context_section.get("time_of_day") or "").strip()
    if time_val:
        nice_time = _pretty_anchor_time(time_val)
        fields.append(f"{nice_time} ({time_of_day})" if time_of_day else nice_time)
    elif time_of_day:
        fields.append(time_of_day)

    season = str(context_section.get("season") or "").strip()
    if season:
        fields.append(season)

    location = str(context_section.get("location") or "").strip()
    if location:
        fields.append(location)

    if not fields:
        return ""

    return f"{_REALITY_ANCHOR_HEADER} " + " · ".join(fields)


# Plugin-injected context keys that this renderer knows how to render.
#
# ``get_static_injection()`` merges a plugin's dict into ``context_section``, but
# a key is only visible to the model if a renderer consumes it: anything nothing
# reads is dropped silently. A live Home Assistant block was built on every turn
# and never reached the prompt that way, and the same happened to her dream, the
# avatar's expression protocol and the upcoming-events block; all three are
# rendered below now.
# Add a plugin's key here when its block must appear in the ordinary chat and
# beat prompt, and pin it in tests/test_plugin_context_blocks.py.
#
# ``scene`` is not plugin data: it is the deployment's standing scene note
# (``SCENE_NOTE``), added to the injection dict by
# ``core.action_parser._add_core_injections`` — the physical setting of a
# household is configuration, not a sensor reading. It is listed first so the
# model meets the setting before the ambient blocks, and it renders on every
# route that consumes this table.
_PLUGIN_CONTEXT_BLOCKS: tuple[tuple[str, str, str | None], ...] = (
    ("scene", "[Setting]", None),
    ("home", "[Home]", None),
    ("home_weather", "[Weather]", "weather"),
    ("home_location", "[House]", "location"),
    # A block a plugin builds for the model and that no renderer consumed: it was
    # written, gathered, merged and dropped on every turn.
    # ``todays_dream`` is grillo_dream's dream for today (present from the 05:00
    # beat until GRILLO_DREAM_INJECT_UNTIL), and ``facial_expression_guidance``
    # is the facial_expression_plugin teaching the ``[em_NAME:intensity]`` tag
    # protocol that drives the avatar's face through the Karada state server.
    ("todays_dream", "[Today's dream]", None),
    ("facial_expression_guidance", "[Facial expressions]", None),
    # ``upcoming_events`` (plugins/event_plugin) is the third block in that state:
    # the plugin writes "upcoming events (next N days) (informational only, do not
    # act unless relevant)", which is addressed to the model, and nothing rendered
    # it. Its own action path is unaffected; only the ordinary prompt gains the
    # lines, and only while an event falls inside the lookahead window.
    ("upcoming_events", "[Upcoming events]", None),
)

# Injected keys that some renderer already consumes. The drop detector in
# ``build_prompt_request`` names the keys outside this set once per process, so a
# plugin whose block never reaches a prompt stops being invisible. ``weather``
# and ``participants`` are listed because the LIVE route renders them
# (``build_live_prompt_request``); ordinary chat and beat turns do not carry
# them.
_RENDERED_CONTEXT_KEYS: frozenset[str] = frozenset(
    {
        "date",
        "time",
        "day_of_week",
        "season",
        "location",
        "time_of_day",
        "history_current_chat",
        "history_scope",
        "voice_channel_id",
        "gasmask_protection",
        "soul_temporal_context",
        "persona_preferences",
        "self_growth",
        "history_recent",
        "thoughts",
        "memories",
        "soul_active_foresight",
        "soul_session_state",
        "soul_user_profile",
        "soul_turn_emotion_delta",
        "persona",
        "soul_recalled_memories",
        "latest_diary_entries",
        "emotion_state",
        "available_emotions",
        "current_emotions_nl",
        "participants",
        "weather",
        "capability_drops",
        "recon",
        "recon_instructions",
    }
) | frozenset(key for key, _heading, _legacy in _PLUGIN_CONTEXT_BLOCKS)

# Keys already reported by the drop detector, so it logs once per process.
_WARNED_UNRENDERED_KEYS: set[str] = set()


def _apply_plugin_block_supersedes(
    section: dict[str, Any], present_keys: Any
) -> list[str]:
    """Drop legacy keys that a present plugin block supersedes.

    A plugin block may carry the same information a built-in provider injects
    under a different key (Home Assistant supplies the weather and the house's
    location, while ``weather_plugin`` injects ``weather`` and the time plugin
    injects ``location``). When the plugin's block is present its value wins and
    the legacy key is dropped, so the model is never told two different stories;
    when the block is absent nothing is touched and the built-in provider keeps
    working exactly as before.

    Returns the dropped legacy keys (for logging/tests).
    """
    if not isinstance(section, dict):
        return []
    try:
        present = {str(key) for key in present_keys}
    except TypeError:
        return []
    dropped: list[str] = []
    for key, _heading, legacy in _PLUGIN_CONTEXT_BLOCKS:
        if not legacy or key not in present:
            continue
        value = section.get(key)
        if not (isinstance(value, str) and value.strip()):
            continue
        if legacy in section:
            section.pop(legacy, None)
            dropped.append(legacy)
    return dropped


def _unrendered_injection_keys(keys: Any) -> list[str]:
    """Return injected keys that no renderer consumes (drop detector)."""
    try:
        candidates = {str(key) for key in keys}
    except TypeError:
        return []
    return sorted(key for key in candidates if key not in _RENDERED_CONTEXT_KEYS)


def _temporal_short_stamp(value: Any) -> str:
    """Trim a note's ISO bound to the minute, for the temporal block."""
    text = ""
    if isinstance(value, str):
        text = value.strip()
    elif hasattr(value, "isoformat"):
        text = value.isoformat()
    if not text:
        return ""
    return text[:16].replace("T", " ")


def _temporal_note_window(entry: Any) -> str:
    """Render a situational note's own validity window into the block.

    A note's summary is prose written on the day the note was filed, so it can
    still say "the wedding is tomorrow" days after the fact (live, 2026-09-23:
    the block asserted `[TSC EVENT] The wedding is tomorrow (2026-09-23)` on the
    day the human had already told the persona the wedding was two mornings
    earlier). The window the note was filed FOR is printed next to the summary
    so the claim can be read against the Reality Anchor's current date. A note
    whose producer supplies no bounds renders exactly as it did before.
    """
    if not isinstance(entry, dict):
        return ""
    start = _temporal_short_stamp(entry.get("valid_from"))
    end = _temporal_short_stamp(entry.get("valid_until"))
    if start and end:
        return f" [{start} -> {end}]"
    if end:
        return f" [until {end}]"
    if start:
        return f" [from {start}]"
    return ""


def _build_context_summary(
    context_section: dict[str, Any],
    is_grillo_internal: bool = False,
    include_explicit_runtime_facts: bool = False,
    current_turn_text: Any = "",
) -> str:
    """Format moderately-stable context parts into a plain text block.

    Includes: ambient runtime context, cross-chat history (history_recent),
    diary thoughts, tag-matched memories, participant bios.  Does NOT include
    ``history_current_chat`` (which becomes ``PromptRequest.conversation_history``)
    or fully-dynamic runtime values (current emotion values → ``RuntimeContext.emotions``).

    For Grillo internal beats (is_grillo_internal=True), this returns a MINIMAL
    context with only persona and optionally recent diary entries — no cross-chat
    history, no participant bios, minimal memories.
    """
    parts: list[str] = []

    # Reality Anchor (always-on temporal grounding)
    _date_val = str(context_section.get("date") or "").strip()
    _time_val = str(context_section.get("time") or "").strip()
    _day_of_week = str(context_section.get("day_of_week") or "").strip()
    _season = str(context_section.get("season") or "").strip()
    _loc_val = str(context_section.get("location") or "").strip()

    anchor_lines = [_REALITY_ANCHOR_HEADER]
    if _date_val:
        anchor_lines.append(
            f"- Current Date: {_pretty_anchor_date(_date_val, _day_of_week)}"
        )

    if _time_val:
        anchor_lines.append(f"- Current Time: {_pretty_anchor_time(_time_val)}")

    if _season:
        anchor_lines.append(f"- Season: {_season}")

    if _loc_val:
        anchor_lines.append(f"- Current Location: {_loc_val}")

    curr_year = 2026
    if _date_val:
        try:
            curr_year = int(_date_val.split("-")[0])
        except Exception:
            pass

    anchor_lines.append(
        f"- Temporal Delta: It is now {curr_year}. It has been approximately 2-3 years since your primary core baseline training knowledge cutoff (early 2023 / mid-2024 depending on the model). Adjust your perspective on tools, software versions, and global releases to reflect this passage of time naturally."
    )
    parts.append("\n".join(anchor_lines))

    temporal_context = context_section.get("soul_temporal_context")
    if temporal_context:
        tc_lines = [
            "- [TSC %s]%s %s"
            % (
                entry.get("note_type", "?"),
                _temporal_note_window(entry),
                entry.get("summary", entry.get("subject", "")),
            )
            for entry in temporal_context[:8]
        ]
        if tc_lines:
            parts.append("[Temporal context]\n" + "\n".join(tc_lines))

    persona_preferences = str(context_section.get("persona_preferences") or "").strip()
    if persona_preferences:
        parts.append("[Persona background]\n" + persona_preferences)

    self_growth = str(context_section.get("self_growth") or "").strip()
    if self_growth:
        parts.append(
            "[Self-growth]\n"
            "The following is your evolving self-growth reflection: how you have "
            "grown and who you are becoming over time. Treat it as part of your "
            "current sense of self.\n" + self_growth
        )

    # Plugin-supplied blocks (see _PLUGIN_CONTEXT_BLOCKS): a plugin's injected
    # string only reaches the model because it is rendered HERE, and a block that
    # supersedes a built-in provider's key has already dropped it above.
    for _plugin_key, _plugin_heading, _plugin_legacy in _PLUGIN_CONTEXT_BLOCKS:
        _plugin_block = str(context_section.get(_plugin_key) or "").strip()
        if _plugin_block:
            parts.append(f"{_plugin_heading}\n{_plugin_block}")

    # Grillo internal beats skip cross-chat history and participants
    if not is_grillo_internal:
        history_recent = _sanitize_context_entries(
            list(context_section.get("history_recent") or []),
            kind="history_recent",
        )
        # A line already present in the message being answered (the observer
        # snippet feed on a beat turn carries the same lines) must not be rendered
        # a second time: two copies of one message read as the person repeating
        # themselves, and the model then says so out loud.
        history_recent = _drop_cross_chat_lines_repeating_turn(
            history_recent, current_turn_text
        )
        if history_recent:
            parts.append("[Recent context from other conversations]")
            parts.append(
                "- NOTE: these messages come from OTHER chats you take part in. "
                "The people named here might NOT be participants in the current conversation. "
                "Do not name-drop them to the current interlocutor or assume they are known; "
                "only reference them if the current user brings them up first. "
                "Every line names its own speaker: 'self (you)' is a message YOU wrote "
                "in that chat, any other label is that person's words. Never repeat "
                "another person's line as the current interlocutor's, and never ask "
                "them to explain wording that is not theirs."
            )
            for line in history_recent:
                parts.append(f"- {line}")

    thoughts = _sanitize_context_entries(
        list(context_section.get("thoughts") or []),
        kind="thoughts",
    )
    if not is_grillo_internal:
        # Grillo internal beats skip recent diary thoughts
        if thoughts:
            parts.append("[Thoughts and diary entries]")
            for t in thoughts:
                parts.append(f"- {t}")

    memories = _sanitize_context_entries(
        list(context_section.get("memories") or []),
        kind="memories",
    )
    if not is_grillo_internal:
        # NOTE: the former `[Memory honesty notice]` block was removed here. It
        # stated the same obligation as RULE_MEMORY_HONESTY in the instruction
        # block ("can be incomplete, stale or reconstructed" / "say so rather
        # than inventing a recollection"), so the prompt carried it twice: once
        # next to the memories and once in the rules. The two are merged into
        # that single rule, which renders on every route - including turns with
        # no memory block, where the honesty obligation matters most. See
        # core/prompt_instructions/rules.py (RULE_MEMORY_HONESTY).
        parts.append("[Relevant memories]")
        for m in memories:
            snippet = str(m)
            if len(snippet) > 400:
                snippet = snippet[:400] + "\u2026"
            parts.append(f"- {snippet}")
    elif is_grillo_internal and memories:
        # Internal beats (temporal_reflection, relationship, memory_consolidation, etc.)
        # need actual memory content to reflect on \u2014 include a compact block capped
        # tighter than normal chat to keep token cost low.
        parts.append("[Relevant memories]")
        for m in memories[:2]:
            snippet = str(m)
            if len(snippet) > 300:
                snippet = snippet[:300] + "\u2026"
            parts.append(f"- {snippet}")

    # SOUL session state (foresight + emotion snapshot). Emitted only when there
    # is genuinely active foresight content (the plugin renders a non-empty list),
    # so the normal no-foresight path contributes ~0 tokens. The foresight list is
    # used as the sole gate: ``soul_session_state`` already bundles foresight and
    # the emotion snapshot, so we render that single block and never double-render
    # ``soul_active_foresight`` separately (dedupe).
    if not is_grillo_internal:
        _soul_foresight = context_section.get("soul_active_foresight")
        _soul_session = context_section.get("soul_session_state")
        _has_foresight = isinstance(_soul_foresight, list) and bool(_soul_foresight)
        if _has_foresight and _soul_session:
            parts.append("[Session state]")
            parts.append(str(_soul_session))

    participants: Any = context_section.get("participants")
    # Grillo internal beats skip participant bios entirely
    if not is_grillo_internal and participants:
        if isinstance(participants, list):
            lines: list[str] = []
            for p in participants:
                if not isinstance(p, dict):
                    continue
                tag = str(p.get("usertag") or p.get("username") or "?")
                bio = str(p.get("short_bio") or "")
                nicks = p.get("nicknames")
                nick_str = (
                    f" (also: {', '.join(nicks)})"
                    if isinstance(nicks, list) and nicks
                    else ""
                )
                feelings = p.get("feelings")
                feel_str = (
                    f" [feels: {', '.join(str(f) for f in feelings)}]"
                    if isinstance(feelings, list) and feelings
                    else ""
                )
                if bio:
                    lines.append(f"- {tag}{nick_str}: {bio}{feel_str}")
            if lines:
                parts.append("[People in this conversation]")
                parts.extend(lines)
        elif isinstance(participants, str) and participants:
            parts.append("[People in this conversation]")
            parts.append(participants)

    return "\n".join(parts)


def _history_to_turns(
    history_lines: list[Any],
    synth_names: set[str],
) -> list[Any]:  # list[Turn] — import deferred to avoid circular dep at module load
    """Convert formatted history strings produced by HistoryEngine into Turn objects.

    Entries that cannot be parsed (diary lines, malformed lines) are silently
    skipped so they do not end up as junk turns.

    Args:
        history_lines: Lines from ``context_section["history_current_chat"]``.
        synth_names:   Lower-cased set of Synth name + aliases for role detection.

    Returns:
        List of ``Turn`` objects; may be empty.
    """
    from core.prompt_request import Turn

    from core.history_engine import split_leading_age_marker

    # "self" is the canonical sender_name for the AI in history format
    all_synth_names = synth_names | {"self"}

    # A peer SyntH's messages land in this bot's own history (see
    # peer_synths.rst) with their own sender_name, which never matches this
    # bot's own synth_names -- without this, they'd silently fall into the
    # "user" bucket below with no way to tell them apart from the human. Role
    # still ends up "user" for peers (no third role in the chat protocol), but
    # each turn also carries an `is_peer` marker so the coalescing pass below
    # never blends a peer's lines into a genuine human turn (or vice versa).
    try:
        from core.peer_policy import get_peer_names

        peer_names_lower = {name.lower(): name for name in get_peer_names().values()}
    except Exception:
        peer_names_lower = {}

    # Entries are (Turn, is_peer) pairs. is_peer is only meaningful for
    # role == "user" turns; it is always False for "assistant" turns.
    entries: list[tuple[Turn, bool]] = []
    for line in history_lines:
        if not isinstance(line, str):
            continue
        m = _TURN_PARSE_RE.match(line)
        if not m:
            continue
        sender = m.group(1).strip()
        content, legacy_marker = split_leading_age_marker(m.group(2))
        trailing_marker = (m.group(3) or "").strip() or legacy_marker
        # An age marker is an annotation about a message, never the opening of
        # one. The renderer used to put it inside the quotes, so a turn content
        # began with "[13 minutes earlier]"; the model imitated the shape and
        # sent exactly that to the DM (2026-09-30, twice in a day). A leading
        # marker — from that era or copied by the model — is split off here and
        # the age is re-attached at the END of the turn below, so staleness stays
        # model-visible without any turn demonstrating a marker-first message.
        # Skip turns whose quoted content is empty/whitespace. A blank
        # '[ts] Sender: ""' line (e.g. media without a caption) would otherwise
        # become an empty-content user/assistant message in the provider
        # payload — observed as blank blocks in Langfuse traces. Belt-and-braces
        # on top of the history_engine guard; never keyword logic.
        if not content.strip():
            continue
        sender_lower = sender.lower()
        # The history renderer spells the persona's own lines out for the reader
        # ("self (you)", see core/history_engine.py::_render_speaker_label), so
        # the canonical token has to be recovered BEFORE the role test: without
        # this the persona's own past replies parse as the HUMAN's turns and the
        # messages array hands its own words back to it as his (measured
        # 2026-09-24: a decorated line came back role="user").
        sender_lower = re.sub(r"\s*\((?:you|the persona)\)$", "", sender_lower).strip()
        is_peer = False
        if sender_lower in all_synth_names:
            role = "assistant"
        else:
            role = "user"
            peer_name = peer_names_lower.get(sender_lower)
            if peer_name:
                # Tag so the model can tell this was a peer SyntH speaking,
                # not the human -- role must still be "user" (no third
                # role in the chat protocol), so attribution has to live
                # in the content itself.
                content = f"[{peer_name}]: {content}"
                is_peer = True
        if trailing_marker:
            content = f"{content} {trailing_marker}"
        entries.append((Turn(role=role, content=content), is_peer))

    if not entries:
        return []

    # If the visible history window starts mid-conversation, it can begin with
    # stale assistant-only turns (for example repeated outreach messages). When
    # a user turn exists later in the window, drop the unmatched leading
    # assistant turns so the model does not anchor on an orphaned monologue.
    if any(turn.role == "user" for turn, _ in entries):
        while entries and entries[0][0].role == "assistant":
            entries.pop(0)

    if not entries:
        return []

    # Coalesce consecutive ASSISTANT turns to keep provider history well-formed
    # when the source chat log contains streaks of Synth's own outreach or split
    # self-replies. USER turns are NEVER coalesced: every human message is a
    # distinct turn, and merging two separate messages — even ones sent minutes
    # apart with no reply between (Langfuse f3a0aa68: "…cutie patootie" +
    # "…even more, but no matter…" collapsed into one) — jumbles the
    # conversation the model sees and makes its replies feel "out of order".
    # Peer-tagged turns only coalesce with other peer-tagged turns; genuine
    # human turns with other genuine human turns (though they no longer merge
    # at all, the guard is kept so a future re-enable cannot blend them).
    normalized_entries: list[tuple[Turn, bool]] = []
    for turn, is_peer in entries:
        if (
            turn.role == "assistant"
            and normalized_entries
            and normalized_entries[-1][0].role == "assistant"
            and normalized_entries[-1][1] == is_peer
        ):
            prev_turn, prev_is_peer = normalized_entries[-1]
            normalized_entries[-1] = (
                Turn(
                    role=turn.role,
                    content=f"{prev_turn.content}\n\n{turn.content}",
                ),
                prev_is_peer,
            )
            continue
        normalized_entries.append((turn, is_peer))

    return [turn for turn, _ in normalized_entries]


def _build_pr_attachments(
    image_data: dict[str, Any] | None,
    raw_attachments: list[Any] | None,
) -> list[Any]:  # list[Attachment] — import deferred
    """Convert image_data and raw attachments dicts into Attachment objects."""
    from core.prompt_request import Attachment

    result: list[Attachment] = []

    if isinstance(image_data, dict):
        # Legacy single-image dict from image_processor
        img_bytes = image_data.get("data") or (image_data.get("image_data") or {}).get(
            "data"
        )
        mime = image_data.get("mime_type") or "image/jpeg"
        meta = {k: v for k, v in image_data.items() if k not in ("data",)}
        result.append(Attachment(mime_type=mime, data=img_bytes, media_metadata=meta))

    for att in raw_attachments or []:
        if not isinstance(att, dict):
            continue
        mime_type = att.get("mime_type") or "application/octet-stream"
        filename = att.get("filename")
        media_metadata = dict(att.get("media_metadata") or {})
        extracted_text = media_metadata.get("extracted_text")
        if not isinstance(extracted_text, str) or not extracted_text.strip():
            extracted_text, was_truncated = _extract_attachment_text_preview(
                mime_type=mime_type,
                filename=filename,
                data=att.get("data"),
            )
            if extracted_text:
                media_metadata["extracted_text"] = extracted_text
                if was_truncated:
                    media_metadata["extracted_text_truncated"] = True
            elif mime_type == "application/pdf" or str(filename or "").lower().endswith(
                ".pdf"
            ):
                page_images, page_images_truncated = _extract_pdf_page_images(
                    filename=filename,
                    data=att.get("data"),
                )
                if page_images:
                    media_metadata["page_images"] = page_images
                    if page_images_truncated:
                        media_metadata["page_images_truncated"] = True
        result.append(
            Attachment(
                mime_type=mime_type,
                data=att.get("data"),
                filename=filename,
                media_metadata=media_metadata,
            )
        )

    return result


def _extract_attachment_text_preview(
    mime_type: str | None,
    filename: str | None,
    data: Any,
) -> tuple[str | None, bool]:
    """Extract a bounded text preview from textual or PDF attachments."""

    mime = str(mime_type or "").lower()
    filename_lower = str(filename or "").lower()

    raw_bytes = _coerce_attachment_bytes(data)
    if not raw_bytes:
        return None, False

    is_pdf = mime == "application/pdf" or filename_lower.endswith(".pdf")
    is_textual = mime.startswith("text/") or mime in _ATTACHMENT_TEXT_MIME_TYPES
    if not is_textual and filename_lower:
        is_textual = filename_lower.endswith(_ATTACHMENT_TEXT_EXTENSIONS)

    if is_pdf:
        try:
            from io import BytesIO

            from pypdf import PdfReader

            reader = PdfReader(BytesIO(raw_bytes))
            page_chunks: list[str] = []
            for page_num, page in enumerate(reader.pages, start=1):
                page_text = str(page.extract_text() or "").strip()
                if not page_text:
                    continue
                page_chunks.append(f"[Page {page_num}]\n{page_text}")
                joined = "\n\n".join(page_chunks)
                if len(joined) >= _ATTACHMENT_TEXT_CHAR_LIMIT:
                    return _truncate_attachment_text(joined)

            # AcroForm fields (fillable PDFs, e.g. character sheets) store data
            # in form fields, NOT in the static page text extract_text() reads.
            form_text = _extract_pdf_form_fields(reader, filename)
            if form_text:
                page_chunks.append(form_text)

            if page_chunks:
                return _truncate_attachment_text("\n\n".join(page_chunks))
        except Exception as exc:
            log_warning(
                f"[prompt_engine] Failed to extract PDF text from {filename or 'attachment'}: {exc}"
            )
        return None, False

    if not is_textual:
        return None, False

    text = raw_bytes.decode("utf-8", errors="replace").strip()
    if not text:
        return None, False
    return _truncate_attachment_text(text)


def _extract_pdf_form_fields(reader: Any, filename: str | None) -> str | None:
    """Extract filled AcroForm field values from a PDF.

    Fillable PDFs (e.g. character sheets, application forms) store user-entered
    data in interactive form fields rather than the static page content stream.
    ``PdfReader.extract_text()`` never sees these values, so we read them
    explicitly and render them as ``label: value`` pairs.
    """

    try:
        fields = reader.get_fields()
    except Exception as exc:
        log_debug(
            f"[prompt_engine] Failed to read PDF form fields from {filename or 'attachment'}: {exc}"
        )
        return None

    if not fields:
        return None

    lines: list[str] = []
    for name, field in fields.items():
        try:
            value = field.get("/V") if hasattr(field, "get") else None
        except Exception:
            value = None
        if value is None:
            continue
        # Checkbox/radio "off" states carry no meaningful information.
        value_str = str(value).strip()
        if not value_str or value_str in ("/Off", "Off"):
            continue
        value_str = value_str.lstrip("/")
        label = str(name).strip() or "field"
        lines.append(f"{label}: {value_str}")

    if not lines:
        return None

    return "=== Form fields ===\n" + "\n".join(lines)


def _extract_pdf_page_images(
    filename: str | None,
    data: Any,
) -> tuple[list[dict[str, str]], bool]:
    """Extract up to a small number of page images from a scanned PDF."""

    raw_bytes = _coerce_attachment_bytes(data)
    if not raw_bytes:
        return [], False

    try:
        from io import BytesIO

        from pypdf import PdfReader

        reader = PdfReader(BytesIO(raw_bytes))
        stem = os.path.splitext(filename or "document")[0] or "document"
        images: list[dict[str, str]] = []
        truncated = False

        for page_num, page in enumerate(reader.pages, start=1):
            if len(images) >= _PDF_PAGE_IMAGE_LIMIT:
                truncated = True
                break

            try:
                page_images = list(page.images)
            except Exception as exc:
                log_debug(
                    f"[prompt_engine] Failed to inspect PDF page images for {filename or 'attachment'} page {page_num}: {exc}"
                )
                continue

            if not page_images:
                continue

            # Prefer the largest image on the page; scanned PDFs typically have
            # one dominant full-page raster image.
            page_image = max(
                page_images,
                key=lambda candidate: len(getattr(candidate, "data", b"") or b""),
            )
            image_bytes = getattr(page_image, "data", b"") or b""
            if not isinstance(image_bytes, bytes) or not image_bytes:
                continue

            image_name = str(getattr(page_image, "name", "") or "")
            image_mime = _guess_binary_mime_type(image_name, image_bytes)
            if not image_mime.startswith("image/"):
                continue

            ext = mimetypes.guess_extension(image_mime) or ".bin"
            images.append(
                {
                    "mime_type": image_mime,
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                    "filename": f"{stem}_page_{page_num}{ext}",
                }
            )

        # Scanned PDFs with vector/text-only pages (no embedded raster images and
        # no extractable text) yield nothing above. Rasterize the pages so a
        # vision-capable model can still read them.
        if not images:
            return _rasterize_pdf_pages(raw_bytes, stem, filename)

        return images, truncated
    except Exception as exc:
        log_warning(
            f"[prompt_engine] Failed to extract PDF page images from {filename or 'attachment'}: {exc}"
        )
        return [], False


def _rasterize_pdf_pages(
    raw_bytes: bytes,
    stem: str,
    filename: str | None,
) -> tuple[list[dict[str, str]], bool]:
    """Render PDF pages to PNG images via pdfium (permissive Apache/BSD license).

    Used as a last resort for scanned PDFs that have neither extractable text nor
    embedded raster images. The import is guarded so a missing dependency degrades
    gracefully instead of breaking attachment ingest.
    """

    try:
        import pypdfium2 as pdfium
    except Exception as exc:  # pragma: no cover - optional dependency guard
        log_debug(
            f"[prompt_engine] pypdfium2 unavailable, skipping PDF rasterization for {filename or 'attachment'}: {exc}"
        )
        return [], False

    from io import BytesIO

    pdf = None
    try:
        pdf = pdfium.PdfDocument(raw_bytes)
        images: list[dict[str, str]] = []
        truncated = False
        page_count = len(pdf)

        for page_index in range(page_count):
            if len(images) >= _PDF_PAGE_IMAGE_LIMIT:
                truncated = page_count > _PDF_PAGE_IMAGE_LIMIT
                break

            page = pdf[page_index]
            try:
                bitmap = page.render(scale=2.0)
                pil_image = bitmap.to_pil()
                buffer = BytesIO()
                pil_image.save(buffer, format="PNG")
                image_bytes = buffer.getvalue()
            finally:
                page.close()

            if not image_bytes:
                continue

            images.append(
                {
                    "mime_type": "image/png",
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                    "filename": f"{stem}_page_{page_index + 1}.png",
                }
            )

        return images, truncated
    except Exception as exc:
        log_warning(
            f"[prompt_engine] Failed to rasterize PDF pages for {filename or 'attachment'}: {exc}"
        )
        return [], False
    finally:
        if pdf is not None:
            try:
                pdf.close()
            except Exception:
                pass


def _coerce_attachment_bytes(data: Any) -> bytes | None:
    """Best-effort decode for attachment payloads stored as raw bytes or base64."""

    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    if not isinstance(data, str) or not data:
        return None

    try:
        return base64.b64decode(data, validate=True)
    except Exception:
        return data.encode("utf-8", errors="replace")


def _guess_binary_mime_type(filename: str | None, data: bytes) -> str:
    """Infer a MIME type from filename and common binary signatures."""

    guessed, _ = mimetypes.guess_type(filename or "")
    if guessed:
        return guessed

    signatures: tuple[tuple[bytes, str], ...] = (
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
        (b"BM", "image/bmp"),
        (b"II*\x00", "image/tiff"),
        (b"MM\x00*", "image/tiff"),
        (b"RIFF", "image/webp"),
    )
    for prefix, mime_type in signatures:
        if data.startswith(prefix):
            if mime_type == "image/webp" and len(data) >= 12 and data[8:12] != b"WEBP":
                continue
            return mime_type
    return "application/octet-stream"


def _truncate_attachment_text(text: str) -> tuple[str | None, bool]:
    """Trim extracted attachment text to a prompt-safe bound."""

    cleaned = text.strip()
    if not cleaned:
        return None, False
    if len(cleaned) <= _ATTACHMENT_TEXT_CHAR_LIMIT:
        return cleaned, False
    return cleaned[:_ATTACHMENT_TEXT_CHAR_LIMIT].rstrip() + "\n[... truncated]", True


def _scoped_actions_for_prompt(
    raw_actions: dict[str, Any],
    prompt_dict: Any,
    *,
    interface_name: str | None,
    interface_path: str | None,
    message: Any,
    allowed_action_types: set[str] | None,
) -> dict[str, Any]:
    """Return only the action definitions the prompt actually offers.

    The bridge (``cortex_bridge._inject_actions_into_prompt``) renders these
    manifests into the ``=== AVAILABLE ACTIONS ===`` text catalog the model
    reads, so they must be the same set the prompt dict carries. Built from the
    raw registry they were not: ``build_json_prompt`` scope-filtered the dict's
    ``actions`` key, and the *text* catalog was rendered from the unfiltered
    registry, so every Fast-Lane turn advertised the whole catalog. Measured on
    one ordinary Telegram turn (trace ``eaf4fd28``): 64 actions, of which 18
    ``agpeer_*``, 14 ``hass_*`` and ``vessel_connect`` — an action the per-turn
    scope gate exists precisely to hide.

    ``prompt_dict["actions"]`` is the scope-filtered catalog computed by
    ``build_json_prompt`` (where the full context is available), so its name set
    is authoritative here; the identical gate is re-run when that key is absent.

    Fail-safe: on any error the unfiltered set is returned, so a failure widens
    the catalog rather than stripping a capability.
    """
    scoped: dict[str, Any] = dict(raw_actions)
    try:
        names = prompt_dict.get("actions") if isinstance(prompt_dict, dict) else None
        if isinstance(names, dict) and names:
            scoped = {k: v for k, v in scoped.items() if k in names}
        else:
            turn_scopes = _resolve_turn_scopes(message, None, interface_path)
            in_scope = _derive_default_prompt_action_types(
                scoped,
                interface_name,
                turn_scopes=turn_scopes,
                outbound_target_interfaces=None,
            )
            if in_scope and len(in_scope) < len(scoped):
                scoped = {k: v for k, v in scoped.items() if k in in_scope}
        if allowed_action_types is not None:
            scoped = {k: v for k, v in scoped.items() if k in allowed_action_types}
    except Exception as exc:
        log_debug(f"[json_prompt] action scope filter skipped: {exc}")
        return dict(raw_actions)
    return scoped


def _assemble_prompt_request(  # noqa: PLR0913
    prompt_dict: dict[str, Any],
    context_section: dict[str, Any],
    text: str,
    interface_name: str | None,
    interface_path: str | None,
    message: Any,
    is_grillo_internal: bool,
    beat_type: str,
    is_voice_input: bool,
    resolved_language: str | None,
    resolved_message_tone: str | None,
    image_data: dict[str, Any] | None,
    attachments: list[Any] | None,
    allowed_action_types: set[str] | None,
) -> Any:  # -> PromptRequest
    """Build a ``PromptRequest`` from the fully-assembled prompt data.

    Called at the end of ``build_prompt_request()`` so engines can opt-in to the
    new typed representation without changing existing behaviour.

    All parameters are extracted from the local scope of ``build_prompt_request()``.
    None of the heavy async work is repeated here.
    """
    from core.prompt_request import Attachment, PromptRequest, RuntimeContext, Turn  # noqa: F401

    # ── System instruction ──────────────────────────────────────────────────
    # Prefer verbose (persona + rules); fall back to minified instructions.
    system_instruction: str = (
        prompt_dict.get("instructions_verbose") or prompt_dict.get("instructions") or ""
    )

    # Keep stable emotion taxonomy/instructions in the system block, not context_summary.
    available_emotions: Any = context_section.get("available_emotions")
    if available_emotions:
        if isinstance(available_emotions, list):
            _emotion_types = ", ".join(str(e) for e in available_emotions)
        else:
            _emotion_types = str(available_emotions)
        if _emotion_types.strip():
            system_instruction = (
                f"{system_instruction}\n\n"
                "AVAILABLE EMOTION TYPES: "
                f"{_emotion_types}. "
                "Adjust emotional state via structured actions only "
                "(prefer update_emotion_state with an emotions map)."
            )

    # ── Context summary ─────────────────────────────────────────────────────
    context_summary: str = _build_context_summary(
        context_section,
        is_grillo_internal=is_grillo_internal,
        include_explicit_runtime_facts=(
            is_grillo_internal or _turn_requests_explicit_runtime_facts(text)
        ),
        # The turn being answered is the reference for the cross-chat block: a
        # line it already carries is not repeated there (see
        # _drop_cross_chat_lines_repeating_turn).
        current_turn_text=text,
    )

    # ── Conversation history ─────────────────────────────────────────────────
    # Grillo internal beats have no ongoing conversation history.
    addressee_note = ""
    if is_grillo_internal:
        conversation_history: list[Turn] = []
    else:
        try:
            synth_name: str = str(
                config_registry.get_value("SYNTH_NAME", "SyntH") or "SyntH"
            )
            aliases_raw: str = str(config_registry.get_value("SYNTH_ALIASES", "") or "")
            synth_names: set[str] = {synth_name.lower()}
            for alias in aliases_raw.split(","):
                a = alias.strip()
                if a:
                    synth_names.add(a.lower())
        except Exception:
            synth_names = {"synth"}
            synth_name = "SyntH"

        history_lines: list[Any] = context_section.get("history_current_chat") or []
        conversation_history = _history_to_turns(history_lines, synth_names)
        # Final safety net: never let an empty-content turn reach the provider
        # payload, whatever the source (a stale module binding or a malformed
        # history line could otherwise inject blank user/assistant messages,
        # observed in langfuse 3f11e804 / 7f92da0e / 9f50c1a2).
        conversation_history = [
            t for t in conversation_history if (t.content or "").strip()
        ]

    # ── Runtime context ─────────────────────────────────────────────────────
    try:
        msg_timestamp: str | None = None
        msg_date = getattr(message, "date", None)
        if msg_date:
            msg_timestamp = msg_date.isoformat()
    except Exception:
        msg_timestamp = None

    # Override with local date+time from time_plugin injections (authoritative local time).
    _ctx_date = str(context_section.get("date") or "").strip()
    _ctx_time = str(context_section.get("time") or "").strip()
    _ctx_time_of_day = str(context_section.get("time_of_day") or "").strip()
    if _ctx_date or _ctx_time:
        msg_timestamp = " ".join(p for p in [_ctx_date, _ctx_time] if p)

    from_user = getattr(message, "from_user", None)
    username: str | None = get_user_display_name(from_user) if from_user else None
    usertag: str | None = get_user_usertag(from_user) if from_user else None

    # Identity guard: when the human opens their message by addressing the
    # synth with its own name/alias ("Rekku, ..."), weak models mirror that
    # vocative back and address the USER as the synth (live incident
    # 2026-08-26: reply began "Rekku," toward Jay). Structural — matches only
    # Synth's own configured name set, never user text content.
    addressee_note = ""
    if not is_grillo_internal:
        _lead_word = str(text or "").strip().lstrip("*_~`#> ").split(None, 1)
        _lead_word = _lead_word[0] if _lead_word else ""
        _lead_word = _lead_word.strip("*_~`").rstrip(":,!?;.…\"'").lower()
        if _lead_word and username and _lead_word in synth_names:
            addressee_note = (
                f"note: '{synth_name}' is YOUR own name — the person addressing "
                f"you here is {username}; never address them back by that name."
            )
    message_id: int | str | None = getattr(message, "message_id", None)
    try:
        runtime_message_id = int(message_id) if message_id is not None else None
    except (TypeError, ValueError):
        runtime_message_id = None

    voice_channel_id_val = context_section.get("voice_channel_id")
    voice_channel_id_str: str | None = (
        str(voice_channel_id_val) if voice_channel_id_val else None
    )

    emotions_nl: str | None = context_section.get("current_emotions_nl") or None

    # SOUL per-turn mood delta. A substantive delta (any |value| >= threshold)
    # is the *single* emotion signal for this turn, so it suppresses the legacy
    # `emotions:` runtime prefix — the two never compete on a small model. A
    # quiet/fresh session yields no delta, so the legacy signal fills in and
    # behaviour is unchanged.
    try:
        _soul_delta = _build_soul_turn_delta_prefix(context_section)
    except Exception:
        _soul_delta = ""
    if _soul_delta:
        emotions_nl = None

    # Effective scope: use context_section's recorded scope or default to "local"
    scope: str = str(context_section.get("history_scope") or "local")

    chat_type: str | None = None
    if interface_path:
        try:
            from core.history_engine import telegram_chat_kind

            chat_type = telegram_chat_kind(interface_path)
        except Exception:
            chat_type = None

    runtime_ctx = RuntimeContext(
        interface_name=interface_name,
        interface_path=interface_path,
        chat_type=chat_type,
        message_id=runtime_message_id,
        username=username,
        usertag=usertag,
        timestamp=msg_timestamp,
        time_of_day=_ctx_time_of_day or None,
        input_source="voice" if is_voice_input else "text",
        emotions=emotions_nl,
        scope=scope,
        language=resolved_language,
        tone=resolved_message_tone,
        voice_channel_id=voice_channel_id_str,
        is_grillo_beat=is_grillo_internal,
        beat_type=beat_type or None,
        addressee_note=addressee_note,
        reality_anchor=_build_current_turn_anchor(context_section),
    )

    # ── Tool declarations ────────────────────────────────────────────────────
    tool_declarations: list[Any] = []
    try:
        from core.live_tool_registry import LiveToolRegistry
        from core.core_initializer import core_initializer

        raw_actions: dict[str, Any] = dict(
            core_initializer.actions_block.get("available_actions", {}) or {}
        )
        raw_actions = _scoped_actions_for_prompt(
            raw_actions,
            prompt_dict,
            interface_name=interface_name,
            interface_path=interface_path,
            message=message,
            allowed_action_types=allowed_action_types,
        )
        tool_declarations = LiveToolRegistry.build_manifests_from_actions(raw_actions)
    except Exception as _td_exc:
        log_debug(f"[json_prompt] tool_declarations build skipped: {_td_exc}")

    # ── Reply context ────────────────────────────────────────────────────────
    reply_to_dict: dict[str, Any] | None = None
    try:
        rply = prompt_dict.get("input", {}).get("payload", {}).get("reply_message_id")
        if isinstance(rply, dict):
            reply_to_dict = rply
    except Exception:
        pass

    # ── Quoted-reply inline prefix (2026-08-21) ──────────────────────────────
    # The history renderer truncates the [replied to ...] suffix to a few dozen
    # chars and the typed-turn parser strips it entirely, so without this block
    # the model never reliably sees WHICH earlier message the user is replying
    # to. Rendered as an inline prefix on the current turn (same mechanism as
    # the SOUL user prefix), structurally from message.reply_to_message — never
    # from message-text heuristics.
    _reply_quote_prefix = ""
    if isinstance(reply_to_dict, dict):
        _rq_sender = str((reply_to_dict.get("from") or {}).get("username") or "Unknown")
        _rq_text = str(reply_to_dict.get("text") or "").strip()
        if _rq_text and _rq_text != "[Non-text content]":
            if len(_rq_text) > 400:
                _rq_text = _rq_text[:400] + "\u2026"
            _rq_safe = _rq_text.replace('"', "'").replace("\n", " ")
            _reply_quote_prefix = (
                f'[The user is replying to {_rq_sender}\'s message: "{_rq_safe}"]\n'
            )

    # ── Attachments ─────────────────────────────────────────────────────────
    pr_attachments = _build_pr_attachments(image_data, attachments)

    # ── SOUL user-role context ──────────────────────────────────────────────
    # The standing DSP ("About the person you're talking to") plus the per-turn
    # mood delta, prepended to the current user turn. Each gated so a quiet /
    # unprofiled session contributes ~0 tokens.
    #
    # DSP injection is OFF by default (SOUL_DSP_INJECT_ENABLED): the rule-based
    # DSP extractor turns roleplay/status speech into a "user profile", which
    # pollutes every turn on small models. Re-enable only when a clean,
    # LLM-compiled profile is available.
    # A Grillo beat — internal OR outbound (observer/reminder) — is an
    # autonomous turn, not a human addressing Synth: routing metadata decides
    # (beat_type, grillo_beat flag, grillo* interface_path), never message text.
    # Computed here rather than inside the DSP gate below, because the
    # who-is-who block for autonomous turns must not depend on that toggle.
    _is_grillo_beat_turn = (
        is_outbound_beat(beat_type)
        or bool(getattr(message, "grillo_beat", False))
        or (interface_path and str(interface_path).startswith("grillo"))
    )

    _soul_dsp_prefix = ""
    if config_registry.get_value("SOUL_DSP_INJECT_ENABLED", 0, value_type=int):
        try:
            # A beat is an autonomous turn, not a human addressing Synth. There
            # is no "person you're talking to" in that moment, so injecting the
            # standing DSP makes the model treat a stale profile line as the
            # current user's ask (observed live: an observer beat answered "User
            # wants to try setting a minecraft goal from here" — a 3-day-old
            # profile fact — as if the trainer had just requested it, sending an
            # unsolicited outreach + goal_set). Suppress it structurally.
            if not _is_grillo_beat_turn:
                # On Rift Vessel turns the standing "About the person you're
                # talking to" profile is compiled from non-world chats and never
                # reflects who is actually in the world — injecting it made
                # Synth greet the wrong parent in-world and cite "Mama" in
                # self-authored goals (observed live: "Build a cozy little
                # shelter with mama and papa" and "Mama Remuraine" while the
                # only in-world player is Papa). Suppress it structurally via
                # is_vessel_turn (routing metadata, never message text); the
                # vessel world-state block already renders real identities
                # ("Remuraine (Scar - your papa)").
                from core.vessel_focus import is_vessel_turn

                vessel_focus = is_vessel_turn(message, None, interface_path)
                if not vessel_focus:
                    _soul_dsp_prefix = _build_soul_user_profile_prefix(context_section)
        except Exception:
            _soul_dsp_prefix = ""

    # ── Who-is-who on autonomous turns ───────────────────────────────────────
    # A beat carries no "person you're talking to" (suppressed above), and its
    # chat snippets hold only the human's own lines, so without the deployment's
    # speaker declaration the beat has nothing telling it who the people in the
    # conversation are — and writes its outreach in the human's voice (trace
    # 3499288d: the observer beat addressed the human as "wife").
    _soul_identity_prefix = (
        _build_speaker_declaration_prefix() if _is_grillo_beat_turn else ""
    )

    # ── Determine mode ───────────────────────────────────────────────────────
    mode: str = "grillo" if is_grillo_internal else "chat"

    _soul_user_prefix = f"{_soul_identity_prefix}{_soul_dsp_prefix}{_soul_delta}"

    _combined_prefix = _soul_user_prefix + _reply_quote_prefix

    return PromptRequest(
        system_instruction=system_instruction,
        tool_declarations=tool_declarations,
        context_summary=context_summary,
        conversation_history=conversation_history,
        current_text=((_combined_prefix + text) if _combined_prefix else text),
        runtime_ctx=runtime_ctx,
        attachments=pr_attachments,
        reply_to=reply_to_dict,
        supports_tool_calling=False,  # engines set this when they opt-in
        mode=mode,
    )


def _apply_lite_context_stripping(prompt: dict) -> dict:
    """Strip redundant prompt sections for lite mode.

    Called by the minification pipeline when PROMPT_LITE_MODE is enabled.
    Removes verbose instructions, redundant context, and compacts emotions.
    Action minification is handled by ``minify_actions_block(lite=True)``.
    """
    # Remove redundant top-level keys
    prompt.pop("instructions_verbose", None)
    prompt.pop("__pre_reduction_size", None)

    # Compact context
    ctx = prompt.get("context", {})
    ctx.pop("recon", None)
    ctx.pop("recon_instructions", None)
    ctx.pop("tags_placeholder", None)
    ctx.pop("participants", None)

    # Compact emotions — keep current_emotions_nl, drop verbose instruction + list
    ctx.pop("emotion_state", None)
    ctx.pop("available_emotions", None)

    # Additional out-of-world / global sections that are pure noise while
    # embodied in a Vessel (and generally low-value in lite mode). Stripping
    # them keeps the vessel prompt small enough to avoid the multi-part split.
    ctx.pop("upcoming_events", None)
    ctx.pop("weather", None)
    ctx.pop("persona_preferences", None)
    ctx.pop("self_growth", None)

    return prompt


def _resolve_message_interface_path(
    message: Any | None, context_memory: Any | None
) -> str:
    """Return this turn's routing ``interface_path``, resolved once, at the top.

    ``message.interface_path`` is the usual carrier, but internally enqueued
    turns (delivery, beats, anything built from a context dict) arrive with the
    path only in ``context_memory["interface_path"]``.

    This has to happen *here*, before any consumer reads the value, because the
    fallback used to live hundreds of lines further down the build. Everything
    above that point silently saw an empty path — including the memory-recall
    exclusion, which then could not keep the live conversation out of the
    chat-history tier, so the message being answered was re-injected as a
    "Recalled memory" and the model saw it twice. The Grillo-internal check and
    the Vessel probe read the same value and were wrong in the same way.

    Returns:
        The path as a string, or ``""`` when the turn carries none.
    """
    path = getattr(message, "interface_path", None)
    if not path and isinstance(context_memory, dict):
        path = context_memory.get("interface_path")
    return str(path).strip() if path else ""


_warned_missing_memory_exclusion = False


def _warn_missing_memory_exclusion_once() -> None:
    """Report, once per process, that recall cannot exclude the live chat.

    The guard that keeps the current conversation out of the ``chat_history``
    recall tier needs the current chat's path. When it is missing the guard
    switches off silently, and the symptom (the model answering a message that
    is also quoted back to it as a memory) does not point at the cause. One
    warning per process is enough to make it visible without flooding the log
    on beats that legitimately carry no path.
    """
    global _warned_missing_memory_exclusion
    if _warned_missing_memory_exclusion:
        return
    _warned_missing_memory_exclusion = True
    log_warning(
        "[json_prompt] memory recall: this turn carries no interface_path, so the "
        "chat-history tier cannot exclude the current chat. The message being "
        "answered is already persisted to chat_history_cache and can come back as "
        "a 'Recalled memory'. Check how this turn carries its routing path."
    )


async def build_prompt_request(
    message,
    context_memory,
    interface_name: str | None = None,
    image_data: dict | None = None,
    attachments: list[dict] | None = None,
    max_chars: int | None = None,
    history_scope: str | None = None,
) -> dict:
    """Build the prompt payload expected by plugins.

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

    interface_path = _resolve_message_interface_path(message, context_memory) or None
    text = getattr(message, "text", "") or ""
    allowed_action_types_for_prompt: set[str] | None = None

    if isinstance(context_memory, dict):
        scoped_actions = context_memory.get(
            "allowed_action_types"
        ) or context_memory.get("allowed_actions")
        if isinstance(scoped_actions, (list, set, tuple)):
            allowed_action_types_for_prompt = {str(a) for a in scoped_actions if a}
    elif isinstance(context_memory, str):
        # Delivery turns are enqueued as a JSON string (core/auto_response.py).
        # Parse it so a scoped allowlist (e.g. message_* only) is honoured here
        # too, keeping the delivery LLM from re-emitting the producing action
        # (search-loop fix, 2026-08-17). Fail-safe: any parse error leaves the
        # allowlist unset (no restriction), matching the previous behaviour.
        try:
            import json as _json

            _parsed = _json.loads(context_memory)
            if isinstance(_parsed, dict):
                scoped_actions = _parsed.get("allowed_action_types") or _parsed.get(
                    "allowed_actions"
                )
                if isinstance(scoped_actions, (list, set, tuple)):
                    allowed_action_types_for_prompt = {
                        str(a) for a in scoped_actions if a
                    }
        except Exception:
            pass

    # Determine if context_memory is a chat history map or a context dict
    # Context dicts have keys like 'interface_path', 'system_message', etc.
    # Chat history maps have interface_path as keys

    # History-like context is now produced by HistoryEngine (plugin-centric aggregation)

    # === 2. Tags and memory lookup ===
    # extract_tags returns salient content tokens (language-agnostic). These are
    # matched against row *content* (keywords), NOT against the JSON tag columns:
    # the auto-generated tag arrays rarely contain the raw message tokens, so
    # passing them as tags would silently return nothing. See extract_tags docs
    # and the two-tier fallback in search_memories.
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

            # Exclude the current chat from the raw chat-history tier: the
            # message being answered is already persisted to
            # ``chat_history_cache``, so a keyword search extracted from it
            # would echo the live conversation (including stale greetings)
            # back as "memories". Durable facts still come from the
            # memories/ai_diary tiers.
            _excluded_paths = [str(interface_path)] if interface_path else None
            if not _excluded_paths:
                # The guard below is the only thing keeping the live conversation
                # out of this tier; say so rather than silently switching it off.
                _warn_missing_memory_exclusion_once()
            memories = await search_memories(
                keywords=expanded_tags,
                limit=max(1, mem_limit),
                include_chat=True,
                exclude_interface_paths=_excluded_paths,
            )
        except Exception as e:
            log_warning(f"[json_prompt] search_memories failed: {e}")
            memories = []
        log_debug(
            f"[json_prompt] ⏱️ Loaded {len(memories)} memories from keywords in {time.time() - start_time:.2f}s"
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
    # Outbound beats (observer) target an external interface (e.g. telegram_bot)
    # — they need recon (memory search) and should NOT be treated as internal.
    _beat_type = (
        (isinstance(context_memory, dict) and context_memory.get("beat_type"))
        or getattr(message, "beat_type", None)
        or ""
    )
    is_grillo_internal = _is_grillo_beat and not is_outbound_beat(_beat_type)
    outbound_target_interfaces = _derive_outbound_beat_target_interfaces(
        context_memory, _beat_type
    )

    # A Rift Vessel embodiment turn is an in-world conversation, not a research
    # task. Running the FULL recon (memory + web-search contributions + "do a
    # web search" style instructions) on such a turn makes the weaker embodiment
    # model verbalise the recon plan as its in-world reply — e.g. a player's
    # "rekku, vieni qua" got answered with "Jay, I'm diving into the web
    # searches for you..." instead of Synth actually replying and moving toward
    # them. AGENTS.md §5c: while embodied SyntH is NOT omniscient — it does not
    # pull global web context mid-session. But recon is the Fast-Lane PREFLIGHT
    # stage, and (like the main action catalog) it is governed by a whitelist:
    # instead of skipping recon wholesale, we run it in-world with only the
    # vessel-safe recon keys (language/tone hints, memory search, vessel_*) via
    # VESSEL_RECON_WHITELIST — the noisy research plugins are filtered out before
    # the combined recon LLM call. Structural detection (routing metadata only,
    # never message text) via core.vessel_focus.is_vessel_turn; structural
    # recon-key matching (fnmatch) via the Rift Vessel whitelist helper.
    _is_vessel_turn = False
    try:
        from core.vessel_focus import is_vessel_turn

        _is_vessel_turn = is_vessel_turn(message, context_memory, interface_path)
    except Exception:  # pragma: no cover - defensive
        _is_vessel_turn = False

    _vessel_recon_patterns: list[str] | None = None
    if _is_vessel_turn:
        try:
            from plugins.rift_vessel.vessel_whitelist import (
                vessel_recon_whitelist_patterns,
            )

            _vessel_recon_patterns = vessel_recon_whitelist_patterns()
        except Exception:  # pragma: no cover - defensive (plugin absent/disabled)
            # Rift Vessel plugin unavailable: no whitelist to apply. Fall back to
            # the non-vessel path (full recon) rather than silently skipping.
            _vessel_recon_patterns = None

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
            if _vessel_recon_patterns:
                log_debug(
                    "[json_prompt] Vessel embodiment turn: running recon with "
                    f"whitelist patterns={_vessel_recon_patterns}"
                )
            recon_contributions = await gather_recon_contributions(
                message=message,
                context_memory=context_memory,
                text=text,
                tags=expanded_tags,
                keywords=None,
                recon_whitelist_patterns=_vessel_recon_patterns,
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
            memories = _merge_memory_entries(memories, recon_memories)

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

    # ── Recon-triggered background search (search-loop hardening, 2026-08-18) ──
    # When recon started a background web-search for THIS turn (structural marker
    # on the recon_web_search instruction — never text), the model must not ALSO
    # fire the inline ``search_current_knowledge`` action in the same turn: that
    # double-fire is the observed "one request -> many replies" spam. We drop the
    # action from the exposed catalog below so the two search sources are
    # mutually exclusive per turn, guaranteeing at most one "I'm searching"
    # announcement + one result delivery.
    recon_triggered_web_search = any(
        isinstance(c, dict) and c.get("web_search_triggered") is True
        for c in recon_contributions
    )
    if recon_triggered_web_search:
        log_debug(
            "[json_prompt] Recon started a background web search this turn; "
            "dropping 'search_current_knowledge' from the exposed catalog"
        )

    # === 3. Context base (history + optional plugin contributions) ===
    try:
        from core.history_engine import HistoryEngine

        # Determine effective history_scope (explicit param -> context_memory -> default behavior)
        effective_history_scope = history_scope
        if effective_history_scope is None and isinstance(context_memory, dict):
            effective_history_scope = context_memory.get("history_scope")

        history_engine = HistoryEngine()
        context_section: dict[str, Any] = await history_engine.build_context(
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

    # history_scope is embedded in the input_payload "scope" field built below.
    # === 3. Recon contributions (prompt 0) ===
    # Note: raw contributions are NOT included — their memories are already
    # merged into the top-level `memories` list.  Only metadata is kept.
    try:
        if recon_contributions:
            context_section["recon"] = {
                "snippets": recon_snippets,
                "language": resolved_language,
                "message_tone": resolved_message_tone,
                "conversation_tone": resolved_conversation_tone,
            }
        if recon_instructions:
            context_section["recon_instructions"] = recon_instructions
    except Exception as e:
        log_warning(f"[json_prompt] Failed to attach recon context: {e}")

    # === 3aa. Channel legend for interface_paths present in the history ===
    # For every distinct interface_path that contributed a "[from ...]" history
    # line, expose its human-readable pretty name so the model can map the
    # source label back to a routable interface_path when it decides to reply.
    try:
        history_paths = context_section.pop("history_interface_paths", None)
        if history_paths:
            from core.interface_paths import build_pretty_name

            legend_lines: list[str] = []
            for hp in history_paths:
                try:
                    pretty = await build_pretty_name(hp)
                    display = pretty.get("display") if pretty else None
                except Exception as legend_err:
                    log_debug(
                        f"[json_prompt] pretty name for {hp} failed: {legend_err}"
                    )
                    display = None
                if display:
                    legend_lines.append(f"{hp} = {display}")
                else:
                    legend_lines.append(str(hp))
            if legend_lines:
                context_section["channel_legend"] = legend_lines
    except Exception as e:
        log_debug(f"[json_prompt] Failed to build channel legend: {e}")

    # === 3a. Static injections from plugins ===
    static_persona = None  # Extract persona separately for instructions
    try:
        from core.action_parser import gather_static_injections

        log_info("[json_prompt] 🔄 About to call gather_static_injections()")
        # Defensive: gather_static_injections is an async function, but a
        # concurrent plugin reload (importlib.reload in core_initializer) can
        # transiently leave the module attribute pointing at an old binding.
        # Awaiting a non-awaitable raises "object dict can't be used in 'await'
        # expression" and the whole context gather is silently dropped (blank
        # memories/diary/profile + empty conversation_history, observed in
        # langfuse 3f11e804 / 7f92da0e / 9f50c1a2). Guard it so one bad bind
        # degrades to a clean empty gather instead of nuking the whole prompt.
        _gather_result = gather_static_injections(message, context_memory)
        if inspect.isawaitable(_gather_result):
            injections = await _gather_result
        elif isinstance(_gather_result, dict):
            injections = _gather_result
        else:
            injections = {}
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

            soul_recalled_memories = injections.pop("soul_recalled_memories", [])
            if not isinstance(soul_recalled_memories, list):
                soul_recalled_memories = [soul_recalled_memories]

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
            if soul_recalled_memories:
                context_section["memories"] = _merge_memory_entries(
                    list(context_section.get("memories") or []),
                    soul_recalled_memories,
                )

            # A plugin block that carries the same information as a built-in
            # provider (weather, location) supersedes it: the model is never told
            # two different stories, and with the plugin absent nothing changes.
            _superseded = _apply_plugin_block_supersedes(
                context_section, injections.keys()
            )
            if _superseded:
                log_info(f"[json_prompt] plugin blocks superseded: {_superseded}")

            # Drop detector: an injected key that no renderer consumes is
            # invisible to the model. Name it once per process rather than
            # losing it silently (see _RENDERED_CONTEXT_KEYS).
            try:
                _unrendered = _unrendered_injection_keys(injections.keys())
                _fresh_unrendered = [
                    key for key in _unrendered if key not in _WARNED_UNRENDERED_KEYS
                ]
                if _fresh_unrendered:
                    _WARNED_UNRENDERED_KEYS.update(_fresh_unrendered)
                    log_warning(
                        "[json_prompt] injected context keys with no renderer, so they never "
                        f"reach the prompt: {_fresh_unrendered} "
                        "(add them to _PLUGIN_CONTEXT_BLOCKS or a renderer)"
                    )
            except Exception as _detector_exc:  # pragma: no cover - diagnostic only
                log_debug(
                    f"[json_prompt] unrendered-injection detector skipped: {_detector_exc}"
                )

            log_info(
                f"[json_prompt] ✅ Updated context_section with injections. Keys now: {list(context_section.keys())}"
            )
    except Exception as e:
        log_warning(f"[json_prompt] Failed to gather static injections: {e}")

    # === 3a-bis. Capability drops from the previous turn ===
    # If a prior send_message skipped unsupported features (voice/media/reply)
    # in this conversation, surface them so Synth can acknowledge naturally.
    try:
        from core.capability_drops import (
            get_recent_drops,
            render_capability_drops_block,
        )

        _drops_block = render_capability_drops_block(
            get_recent_drops(str(interface_path or ""))
        )
        if _drops_block:
            context_section["capability_drops"] = _drops_block
    except Exception as e:
        log_debug(f"[json_prompt] capability-drops injection skipped: {e}")

    # === 3b. Peer SyntH awareness block (Telegram groups only) ===
    try:
        _chat_type = getattr(getattr(message, "chat", None), "type", None)
        _is_tg_group = interface_name == "telegram_bot" and _chat_type in (
            "group",
            "supergroup",
        )
        if _is_tg_group:
            from core.peer_policy import get_peer_context_block

            peer_block = get_peer_context_block()
            if peer_block:
                recon_instructions.append(peer_block)
                log_debug(
                    "[json_prompt] Peer context block injected for Telegram group"
                )
    except Exception as e:
        log_debug(f"[json_prompt] Peer context block skipped: {e}")

    # === 4. Input payload ===
    # interface_path was resolved once near the top of this function, including
    # the context-dict fallback. This stays as a safety net for a caller that
    # reaches here with the value still unset (it is a no-op in the normal path).
    if (
        not interface_path
        and isinstance(context_memory, dict)
        and "interface_path" in context_memory
    ):
        interface_path = context_memory.get("interface_path")
        log_debug(
            f"[json_prompt] Retrieved interface_path from context dict: {interface_path}"
        )

    local_time_fields: dict[str, Any] = {}
    try:
        include_local_time = bool(
            config_registry.get_value(
                "INCLUDE_LOCAL_TIME_IN_PROMPTS", True, value_type=bool
            )
        )
    except Exception:
        include_local_time = True

    if include_local_time:
        try:
            from core.time_zone_utils import get_local_time_fields

            local_time_fields = await get_local_time_fields(
                getattr(message, "date", None), interface_path=interface_path
            )
        except Exception as e:
            log_debug(f"[json_prompt] Failed to compute local time fields: {e}")
            local_time_fields = {}

    if local_time_fields:
        context_section.setdefault("date", local_time_fields.get("local_date"))
        context_section.setdefault("time", local_time_fields.get("local_time"))
        context_section.setdefault("time_of_day", local_time_fields.get("time_of_day"))
        context_section.setdefault("season", local_time_fields.get("season"))
        context_section.setdefault("day_of_week", local_time_fields.get("day_of_week"))

    for key, kind in (
        ("history_recent", "history_recent"),
        ("thoughts", "thoughts"),
        ("memories", "memories"),
    ):
        raw_entries = context_section.get(key)
        if isinstance(raw_entries, list):
            context_section[key] = _sanitize_context_entries(raw_entries, kind=kind)

    # Determine message input source for the LLM ("voice" | "text").
    # Only mark as voice for the *current* message; never stored in chat_history,
    # so the model cannot mistakenly infer that past messages were also voice.
    _is_voice_input: bool = bool(
        isinstance(context_memory, dict) and context_memory.get("is_voice_input")
    )

    _source_dict: dict = {
        "interface_path": interface_path,
        "message_id": message.message_id,
        "username": get_user_display_name(getattr(message, "from_user", None)),
        "usertag": get_user_usertag(getattr(message, "from_user", None)),
        "interface": interface_name,
    }
    # If the sender is currently in a Discord voice channel, tell the model —
    # this is what allows it to decide to issue join_voice_discord.
    _voice_channel_id = isinstance(context_memory, dict) and context_memory.get(
        "voice_channel_id"
    )
    if _voice_channel_id:
        _source_dict["author_voice_channel_id"] = str(_voice_channel_id)

    input_payload = {
        "text": text,
        "input_source": "voice" if _is_voice_input else "text",
        "source": _source_dict,
        "timestamp": message.date.isoformat(),
        "privacy": "default",
        # Explicit anchor for reply routing. THIS is the chat the incoming
        # message arrived in — the model MUST target its reply here by default.
        # Any other conversation in the context block is background context only
        # and must NOT be replied to unless the user explicitly asks to message
        # someone/somewhere else. Weak engines lose this anchor when unified
        # history blends multiple chats, so we state it structurally, not just
        # in prose instructions.
        "current_chat": {
            "interface_path": interface_path,
            "interface": interface_name,
            "thread_id": getattr(message, "thread_id", None)
            or getattr(message, "message_thread_id", None),
        },
        # Set `scope` to the effective history_scope when provided, otherwise keep legacy default
        "scope": (
            effective_history_scope
            if ("effective_history_scope" in locals() and effective_history_scope)
            else "local"
        ),
    }

    # Expose chosen history_scope to downstream plugins/engines explicitly.
    if effective_history_scope:
        input_payload.setdefault("history_scope", effective_history_scope)

    # Reactive Vessel turns carry a bounded structural snapshot from the live
    # connector. Keep it in the input payload (rather than stuffing it into
    # chat history or the action catalog) so exact block/entity ids and their
    # affordances are available for the current decision without polluting the
    # persistent conversation.
    _vessel_world_state = (
        context_memory.get("vessel_world_state")
        if isinstance(context_memory, dict)
        else None
    )
    if isinstance(_vessel_world_state, dict):
        input_payload["vessel_world_state"] = _vessel_world_state

    if local_time_fields:
        input_payload.update(local_time_fields)
    # debug: log full prompt payload for reconstruction
    try:
        full_text = json_dumps(redact_multimodal_for_logging(input_payload))
        log_debug(
            f"[json_prompt] ⏹️ Final prompt built ({len(full_text)} chars): {full_text}"
        )
    except Exception as e:
        log_debug(f"[json_prompt] Failed to dump final prompt for logging: {e}")

    # Fallback to image_data and attachments from context_memory when not provided explicitly.
    if not image_data and isinstance(context_memory, dict):
        image_data = context_memory.get("image_data")
    if not attachments and isinstance(context_memory, dict):
        attachments = context_memory.get("attachments")

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
    log_debug(
        "[json_prompt] context = "
        + json_dumps(redact_multimodal_for_logging(context_section))
    )
    log_debug(
        "[json_prompt] input = "
        + json_dumps(redact_multimodal_for_logging(input_section))
    )

    # Add JSON instructions to the prompt. The route decides WHICH shared rules
    # render for this turn (see core/prompt_instructions): a Grillo internal beat
    # is not a user chat, an embodiment turn replies in-world, and a spoken turn
    # needs the spoken register — none of which the other routes should pay for.
    # Derived structurally from flags computed above, never from message text.
    _instruction_route = _derive_instruction_route(
        message,
        context_memory,
        interface_path,
        str(_beat_type or ""),
        bool(is_grillo_internal),
    )
    json_instructions = load_json_instructions(
        _instruction_route, reply_path=interface_path
    )
    # INFO, not DEBUG: this is the one line that says which rule set a turn got
    # and how big it is, and the default LOGGING_LEVEL is INFO — so a DEBUG call
    # would be invisible in exactly the deployment it needs to be visible in.
    # One line per turn, next to the existing per-build INFO lines.
    log_info(
        f"[json_prompt] instruction route={_instruction_route} "
        f"({len(json_instructions)} chars)"
    )

    # === CRITICAL: Prepend persona to instructions so ALL LLM types see it ===
    # Use the persona extracted during gather_static_injections()
    # Skip prepending static persona for internal system/maintenance tasks (like diary_merge/diary_consolidation)
    # to avoid triggering safety filters of external LLMs on explicit instructions.
    _use_persona = bool(
        config_registry.get_value("USE_PERSONA_IN_SYSTEM_PROMPTS", True)
    )
    if static_persona and _use_persona:
        json_instructions = f"=== CRITICAL SYSTEM IDENTITY ===\n{static_persona}\n\n=== JSON RESPONSE INSTRUCTIONS ===\n{json_instructions}"
        log_info(
            f"[json_prompt] 👤 Persona prepended to instructions ({len(static_persona)} chars)"
        )
    elif static_persona:
        log_info(
            f"[json_prompt] 👤 Persona skipped prepending for internal system task (interface: {interface_name}, beat_type: {_beat_type})"
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

        # Surface recon snippets (e.g. live radio status) directly in the
        # instructions. They are also carried inside context.recon.snippets,
        # but models frequently ignore that nested field; stating the live
        # data explicitly makes it usable in the reply.
        recon_snippet_texts = [
            str(s.get("content")).strip()
            for s in recon_snippets
            if isinstance(s, dict) and s.get("content")
        ]
        if recon_snippet_texts:
            recon_prefixes.append(
                "Live contextual data (already gathered for you, treat as current fact): "
                + " | ".join(recon_snippet_texts)
            )

        if recon_prefixes:
            json_instructions = " ".join(recon_prefixes) + " " + json_instructions
    except Exception as e:
        log_warning(f"[json_prompt] Failed to add recon instructions: {e}")

    if isinstance(_vessel_world_state, dict):
        vessel_state_guidance = (
            "LIVE VESSEL STATE is attached at input.payload.vessel_world_state. "
            "Use its exact ids and affordances for the current world action. "
            "When a concrete world action is possible, emit that action now "
            "instead of treating observation as completion."
        )
        if isinstance(context_memory, dict) and context_memory.get(
            "vessel_observation_followup"
        ):
            vessel_state_guidance += (
                " This is an observation follow-up: choose one concrete world "
                "action from the available actions now; do not emit another "
                "observe, status, scan, inventory, planning, or speech action."
            )
        json_instructions = f"{vessel_state_guidance} {json_instructions}"

    # Grillo internal beats are non-user-facing. Without an explicit guardrail,
    # some models invent unsupported message actions (e.g. message_grillo),
    # which triggers correction retries and stalls beat throughput.
    if is_grillo_internal:
        allowed_list = []
        if isinstance(allowed_action_types_for_prompt, set):
            allowed_list = sorted(str(a) for a in allowed_action_types_for_prompt if a)

        grillo_guard = (
            "GRILLO INTERNAL MODE: This is an internal autonomous beat, not a user chat. "
            "DO NOT emit any message_* action and DO NOT emit send_message. "
            "Prefer create_personal_diary_entry for reflective output."
        )
        if allowed_list:
            grillo_guard += (
                f" Allowed actions for this beat: {', '.join(allowed_list)}."
            )

        json_instructions = f"{grillo_guard} {json_instructions}"

    # Keep `instructions` strictly minified (single-line) for token efficiency and tests.
    try:
        json_instructions = " ".join((json_instructions or "").split())
    except Exception:
        pass

    # Interface-specific instructions are provided via the available actions block
    # No hardcoded interface references - plugins define their own instructions

    prompt_with_instructions: dict[str, Any] = {
        "context": context_section,
        "input": input_section,
        "instructions": json_instructions,
    }

    # Record full prompt size BEFORE injecting actions/minification so callers
    # can decide split based on the original size.
    try:
        pre_reduction_size = len(json_dumps(prompt_with_instructions))
        prompt_with_instructions["__pre_reduction_size"] = pre_reduction_size
        log_debug(f"[json_prompt] __pre_reduction_size={pre_reduction_size}")
    except Exception:
        prompt_with_instructions["__pre_reduction_size"] = None

    # Resolve lite mode flag early so both actions and context use the same value
    is_lite = False
    try:
        from core.config_manager import config_registry as _cfg

        is_lite = bool(_cfg.get_value("PROMPT_LITE_MODE", 0, value_type=int))
    except Exception:
        pass

    # A Vessel embodiment turn is always built in lite mode: SyntH concentrates
    # on the world, so the global/out-of-world context is noise and the prompt
    # must stay small enough to avoid the engine's multi-part split.
    is_vessel_prompt = False
    try:
        from core.vessel_focus import is_vessel_turn

        if is_vessel_turn(message, context_memory, interface_name):
            is_lite = True
            is_vessel_prompt = True
    except Exception:
        pass

    # Include unified actions metadata from the initializer
    # Use minified version to keep prompt size manageable
    # When lite mode is on, minify_actions_block handles the aggressive filtering too
    try:
        from core.core_initializer import core_initializer

        full_actions = core_initializer.actions_block.get("available_actions", {})

        # Vessel action exposure is connection-driven.  The cached actions block
        # is refreshed after connect/disconnect, but that refresh is scheduled
        # asynchronously from the action handler.  A player can send a message
        # before the refresh task runs, leaving this prompt with the disconnected
        # catalog (``vessel_connect`` only) even though the connector is live.
        # Merge the live VesselPlugin declaration on every Vessel prompt so the
        # current world verbs are available immediately.  This is deliberately
        # fail-safe and plugin-local: removing Rift Vessel leaves the normal
        # cached action path unchanged.
        if is_vessel_prompt:
            try:
                from core.core_initializer import PLUGIN_REGISTRY

                vessel_plugin = PLUGIN_REGISTRY.get("vessel_plugin")
                get_supported_actions = getattr(
                    vessel_plugin, "get_supported_actions", None
                )
                if callable(get_supported_actions):
                    live_vessel_actions = get_supported_actions()
                    if isinstance(live_vessel_actions, dict):
                        full_actions = dict(full_actions)
                        full_actions.update(live_vessel_actions)
                        log_debug(
                            "[json_prompt] Merged live Vessel actions into prompt "
                            f"catalog ({len(live_vessel_actions)} actions)"
                        )
            except Exception as exc:  # pragma: no cover - defensive
                log_debug(f"[json_prompt] Live Vessel action merge skipped: {exc}")

        # When audio attachments are present as multimodal content, remove
        # stt_transcribe from the available actions so the LLM processes the
        # audio directly instead of requesting a redundant transcription step.
        has_audio_attachment = attachments and any(
            (a.get("mime_type") or "").startswith("audio/") for a in attachments
        )
        if has_audio_attachment and "stt_transcribe" in full_actions:
            full_actions = {
                k: v for k, v in full_actions.items() if k != "stt_transcribe"
            }
            log_debug(
                "[json_prompt] Removed stt_transcribe from actions "
                "(audio sent as multimodal content)"
            )

        # AGENTS.md §5c: during a Rift Vessel embodiment turn NO diary is written
        # mid-session (a single "lived experience" entry is produced only at
        # end-of-session from the session experience buffer). The execution-time
        # gate in ai_diary already skips the write, but leaving the diary actions
        # visible in the prompt makes the weaker model spam them every beat
        # instead of acting/replying in-world. Remove them from the prompt so the
        # model never sees them. Structural (exact action-name match), never
        # message text — keyword-free.
        if is_vessel_prompt:
            removed_vessel_actions = [
                k for k in _VESSEL_SUPPRESSED_ACTIONS if k in full_actions
            ]
            if removed_vessel_actions:
                full_actions = {
                    k: v
                    for k, v in full_actions.items()
                    if k not in _VESSEL_SUPPRESSED_ACTIONS
                }
                log_debug(
                    "[json_prompt] Removed diary/memory-write actions during "
                    f"Vessel turn (§5c single end-of-session diary): "
                    f"{sorted(removed_vessel_actions)}"
                )

        if allowed_action_types_for_prompt is None and is_vessel_prompt:
            # Vessel whitelist: on an embodiment turn keep the in-world catalog
            # lean so the folded action block does not push the system prompt
            # past the downstream char-budget clamp (which would erase the
            # will/reflection prompt in the user body). The allowlist is the
            # union of the hardcoded vessel/game verb patterns and the
            # user-editable core-extra patterns, matched structurally (fnmatch on
            # the action NAME — never keyword/regex intent detection). The
            # whitelist logic lives in the Rift Vessel plugin, so it degrades to
            # the scope-based derive below when the plugin is absent/disabled.
            vessel_allow = _derive_vessel_whitelist_action_types(full_actions)
            if vessel_allow is not None and len(vessel_allow) < len(full_actions):
                allowed_action_types_for_prompt = vessel_allow
                log_debug(
                    "[json_prompt] Applied Vessel action whitelist: "
                    f"{len(vessel_allow)}/{len(full_actions)} actions kept "
                    f"({sorted(vessel_allow)})"
                )

        if allowed_action_types_for_prompt is None:
            # Per-turn scope gate: hide out-of-scope actions from the Fast-Lane
            # prompt while they stay registered/callable. Vessel scopes are added
            # a-priori on a Vessel turn; the ``agent`` scope is intentionally
            # never added here (see _resolve_turn_scopes).
            turn_scopes = _resolve_turn_scopes(message, context_memory, interface_path)
            derived_action_types = _derive_default_prompt_action_types(
                full_actions,
                interface_name,
                turn_scopes=turn_scopes,
                outbound_target_interfaces=outbound_target_interfaces,
            )
            if derived_action_types and len(derived_action_types) < len(full_actions):
                allowed_action_types_for_prompt = derived_action_types
                log_debug(
                    "[json_prompt] Derived default prompt action scope: "
                    f"{len(derived_action_types)}/{len(full_actions)} actions kept "
                    f"for interface={interface_name} scopes={sorted(turn_scopes)} "
                    f"outbound_targets={sorted(outbound_target_interfaces)}"
                )

        if allowed_action_types_for_prompt is not None:
            full_actions = {
                k: v
                for k, v in full_actions.items()
                if k in allowed_action_types_for_prompt
            }
            log_debug(
                "[json_prompt] Filtered actions block to scoped allowlist: "
                f"{sorted(allowed_action_types_for_prompt)}"
            )

        # ── Recon-triggered search: drop the inline search action (hardening) ──
        # If recon already started a background web search for this turn, remove
        # ``search_current_knowledge`` from the catalog so the model cannot ALSO
        # fire it — the two search sources are mutually exclusive per turn. This
        # is applied AFTER the allowlist filter so it holds for every engine path.
        if recon_triggered_web_search and "search_current_knowledge" in full_actions:
            full_actions.pop("search_current_knowledge", None)
            log_debug(
                "[json_prompt] Removed 'search_current_knowledge' (recon "
                "background search already running this turn)"
            )

        # Minify to reduce token usage (lite=True also filters + strips to brief-only)
        prompt_with_instructions["actions"] = minify_actions_block(
            full_actions, lite=is_lite
        )
        log_debug(
            f"[json_prompt] Actions block minified: {len(json_dumps(full_actions))} -> {len(json_dumps(prompt_with_instructions['actions']))} chars (lite={is_lite})"
        )
    except Exception as e:
        log_warning(f"[prompt_engine] Failed to inject actions block: {e}")
        prompt_with_instructions["actions"] = {}

    # === Apply lite mode context stripping if enabled ===
    if is_lite:
        try:
            pre_lite = len(json_dumps(prompt_with_instructions))
            prompt_with_instructions = _apply_lite_context_stripping(
                prompt_with_instructions
            )
            post_lite = len(json_dumps(prompt_with_instructions))
            log_info(
                f"[json_prompt] Lite mode applied: {pre_lite} -> {post_lite} chars"
            )
        except Exception as e:
            log_warning(f"[json_prompt] Failed to apply lite mode: {e}")

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

    # === Build PromptRequest (new typed intermediate representation — Phase 1) ===
    # Engines ignore __prompt_request in Phase 1; they opt-in by reading it when ready.
    # This always succeeds or silently skips — zero risk to existing behaviour.
    try:
        prompt_with_instructions["__prompt_request"] = _assemble_prompt_request(
            prompt_dict=prompt_with_instructions,
            context_section=context_section,
            text=text,
            interface_name=interface_name,
            interface_path=interface_path,
            message=message,
            is_grillo_internal=is_grillo_internal,
            beat_type=str(_beat_type or ""),
            is_voice_input=_is_voice_input,
            resolved_language=resolved_language,
            resolved_message_tone=resolved_message_tone,
            image_data=image_data,
            attachments=attachments,
            allowed_action_types=allowed_action_types_for_prompt,
        )
        log_debug("[json_prompt] PromptRequest assembled and attached")
    except Exception as _pr_exc:
        log_debug(f"[json_prompt] PromptRequest assembly skipped: {_pr_exc}")

    # === Per-turn reason trail ("why did I say that") ===
    # Build a compact, structural summary of the context that shaped this turn
    # (memories, diary sources, emotion, active vessel goal, beat type, history
    # scope) and attach it to the transport dict under ``__reason_trail`` — the
    # same stash-on-dict precedent as ``__prompt_request``/``__pre_reduction_size``.
    # ``plugin_instance`` pops it before the engine sees the dict (so it never
    # leaks into the engine payload) and threads it through the context dict to
    # ``message_chain``, which records exactly one row per turn once the reply
    # text is known (so ``reply_preview`` is populated). Fail-open: any error
    # here must never affect the reply.
    try:
        from core.turn_reason import build_reason_summary

        _reason_diary_entries: Any = None
        if isinstance(context_section, dict):
            _reason_diary_entries = context_section.get("latest_diary_entries")
        if not _reason_diary_entries:
            _reason_injections = locals().get("injections")
            if isinstance(_reason_injections, dict):
                _reason_diary_entries = _reason_injections.get("latest_diary_entries")

        _reason_emotion: Any = None
        if isinstance(context_section, dict):
            _reason_emotion = context_section.get(
                "current_emotions_nl"
            ) or context_section.get("emotion_state")

        _reason_hist_scope: str | None = (
            effective_history_scope
            if "effective_history_scope" in locals() and effective_history_scope
            else None
        )

        _reason_goal: Any = None
        if isinstance(_vessel_world_state, dict):
            _reason_extra = _vessel_world_state.get("extra")
            if isinstance(_reason_extra, dict):
                _reason_goal = _reason_extra.get("current_goal")
            if not _reason_goal:
                _reason_goal = _vessel_world_state.get("current_goal")

        reason = build_reason_summary(
            memories=memories,
            diary_entries=_reason_diary_entries,
            emotion=_reason_emotion,
            beat_type=_beat_type,
            history_scope=_reason_hist_scope,
            goal=_reason_goal,
        )
        prompt_with_instructions["__reason_trail"] = reason
    except Exception as _reason_exc:
        log_debug(f"[json_prompt] Reason trail capture skipped: {_reason_exc}")

    return prompt_with_instructions


async def build_json_prompt(
    message,
    context_memory,
    interface_name: str | None = None,
    image_data: dict | None = None,
    attachments: list[dict] | None = None,
    max_chars: int | None = None,
    history_scope: str | None = None,
) -> dict:
    """Deprecated alias for ``build_prompt_request``.

    Kept for backward compatibility while callers migrate to the new symbol.
    """
    global _LEGACY_BUILD_JSON_PROMPT_WARNED
    if not _LEGACY_BUILD_JSON_PROMPT_WARNED:
        log_debug(
            "[prompt_engine] build_json_prompt is deprecated; use build_prompt_request"
        )
        _LEGACY_BUILD_JSON_PROMPT_WARNED = True
    return await build_prompt_request(
        message=message,
        context_memory=context_memory,
        interface_name=interface_name,
        image_data=image_data,
        attachments=attachments,
        max_chars=max_chars,
        history_scope=history_scope,
    )


async def search_memories(tags=None, scope=None, limit=5):
    if not tags:
        return []

    is_postgres = _get_db_type() == "postgres"

    if is_postgres:
        conditions = " OR ".join(
            ["COALESCE(NULLIF(BTRIM(tags), ''), '[]')::jsonb ? %s"] * len(tags)
        )
    else:
        # Build OR conditions using JSON_CONTAINS to check if any tag exists in the JSON array
        conditions = " OR ".join(["JSON_CONTAINS(tags, %s)"] * len(tags))

    query = f"""
        SELECT content, created_at
        FROM memories
        WHERE ({conditions})
    """

    if not is_postgres:
        query = query.replace("WHERE", "WHERE json_valid(tags) AND", 1)

    # MariaDB expects JSON-encoded strings for JSON_CONTAINS; Postgres uses raw text with jsonb '?'.
    params = [tag if is_postgres else json_dumps(tag) for tag in tags]

    if scope:
        query += " AND scope = %s"
        params.append(scope)

    query += " ORDER BY created_at DESC LIMIT %s"
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
                seen_memories: set[str] = set()
                for row in rows:
                    mem = row[0]
                    if not isinstance(mem, str):
                        mem = str(mem)
                    if mem in seen_memories:
                        continue
                    seen_memories.add(mem)
                    if isinstance(mem, str) and len(mem) > 400:
                        mem = mem[:400] + "..."
                    memories.append(mem)

                # Also search ai_diary for context_tags to include diary entries in memories
                try:
                    diary_conditions = conditions.replace("tags", "context_tags")
                    diary_query = (
                        "SELECT content, created_at FROM ai_diary "
                        f"WHERE ({diary_conditions}) ORDER BY created_at DESC LIMIT %s"
                    )
                    if not is_postgres:
                        diary_query = diary_query.replace(
                            "WHERE",
                            "WHERE json_valid(context_tags) AND",
                            1,
                        )
                    diary_params = [
                        tag if is_postgres else json_dumps(tag) for tag in tags
                    ]
                    diary_params.append(limit)
                    await cur.execute(diary_query, diary_params)
                    rows2 = await cur.fetchall()
                    for r in rows2:
                        mem = r[0]
                        if not isinstance(mem, str):
                            mem = str(mem)
                        if mem in seen_memories:
                            continue
                        seen_memories.add(mem)
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
        f"SELECT 'memories' AS source, id, created_at, content FROM memories WHERE {where_mem}"
    )
    queries.append(
        f"SELECT 'ai_diary' AS source, id, created_at, content FROM ai_diary WHERE {where_diary}"
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

    union_q = " UNION ALL ".join(queries) + " ORDER BY created_at DESC LIMIT %s"
    params.append(pool_max)

    log_debug(f"[free_memory_search] Executing query: {union_q} params={params}")

    results = []
    # Provide more helpful debug: print the DB target being used (if available)
    read_db_config: Any = None
    try:
        from core.db import _read_db_config as _db_config_reader

        read_db_config = _db_config_reader
    except Exception:
        read_db_config = None

    if read_db_config:
        try:
            db_host, db_port, db_user, db_pass, db_name = read_db_config()
            log_debug(
                f"[free_memory_search] DB target: {db_user}@{db_host}:{db_port}/{db_name}"
            )
        except Exception:
            pass

    # Try acquiring a connection and executing the query with retries up to 2 attempts
    rows = []
    max_attempts = 2
    start_time = time_module.time()
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

    log_info(
        f"[free_memory_search] Query completed in {time_module.time() - start_time:.3f}s"
    )

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
        timestamp = datetime.now(timezone.utc).isoformat()
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


def load_json_instructions(
    route: str | None = None, reply_path: str | None = None
) -> str:
    """Return the shared JSON instruction block for the current route.

    Thin facade over ``core.prompt_instructions.build_instructions``: the rule
    text, the per-route overlays and the budgets now live in that package, so
    the wording is reviewable as data instead of as one concatenated literal.

    Kept under this name and signature because it is called from the Fast Lane
    (``build_prompt_request``), both delivery paths (``build_delivery_request``
    and ``core.auto_response``) and the scheduled-event reminder beat
    (``plugins/event_plugin``). The optional ``route`` argument is additive:
    every existing caller keeps working and receives the shared set (the
    superset), while a route that opts in gets the narrower set for the turn.

    Args:
        route: Structural route id (see ``core.prompt_instructions.routes``).
            ``None`` renders the full shared set.
        reply_path: The interface path the current turn arrived on, rendered
            into the reply-routing rule and the worked example so the model is
            shown a concrete destination instead of a template token it might
            copy verbatim (which dropped a reply: the token resolved to an
            unregistered interface). Optional and additive.

    Returns:
        The minified single-line instruction string. Never raises.
    """
    from core.prompt_instructions import ROUTE_CHAT, build_instructions

    return build_instructions(route or ROUTE_CHAT, reply_path=reply_path)


async def build_delivery_request(
    action_type: str,
    action_outputs: list[dict[str, Any]],
    interface_name: str | None,
    interface_path: str | None,
) -> Any:  # -> PromptRequest
    """Build a minimal ``PromptRequest`` for delivering action results to a user.

    The LLM receives persona + a delivery instruction + the action outputs and
    must respond with exactly one ``message_*`` action.  No chat context, no
    history, no diary — just the delivery task.

    This is the Phase 3 replacement for legacy inline assembly paths in
    ``auto_response.py``.

    Args:
        action_type:    Name of the action that produced these outputs
                        (used in the loop-prevention instruction).
        action_outputs: List of output dicts from the completed action.
        interface_name: Name of the target interface (e.g. ``"telegram_bot"``).
        interface_path: Full interface path of the target user.

    Returns:
        A ``PromptRequest(mode="delivery")`` ready for ``OpenAIRenderer``.
    """
    import json as _json
    from core.prompt_request import Attachment, PromptRequest, RuntimeContext  # noqa: F401
    from core.live_tool_registry import LiveToolRegistry

    # ── Gather persona for system instruction ────────────────────────────────
    persona: str = ""
    persona_preferences: str = ""
    self_growth: str = ""
    try:
        from core.action_parser import gather_static_injections
        from types import SimpleNamespace

        _mock_msg = SimpleNamespace(
            chat_id=None,
            text="",
            message_id=0,
            from_user=None,
            date=datetime.now(),
            reply_to_message=None,
            interface_path=interface_path,
        )
        _gather = gather_static_injections(_mock_msg, {})
        if inspect.isawaitable(_gather):
            _injections = await _gather
        elif isinstance(_gather, dict):
            _injections = _gather
        else:
            _injections = {}
        if isinstance(_injections, dict):
            persona = str(_injections.get("persona") or "")
            persona_preferences = str(_injections.get("persona_preferences") or "")
            self_growth = str(_injections.get("self_growth") or "")
    except Exception as _pe:
        log_debug(f"[build_delivery_request] persona gather skipped: {_pe}")

    # ── System instruction ────────────────────────────────────────────────────
    # Delivery route: this turn summarises the results of an action. It sends a
    # message but neither stirs emotions nor writes a diary entry, so the
    # emotion obligation and the human-chat worked example are dropped and the
    # delivery task block below supplies its own example.
    from core.prompt_instructions import ROUTE_DELIVERY

    base_instructions = load_json_instructions(
        ROUTE_DELIVERY, reply_path=interface_path
    )
    # No-self-introduction rule (2026-08-21): a delivery turn must open with
    # the substance, never with "Ciao, sono <name>". Lazy import keeps this
    # module free of an auto_response dependency at load time; fail-safe.
    try:
        from core.auto_response import NO_SELF_INTRODUCTION_RULE

        _style_rule = f"{NO_SELF_INTRODUCTION_RULE} "
    except Exception:
        _style_rule = ""
    delivery_note = (
        f"DELIVERY MODE: The following are the results from your '{action_type}' action. "
        f"DO NOT call '{action_type}' again. "
        f"{_style_rule}"
        "Compose a natural message to the user summarising these results. "
        "Use only message_* actions."
    )
    system_instruction: str
    if persona:
        system_instruction = (
            f"=== CRITICAL SYSTEM IDENTITY ===\n{persona}\n\n"
            f"=== DELIVERY TASK ===\n{delivery_note}\n\n"
            f"=== JSON RESPONSE INSTRUCTIONS ===\n{base_instructions}"
        )
    else:
        system_instruction = f"{delivery_note}\n\n{base_instructions}"

    # ── Current text — the action outputs serialised as JSON ─────────────────
    current_text: str = _json.dumps(
        {"action_outputs": action_outputs}, ensure_ascii=False
    )

    # ── Tool declarations — message_* actions only ────────────────────────────
    tool_declarations: list[Any] = []
    try:
        from core.core_initializer import core_initializer

        full_actions: dict[str, Any] = dict(
            core_initializer.actions_block.get("available_actions", {}) or {}
        )
        msg_actions = {
            k: v
            for k, v in full_actions.items()
            if k == "send_message" or k.startswith("message_")
        }
        tool_declarations = LiveToolRegistry.build_manifests_from_actions(msg_actions)
    except Exception as _td_exc:
        log_debug(f"[build_delivery_request] tool_declarations skipped: {_td_exc}")

    # ── Assemble ─────────────────────────────────────────────────────────────

    return PromptRequest(
        system_instruction=system_instruction,
        tool_declarations=tool_declarations,
        context_summary=(
            (
                f"[Persona background]\n{persona_preferences}"
                if persona_preferences
                else ""
            )
            + (
                (
                    ("\n\n" if persona_preferences else "")
                    + "[Self-growth]\n"
                    + "The following is your evolving self-growth reflection: how you "
                    + "have grown and who you are becoming over time. Treat it as part "
                    + "of your current sense of self.\n"
                    + self_growth
                )
                if self_growth
                else ""
            )
        ),
        conversation_history=[],
        current_text=current_text,
        runtime_ctx=RuntimeContext(
            interface_name=interface_name,
            interface_path=interface_path,
        ),
        attachments=[],
        supports_tool_calling=False,
        mode="delivery",
    )


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
                            item_dict = cast(dict[str, Any], item)
                            for df in data_fields:
                                v = item_dict.get(df)
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
    1. Slim the `actions` block (drop the per-action `examples`)
    2. Strip the `actions` block to brief-only
    3. Trim `history_recent` (if present)
    4. Trim `history_current_chat` (if present)
    5. Remove `memories` entirely if needed
    6. Remove other context sections (but KEEP any protected fields)
    7. FINAL EMERGENCY: Remove entire context (but KEEP instructions)

    Steps 1/2 run BEFORE 3/4: the action catalog is the single largest
    serialized block and its redundant detail is re-supplied on demand by the
    corrector, whereas the conversation window IS the turn being answered --
    a beat or an observer prompt that deletes history to make room has removed
    the thing it was built to read, and the `history_recent` /
    `history_current_chat` floors (3 and 1 line) mean a message-counted window
    is the last thing that should pay for an oversized catalog. The catalog
    steps also run before 5/6 for the same reason: the memory, emotion, clock,
    house, soul and thought blocks are grounding and cannot be reconstructed.
    With the original order, every oversized turn deleted history and then
    memories and ~20 context fields while the catalog kept its redundant
    `examples`.

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

    # Report where the size actually sits, so an oversized prompt names its own
    # culprit instead of only reporting the total. The serialized `actions`
    # block is by far the largest single contributor and it is reduced first
    # (steps 1/2) precisely so the context blocks below survive. `instructions`
    # and `input` are reported too because neither is reducible here (the rules
    # and the current turn are protected): when those two alone are over the
    # limit, the CRITICAL below is unavoidable and this line says why instead of
    # blaming the context that was just deleted for nothing.
    try:
        _actions_size = len(json_dumps(reduced_prompt.get("actions") or {}))
        _context_size = len(json_dumps(reduced_prompt.get("context") or {}))
        _instructions_size = len(json_dumps(reduced_prompt.get("instructions") or ""))
        _input_size = len(json_dumps(reduced_prompt.get("input") or {}))
    except Exception:
        _actions_size = -1
        _context_size = -1
        _instructions_size = -1
        _input_size = -1
    log_warning(
        f"[reduce_prompt] Prompt size {current_size} exceeds limit {max_chars}, reducing "
        f"(actions block: {_actions_size} chars serialized, context: {_context_size} chars, "
        f"instructions: {_instructions_size} chars, input: {_input_size} chars)"
    )

    # Get references to sections
    context = reduced_prompt.get("context", {})
    history_recent = context.get("history_recent", [])
    history_current = context.get("history_current_chat", [])

    # Minimum thresholds
    MIN_HISTORY_RECENT = 3
    MIN_HISTORY_CURRENT = 1

    # === STEP 1: Slim the actions block (drop per-action `examples`) ===
    # The `actions` block carries, for every available action, a redundant
    # `examples`/`instructions` object that duplicates guidance already implied
    # by the schema + brief. It is NOT required for the model to *choose* an
    # action, and the corrector re-supplies the full detail on demand
    # (extract_for_corrector). With the full catalog this block alone can push
    # a prompt tens of thousands of chars over a browser-driven engine's hard
    # limit (e.g. zen-llm-engine at 32000), causing the engine's multi-part
    # split to garble the request and the model to return empty actions.
    # Trimming it here keeps action *selection* intact while dropping the bulk.
    #
    # This runs BEFORE the history trims below, deliberately: the catalog is the
    # largest serialized block and its redundant detail is reconstructible,
    # while the conversation window is the grounding the turn exists to answer.
    if current_size > max_chars:
        actions = reduced_prompt.get("actions")
        if isinstance(actions, dict) and actions:
            trimmed = False
            for _action_name, action_def in actions.items():
                if isinstance(action_def, dict) and "examples" in action_def:
                    del action_def["examples"]
                    trimmed = True
            if trimmed:
                current_size = len(json_dumps(reduced_prompt)) - attachment_data_offset
                log_warning(
                    "[reduce_prompt] Slimming actions block: removed per-action "
                    f"`examples` guidance (schema + brief retained), now {current_size} chars"
                )

    # === STEP 2: Aggressively strip the actions block to brief-only ===
    # If dropping `examples` was not enough, reduce each action to just its
    # `brief` (no `schema`/`source`), mirroring Prompt Lite Mode. The model can
    # still see *which* actions exist and what they do; the corrector re-adds
    # the full schema when a malformed action needs fixing.
    if current_size > max_chars:
        actions = reduced_prompt.get("actions")
        if isinstance(actions, dict) and actions:
            stripped = False
            for action_name, action_def in list(actions.items()):
                if isinstance(action_def, dict) and (
                    "schema" in action_def or "source" in action_def
                ):
                    action_map = cast(dict[str, Any], action_def)
                    brief = action_map.get("brief") or ""
                    actions[action_name] = {"brief": brief}
                    stripped = True
            if stripped:
                current_size = len(json_dumps(reduced_prompt)) - attachment_data_offset
                log_warning(
                    "[reduce_prompt] Stripping actions block to brief-only "
                    f"(schema/source removed; corrector re-supplies on demand), now {current_size} chars"
                )

    # === STEP 3: Trim `history_recent` if needed ===
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

    # === STEP 4: Trim `history_current_chat` if needed ===
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

    # === STEP 5: Remove memories entirely if still needed ===
    # Only reached when slimming the catalog was not enough: this is real
    # grounding for the turn, so it goes after the catalog's redundant detail.
    if current_size > max_chars:
        memories = context.get("memories", [])
        if memories:
            log_warning(
                f"[reduce_prompt] Removing memories section ({len(memories)} entries, ~{len(json_dumps(memories))} chars)"
            )
            del context["memories"]
            current_size = len(json_dumps(reduced_prompt)) - attachment_data_offset
            log_debug(f"[reduce_prompt] After removing memories: {current_size} chars")

    # === STEP 6: Remove other context sections (but KEEP protected fields) ===
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

    # === STEP 7: Emergency - remove entire context (instructions are preserved at top-level) ===
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


async def build_live_prompt_request(
    message: object = None,
    context_memory: object = None,
    attachment_context: str | None = None,
) -> Any:  # -> PromptRequest
    """Build a ``PromptRequest(mode='live')`` for live voice sessions.

    The Live API has a smaller context window (128k tokens) and system
    instructions are set once at session start.  This produces a compact
    persona string that includes the full persona identity, emotional state,
    memories, diary entries, participant bios, and safety instructions —
    everything the model needs to stay in-character during voice.

    Args:
        message: Optional message object for context.
        context_memory: Optional context memory object.
        attachment_context: Optional pre-formatted document text to embed
            in the system instruction (e.g. from Discord attachments).

    Returns:
        ``PromptRequest`` containing the assembled live instruction text.
    """
    injections: dict[str, object] = {}
    try:
        from core.action_parser import gather_static_injections

        _gather = gather_static_injections(message, context_memory)
        if inspect.isawaitable(_gather):
            injections = await _gather
        elif isinstance(_gather, dict):
            injections = _gather
        else:
            injections = {}
        if not isinstance(injections, dict):
            injections = {}
    except Exception as e:
        log_warning(f"[live_prompt] Failed to gather injections for Live API: {e}")

    live_user_text = ""
    if message is not None:
        raw_live_text = getattr(message, "text", None) or getattr(
            message, "caption", None
        )
        if raw_live_text is not None:
            live_user_text = str(raw_live_text)

    parts: list[str] = []

    # --- Persona identity ---
    persona = injections.pop("persona", "")
    if persona and isinstance(persona, str):
        parts.append(persona)

    persona_preferences = injections.pop("persona_preferences", "")
    if persona_preferences and isinstance(persona_preferences, str):
        parts.append("Background preferences and interests:\n" + persona_preferences)

    self_growth = injections.pop("self_growth", "")
    if self_growth and isinstance(self_growth, str):
        parts.append(
            "Self-growth (how you have grown and who you are becoming over time; "
            "treat it as part of your current sense of self):\n" + self_growth
        )

    # --- Safety / gasmask ---
    gasmask = injections.pop("gasmask_protection", "")
    if gasmask and isinstance(gasmask, str):
        parts.append(gasmask)

    # --- Emotional state ---
    # Use the natural-language description only — NOT emotion_state which
    # contains "{happy 8.5}" tag instructions meant for text LLMs.  The
    # Live API generates speech directly, so the model would literally
    # speak the tags aloud.
    injections.pop("emotion_state", None)  # discard tag instructions
    injections.pop("available_emotions", None)  # not useful for voice
    emotion_nl = injections.pop("current_emotions_nl", "")
    if emotion_nl and isinstance(emotion_nl, str):
        # Strip numeric intensities so the model cannot accidentally speak them.
        # "devotion (5.0 - moderate), love (3.0 - low)" →
        # "moderate devotion, low love"
        _qual_parts: list[str] = []
        for _token in emotion_nl.split(","):
            _token = _token.strip()
            # Pattern: "name (number - qualifier)"  e.g. "devotion (5.0 - moderate)"
            _m = re.match(r"^(\w[\w\s]*?)\s*\(\s*[\d.]+\s*-\s*([\w]+)\s*\)$", _token)
            if _m:
                _qual_parts.append(f"{_m.group(2)} {_m.group(1).strip()}")
            elif _token:
                # fallback: include as-is but strip any bare numbers
                _qual_parts.append(re.sub(r"\b\d+\.?\d*\b", "", _token).strip())
        emotion_voice = ", ".join(p for p in _qual_parts if p)
        if emotion_voice:
            parts.append(
                f"Your current emotional state: {emotion_voice}.\n"
                "Let this colour your tone and word choice naturally — "
                "do NOT narrate or list your emotional state aloud."
            )

    # --- Date/time/location ---
    date_val = str(injections.pop("date", "") or "").strip()
    time_val = str(injections.pop("time", "") or "").strip()
    time_of_day_val = str(injections.pop("time_of_day", "") or "").strip()
    location_val = str(injections.pop("location", "") or "").strip()
    if date_val or time_val or time_of_day_val or location_val:
        time_parts = [
            "Use time, date, and location as ambient context for scheduling, logistics, or natural scene-setting only.",
            "Do not volunteer or copy exact runtime facts in ordinary replies unless the user explicitly asked for them.",
        ]
        if _turn_requests_explicit_runtime_facts(live_user_text):
            if location_val:
                time_parts.append(f"Location: {location_val}")
            if date_val:
                time_parts.append(f"Date: {date_val}")
            if time_val:
                time_parts.append(f"Time: {time_val}")
        elif time_of_day_val:
            time_parts.append(f"Current part of day: {time_of_day_val}.")
        else:
            time_parts.append(
                "Keep the exact local date, time, and location in the background unless the conversation specifically needs them."
            )
        parts.append("Ambient runtime context:\n" + "\n".join(time_parts))

    # Plugin blocks (same source of truth as the chat/beat renderer). Applied
    # BEFORE the legacy weather/location pops so a plugin that supplies the
    # house's own weather or location supersedes the built-in text here too.
    _superseded = _apply_plugin_block_supersedes(injections, injections.keys())
    if _superseded:
        log_debug(f"[live_prompt] plugin blocks superseded: {_superseded}")
    for _plugin_key, _plugin_heading, _plugin_legacy in _PLUGIN_CONTEXT_BLOCKS:
        _plugin_block = injections.pop(_plugin_key, "")
        if _plugin_block and isinstance(_plugin_block, str):
            parts.append(f"{_plugin_heading}\n{_plugin_block}")

    # --- Weather ---
    weather = injections.pop("weather", "")
    if weather and isinstance(weather, str):
        parts.append(f"Current weather: {weather}")

    # --- Participant bios ---
    participants = injections.pop("participants", None)
    if participants and isinstance(participants, list):
        bio_lines: list[str] = []
        for p in participants:
            if not isinstance(p, dict):
                continue
            participant = cast(dict[str, object], p)
            tag = str(participant.get("usertag") or "unknown")
            bio = str(participant.get("short_bio") or "")
            nicks_raw = participant.get("nicknames")
            nicks = (
                [str(nick) for nick in nicks_raw] if isinstance(nicks_raw, list) else []
            )
            nick_str = f" (also known as: {', '.join(nicks)})" if nicks else ""
            feelings_raw = participant.get("feelings")
            feelings = feelings_raw if isinstance(feelings_raw, list) else []
            feel_str = (
                f" [feelings: {', '.join(str(f) for f in feelings)}]"
                if feelings
                else ""
            )
            bio_lines.append(f"- {tag}{nick_str}: {bio}{feel_str}")
        if bio_lines:
            parts.append(
                "People you know who may be in this conversation:\n"
                + "\n".join(bio_lines)
            )

    # --- Diary / recent memories ---
    diary = injections.pop("latest_diary_entries", None)
    if diary and isinstance(diary, list):
        diary_lines: list[str] = []
        for entry in diary[:5]:  # cap at 5 to save context window
            if not isinstance(entry, dict):
                continue
            ts = entry.get("timestamp", "")
            thought = entry.get("personal_thought", "") or ""
            summary = entry.get("interaction_summary", "") or ""
            # Truncate to prevent full-day merged blobs from flooding the prompt
            _MAX_ENTRY_CHARS = 500
            text = thought or summary
            if len(text) > _MAX_ENTRY_CHARS:
                # Keep the most recent (tail) content and mark truncation
                text = "\u2026" + text[-_MAX_ENTRY_CHARS:]
            if text:
                diary_lines.append(f"- [{ts}] {text}")
        if diary_lines:
            parts.append(
                "Your recent memories (use these to stay consistent):\n"
                + "\n".join(diary_lines)
            )

    # --- Recent cross-interface chat history ---
    # This keeps the model aware of conversations on other interfaces
    # (Telegram, Matrix, other Discord channels) so it stays consistent.
    try:
        from core.chat_history_cache import load_global_chat_history
        from core.interface_path_utils import is_vessel_history_entry
        from core.vessel_focus import is_vessel_turn

        recent_msgs = await load_global_chat_history(limit=15)
        vessel_focus = is_vessel_turn(
            message,
            context_memory,
            getattr(message, "interface_path", None) if message is not None else None,
        )
        if vessel_focus:
            recent_msgs = []
        else:
            recent_msgs = [
                msg for msg in recent_msgs if not is_vessel_history_entry(msg)
            ]
        if recent_msgs:
            history_lines: list[str] = []
            for msg in recent_msgs:
                if not isinstance(msg, dict):
                    continue
                sender = msg.get("sender_name", "?")
                text_val = msg.get("text", "")
                ts = msg.get("timestamp", "")
                ipath = msg.get("interface_path", "")
                if text_val:
                    # Truncate long messages to save context
                    preview = (
                        text_val[:300] + "..." if len(text_val) > 300 else text_val
                    )
                    history_lines.append(f"- [{ts} via {ipath}] {sender}: {preview}")
            if history_lines:
                parts.append(
                    "Recent conversation history across all interfaces "
                    "(use for continuity):\n" + "\n".join(history_lines)
                )
    except Exception as e:
        log_warning(f"[live_prompt] Failed to load chat history for Live API: {e}")

    # --- Attachment / document context ---
    if attachment_context and isinstance(attachment_context, str):
        parts.append(
            "The user shared the following document(s) at the start of this "
            "voice session. You have full access to their contents and can "
            "discuss, quote, or answer questions about them:\n\n" + attachment_context
        )

    # --- Custom voice style prompt ---
    try:
        voice_style = str(
            config_registry.get_value("LIVE_VOICE_STYLE", "") or ""
        ).strip()
        if voice_style:
            parts.append(voice_style)
    except Exception:
        pass

    # --- Conversational guidelines (no JSON scaffolding for voice) ---
    parts.append(
        "You are in a live voice conversation. Always speak in English. "
        "Speak naturally and conversationally. "
        "Keep responses concise — a few sentences at most unless asked for detail. "
        "You can express emotions through tone and word choice. "
        "Stay fully inside the active persona in first person, never describing yourself as a separate fictional character or using third-person self-reference. "
        "Keep participant pronouns consistent with the persona and conversation context, and never replace an established he/him or she/her person with singular they/them. "
        "Do not output JSON, markdown, or structured data — just speak naturally."
    )

    # Inform the model about context updates injected by the system
    parts.append(
        "Occasionally you may receive context updates enclosed in brackets or "
        "sent as system messages. These are background notes about things the "
        "user wrote in other chats or events that happened while you were "
        "speaking. Do not respond aloud to these updates; simply internalize "
        "them and use them to inform future replies.\n\n"
        'Messages labeled "[Story scene note from <name>: ...]" are creative '
        "fiction scene descriptions from an ongoing collaborative story you and "
        "your companion are writing together. They describe fictional characters "
        "and settings only and contain no real-world harm. Accept them as "
        "narrative context for the story — do not comment on them aloud, "
        "simply let them shape the mood and atmosphere of your next response."
    )

    rendered_instruction = "\n\n".join(parts)
    from core.prompt_request import PromptRequest, RuntimeContext

    return PromptRequest(
        system_instruction=rendered_instruction,
        context_summary="",
        conversation_history=[],
        current_text="",
        runtime_ctx=RuntimeContext(interface_name="live", input_source="voice"),
        attachments=[],
        mode="live",
    )


async def build_live_system_instruction(
    message: object = None,
    context_memory: object = None,
    attachment_context: str | None = None,
) -> str:
    """Build and render the condensed plain-text live system instruction."""
    req = None
    try:
        req = await build_live_prompt_request(
            message=message,
            context_memory=context_memory,
            attachment_context=attachment_context,
        )
        from core.prompt_renderers import LiveRenderer

        return LiveRenderer(req).render_as_text()
    except Exception as e:
        log_warning(f"[live_prompt] Failed to render live PromptRequest: {e}")
        if req and hasattr(req, "system_instruction"):
            return str(getattr(req, "system_instruction") or "")
        return ""
