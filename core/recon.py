import asyncio
import json
import threading
import time
from typing import Any, Dict, List, Tuple
from core.logging_utils import log_debug, log_info, log_warning
from core.config_manager import config_registry
from core.beat_utils import is_outbound_beat

_RECON_HINT_CACHE: dict[str, dict[str, Any]] = {}
_lingua_detector: Any | None = None
_lingua_detector_lock = threading.Lock()

# Expose config flags
try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "ENABLE_RECON",
        label="Enable Recon (preflight)",
        default=True,
        value_type=bool,
        ui_type="bool",
        description="Enable Recon preflight contributions from plugins",
        scope="agent",
        component="recon",
        needs_component_reload=False,
    )
    register_exposed_var(
        "RECON_MAX_RESULTS",
        label="Recon Max Results",
        default=5,
        value_type=int,
        ui_type="number",
        description="Maximum number of recon contributions to use per plugin",
        scope="agent",
        component="recon",
        needs_component_reload=False,
    )
    register_exposed_var(
        "RECON_TIMEOUT",
        label="Recon Timeout (s)",
        default=180,
        value_type=int,
        ui_type="number",
        description="Timeout in seconds for recon plugin calls",
        scope="agent",
        component="recon",
        needs_component_reload=False,
    )
    register_exposed_var(
        "LANGUAGE_DETECTOR_TIMEOUT",
        label="Language Detector Timeout (s)",
        default=2,
        value_type=int,
        ui_type="number",
        description="Timeout in seconds for language detector hooks",
        scope="agent",
        component="recon",
        needs_component_reload=False,
        advanced=True,
    )
    register_exposed_var(
        "TONE_DETECTOR_TIMEOUT",
        label="Tone Detector Timeout (s)",
        default=2,
        value_type=int,
        ui_type="number",
        description="Timeout in seconds for tone detector hooks",
        scope="agent",
        component="recon",
        needs_component_reload=False,
        advanced=True,
    )
    register_exposed_var(
        "INTERFACE_LANGUAGE_OVERRIDES",
        label="Interface Language Overrides",
        default={},
        value_type=dict,
        ui_type="json",
        description="Mapping interface_path -> language_code",
        scope="agent",
        component="recon",
        needs_component_reload=False,
        hidden=True,
    )
    register_exposed_var(
        "INTERFACE_TONE_OVERRIDES",
        label="Interface Tone Overrides",
        default={},
        value_type=dict,
        ui_type="json",
        description="Mapping interface_path -> tone",
        scope="agent",
        component="recon",
        needs_component_reload=False,
        hidden=True,
    )
    register_exposed_var(
        "DEFAULT_GRILLO_LANGUAGE",
        label="Default Grillo Language",
        default="en",
        value_type=str,
        ui_type="string",
        description="Default language for Grillo/internal prompts",
        scope="agent",
        component="recon",
        needs_component_reload=False,
        advanced=True,
    )
    register_exposed_var(
        "DEFAULT_GRILLO_TONE",
        label="Default Grillo Tone",
        default="neutral",
        value_type=str,
        ui_type="string",
        description="Default tone for Grillo/internal prompts",
        scope="agent",
        component="recon",
        needs_component_reload=False,
        advanced=True,
    )
    register_exposed_var(
        "PROJECT_DEFAULT_LANGUAGE",
        label="Project Default Language",
        default="en",
        value_type=str,
        ui_type="string",
        description="Fallback language when no hints are provided",
        scope="agent",
        component="recon",
        needs_component_reload=False,
        advanced=True,
    )
    register_exposed_var(
        "PROJECT_DEFAULT_TONE",
        label="Project Default Tone",
        default="neutral",
        value_type=str,
        ui_type="string",
        description="Fallback tone when no hints are provided",
        scope="agent",
        component="recon",
        needs_component_reload=False,
    )
    register_exposed_var(
        "RECON_HINT_CACHE_TTL_SECONDS",
        label="Recon Hint Cache TTL (s)",
        default=300,
        value_type=int,
        ui_type="number",
        description="TTL in seconds for cached Recon language/tone hints",
        scope="agent",
        component="recon",
        needs_component_reload=False,
        advanced=True,
    )
    register_exposed_var(
        "RECON_LOCAL_LANGUAGE_PRECHECK",
        label="Recon Local Language Precheck",
        default=True,
        value_type=bool,
        ui_type="bool",
        description="Use local lingua language detection before invoking Recon LLM",
        scope="agent",
        component="recon",
        needs_component_reload=False,
        advanced=True,
    )
