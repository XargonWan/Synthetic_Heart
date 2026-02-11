# core/config.py

import os
import json
import asyncio

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover - fallback when dotenv not installed

    def load_dotenv(*args, **kwargs):
        return False


from core.db import get_conn_ctx
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

"""
notify_trainer(chat_id: int, message: str) -> None
Send a notification to the trainer via the centralized logic in core/notifier.py.
"""

# ✅ Load all environment variables from .env
load_dotenv(dotenv_path="/app/.env", override=False)


def _parse_trainer_ids(raw_value: str) -> dict[str, int]:
    """Parse TRAINER_IDS string into a mapping."""
    mapping = {}
    if not raw_value:
        return mapping
    for entry in raw_value.split(","):
        if ":" in entry:
            interface_name, trainer_id = entry.split(":", 1)
            mapping[interface_name.strip()] = int(trainer_id.strip())
    return mapping


def _parse_trainer_ids(raw_value: str) -> dict[str, int]:
    """Parse TRAINER_IDS string into a mapping."""
    mapping = {}
    if not raw_value:
        return mapping
    for entry in raw_value.split(","):
        if ":" in entry:
            interface_name, trainer_id = entry.split(":", 1)
            mapping[interface_name.strip()] = int(trainer_id.strip())
    return mapping


# Trainer IDs configuration
_TRAINER_IDS_RAW = config_registry.get_var(
    "TRAINER_IDS",
    "",
    label="Trainer IDs",
    description="Comma separated mapping of interface trainer IDs. Example: telegram_bot:123456,discord_interface:654321",
    group="core",
    component="core",
    tags=["key_value_list"],
)


def get_trainer_ids() -> dict[str, int]:
    """Parse and return current trainer IDs mapping."""
    return _parse_trainer_ids(str(_TRAINER_IDS_RAW))


def get_trainer_id(interface_name: str) -> int | None:
    """Return the trainer ID for the given interface."""
    return get_trainer_ids().get(interface_name)
    return None


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

# LLM Configuration
LLM_MODE = config_registry.get_var(
    "LLM_MODE",
    "manual",
    label="LLM Mode",
    description="Legacy compatibility flag for the active LLM mode.",
    group="core",
    component="core",
    tags=["bootstrap"],  # Hidden from UI - LLM is managed via Components tab
)

# === Persistent LLM mode ===

# Exposed configuration for active LLM (managed via Components tab). We'll register a
# visible choice list later once the available engines are enumerated.
# NOTE: Do NOT use "bootstrap" tag - this config MUST be loaded from DB
# Placeholder registration kept to ensure key exists during import; we'll re-register
# with choices after enumerating available engines further down.
ACTIVE_LLM = config_registry.get_var(
    "ACTIVE_LLM",
    "selenium_chatgpt",
    label="Active LLM",
    description="The currently active LLM engine. Synced with the Components tab.",
    group="core",
    component="core",
    hidden=True,  # Temporarily hidden until we re-register with choices
)


async def get_active_llm():
    """Get the currently active LLM engine from config registry.

    This function ensures the value is loaded from the database if available,
    not just the default value that was set during module import.
    """
    try:
        # Force retrieval from config registry to get the most up-to-date value
        # This ensures we load from DB even if called before load_all_from_db()
        current_value = config_registry.get_value("ACTIVE_LLM", "selenium_chatgpt")

        # Check for None, empty string, or literal "None" string
        if current_value and current_value != "" and current_value != "None":
            log_debug(f"[config] 🧠 Active LLM: {current_value}")
            return current_value
    except Exception as e:
        log_error(f"[config] ❌ Error reading ACTIVE_LLM: {repr(e)}")

    # Default fallback
    return "selenium_chatgpt"


async def set_active_llm(name: str):
    """Save the active LLM engine to config registry and database."""
    try:
        await config_registry.set_value("ACTIVE_LLM", name)
        log_info(f"[config] 💾 Saved active LLM to database: {name}")
    except Exception as e:
        log_error(f"[config] ❌ Error saving ACTIVE_LLM to database: {repr(e)}")
        raise


