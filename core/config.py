# core/config.py

import os
import json
import asyncio
import time

from core.variables_engine import register_exposed_var as _register_exposed_var

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover - fallback when dotenv not installed

    def load_dotenv(*args, **kwargs):
        return False


# aiomysql is optional at import time — make the import lazy/fail-safe so
# importing core.config doesn't raise in environments where aiomysql isn't
# installed (e.g., lightweight tests or build-time checks). Modules that need
# aiomysql at runtime should check `aiomysql` is not None and raise a clear
# error if necessary.
try:
    import aiomysql
except Exception:
    aiomysql = None

from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.config_manager import config_registry
from core.languages import normalize_lang

"""
notify_trainer(chat_id: int, message: str) -> None
Send a notification to the trainer via the centralized logic in core/notifier.py.
"""

# ✅ Load all environment variables from .env
load_dotenv(dotenv_path="/app/.env", override=False)


def _parse_trainer_ids(raw_value: str) -> dict[str, int | str]:
    """Parse TRAINER_IDS string into a mapping."""
    mapping: dict[str, int | str] = {}
    if not raw_value:
        return mapping
    for entry in raw_value.split(","):
        if ":" not in entry:
            continue
        interface_name, trainer_id = entry.split(":", 1)
        interface_name = interface_name.strip()
        trainer_id = trainer_id.strip()
        if not interface_name or not trainer_id:
            continue
        try:
            mapping[interface_name] = int(trainer_id)
        except ValueError:
            mapping[interface_name] = trainer_id
    return mapping


# Trainer IDs configuration
_TRAINER_IDS_RAW = config_registry.get_var(
    "TRAINER_IDS",
    "",
    label="Trainer IDs",
    description=(
        "Trainer IDs by interface (each entry is interface name + trainer id)."
    ),
    group="core",
    component="core",
    tags=["key_value_list"],
)


def get_trainer_ids() -> dict[str, int | str]:
    """Parse and return current trainer IDs mapping."""
    return _parse_trainer_ids(str(_TRAINER_IDS_RAW))


def get_trainer_id(interface_name: str) -> int | str | None:
    """Return the trainer ID for the given interface."""
    return get_trainer_ids().get(interface_name)


# Backwards compatibility: module-level TRAINER_IDS mapping expected by some
# modules (e.g. core.notifier). This is populated at import time from the
# underlying config registry. Callers that need up-to-date values should use
# get_trainer_ids() instead, but we keep this symbol to avoid import errors.
TRAINER_IDS = get_trainer_ids()

# Trainer Name configuration
TRAINER_NAME = config_registry.get_var(
    "TRAINER_NAME",
    "Trainer",
    label="Trainer Name",
    description="The name of the trainer/mentor who has responsibility over this SyntH. This will appear in the bio.",
    group="core",
    component="core",
)


def get_trainer_display_name() -> str:
    """Resolve the configured trainer name(s) for use in prompt text.

    Read live from the config registry so runtime edits take effect without a
    restart, falling back to the module-level ``TRAINER_NAME``. Returns an empty
    string when no real name is configured (still the ``"Trainer"`` placeholder)
    so callers can omit the reference instead of leaking a default. A
    comma-separated multi-trainer value is preserved verbatim.
    """
    try:
        raw = config_registry.get_value("TRAINER_NAME", "Trainer")
    except Exception:
        raw = TRAINER_NAME
    name = str(raw or "").strip()
    if not name or name == "Trainer":
        return ""
    return name


BASE_CORTEX = config_registry.get_var(
    "BASE_CORTEX",
    "",
    label="Base Cortex",
    description="Default cortex engine used system-wide unless overridden by scope.",
    group="core",
    component="cortex",
    hidden=True,  # Managed via the Cortex Engines component selector
    allow_env_override=False,
)

GRILLO_CORTEX = config_registry.get_var(
    "GRILLO_CORTEX",
    "Default",
    label="Grillo Cortex",
    description="Cortex engine used for Grillo (Default means Base Cortex).",
    group="core",
    component="cortex",
    hidden=True,  # Managed via the Cortex Engines scope selectors
    allow_env_override=False,
)

TRAINER_CORTEX = config_registry.get_var(
    "TRAINER_CORTEX",
    "Default",
    label="Trainer Cortex",
    description="Cortex engine used for Trainer-originated requests ("
    "Default"
    " means Base Cortex).",
    group="core",
    component="cortex",
    hidden=True,  # Managed via the Cortex Engines scope selectors
    allow_env_override=False,
)

LIVE_CORTEX = config_registry.get_var(
    "LIVE_CORTEX",
    "Default",
    label="Live Cortex",
    description="Cortex engine used for live voice sessions (Default means Base Cortex). Only 'live' kind engines are selectable.",
    group="core",
    component="cortex",
    hidden=True,  # Managed via the Cortex Engines scope selectors
    allow_env_override=False,
)

AGENT_CORTEX = config_registry.get_var(
    "AGENT_CORTEX",
    "Default",
    label="Agent Cortex",
    description="Cortex engine used for the agentic loop (Default means Base Cortex). Lets the agent use an LLM better suited for agent/tool-calling work.",
    group="core",
    component="cortex",
    hidden=True,  # Managed via the Cortex Engines scope selectors
    allow_env_override=False,
)

VESSEL_CORTEX = config_registry.get_var(
    "VESSEL_CORTEX",
    "Default",
    label="Rift Vessel (Will) Cortex",
    description="Cortex engine used for Rift Vessel will beats — the slow volition turn where Synth authors its in-world goals (Default means Base Cortex).",
    group="core",
    component="cortex",
    hidden=True,  # Managed via the Cortex Engines scope selectors
    allow_env_override=False,
)