except Exception:
    pass


def _get_lingua_detector() -> Any | None:
    global _lingua_detector
    if _lingua_detector is not None:
        return _lingua_detector
    with _lingua_detector_lock:
        if _lingua_detector is not None:
            return _lingua_detector
        try:
            from lingua import LanguageDetectorBuilder

            _lingua_detector = (
                LanguageDetectorBuilder.from_all_languages()
                .with_minimum_relative_distance(0.1)
                .build()
            )
            log_debug("[recon] lingua detector initialized")
        except Exception as exc:
            log_debug(f"[recon] lingua unavailable: {exc}")
    return _lingua_detector


def _detect_language_locally(text: str | None) -> str | None:
    if not text or not text.strip():
        return None
    detector = _get_lingua_detector()
    if detector is None:
        return None
    try:
        lang = detector.detect_language_of(text)
        if lang is None:
            return None
        return str(lang.iso_code_639_1.name).lower()
    except Exception:
        return None


def _get_hint_cache_ttl_seconds() -> int:
    try:
        ttl = int(
            config_registry.get_value(
                "RECON_HINT_CACHE_TTL_SECONDS", 300, value_type=int
            )
            or 300
        )
        return max(0, ttl)
    except Exception:
        return 300


def _make_hint_cache_key(interface_path: str | None, text: str | None) -> str | None:
    normalized = (text or "").strip().lower()
    if not normalized:
        return None
    iface = (interface_path or "_").strip()
    # Keep keys bounded while preserving enough text for stable deduplication.
    return f"{iface}::{normalized[:512]}"


def _get_cached_hint(key: str | None) -> dict[str, Any] | None:
    if not key:
        return None
    cached = _RECON_HINT_CACHE.get(key)
    if not cached:
        return None
    if float(cached.get("expires_at", 0.0)) < time.time():
        _RECON_HINT_CACHE.pop(key, None)
        return None
    return dict(cached)


def _set_cached_hint(
    key: str | None,
    *,
    language_code: str | None,
    message_tone: str | None,
    conversation_tone: str | None,
) -> None:
    if not key:
        return
    ttl = _get_hint_cache_ttl_seconds()
    if ttl <= 0:
        return
    if not language_code and not message_tone and not conversation_tone:
        return
    _RECON_HINT_CACHE[key] = {
        "language_code": language_code,
        "message_tone": message_tone,
        "conversation_tone": conversation_tone,
        "expires_at": time.time() + float(ttl),
    }