async def switch_active_llm(name: str, use_hot_swap: bool = True):
    """
    Switch to a different active LLM engine.

    This centralizes active LLM changes, serializing concurrent attempts using
    `_llm_switch_lock` to avoid races during persist/load phases.
    """
    from core.config import list_available_llms

    available = list_available_llms()
    if name not in available:
        raise ValueError(
            f"LLM '{name}' is not available. Available: {', '.join(available)}"
        )

    current = await get_active_llm()

    def _get_loaded_plugin_name() -> str | None:
        try:
            from core import plugin_instance

            loaded = getattr(plugin_instance, "plugin", None)
            if loaded is None:
                return None
            return loaded.__class__.__module__.split(".")[-1]
        except Exception:
            return None

    loaded_name = _get_loaded_plugin_name()
    if name == current and loaded_name == name:
        log_debug(
            f"[config] 🔄 LLM already active and loaded: {name}, no switch needed."
        )
        return

    # Ensure only one LLM switch runs at a time
    log_debug(f"[config] ⏳ Waiting to acquire LLM switch lock for '{name}'")
    async with _llm_switch_lock:
        log_debug(f"[config] 🔒 Acquired LLM switch lock for '{name}'")
        # Re-check under lock in case another switch occurred
        current = await get_active_llm()
        loaded_name = _get_loaded_plugin_name()
        if name == current and loaded_name == name:
            log_debug(
                f"[config] 🔄 LLM already active and loaded under lock: {name}, no switch needed."
            )
            return

        # Persist the new LLM choice to config
        try:
            # Only persist if we're actually changing the configured value.
            if name != current:
                await set_active_llm(name)
                log_info(f"[config] 🔄 Switching LLM from {current} to {name}")
            else:
                log_info(
                    f"[config] 🔄 ACTIVE_LLM already '{name}' but loaded plugin is '{loaded_name}', reloading engine"
                )
        except Exception as e:
            log_error(f"[config] ❌ Error persisting active LLM '{name}': {e}", exc=e)
            # Surface the error to the caller (no silent fallback)
            raise

        try:
            if use_hot_swap:
                # Hot-swap: direct plugin reload and ensure plugin start succeeds
                from core.plugin_instance import load_plugin

                await load_plugin(name, ensure_started=True, start_timeout=30.0)
                log_info(f"[config] ✅ LLM hot-swapped to {name}")
                # Notify trainer about successful change
                try:
                    from core.notifier import notify_trainer

                    notify_trainer(f"✅ LLM mode dynamically updated to `{name}`.")
                except Exception as e:  # pragma: no cover - best-effort notify
                    log_warning(
                        f"[config] Failed to notify trainer about LLM change: {e}"
                    )
            else:
                # Full reinitialization
                from core.core_initializer import core_initializer

                await core_initializer.initialize_all()
                log_info(f"[config] ✅ LLM switched to {name} (full reinitialization)")
                # Notify trainer about successful change
                try:
                    from core.notifier import notify_trainer

                    notify_trainer(f"✅ LLM mode dynamically updated to `{name}`.")
                except Exception as e:  # pragma: no cover - best-effort notify
                    log_warning(
                        f"[config] Failed to notify trainer about LLM change: {e}"
                    )
        except Exception as e:
            # Log with traceback and notify trainer about failure
            log_error(f"[config] ❌ Failed to switch LLM to {name}: {e}", exc=e)
            try:
                from core.notifier import notify_trainer

                notify_trainer(f"❌ Failed to switch LLM to `{name}`: {e}")
            except Exception:
                pass
            # Re-raise so callers (and tests) can handle failure deterministically
            raise
        finally:
            log_debug(f"[config] 🔓 Released LLM switch lock for '{name}'")


_log_chat_id: int | None = None  # cached log chat ID
_log_chat_thread_id: int | None = None  # cached log chat thread ID
_log_chat_interface: str | None = None  # cached log chat interface