# Named engine-configuration presets (extra_config + optional model bundles)
# edited from the Engines tab.  Stored as a JSON list; hidden from the generic
# settings grid because it is managed by the dedicated preset UI in
# core/engine_config_presets.py.
ENGINE_CONFIG_PRESETS = config_registry.get_var(
    "ENGINE_CONFIG_PRESETS",
    [],
    label="Engine Config Presets",
    description="Named provider/model engine-config presets (JSON list).",
    group="core",
    component="cortex",
    hidden=True,
    value_type=list,
    allow_env_override=False,
)

# LLM generation request timeout. Caps how long the synth waits for a single
# cortex generation before aborting. On slow hardware a long reply can exceed a
# short timeout, which aborts the HTTP request and makes llama.cpp cancel the
# in-flight task ("should_stop"). The default is intentionally generous so weak
# hardware does not hit an invisible cap; override per host via the
# LLM_GENERATION_TIMEOUT_SEC env var (.env) or the WebUI. A per-endpoint
# extra_config["timeout"] still takes precedence when set.
LLM_GENERATION_TIMEOUT_SEC = config_registry.get_var(
    "LLM_GENERATION_TIMEOUT_SEC",
    1800,
    label="LLM Generation Timeout (s)",
    description=(
        "Maximum time in seconds to wait for a single LLM cortex generation "
        "before aborting. Raise this on slow hardware so long replies are not "
        "cut off mid-generation. Settable via the .env file."
    ),
    value_type=int,
    group="core",
    component="cortex",
)

# ----------------------------------------------------------------------
# Live session synchronization settings
# ----------------------------------------------------------------------
LIVE_SYNC_CHAT_HISTORY = config_registry.get_var(
    "LIVE_SYNC_CHAT_HISTORY",
    True,
    label="Live Sync Chat History",
    description=(
        "When enabled, text messages sent in Discord are forwarded into an "
        "active live voice session and the live prompt includes both the "
        "local live history and the global chat history across interfaces."
    ),
    group="core",
    component="live",
    value_type=bool,
    advanced=True,
)

LIVE_HISTORY_SYNC_INTERVAL = config_registry.get_var(
    "LIVE_HISTORY_SYNC_INTERVAL",
    30,
    label="Live History Sync Interval",
    description=(
        "Interval (seconds) between periodic polls that import recent text "
        "messages into any running live voice session."
    ),
    group="core",
    component="live",
    value_type=int,
    advanced=True,
)

# ----------------------------------------------------------------------
# Live voice configuration
# ----------------------------------------------------------------------
LIVE_VOICE_NAME = config_registry.get_var(
    "LIVE_VOICE_NAME",
    "Aoede",
    label="Live Voice",
    description=(
        "Prebuilt voice for Gemini Live API sessions. "
        "Each voice has a distinct character and tone."
    ),
    group="core",
    component="cortex_live",
)

LIVE_VOICE_STYLE = config_registry.get_var(
    "LIVE_VOICE_STYLE",
    "",
    label="Voice Style Prompt",
    description=(
        "Extra instructions appended to the live system prompt to shape how "
        "the model speaks (e.g. tone, pacing, vocabulary, personality quirks). "
        "Leave empty for default persona behavior."
    ),
    group="core",
    component="cortex_live",
)

_register_exposed_var(
    "LIVE_VOICE_NAME",
    label="Live Voice",
    default="Aoede",
    value_type=str,
    ui_type="select",
    description=(
        "Prebuilt voice for Gemini Live API sessions. "
        "Each voice has a distinct character and tone."
    ),
    scope="live",
    component="cortex_live",
    options=[
        # Female
        "Aoede",
        "Kore",
        "Leda",
        "Zephyr",
        "Autonoe",
        "Achernar",
        "Callirrhoe",
        "Despina",
        "Erinome",
        "Gacrux",
        "Laomedeia",
        "Pulcherrima",
        "Sulafat",
        "Vindemiatrix",
        # Male
        "Puck",
        "Charon",
        "Fenrir",
        "Orus",
        "Achird",
        "Algenib",
        "Algieba",
        "Alnilam",
        "Enceladus",
        "Iapetus",
        "Rasalgethi",
        "Sadachbia",
        "Sadaltager",
        "Schedar",
        "Umbriel",
        "Zubenelgenubi",
    ],
    hidden=True,
)

_register_exposed_var(
    "LIVE_VOICE_STYLE",
    label="Voice Style Prompt",
    default="",
    value_type=str,
    ui_type="textarea",
    description=(
        "Extra instructions appended to the live system prompt to shape how "
        "the model speaks (e.g. tone, pacing, vocabulary, personality quirks). "
        "Leave empty for default persona behavior."
    ),
    scope="live",
    component="cortex_live",
    hidden=True,
)

# Live session feature toggles
LIVE_AFFECTIVE_DIALOG = config_registry.get_var(
    "LIVE_AFFECTIVE_DIALOG",
    False,
    label="Affective Dialog",
    description="Model adapts tone/emotion to match the user's expression.",
    group="core",
    component="cortex_live",
)

LIVE_PROACTIVE_AUDIO = config_registry.get_var(
    "LIVE_PROACTIVE_AUDIO",
    False,
    label="Proactive Audio",
    description="Model can choose not to respond when audio is irrelevant.",
    group="core",
    component="cortex_live",
)

LIVE_THINKING_LEVEL = config_registry.get_var(
    "LIVE_THINKING_LEVEL",
    "minimal",
    label="Live Thinking Level",
    description=(
        "Reasoning depth for the Live session. "
        "'minimal' gives lowest latency; 'high' gives deepest reasoning. "
        "Applies to Gemini 3.1 Flash Live; legacy 2.5 model uses LIVE_THINKING_BUDGET."
    ),
    group="core",
    component="cortex_live",
)

_register_exposed_var(
    "LIVE_THINKING_LEVEL",
    label="Live Thinking Level",
    default="minimal",
    value_type=str,
    ui_type="select",
    options=["minimal", "low", "medium", "high"],
    description=(
        "Reasoning depth for the Live session. "
        "'minimal' gives lowest latency; 'high' gives deepest reasoning."
    ),
    scope="live",
    component="cortex_live",
)

