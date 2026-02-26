import asyncio
import json
from typing import Any, Dict, List, Tuple
from core.logging_utils import log_debug, log_info, log_warning
from core.config_manager import config_registry

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
except Exception:
    pass


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
            cached = await load_chat_history(interface_path)
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


async def gather_recon_contributions(
    message=None,
    context_memory=None,
    text: str | None = None,
    tags: List[str] | None = None,
    keywords: List[str] | None = None,
    max_results: int | None = None,
) -> List[Dict[str, Any]]:
    """Call plugin hooks `get_recon_contributions` and merge results.

    Returns list of normalized contribution dicts.
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

    eligible = [
        p
        for p in plugins
        if hasattr(p, "get_recon_contributions") and _plugin_enabled(p, "RECON")
    ]

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

    # Build combined system prompt
    keys = [p.get_recon_key() for p in recon_plugins]
    system_lines = [
        "This is a Recon prompt, please execute what is requested below:",
        "Return ONLY valid JSON with the following keys:",
        ", ".join(keys) + ".",
    ]
    for plugin in recon_plugins:
        key = plugin.get_recon_key()
        instruction = plugin.get_recon_instruction()
        system_lines.append(f"- {key}: {instruction}")
    system_lines.append("Do not add any extra keys or commentary.")
    system_prompt = "\n".join(system_lines)
    log_debug(f"[recon] system_prompt:\n{system_prompt}")

    # Shared user prompt (message + history)
    local_text, global_text = await _build_recon_history_texts_async(
        message=message, context_memory=context_memory
    )
    user_prompt = (
        f"User message:\n{text.strip() if isinstance(text, str) else ''}\n\n"
        f"Recent local history:\n{local_text}\n\n"
        f"Recent global history:\n{global_text}\n"
    )
    log_debug(f"[recon] user_prompt:\n{user_prompt}")

    # Single LLM call
    engine = None
    try:
        from core.config import get_active_cortex_engine
        from core.cortex_registry import get_cortex_registry

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
            "[recon] Active Cortex engine missing generate_response; skipping Recon"
        )
        return []

    try:
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
        return []

    try:
        from core.transport_layer import extract_json_from_text

        parsed = extract_json_from_text(llm_text, return_metadata=False)
    except Exception:
        parsed = None

    if not isinstance(parsed, dict):
        log_warning("[recon] Combined Recon response did not parse as JSON object")
        log_debug(f"[recon] parsed output: {parsed!r}")
        return []

    # Dispatch responses to plugins
    # normalize keywords into single-word tokens before dispatching to plugins
    norm_keywords = await _normalize_keywords_list(keywords)

    for plugin in recon_plugins:
        key = plugin.get_recon_key()
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