def _parse_mapping(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
            if isinstance(loaded, dict):
                return loaded
        except Exception:
            return {}
    return {}


def _plugin_key(plugin: Any) -> str:
    try:
        module_name = plugin.__class__.__module__.split(".")[-1]
        return module_name.upper()
    except Exception:
        return plugin.__class__.__name__.upper()


def _plugin_enabled(plugin: Any, suffix: str) -> bool:
    key = f"{_plugin_key(plugin)}_{suffix}_ENABLED"
    try:
        return bool(config_registry.get_value(key, True, value_type=bool))
    except Exception:
        return True


def _normalize_contribution(contrib: dict, plugin: Any) -> dict | None:
    if not isinstance(contrib, dict):
        return None

    norm = dict(contrib)

    if "type" not in norm:
        if "snippet" in norm:
            norm["type"] = "snippet"
            norm["content"] = norm.get("snippet")
        elif "content" in norm:
            norm["type"] = "snippet"
        elif "language_code" in norm:
            norm["type"] = "language_hint"
        elif "message_tone" in norm or "conversation_tone" in norm:
            norm["type"] = "tone_hint"

    if "source" not in norm:
        norm["source"] = _plugin_key(plugin).lower()

    if "priority" not in norm:
        try:
            norm["priority"] = int(getattr(plugin, "recon_priority", 0) or 0)
        except Exception:
            norm["priority"] = 0

    return norm


async def _call_recon_plugin(plugin: Any, **kwargs) -> List[Dict[str, Any]]:
    try:
        fn = plugin.get_recon_contributions
        try:
            result = fn(**kwargs)
        except TypeError:
            result = fn()
        if asyncio.iscoroutine(result):
            result = await result
        if not result:
            return []
        if isinstance(result, dict):
            result = [result]
        if isinstance(result, list):
            normalized = []
            for item in result:
                norm = _normalize_contribution(item, plugin)
                if norm:
                    normalized.append(norm)
            return normalized
    except Exception as e:
        log_warning(
            f"[recon] Plugin {plugin.__class__.__name__} recon hook failed: {e}"
        )
    return []


async def _build_recon_history_texts_async(
    *,
    message=None,
    context_memory=None,
) -> Tuple[str, str]:
    local_lines: list[str] = []
    global_lines: list[str] = []

    interface_path = getattr(message, "interface_path", None)
    # Resolve the raw incoming path the same way messages are resolved when
    # persisted (alias/link map + Unified Lane), so the local-history lookup
    # keys line up with how the rows were stored.
    if interface_path:
        try:
            from core.chat_context_manager import _resolve_context_path

            interface_path = _resolve_context_path(interface_path)
        except Exception:
            pass
    try:
        if isinstance(context_memory, dict) and interface_path in context_memory:
            raw = list(context_memory.get(interface_path, []))
            for item in raw[-6:]:
                if isinstance(item, dict):
                    sender = item.get("sender_name") or item.get("sender") or "unknown"
                    content = (
                        item.get("text")
                        or item.get("message_text")
                        or item.get("content")
                        or ""
                    )
                    if content:
                        local_lines.append(f"[{sender}] {content}")
    except Exception:
        pass

    try:
        from core.chat_history_cache import (
            load_chat_history,
            load_global_chat_history,
        )

        if interface_path:
            # match_chat_level=True: after a restart the in-memory context is
            # empty, so this DB read is the only source of local history. An
            # exact-path match silently drops thread-suffixed turns of the same
            # chat (e.g. Telegram reply-in-thread), leaving local history empty
            # while global history (unfiltered) survives. Chat-level matching
            # keeps the two consistent across restarts.
            cached = await load_chat_history(interface_path, match_chat_level=True)
            for item in list(cached)[-6:]:
                sender = item.get("sender_name") or "unknown"
                content = item.get("text") or ""
                if content:
                    local_lines.append(f"[{sender}] {content}")

        global_cached = await load_global_chat_history(limit=6)
        for item in list(global_cached)[-6:]:
            sender = item.get("sender_name") or "unknown"
            content = item.get("text") or ""
            if content:
                global_lines.append(f"[{sender}] {content}")
    except Exception:
        pass

    local_text = "\n".join(local_lines) if local_lines else "(none)"
    global_text = "\n".join(global_lines) if global_lines else "(none)"
    return local_text, global_text


async def _normalize_keywords_list(raw_keywords: List[str] | None) -> List[str]:
    """Normalize Recon keywords into single-word tokens.

    Rules:
    - split on underscores, hyphens and non-alphanumeric chars
    - split camelCase boundaries (e.g. behaviorChange -> behavior, change)
    - lowercase, strip and dedupe while preserving order
    - return an empty list if input is None or no valid tokens
    """
    if not raw_keywords:
        return []

    import re

    def _split_camel(s: str) -> List[str]:
        # Insert space between lower->upper transitions then split
        parts = re.sub("([a-z0-9])([A-Z])", r"\1 \2", s).split()
        return parts

    seen = set()
    out: List[str] = []
    for k in raw_keywords:
        if not k:
            continue
        # replace non-alnum with space, then split camelCase
        k = str(k).strip()
        k = re.sub(r"[^0-9A-Za-z]+", " ", k)
        for part in k.split():
            for sub in _split_camel(part):
                tok = sub.strip().lower()
                if not tok:
                    continue
                if tok not in seen:
                    seen.add(tok)
                    out.append(tok)
    return out


def get_registered_recon_keys() -> set[str]:
    """Return the set of recon keys declared by currently registered plugins.

    Recon plugins are preflight-only: they return ``{}`` from
    ``get_supported_actions()`` and instead expose a single recon key via
    ``get_recon_key()`` (e.g. ``tone_hint``, ``agent_intent``, ``language_hint``).
    These keys are the schema of the *separate* Recon LLM call — they are never
    valid main-pass actions.

    State-retaining browser engines (e.g. ``selenium-llm-engine``) can leak the
    Recon call's JSON schema into the immediately following main-pass response,
    so the main pass emits an ``actions`` array made entirely of these recon
    keys. Callers use this set to structurally drop such leaked entries before
    validation, so a contaminated turn is not starved of deliverable actions.

    The set is derived reflectively from ``PLUGIN_REGISTRY`` — no hardcoded
    keyword list — so it stays correct as recon plugins are added or removed.
    Fully guarded: any failure returns an empty set (drop nothing).
    """
    keys: set[str] = set()
    try:
        from core.core_initializer import PLUGIN_REGISTRY

        plugins = list(PLUGIN_REGISTRY.values())
    except Exception as e:  # pragma: no cover - defensive
        log_warning(f"[recon] get_registered_recon_keys: registry unavailable: {e}")
        return keys

    for plugin in plugins:
        get_key = getattr(plugin, "get_recon_key", None)
        if not callable(get_key):
            continue
        try:
            key = get_key()
        except Exception:
            continue
        if isinstance(key, str) and key.strip():
            keys.add(key.strip())
    return keys


async def gather_recon_contributions(
    message=None,
    context_memory=None,
    text: str | None = None,
    tags: List[str] | None = None,
    keywords: List[str] | None = None,
    max_results: int | None = None,
    recon_whitelist_patterns: List[str] | None = None,
) -> List[Dict[str, Any]]:
    """Call plugin hooks `get_recon_contributions` and merge results.

    Returns list of normalized contribution dicts.

    ``recon_whitelist_patterns`` is the preflight counterpart of the vessel
    action whitelist: when non-empty (an in-world embodiment turn), a recon
    plugin is only kept if its ``get_recon_key()`` matches one of the fnmatch
    patterns. This is structural name matching, never keyword/regex intent
    detection. Fail-safe: a plugin missing a usable key, or whose key raises,
    is excluded on a whitelisted turn; ``None``/empty patterns disable the
    filter entirely (the normal, non-vessel behaviour).
    """
    enabled = bool(config_registry.get_var("ENABLE_RECON", True))
    # start-of-flow logging
    log_debug(
        f"[recon] gather_recon_contributions start: enabled={enabled} text={text!r} tags={tags} keywords={keywords} max_results={max_results}"
    )
    if not enabled:
        log_debug("[recon] ENABLE_RECON disabled, skipping contributions")
        return []

    try:
        if max_results is None:
            max_results = int(
                config_registry.get_value("RECON_MAX_RESULTS", 5, value_type=int) or 5
            )
        else:
            max_results = int(max_results)
    except Exception:
        max_results = 5

    try:
        timeout = int(
            config_registry.get_value("RECON_TIMEOUT", 180, value_type=int) or 180
        )
    except Exception:
        timeout = 180

    contributions: List[Dict[str, Any]] = []
    try:
        from core.core_initializer import PLUGIN_REGISTRY

        plugins = list(PLUGIN_REGISTRY.values())
    except Exception as e:
        log_warning(f"[recon] Failed to access PLUGIN_REGISTRY: {e}")
        plugins = []

    # Preflight whitelist (vessel embodiment turn): resolve the matcher once.
    _use_recon_whitelist = bool(recon_whitelist_patterns)
    _matches_recon_whitelist = None
    if _use_recon_whitelist:
        try:
            from plugins.rift_vessel.vessel_whitelist import matches_whitelist

            _matches_recon_whitelist = matches_whitelist
        except Exception:
            # Rift Vessel plugin unavailable: cannot apply the whitelist. Fall
            # back to the normal (unfiltered) path rather than dropping every
            # plugin, matching the caller's "plugin absent -> full recon" intent.
            _use_recon_whitelist = False

    eligible = []
    for p in plugins:
        has_combined = all(
            hasattr(p, attr)
            for attr in (
                "get_recon_key",
                "get_recon_instruction",
                "parse_recon_response",
            )
        )
        if (has_combined or hasattr(p, "get_recon_contributions")) and _plugin_enabled(
            p, "RECON"
        ):
            # In-world embodiment turn: keep only recon plugins whose recon key
            # matches the vessel whitelist. Structural fnmatch on the key name
            # (never message text). Fail-safe: no usable key -> excluded.
            if _use_recon_whitelist and _matches_recon_whitelist is not None:
                recon_key = ""
                try:
                    if hasattr(p, "get_recon_key"):
                        recon_key = str(p.get_recon_key() or "").strip()
                except Exception:
                    recon_key = ""
                if not recon_key or not _matches_recon_whitelist(
                    recon_key, list(recon_whitelist_patterns or [])
                ):
                    log_debug(
                        f"[recon] Plugin {p.__class__.__name__} recon_key="
                        f"{recon_key!r} not in vessel recon whitelist; skipping"
                    )
                    continue
            # Optional per-turn eligibility hook. A recon plugin may declare
            # ``is_recon_eligible(message, context_memory) -> bool`` to opt out
            # of a given turn *before* its key is baked into the combined recon
            # prompt (returning [] from parse_recon_response is too late — the
            # key is already in the single LLM call). Guarded and backward
            # compatible: any error or a missing hook defaults to eligible.
            if hasattr(p, "is_recon_eligible"):
                try:
                    if not p.is_recon_eligible(message, context_memory):
                        log_debug(
                            f"[recon] Plugin {p.__class__.__name__} opted out of "
                            "this turn via is_recon_eligible; skipping"
                        )
                        continue
                except Exception as exc:
                    log_warning(
                        f"[recon] is_recon_eligible failed for "
                        f"{p.__class__.__name__}: {exc}; treating as eligible"
                    )
            eligible.append(p)

    if not eligible:
        log_debug("[recon] No recon-capable plugins registered; skipping Recon")
        return []

    # Combine recon requests into a SINGLE LLM call to avoid multiple prompts.
    # Plugins must implement get_recon_key(), get_recon_instruction(), and
    # parse_recon_response(data, **kwargs).
    recon_plugins = []
    for plugin in eligible:
        if all(
            hasattr(plugin, attr)
            for attr in (
                "get_recon_key",
                "get_recon_instruction",
                "parse_recon_response",
            )
        ):
            recon_plugins.append(plugin)
        else:
            log_warning(
                f"[recon] Plugin {plugin.__class__.__name__} does not support combined Recon; skipping"
            )

    if not recon_plugins:
        log_debug("[recon] No combined recon plugins available; skipping Recon")
        return []

    recon_specs = []
    for plugin in recon_plugins:
        key = str(plugin.get_recon_key())
        instruction = str(plugin.get_recon_instruction())
        recon_specs.append((plugin, key, instruction))

    # Build combined system prompt
    keys = [key for _, key, _ in recon_specs]
    system_lines = [
        "This is a Recon prompt, please execute what is requested below:",
        "Return ONLY valid JSON with the following keys:",
        ", ".join(keys) + ".",
    ]
    for _, key, instruction in recon_specs:
        system_lines.append(f"- {key}: {instruction}")
    system_lines.append(
        "IMPORTANT for language_hint: base your language detection ONLY on the "
        "Recent local history and human user messages. Ignore the global history "
        "section entirely for language detection. Also ignore assistant/bot "
        "responses — they may contain language errors that should not influence "
        "detection."
    )
    system_lines.append("Do not add any extra keys or commentary.")
    system_prompt = "\n".join(system_lines)
    log_debug(f"[recon] system_prompt:\n{system_prompt}")

    # Shared user prompt (message + history)
    local_text, global_text = await _build_recon_history_texts_async(
        message=message, context_memory=context_memory
    )

    # For outbound beats (observer) the "text" is an internal Grillo prompt (in
    # English) which would mislead language detection.  Use only local history.
    _is_outbound = isinstance(context_memory, dict) and is_outbound_beat(
        context_memory.get("beat_type")
    )
    if _is_outbound:
        user_message_section = (
            "(System-generated proactive prompt — ignore for language detection)"
        )
        log_debug(
            "[recon] Outbound beat detected: excluding prompt text from "
            "language detection"
        )
    else:
        user_message_section = text.strip() if isinstance(text, str) else ""

    user_prompt = (
        f"User message:\n{user_message_section}\n\n"
        f"Recent local history:\n{local_text}\n\n"
        f"Recent global history:\n{global_text}\n"
    )
    log_debug(f"[recon] user_prompt:\n{user_prompt}")

    interface_path = getattr(message, "interface_path", None)
    cache_key = _make_hint_cache_key(interface_path, user_message_section)

    # Local lingua pre-check for lightweight language hints before expensive LLM path.
    use_local_precheck = bool(
        config_registry.get_value(
            "RECON_LOCAL_LANGUAGE_PRECHECK", True, value_type=bool
        )
    )
    local_language = None
    if use_local_precheck:
        local_language = _detect_language_locally(user_message_section)
        if not local_language and _is_outbound:
            local_language = _detect_language_locally(local_text)

    keys_set = set(keys)
    needs_language = "language_hint" in keys_set
    needs_tone = "tone_hint" in keys_set

    llm_text = None
    parsed: dict[str, Any] | None = None
    cached_hint = _get_cached_hint(cache_key)
    if cached_hint:
        has_lang = bool(cached_hint.get("language_code"))
        has_tone = bool(
            cached_hint.get("message_tone") or cached_hint.get("conversation_tone")
        )
        if (not needs_language or has_lang) and (not needs_tone or has_tone):
            parsed = {}
            if has_lang:
                parsed["language_hint"] = {
                    "language_code": str(cached_hint.get("language_code")),
                }
            if has_tone:
                parsed["tone_hint"] = {
                    "message_tone": cached_hint.get("message_tone"),
                    "conversation_tone": cached_hint.get("conversation_tone"),
                }
            log_debug("[recon] Hint cache hit — skipping Recon LLM call")

    # If only language is required and local precheck found a value, skip LLM.
    if parsed is None and local_language and needs_language and not needs_tone:
        parsed = {"language_hint": {"language_code": local_language}}
        log_debug(
            f"[recon] Local lingua precheck resolved language={local_language}; skipping Recon LLM call"
        )

    if parsed is None:
        # Single LLM call
        engine = None
        scope_model: str | None = None
        try:
            from core.config import (
                derive_cortex_scope,
                get_active_cortex_engine,
                get_active_cortex_scope,
            )
            from core.cortex_registry import get_cortex_registry

            scope = derive_cortex_scope(
                context_memory if isinstance(context_memory, dict) else None
            )
            try:
                active_cortex, scope_model = await get_active_cortex_scope(scope=scope)
            except TypeError:
                # Backward/test compatibility: some monkeypatched helpers still
                # expose the older no-kwargs signature.
                active_cortex = await get_active_cortex_engine()
            registry = get_cortex_registry()
            engine = registry.get_engine(active_cortex) or registry.load_engine(
                active_cortex
            )
        except Exception as e:
            log_warning(f"[recon] Failed to load active Cortex engine: {e}")
            engine = None

        if not engine or not hasattr(engine, "generate_response"):
            log_warning(
                "[recon] Active Cortex engine missing generate_response; continuing with empty Recon payload"
            )
            parsed = {}

        llm_text = None
        if parsed is None and engine is not None:
            try:
                from core.config import scope_model_override

                with scope_model_override(engine, scope_model):
                    llm_text = await asyncio.wait_for(
                        engine.generate_response(
                            [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt},
                            ]
                        ),
                        timeout=timeout,
                    )
                log_debug(f"[recon] LLM response:\n{llm_text}")
            except Exception as e:
                log_warning(f"[recon] Combined Recon LLM call failed: {e}")
                llm_text = None

        if llm_text is not None:
            try:
                from core.transport_layer import extract_json_from_text

                parsed = extract_json_from_text(llm_text, return_metadata=False)
            except Exception:
                parsed = None
        elif parsed is None:
            parsed = {}

    if not isinstance(parsed, dict):
        log_warning("[recon] Combined Recon response did not parse as JSON object")
        log_debug(f"[recon] parsed output: {parsed!r}")
        parsed = {}

    if local_language:
        lang_obj = parsed.get("language_hint")
        if needs_language and (
            not isinstance(lang_obj, dict) or not lang_obj.get("language_code")
        ):
            parsed["language_hint"] = {"language_code": local_language}
            log_debug(
                f"[recon] Applied local lingua fallback language={local_language}"
            )

    try:
        _lang_obj = parsed.get("language_hint")
        _tone_obj = parsed.get("tone_hint")
        _set_cached_hint(
            cache_key,
            language_code=(
                str(_lang_obj.get("language_code")).strip()
                if isinstance(_lang_obj, dict) and _lang_obj.get("language_code")
                else None
            ),
            message_tone=(
                str(_tone_obj.get("message_tone")).strip() or None
                if isinstance(_tone_obj, dict)
                else None
            ),
            conversation_tone=(
                str(_tone_obj.get("conversation_tone")).strip() or None
                if isinstance(_tone_obj, dict)
                else None
            ),
        )
    except Exception:
        pass

    # Dispatch responses to plugins
    # normalize keywords into single-word tokens before dispatching to plugins
    norm_keywords = await _normalize_keywords_list(keywords)

    for plugin, key, _ in recon_specs:
        plugin_name = plugin.__class__.__name__
        data = parsed.get(key)
        log_debug(f"[recon] dispatching parsed data to {plugin_name}: {data!r}")
        try:
            res = await plugin.parse_recon_response(
                data,
                message=message,
                context_memory=context_memory,
                text=text,
                tags=tags,
                keywords=norm_keywords,
                max_results=max_results,
                # Pass the raw LLM text so plugins can attempt their own
                # extraction when the JSON parser returned an empty dict.
                _raw_llm_text=llm_text,
            )
        except Exception as e:
            log_warning(f"[recon] Recon plugin {plugin_name} parse failed: {e}")
            res = []
        log_debug(f"[recon] {plugin_name} returned {res!r}")
        if res:
            log_debug(
                f"[recon] Recon plugin {plugin_name} returned {len(res)} contribution(s)"
            )
            contributions.extend(res)

    # Deduplicate naive by (type, content, source, id)
    seen: set[Tuple[str, str, str, str]] = set()
    dedup: List[Dict[str, Any]] = []
    for c in contributions:
        ctype = str(c.get("type", ""))
        content = str(c.get("content", ""))
        source = str(c.get("source", ""))
        cid = str(c.get("id", ""))
        key = (ctype, content[:200], source, cid)
        if key not in seen:
            seen.add(key)
            dedup.append(c)

    dedup.sort(key=lambda x: int(x.get("priority", 0)), reverse=True)
    log_debug(f"[recon] raw contributions before dedup: {contributions!r}")
    log_info(f"[recon] Collected {len(dedup)} contributions from plugins")
    log_debug(f"[recon] final deduplicated contributions: {dedup!r}")
    return dedup