# Legacy config kept for users on the 2.5 fallback model (affective/proactive sessions).
LIVE_THINKING_BUDGET = config_registry.get_var(
    "LIVE_THINKING_BUDGET",
    0,
    label="Live Thinking Budget (legacy 2.5)",
    description=(
        "Internal reasoning token budget for the legacy Gemini 2.5 Live model. "
        "Only used when affective dialog or proactive audio is enabled. "
        "0 = disabled."
    ),
    group="core",
    component="cortex_live",
    hidden=True,
)

LIVE_AUDIO_MIN_RMS = config_registry.get_var(
    "LIVE_AUDIO_MIN_RMS",
    500,
    label="Live Audio Noise Gate (RMS)",
    description=(
        "Minimum RMS amplitude required before a Discord audio packet is forwarded "
        "to the Live API. Packets below this threshold are silently discarded, "
        "preventing mic hiss / background noise from triggering activity_start and "
        "causing the model to transcribe garbage. Typical ambient noise sits around "
        "100–300; speech is usually above 500. Set to 0 to disable the gate."
    ),
    group="core",
    component="cortex_live",
    value_type=int,
)

# --- LogChat configuration (use config_registry so exposed-variable APIs are consistent)
# The LogChat target is stored as a single canonical interface_path
# (e.g. "telegram_bot/-28475648/6"), following the same "interface-path"
# standard used by other routing config vars. The interface name, chat id
# and (optional) thread id are derived from this single value.
_register_exposed_var(
    "LOG_CHAT_ID",
    label="Log Chat",
    default="",
    value_type=str,
    ui_type="interface-path",
    description=(
        "Interface path of the chat used for system/trainer notifications "
        "(e.g. telegram_bot/-28475648/6)."
    ),
    scope="core",
    component="logchat",
)


# ---------------------------------------------------------------------------
# Cortex scope value (engine + optional model) storage helpers
# ---------------------------------------------------------------------------
# Each scope config key (BASE_CORTEX, AGENT_CORTEX, GRILLO_CORTEX,
# TRAINER_CORTEX, LIVE_CORTEX, VESSEL_CORTEX) stores either:
#   - a bare engine name string (legacy): the endpoint's default_model is used;
#   - a JSON object {"engine": "...", "model": "..."}: the model overrides the
#     endpoint default for that scope only.
# These helpers parse/serialize both forms so the rest of the system can move
# to per-scope model selection without breaking existing string values.


def parse_cortex_scope_value(raw: str | None) -> tuple[str, str | None]:
    """Split a raw scope config value into ``(engine, model)``.

    Accepts a bare engine-name string (legacy) or a JSON object with
    ``engine``/``model`` keys. ``model`` is ``None`` when unset, so callers
    fall back to the endpoint's ``default_model``.
    """
    text = str(raw or "").strip()
    if not text:
        return "", None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except Exception:
            return text, None
        if isinstance(data, dict):
            engine = str(data.get("engine") or "").strip()
            model_raw = data.get("model")
            model = str(model_raw).strip() if model_raw else ""
            return engine, (model or None)
        return text, None
    return text, None


def serialize_cortex_scope_value(engine: str, model: str | None = None) -> str:
    """Serialize an ``(engine, model)`` selection for storage.

    Returns a bare engine string when ``model`` is empty (keeps legacy values
    tidy and retrocompatible), otherwise a compact JSON object.
    """
    engine = str(engine or "").strip()
    model = str(model or "").strip()
    if not model:
        return engine
    return json.dumps({"engine": engine, "model": model})


# General rule (see AGENTS.md / user request "regola generale per tutti gli
# override"): whenever a cortex *scope override* (AGENT_CORTEX, GRILLO_CORTEX,
# TRAINER_CORTEX, LIVE_CORTEX, VESSEL_CORTEX, ...) points at an engine that is
# not currently usable, the resolver degrades transparently to the non-override
# Base Cortex. Every such degradation is announced in the logs *and* on LogChat
# so the operator can see the misconfiguration -- but only once per override key
# per process, to avoid flooding LogChat on every prompt build.
_CORTEX_OVERRIDE_FALLBACK_WARNED: set[str] = set()


def _warn_cortex_override_fallback(
    override_key: str, chosen: str, fallback: str
) -> None:
    """Announce an override→Base cortex degradation to logs and LogChat.

    Emitted every time in the logs (cheap, always useful when debugging) but
    pushed to LogChat only once per ``override_key`` per process so a broken
    override does not spam the operator's chat on every resolution.
    """
    log_warning(
        f"[config] ⚠️ Cortex override {override_key}='{chosen}' is unavailable; "
        f"falling back to Base Cortex '{fallback}'. Fix {override_key} to silence this."
    )
    if override_key in _CORTEX_OVERRIDE_FALLBACK_WARNED:
        return
    _CORTEX_OVERRIDE_FALLBACK_WARNED.add(override_key)
    try:
        from core.notifier import notifier

        notifier(
            f"⚠️ Cortex override {override_key} ('{chosen}') is unavailable "
            f"(unregistered or missing cortex capability). Falling back to Base "
            f"Cortex '{fallback}'. Please fix {override_key}."
        )
    except Exception as notify_exc:  # pragma: no cover - defensive
        log_debug(
            f"[config] Could not notify LogChat about the cortex override "
            f"fallback for {override_key}: {notify_exc}"
        )