async def get_log_chat_id() -> int | None:
    """Return the configured log chat ID, if any."""
    global _log_chat_id
    if _log_chat_id is None:
        async with get_conn_ctx() as conn:
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT value FROM settings WHERE `setting_key` = 'log_chat_id'"
                    )
                    row = await cur.fetchone()
                    if row:
                        try:
                            _log_chat_id = int(row["value"])
                            log_debug(
                                f"[config] 📥 Loaded log_chat_id from DB: {_log_chat_id}"
                            )
                        except (ValueError, TypeError):
                            _log_chat_id = None
            except Exception as e:
                log_error(f"[config] ❌ Error in get_log_chat_id(): {repr(e)}")
    return _log_chat_id


async def get_log_chat_interface() -> str | None:
    """Return the configured log chat interface, if any."""
    global _log_chat_interface
    if _log_chat_interface is None:
        async with get_conn_ctx() as conn:
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT value FROM settings WHERE `setting_key` = 'log_chat_interface'"
                    )
                    row = await cur.fetchone()
                    if row:
                        _log_chat_interface = row["value"]
                        log_debug(
                            f"[config] 📥 Loaded log_chat_interface from DB: {_log_chat_interface}"
                        )
            except Exception as e:
                log_error(f"[config] ❌ Error in get_log_chat_interface(): {repr(e)}")
    return _log_chat_interface


async def set_log_chat_id(chat_id: int) -> None:
    """Persist and cache the log chat ID."""
    global _log_chat_id
    _log_chat_id = chat_id
    from core.db import ensure_core_tables

    await ensure_core_tables()
    async with get_conn_ctx() as conn:
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "REPLACE INTO settings (`setting_key`, `value`) VALUES (%s, %s)",
                    ("log_chat", str(chat_id)),
                )
                await conn.commit()
                log_debug(f"[config] 💾 Saved log_chat in DB: {chat_id}")
        except Exception as e:
            log_error(f"[config] ❌ Error in set_log_chat_id(): {repr(e)}")


async def get_log_chat_thread_id() -> int | None:
    """Return the configured log chat thread ID, if any."""
    global _log_chat_thread_id
    if _log_chat_thread_id is None:
        async with get_conn_ctx() as conn:
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT value FROM settings WHERE `setting_key` = 'log_chat_thread_id'"
                    )
                    row = await cur.fetchone()
                    if row:
                        try:
                            _log_chat_thread_id = int(row["value"])
                            log_debug(
                                f"[config] 📥 Loaded log_chat_thread_id from DB: {_log_chat_thread_id}"
                            )
                        except (ValueError, TypeError):
                            _log_chat_thread_id = None
            except Exception as e:
                log_error(f"[config] ❌ Error in get_log_chat_thread_id(): {repr(e)}")
    return _log_chat_thread_id


async def set_log_chat_id_and_thread(
    chat_id: int, thread_id: int | None = None, interface: str = "webui"
) -> None:
    """Persist and cache the log chat ID, thread ID, and interface."""
    global _log_chat_id, _log_chat_thread_id, _log_chat_interface
    _log_chat_id = chat_id
    _log_chat_thread_id = thread_id
    _log_chat_interface = interface
    from core.db import ensure_core_tables

    await ensure_core_tables()
    async with get_conn_ctx() as conn:
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "REPLACE INTO settings (`setting_key`, `value`) VALUES (%s, %s)",
                    ("log_chat_id", str(chat_id)),
                )
                await cur.execute(
                    "REPLACE INTO settings (`setting_key`, `value`) VALUES (%s, %s)",
                    ("log_chat_interface", interface),
                )
                if thread_id is not None:
                    await cur.execute(
                        "REPLACE INTO settings (`setting_key`, `value`) VALUES (%s, %s)",
                        ("log_chat_thread_id", str(thread_id)),
                    )
                else:
                    # Remove thread setting if None
                    await cur.execute(
                        "DELETE FROM settings WHERE `setting_key` = 'log_chat_thread_id'"
                    )
                await conn.commit()
                log_debug(
                    f"[config] 💾 Saved log chat in DB: {chat_id}, thread: {thread_id}, interface: {interface}"
                )
        except Exception as e:
            log_error(f"[config] ❌ Error in set_log_chat_id_and_thread(): {repr(e)}")