async def resolve_language(
    *,
    contributions: List[Dict[str, Any]],
    interface_path: str | None,
    is_grillo_internal: bool,
    message=None,
) -> str | None:
    overrides = _parse_mapping(
        config_registry.get_value("INTERFACE_LANGUAGE_OVERRIDES", {}, value_type=dict)
    )
    if interface_path and interface_path in overrides:
        return str(overrides.get(interface_path) or "").strip() or None

    # Recon contributions
    lang_candidates = [
        c
        for c in contributions
        if isinstance(c, dict)
        and c.get("type") == "language_hint"
        and c.get("language_code")
    ]
    if lang_candidates:
        lang_candidates.sort(key=lambda x: int(x.get("priority", 0)), reverse=True)
        code = str(lang_candidates[0].get("language_code") or "").strip()
        if code:
            return code

    # Detector plugins
    try:
        timeout = int(
            config_registry.get_value("LANGUAGE_DETECTOR_TIMEOUT", 2, value_type=int)
            or 2
        )
    except Exception:
        timeout = 2

    try:
        from core.core_initializer import PLUGIN_REGISTRY

        plugins = list(PLUGIN_REGISTRY.values())
    except Exception:
        plugins = []

    detector_results: List[Tuple[int, str]] = []

    async def _call(plugin):
        try:
            if not hasattr(plugin, "detect_language"):
                return None
            rval = plugin.detect_language(
                message=message, interface_path=interface_path
            )
            if asyncio.iscoroutine(rval):
                rval = await rval
            if rval:
                priority = int(
                    getattr(plugin, "language_priority", getattr(plugin, "priority", 0))
                    or 0
                )
                return priority, str(rval)
        except Exception:
            return None
        return None

    calls = [asyncio.wait_for(_call(p), timeout=timeout) for p in plugins]
    results = await asyncio.gather(*calls, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception) or not r:
            continue
        if isinstance(r, tuple) and len(r) == 2:
            detector_results.append((int(r[0]), str(r[1])))

    if detector_results:
        detector_results.sort(key=lambda x: x[0], reverse=True)
        code = detector_results[0][1].strip()
        if code:
            return code

    if is_grillo_internal:
        return str(config_registry.get_value("DEFAULT_GRILLO_LANGUAGE", "en"))
    return str(config_registry.get_value("PROJECT_DEFAULT_LANGUAGE", "en") or "en")