async def get_active_cortex_engine(scope: str | None = None) -> str:
    """Return the effective cortex engine for a given scope.

    Scope can be "grillo", "trainer", or None. The returned engine must exist
    in the Cortex registry, otherwise a ValueError is raised.
    """
    try:
        base, _ = parse_cortex_scope_value(config_registry.get_value("BASE_CORTEX", ""))
        override_key: str | None = None
        if scope == "grillo":
            override_key = "GRILLO_CORTEX"
        elif scope == "trainer":
            override_key = "TRAINER_CORTEX"
        elif scope == "live":
            override_key = "LIVE_CORTEX"
        elif scope == "agent":
            override_key = "AGENT_CORTEX"
        elif scope == "vessel":
            override_key = "VESSEL_CORTEX"
        else:
            override_key = None

        override_raw = (
            config_registry.get_value(override_key, "Default")
            if override_key is not None
            else "Default"
        )
        override, _ = parse_cortex_scope_value(override_raw)
        if not override:
            override = "Default"

        use_override = override_key is not None and override not in (
            None,
            "",
            "Default",
            "None",
        )

        chosen = override if use_override else base
        if not chosen:
            raise ValueError("BASE_CORTEX is not configured")

        from core.cortex_registry import get_cortex_registry

        reg = get_cortex_registry()
        available = set(reg.get_available_engines())

        # "anthropic" is a real, always-registered built-in engine, so the
        # staleness check below never fires for it -- but without
        # ANTHROPIC_API_KEY configured it doesn't raise, it silently returns a
        # fixed "not configured" string as if it were a genuine completion.
        # The JSON corrector then retries against the same broken engine and
        # loops forever on that identical string (see FIXED_ISSUES.md:
        # "BASE_CORTEX silently reverted to anthropic"). Treat it as
        # unavailable whenever no key is configured so the self-heal path
        # below runs instead of quietly returning a guaranteed-broken engine.
        if (
            "anthropic" in available
            and not str(
                config_registry.get_value("ANTHROPIC_API_KEY", "") or ""
            ).strip()
        ):
            available.discard("anthropic")

        # General rule for *every* cortex scope: an engine that is registered
        # (probe may even read "success") but whose external endpoint is NOT
        # actually cortex-capable is guaranteed to fail as an LLM backend --
        # e.g. AGENT_CORTEX='logfare-mykey', an endpoint whose auto-probe found
        # no cortex capability (capabilities.cortex=false) yet 401s on
        # generate_response. Such an engine passes the plain ``chosen in
        # available`` registration check and would be returned verbatim,
        # starving the scope. Prune every non-cortex external endpoint from
        # ``available`` up front so ``chosen`` (or ``base``/``sibling``) falls
        # into the override→Base degradation below.
        #
        # Key off the *auto-probed* ``capabilities`` map, NOT the merged
        # ``effective_subsystem_map()``: the latter lets a manual
        # ``subsystem_map`` override force cortex=true on top of a failed probe
        # (the exact logfare-mykey/logfare-claude misconfiguration -- probe
        # says cortex=false, operator override says true, endpoint 401s). The
        # honest structural signal of real cortex capability is the probe
        # result. Detection stays purely structural (the capability map),
        # never keyword/string matching.
        try:
            from core.external_endpoints.registry import (
                get_external_endpoint_registry,
            )

            _all_endpoints = await get_external_endpoint_registry().list_endpoints(
                enabled_only=True
            )
            _non_cortex = {
                ep.engine_name()
                for ep in _all_endpoints
                if not ep.capabilities.get("cortex")
            }
            if _non_cortex:
                available.difference_update(_non_cortex)
        except Exception as cap_exc:  # pragma: no cover - defensive
            log_debug(
                f"[config] Could not verify cortex capability of external "
                f"endpoints: {cap_exc}"
            )

        if chosen not in available:
            # Before treating this as a genuinely stale/removed engine, check
            # whether it's a still-configured external endpoint (e.g. Venice2)
            # that simply hasn't (re)registered into the in-memory
            # CortexRegistry yet -- this happens transiently around startup or
            # endpoint edits. Persisting the fallback below in that case would
            # silently and *permanently* discard the user's real selection,
            # since get_default_engine() just returns whichever built-in engine
            # module sorts first on disk (currently "anthropic") -- not a
            # meaningful default. See AGENTS.md SS12 for the incident this guards
            # against (BASE_CORTEX kept reverting to anthropic).
            try:
                from core.external_endpoints.registry import (
                    get_external_endpoint_registry,
                )

                endpoints = await get_external_endpoint_registry().list_endpoints(
                    enabled_only=True
                )
                # Only keep ``chosen`` if it is a configured external endpoint
                # that the auto-probe found genuinely *cortex*-capable. An
                # endpoint whose probed ``capabilities.cortex`` is False (e.g. an
                # STT/vision-only key, or one whose cortex probe failed) is
                # guaranteed to fail as an LLM engine -- keeping it here would
                # starve the scope (the exact AGENT_CORTEX=logfare-mykey 401
                # case). We deliberately read the probed ``capabilities`` and
                # NOT ``effective_subsystem_map()``, because a manual
                # ``subsystem_map`` override can force cortex=true on top of a
                # failed probe -- which is precisely the misconfiguration this
                # guards against. Detection is purely structural (the capability
                # map), never keyword/string matching. This makes the
                # override→Base degradation below the *general rule* for every
                # scope, not just a one-off transient-registration escape hatch.
                cortex_endpoints = {
                    ep.engine_name()
                    for ep in endpoints
                    if ep.capabilities.get("cortex")
                }
                if chosen in cortex_endpoints:
                    log_warning(
                        f"[config] ⚠️ Cortex engine '{chosen}' is a configured "
                        "external endpoint not yet registered in the CortexRegistry "
                        "-- keeping it instead of silently switching away."
                    )
                    log_debug(
                        f"[config] 🧠 Active Cortex ({scope or 'base'}): {chosen}"
                    )
                    return chosen
            except Exception as ext_exc:
                log_warning(
                    f"[config] Failed to check external endpoints for '{chosen}': {ext_exc}"
                )

            # Stale engine name in DB (e.g. removed engine from a previous branch).
            # Fall back to the registry default rather than leaving the system broken.
            updates: list[tuple[str, str]] = []
            if use_override and override_key is not None and base in available:
                fallback = base
                updates.append((override_key, "Default"))
            else:
                try:
                    fallback = reg.get_default_engine()
                except ValueError:
                    raise ValueError(f"Cortex engine '{chosen}' is not registered")
                if fallback == "anthropic":
                    # get_default_engine() has no concept of credential
                    # availability -- it just returns whichever built-in
                    # engine module sorts first on disk, which is
                    # "anthropic". If no key is configured this is a
                    # guaranteed-broken pick. Reuse whichever engine is
                    # already validly configured for the sibling
                    # trainer/grillo scope on this same instance instead of
                    # guessing at an arbitrary external endpoint.
                    for sibling_key in ("TRAINER_CORTEX", "GRILLO_CORTEX"):
                        sibling, _ = parse_cortex_scope_value(
                            config_registry.get_value(sibling_key, "Default")
                        )
                        if (
                            sibling
                            and sibling not in ("Default", "None")
                            and sibling in available
                        ):
                            fallback = sibling
                            break
                if use_override and override_key is not None:
                    updates.append((override_key, "Default"))
                if base != fallback:
                    updates.append(("BASE_CORTEX", fallback))

            if use_override and override_key is not None:
                # Override→Base degradation is the general rule: announce it in
                # the logs *and* on LogChat (once per override key) so the
                # operator sees the misconfiguration.
                _warn_cortex_override_fallback(override_key, chosen, fallback)
            else:
                log_warning(
                    f"[config] ⚠️ Cortex engine '{chosen}' is no longer registered. "
                    f"Falling back to '{fallback}'. "
                    f"Update BASE_CORTEX to silence this warning."
                )
            try:
                seen_keys: set[str] = set()
                for key, value in updates:
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    await config_registry.set_value(key, value)
            except Exception:
                pass
            chosen = fallback

        log_debug(f"[config] 🧠 Active Cortex ({scope or 'base'}): {chosen}")
        return chosen
    except Exception as e:
        log_error(f"[config] ❌ Error resolving active cortex: {repr(e)}")
        raise