def get_log_chat_id_sync() -> int | None:
    """Synchronous helper to fetch cached log chat ID, loading from DB if needed."""
    global _log_chat_id
    if _log_chat_id is not None:
        return _log_chat_id
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        # Cannot perform blocking DB fetch; return None until explicitly loaded
        return _log_chat_id
    return asyncio.run(get_log_chat_id())


def get_log_chat_interface_sync() -> str | None:
    """Synchronous helper to fetch cached log chat interface."""
    global _log_chat_interface
    if _log_chat_interface is not None:
        return _log_chat_interface
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        # Cannot perform blocking DB fetch; return None until explicitly loaded
        return _log_chat_interface
    return asyncio.run(get_log_chat_interface())


def get_log_chat_thread_id_sync() -> int | None:
    """Synchronous helper to fetch cached log chat thread ID."""
    global _log_chat_thread_id
    if _log_chat_thread_id is not None:
        return _log_chat_thread_id
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return _log_chat_thread_id
    return asyncio.run(get_log_chat_thread_id())


def list_available_llms():
    engines_dir = os.path.join(os.path.dirname(__file__), "../llm_engines")
    return sorted(
        fname.removesuffix(".py")
        for fname in os.listdir(engines_dir)
        if fname.endswith(".py") and not fname.startswith("__")
    )


# --- Compatibility helpers for Cortex (used by WebUI components tab)
# These functions provide a backward-compatible shim for older WebUI code
# that expects simple helpers in core.config. They delegate to the
# CortexRegistry where possible to avoid duplicating discovery logic.

def list_available_cortexs():
    """Return a sorted list of known cortex kinds (e.g., 'llm', 'live', 'agent').

    This is a synchronous helper kept for backward compatibility with
    existing WebUI code that imports it directly from core.config.
    """
    try:
        from core.cortex_registry import get_cortex_registry

        reg = get_cortex_registry()
        kinds = {meta.get("cortex", "llm") for meta in reg._engine_meta.values()}
        if not kinds:
            return ["llm"]
        return sorted(kinds)
    except Exception:
        return ["llm"]


def list_available_cortex_engines(kind: str | None = None):
    """Return available engine names for a given cortex kind.

    If kind is None or 'llm' this will fall back to legacy LLM discovery.
    """
    try:
        if kind is None or kind == "llm":
            return list_available_llms()
        from core.cortex_registry import get_cortex_registry

        reg = get_cortex_registry()
        return reg.get_available_engines(kind)
    except Exception:
        return []


async def get_active_cortex_engine():
    """Async helper to get the currently active cortex engine.

    For now this delegates to the legacy ACTIVE_LLM config to preserve
    existing behaviour until Cortex-wide configuration is introduced.
    """
    try:
        return await get_active_llm()
    except Exception:
        # Fallback: return the default LLM
        return "manual"


async def get_active_cortex():
    """Return the cortex kind for the active engine (async).

    This inspects the CortexRegistry metadata for the configured engine
    and returns its declared cortex kind, defaulting to 'llm'.
    """
    try:
        engine = await get_active_cortex_engine()
        from core.cortex_registry import get_cortex_registry

        reg = get_cortex_registry()
        meta = reg._engine_meta.get(engine, {})
        return meta.get("cortex", "llm")
    except Exception:
        return "llm"


# Make ACTIVE_LLM visible in the Settings UI as a choice/combo, synced with available engines
# We cannot re-register the key (it already exists), so update the internal definition if present.
try:
    choices = list_available_llms()
    existing = config_registry._definitions.get("ACTIVE_LLM")
    if existing:
        existing.hidden = False
        existing.description = (
            "The currently active LLM engine. Synced with the Components tab."
        )
        existing.constraints = {"choices": choices}
except Exception as e:
    log_warning(f"[config] Could not populate ACTIVE_LLM choices: {e}")

# === Global model management ===
MODEL_FILE = os.path.join(os.path.dirname(__file__), "model_config.json")

# Lock used to serialize concurrent LLM switches to avoid races
_llm_switch_lock = asyncio.Lock()


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