async def resolve_tone(
    *,
    contributions: List[Dict[str, Any]],
    interface_path: str | None,
    is_grillo_internal: bool,
    message=None,
) -> Tuple[str | None, str | None]:
    overrides = _parse_mapping(
        config_registry.get_value("INTERFACE_TONE_OVERRIDES", {}, value_type=dict)
    )
    if interface_path and interface_path in overrides:
        tone = str(overrides.get(interface_path) or "").strip()
        if tone:
            return tone, None

    # Recon contributions
    tone_candidates = [
        c
        for c in contributions
        if isinstance(c, dict)
        and c.get("type") == "tone_hint"
        and (c.get("message_tone") or c.get("conversation_tone"))
    ]
    if tone_candidates:
        tone_candidates.sort(key=lambda x: int(x.get("priority", 0)), reverse=True)
        top = tone_candidates[0]
        msg_tone = str(top.get("message_tone") or "").strip() or None
        convo_tone = str(top.get("conversation_tone") or "").strip() or None
        return msg_tone, convo_tone

    # Detector plugins
    try:
        timeout = int(
            config_registry.get_value("TONE_DETECTOR_TIMEOUT", 2, value_type=int) or 2
        )
    except Exception:
        timeout = 2

    try:
        from core.core_initializer import PLUGIN_REGISTRY

        plugins = list(PLUGIN_REGISTRY.values())
    except Exception:
        plugins = []

    detector_results: List[Tuple[int, dict | str]] = []

    async def _call(plugin):
        try:
            if not hasattr(plugin, "detect_tone"):
                return None
            rval = plugin.detect_tone(message=message, interface_path=interface_path)
            if asyncio.iscoroutine(rval):
                rval = await rval
            if rval:
                priority = int(
                    getattr(plugin, "tone_priority", getattr(plugin, "priority", 0))
                    or 0
                )
                return priority, rval
        except Exception:
            return None
        return None

    calls = [asyncio.wait_for(_call(p), timeout=timeout) for p in plugins]
    results = await asyncio.gather(*calls, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception) or not r:
            continue
        if isinstance(r, tuple) and len(r) == 2:
            detector_results.append((int(r[0]), r[1]))

    if detector_results:
        detector_results.sort(key=lambda x: x[0], reverse=True)
        payload = detector_results[0][1]
        if isinstance(payload, dict):
            msg_tone = str(payload.get("message_tone") or "").strip() or None
            convo_tone = str(payload.get("conversation_tone") or "").strip() or None
            return msg_tone, convo_tone
        if isinstance(payload, str):
            return payload.strip(), None

    if is_grillo_internal:
        return str(config_registry.get_value("DEFAULT_GRILLO_TONE", "neutral")), None
    return (
        str(config_registry.get_value("PROJECT_DEFAULT_TONE", "neutral") or "neutral"),
        None,
    )