async def get_active_cortex_scope(scope: str | None = None) -> tuple[str, str | None]:
    """Resolve the effective ``(engine, model)`` for a given scope.

    The engine is resolved via :func:`get_active_cortex_engine` (which owns the
    self-heal / fallback logic and returns a bare engine name). The model is the
    per-scope override stored alongside the engine; it is only honoured when the
    resolved engine actually matches the scope's configured engine — if a
    fallback kicked in (stale/removed engine) the stored model no longer applies
    and ``None`` is returned so the endpoint's ``default_model`` is used.

    Resolution order for the model:
      1. the scope override key's model (e.g. ``AGENT_CORTEX``), if the scope's
         configured engine survived resolution;
      2. otherwise the base ``BASE_CORTEX`` model, if the resolved engine equals
         the base engine;
      3. otherwise ``None`` (endpoint default).
    """
    engine = await get_active_cortex_engine(scope=scope)

    override_key: str | None = None
    if scope == "grillo":
        override_key = "GRILLO_CORTEX"
    elif scope == "trainer":
        override_key = "TRAINER_CORTEX"
    elif scope == "live":
        override_key = "LIVE_CORTEX"
    elif scope == "agent":
        override_key = "AGENT_CORTEX"
    elif scope == "vessel":
        override_key = "VESSEL_CORTEX"

    if override_key is not None:
        ov_engine, ov_model = parse_cortex_scope_value(
            config_registry.get_value(override_key, "Default")
        )
        if ov_engine and ov_engine not in ("Default", "None") and ov_engine == engine:
            return engine, ov_model

    base_engine, base_model = parse_cortex_scope_value(
        config_registry.get_value("BASE_CORTEX", "")
    )
    if base_engine and base_engine == engine:
        return engine, base_model

    return engine, None


def scope_model_override(engine_instance: object, model: str | None):
    """Return a context manager applying a per-scope ``model`` to ``engine_instance``.

    Scope-aware call sites resolve ``(engine_name, model)`` via
    :func:`get_active_cortex_scope`, load the engine instance from the cortex
    registry, then wrap the generation call with this helper. When the instance
    is an external ``CortexBridge`` it delegates to the bridge's own
    ``scope_model_override`` (transient, per-call); for built-in engines (which
    have no per-call model concept) it is a no-op. This keeps the ``isinstance``
    detail in one place instead of every call site.
    """
    override = getattr(engine_instance, "scope_model_override", None)
    if callable(override) and model:
        try:
            return override(model)
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"[config] scope_model_override failed: {exc}")
    from contextlib import nullcontext

    return nullcontext()


def derive_cortex_scope(context: dict | None) -> str | None:
    """Return the scope string implied by *context*, or ``None`` for the base engine.

    Reads the same context flags used throughout the message chain so that
    Recon, the main LLM call, and Debrief all route to the same engine for a
    given request.  This is the single authoritative place that maps context
    keys to scope strings.

    Scope values mirror those accepted by :func:`get_active_cortex_engine`:
    ``"trainer"``, ``"grillo"``, ``"vessel"``, or ``None`` (base engine).
    """
    if not isinstance(context, dict):
        return None
    if context.get("is_trainer"):
        return "trainer"
    # Diary consolidation ("diary_merge") is a grillo-family background task, but
    # it is re-dispatched as its own interface without the ``grillo_beat`` flag.
    # Route it explicitly to the grillo scope so it follows GRILLO_CORTEX. Without
    # this it falls through to BASE_CORTEX and silently breaks whenever the base
    # engine is not usable (e.g. a keyless Anthropic base) — a near-undebuggable
    # failure for end users.
    if context.get("grillo_beat") or context.get("diary_merge_beat"):
        return "grillo"
    # Rift Vessel embodiment turns (the slow will beat where Synth authors its
    # in-world goals) route to VESSEL_CORTEX. Detection is purely structural
    # routing metadata (never message text), reusing the single canonical
    # detector shared with history_engine/agent_router.
    try:
        from core.interface_path_utils import is_vessel_embodiment_context

        if is_vessel_embodiment_context(context):
            return "vessel"
    except Exception:
        pass
    return None


async def set_base_cortex(name: str, model: str | None = None) -> None:
    """Persist the base cortex engine selection (with an optional model)."""
    value = serialize_cortex_scope_value(name, model)
    try:
        await config_registry.set_value("BASE_CORTEX", value)
        log_info(f"[config] 💾 Saved base cortex to database: {value}")
    except Exception as e:
        log_error(f"[config] ❌ Error saving BASE_CORTEX to database: {repr(e)}")
        raise


async def set_scope_cortex(scope: str, name: str, model: str | None = None) -> None:
    """Persist a scope-specific cortex override (with an optional model)."""
    if scope == "grillo":
        key = "GRILLO_CORTEX"
    elif scope == "live":
        key = "LIVE_CORTEX"
    elif scope == "agent":
        key = "AGENT_CORTEX"
    elif scope == "vessel":
        key = "VESSEL_CORTEX"
    else:
        key = "TRAINER_CORTEX"
    value = serialize_cortex_scope_value(name, model)
    try:
        await config_registry.set_value(key, value)
        log_info(f"[config] 💾 Saved {key} to database: {value}")
    except Exception as e:
        log_error(f"[config] ❌ Error saving {key} to database: {repr(e)}")
        raise


# ---------------------------------------------------------------------------
# Per-path cortex overrides (in-memory, volatile — reset on restart)
# Used by LiveSessionManager to route a specific interface_path to a live engine
# without affecting the global BASE_CORTEX or scope overrides.
# ---------------------------------------------------------------------------
_path_cortex_overrides: dict[str, str] = {}


def set_path_cortex_override(interface_path: str, engine_name: str) -> None:
    """Override the cortex engine for a specific interface_path (in-memory, volatile)."""
    _path_cortex_overrides[interface_path] = engine_name
    log_info(
        f"[config] 🔀 Per-path cortex override set: {interface_path} → {engine_name}"
    )


def clear_path_cortex_override(interface_path: str) -> None:
    """Remove the per-path cortex override, restoring normal routing."""
    removed = _path_cortex_overrides.pop(interface_path, None)
    if removed is not None:
        log_info(f"[config] 🔀 Per-path cortex override cleared for: {interface_path}")


async def get_active_cortex_for_path(
    interface_path: str | None,
    scope: str | None = None,
) -> str:
    """Resolve the cortex engine for an interface_path.

    Priority:
    1. Per-path in-memory override (set e.g. during a live voice session).
    2. get_active_cortex_engine(scope) — normal scope-based routing.
    """
    if interface_path and interface_path in _path_cortex_overrides:
        engine = _path_cortex_overrides[interface_path]
        log_debug(f"[config] 🧠 Per-path cortex ({interface_path}): {engine}")
        return engine
    return await get_active_cortex_engine(scope=scope)


# ---------------------------------------------------------------------------
# Vox per-language engine overrides
# ---------------------------------------------------------------------------
# A single global JSON map (VOX_LANGUAGE_OVERRIDES) routes TTS to a different
# engine/model/voice depending on the detected language of the text, e.g.
# {"it": {"engine": "fish-audio", "model": "s2.1-pro", "voice": "maria"},
#  "en": {"engine": "kitten", "model": "", "voice": "luna"}}.
# When a language is not present (or its engine is "disabled") the caller falls
# back to the normal ACTIVE_VOX_ENGINE / VOX_DEFAULT_MODEL / <ENGINE>_VOICE flow.


def get_vox_language_override(language: str | None) -> dict | None:
    """Return the Vox override entry for ``language``, or ``None``.

    The lookup key is normalised (region stripped, lowercased) so ``it-it``
    matches an ``"it"`` entry. Returns ``None`` when there is no override for
    the language, when the map is empty/invalid, or when the matched entry's
    engine is ``"disabled"`` (explicit opt-out → use the default engine).

    NOTE: this is the synchronous, cache-only variant. It reads the value that
    was loaded into the registry at startup (``load_all_from_db``). Because the
    registry deliberately skips DB loads inside a running event loop, callers
    running inside ``async`` code (e.g. ``VoxPlugin.speak``) must use
    :func:`get_vox_language_override_async` instead, which reads the persisted
    DB value directly.
    """
    norm = normalize_lang(language)
    if not norm:
        return None
    try:
        raw = config_registry.get_value("VOX_LANGUAGE_OVERRIDES", "{}", value_type=str)
        mapping = json.loads(raw) if raw else {}
    except Exception as exc:
        log_warning(f"[config] VOX_LANGUAGE_OVERRIDES parse failed: {exc}")
        return None
    if not isinstance(mapping, dict):
        return None
    entry = mapping.get(norm)
    if not isinstance(entry, dict):
        return None
    engine = entry.get("engine")
    if engine == "disabled":
        return None
    return entry


# In-memory cache so we don't hit the DB on every single TTS call. The cache is
# invalidated whenever the override map is written (see ``_invalidate_vox_lang_override_cache``).
_vox_lang_override_cache: dict | None = None
_vox_lang_override_cache_at: float = 0.0
_VOX_LANG_OVERRIDE_CACHE_TTL_S = 5.0


def _invalidate_vox_lang_override_cache() -> None:
    """Drop the cached override map (called after a successful write)."""
    global _vox_lang_override_cache, _vox_lang_override_cache_at
    _vox_lang_override_cache = None
    _vox_lang_override_cache_at = 0.0


async def get_vox_language_override_async(language: str | None) -> dict | None:
    """Async variant of :func:`get_vox_language_override`.

    Reads the persisted ``VOX_LANGUAGE_OVERRIDES`` value directly from the DB
    (via ``get_persisted_value``, which is safe inside a running event loop),
    bypassing the registry's "skip DB load in async context" behaviour. Results
    are cached in-memory for a short TTL to avoid a query per TTS call.
    """
    norm = normalize_lang(language)
    if not norm:
        return None
    global _vox_lang_override_cache, _vox_lang_override_cache_at
    now = time.time()
    mapping = _vox_lang_override_cache
    if (
        mapping is None
        or (now - _vox_lang_override_cache_at) > _VOX_LANG_OVERRIDE_CACHE_TTL_S
    ):
        try:
            raw = await config_registry.get_persisted_value(
                "VOX_LANGUAGE_OVERRIDES", "{}"
            )
            mapping = json.loads(raw) if raw else {}
        except Exception as exc:
            log_warning(f"[config] VOX_LANGUAGE_OVERRIDES parse failed: {exc}")
            mapping = {}
        if not isinstance(mapping, dict):
            mapping = {}
        _vox_lang_override_cache = mapping
        _vox_lang_override_cache_at = now
    entry = mapping.get(norm)
    if not isinstance(entry, dict):
        return None
    engine = entry.get("engine")
    if engine == "disabled":
        return None
    return entry


async def switch_active_cortex_engine(name: str, use_hot_swap: bool = True):
    """Switch the Base Cortex engine and reload the active plugin."""
    from core.cortex_registry import get_cortex_registry

    reg = get_cortex_registry()
    if name not in reg.get_available_engines():
        raise ValueError(
            f"Cortex engine '{name}' is not available. Available: {', '.join(reg.get_available_engines())}"
        )

    current = config_registry.get_value("BASE_CORTEX", "")

    def _get_loaded_plugin_name() -> str | None:
        try:
            from core import plugin_instance

            loaded = getattr(plugin_instance, "plugin", None)
            if loaded is None:
                return None
            # Reverse-lookup in the registry so that direct-instance engines
            # (e.g. ExternalCortexEngine registered as "ext_xyz") are matched
            # by their registry key, not by their Python module name.
            try:
                from core.cortex_registry import get_cortex_registry

                reg = get_cortex_registry()
                for engine_name in reg.get_available_engines():
                    if reg.get_engine(engine_name) is loaded:
                        return engine_name
            except Exception:
                pass
            # Fallback: module-based name for non-direct engines
            return loaded.__class__.__module__.split(".")[-1]
        except Exception:
            return None

    loaded_name = _get_loaded_plugin_name()
    if name == current and loaded_name == name:
        log_debug(
            f"[config] 🔄 Cortex already active and loaded: {name}, no switch needed."
        )
        return

    # Ensure only one cortex switch runs at a time
    log_debug(f"[config] ⏳ Waiting to acquire Cortex switch lock for '{name}'")
    async with _cortex_switch_lock:
        log_debug(f"[config] 🔒 Acquired Cortex switch lock for '{name}'")
        current = config_registry.get_value("BASE_CORTEX", "")
        loaded_name = _get_loaded_plugin_name()
        if name == current and loaded_name == name:
            log_debug(
                f"[config] 🔄 Cortex already active and loaded under lock: {name}, no switch needed."
            )
            return

        try:
            if name != current:
                await set_base_cortex(name)
                log_info(f"[config] 🔄 Switching Cortex from {current} to {name}")
            else:
                log_info(
                    f"[config] 🔄 BASE_CORTEX already '{name}' but loaded plugin is '{loaded_name}', reloading engine"
                )
        except Exception as e:
            log_error(f"[config] ❌ Error persisting base cortex '{name}': {e}", exc=e)
            raise

        try:
            if use_hot_swap:
                from core.plugin_instance import load_plugin

                await load_plugin(name, ensure_started=True, start_timeout=30.0)
                log_info(f"[config] ✅ Cortex hot-swapped to {name}")
                try:
                    from core.notifier import notify_trainer

                    notify_trainer(f"✅ Cortex engine dynamically updated to `{name}`.")
                except Exception as e:  # pragma: no cover
                    log_warning(
                        f"[config] Failed to notify trainer about Cortex change: {e}"
                    )
            else:
                from core.core_initializer import core_initializer

                await core_initializer.initialize_all()
                log_info(
                    f"[config] ✅ Cortex switched to {name} (full reinitialization)"
                )
                try:
                    from core.notifier import notify_trainer

                    notify_trainer(f"✅ Cortex engine dynamically updated to `{name}`.")
                except Exception as e:  # pragma: no cover
                    log_warning(
                        f"[config] Failed to notify trainer about Cortex change: {e}"
                    )
        except Exception as e:
            log_error(f"[config] ❌ Failed to switch Cortex to {name}: {e}", exc=e)
            try:
                from core.notifier import notify_trainer

                notify_trainer(f"❌ Failed to switch Cortex to `{name}`: {e}")
            except Exception:
                pass
            # Re-raise so callers (and tests) can handle failure deterministically
            raise
        finally:
            log_debug(f"[config] 🔓 Released Cortex switch lock for '{name}'")


# The LogChat target is stored as a single canonical interface_path in the
# `LOG_CHAT_ID` config key (e.g. "telegram_bot/-28475648/6"). The cache holds
# that raw path; the interface name, chat id and thread id are derived from it.
_log_chat_path: str | None = None  # cached log chat interface_path


def _parse_log_chat_path(path: str | None) -> tuple[str | None, int | None, int | None]:
    """Split a LogChat interface_path into (interface, chat_id, thread_id).

    Returns (None, None, None) when the path is empty. chat_id/thread_id are
    returned as int when numeric, otherwise None.
    """
    if not path:
        return (None, None, None)

    from core.interface_path_utils import extract_legacy_ids

    parts = extract_legacy_ids(path)
    interface = parts.get("interface") or None

    def _to_int(value: str | None) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    return (interface, _to_int(parts.get("chat_id")), _to_int(parts.get("thread_id")))


def _load_log_chat_path() -> str | None:
    """Load and cache the raw LogChat interface_path from config_registry."""
    global _log_chat_path
    if _log_chat_path is None:
        try:
            raw = config_registry.get_value("LOG_CHAT_ID", "")
            _log_chat_path = raw if raw else None
            log_debug(
                f"[config] 📥 Loaded LOG_CHAT_ID (path) via config_registry: {_log_chat_path}"
            )
        except Exception as e:
            log_error(f"[config] ❌ Error in _load_log_chat_path(): {repr(e)}")
    return _log_chat_path


def _on_log_chat_id_changed(new_value: object) -> None:
    """Invalidate the cached path when LOG_CHAT_ID changes from any source.

    The WebUI writes LOG_CHAT_ID directly via config_registry.set_value (the
    generic exposed-var endpoint), bypassing set_log_chat_id_and_thread, so the
    module-level cache must be refreshed on every change.
    """
    global _log_chat_path
    _log_chat_path = str(new_value) if new_value else None


try:
    config_registry.add_listener("LOG_CHAT_ID", _on_log_chat_id_changed)
except Exception as e:  # pragma: no cover - registry not ready
    log_debug(f"[config] Could not register LOG_CHAT_ID listener: {repr(e)}")


async def get_log_chat_id() -> int | None:
    """Return the configured log chat ID, derived from the `LOG_CHAT_ID` path."""
    _, chat_id, _ = _parse_log_chat_path(_load_log_chat_path())
    return chat_id


async def get_log_chat_interface() -> str | None:
    """Return the configured log chat interface, derived from the `LOG_CHAT_ID` path."""
    interface, _, _ = _parse_log_chat_path(_load_log_chat_path())
    return interface


async def get_log_chat_thread_id() -> int | None:
    """Return the configured log chat thread ID, derived from the `LOG_CHAT_ID` path."""
    _, _, thread_id = _parse_log_chat_path(_load_log_chat_path())
    return thread_id


async def set_log_chat_id(chat_id: int) -> None:
    """Persist and cache the log chat as a bare chat id (no interface/thread)."""
    await set_log_chat_id_and_thread(chat_id)


async def set_log_chat_id_and_thread(
    chat_id: int, thread_id: int | None = None, interface: str = "webui"
) -> None:
    """Compose and persist the LogChat target as a single interface_path.

    The interface, chat id and optional thread id are joined into one canonical
    interface_path (e.g. "telegram_bot/-28475648/6") stored in `LOG_CHAT_ID`.
    """
    global _log_chat_path

    from core.interface_path_utils import build_interface_path_from_legacy

    path = build_interface_path_from_legacy(interface, chat_id, thread_id)
    _log_chat_path = path

    try:
        await config_registry.set_value("LOG_CHAT_ID", path)
        log_debug(f"[config] 💾 Saved LOG_CHAT_ID (path) via config_registry: {path}")
    except Exception as e:
        log_error(f"[config] ❌ Error in set_log_chat_id_and_thread(): {repr(e)}")


def get_log_chat_id_sync() -> int | None:
    """Synchronous helper to fetch the cached log chat ID."""
    _, chat_id, _ = _parse_log_chat_path(_load_log_chat_path())
    return chat_id


def get_log_chat_interface_sync() -> str | None:
    """Synchronous helper to fetch the cached log chat interface."""
    interface, _, _ = _parse_log_chat_path(_load_log_chat_path())
    return interface


def get_log_chat_thread_id_sync() -> int | None:
    """Synchronous helper to fetch the cached log chat thread ID."""
    _, _, thread_id = _parse_log_chat_path(_load_log_chat_path())
    return thread_id


def list_available_llms():
    """Return available LLM engine names (delegates to CortexRegistry `llm_provider`)."""
    try:
        from core.cortex_registry import get_cortex_registry

        reg = get_cortex_registry()
        return sorted(reg.get_available_engines("llm_provider"))
    except Exception:
        return []


# --- Compatibility helpers for Cortex (used by WebUI components tab)
# These functions provide a backward-compatible shim for older WebUI code
# that expects simple helpers in core.config. They delegate to the
# CortexRegistry where possible to avoid duplicating discovery logic.


def list_available_cortexs():
    """Return a sorted list of known cortex kinds."""
    try:
        from core.cortex_registry import get_cortex_registry

        reg = get_cortex_registry()
        if reg._cortex_kinds:
            kinds = set(reg._cortex_kinds.keys())
        else:
            kinds = {
                meta.get("cortex", "llm_provider") for meta in reg._engine_meta.values()
            }
        if not kinds:
            return ["llm_provider"]
        return sorted(kinds)
    except Exception:
        return ["llm_provider"]


def list_available_cortex_engines(kind: str | None = None):
    """Return available engine names for a given cortex kind."""
    try:
        from core.cortex_registry import get_cortex_registry

        reg = get_cortex_registry()
        return reg.get_available_engines(kind)
    except Exception:
        return []


async def get_active_cortex():
    """Return the cortex kind for the active engine (async).

    This inspects the CortexRegistry metadata for the configured engine
    and returns its declared cortex kind, defaulting to 'llm_provider'.
    """
    try:
        engine = await get_active_cortex_engine()
        from core.cortex_registry import get_cortex_registry

        reg = get_cortex_registry()
        meta = reg._engine_meta.get(engine, {})
        return meta.get("cortex", "llm_provider")
    except Exception:
        return "llm_provider"


# === Global model management ===
MODEL_FILE = os.path.join(os.path.dirname(__file__), "model_config.json")

# Lock used to serialize concurrent Cortex switches to avoid races
_cortex_switch_lock = asyncio.Lock()


def get_current_model():
    if os.path.exists(MODEL_FILE):
        try:
            with open(MODEL_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("model")
        except Exception:
            return None
    return None


def set_current_model(model: str):
    try:
        with open(MODEL_FILE, "w", encoding="utf-8") as f:
            json.dump({"model": model}, f, indent=2)
    except Exception as e:
        log_error(f"Unable to save model: {repr(e)}")
