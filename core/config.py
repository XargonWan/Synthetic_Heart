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

BASE_CORTEX = config_registry.get_var(
    "BASE_CORTEX",
    "selenium_chatgpt",
    label="Base Cortex",
    description="Default cortex engine used system-wide unless overridden by scope.",
    group="core",
    component="core",
)

GRILLO_CORTEX = config_registry.get_var(
    "GRILLO_CORTEX",
    "Default",
    label="Grillo Cortex",
    description="Cortex engine used for Grillo (Default means Base Cortex).",
    group="core",
    component="core",
)

TRAINER_CORTEX = config_registry.get_var(
    "TRAINER_CORTEX",
    "Default",
    label="Trainer Cortex",
    description="Cortex engine used for Trainer-originated requests ("
    "Default"
    " means Base Cortex).",
    group="core",
    component="core",
)


async def get_active_cortex_engine(scope: str | None = None) -> str:
    """Return the effective cortex engine for a given scope.

    Scope can be "grillo", "trainer", or None. The returned engine must exist
    in the Cortex registry, otherwise a ValueError is raised.
    """
    try:
        base = config_registry.get_value("BASE_CORTEX", "")
        if scope == "grillo":
            override = config_registry.get_value("GRILLO_CORTEX", "Default")
        elif scope == "trainer":
            override = config_registry.get_value("TRAINER_CORTEX", "Default")
        else:
            override = "Default"

        chosen = base if override in (None, "", "Default", "None") else override
        if not chosen:
            raise ValueError("BASE_CORTEX is not configured")

        from core.cortex_registry import get_cortex_registry

        reg = get_cortex_registry()
        if chosen not in reg.get_available_engines():
            raise ValueError(f"Cortex engine '{chosen}' is not registered")

        log_debug(f"[config] 🧠 Active Cortex ({scope or 'base'}): {chosen}")
        return chosen
    except Exception as e:
        log_error(f"[config] ❌ Error resolving active cortex: {repr(e)}")
        raise


async def set_base_cortex(name: str) -> None:
    """Persist the base cortex engine selection.

    Robustness: if persisting into the `config` table fails (DB race/schema
    issue), persist a legacy copy into `settings.base_cortex` so the value
    survives container restarts and will be migrated on next startup.
    """
    try:
        await config_registry.set_value("BASE_CORTEX", name)
        log_info(f"[config] 💾 Saved base cortex to database: {name}")
    except Exception as e:
        log_error(f"[config] ❌ Error saving BASE_CORTEX to database: {repr(e)}")
        raise

    # Verify the value actually landed in `config`; if not, write to legacy
    # `settings.base_cortex` as a best-effort fallback so a restart won't lose it.
    try:
        from core.db import ensure_core_tables

        await ensure_core_tables()
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT value FROM config WHERE config_key = %s",
                    ("BASE_CORTEX",),
                )
                row = await cur.fetchone()
                if not row or (row and row[0] != name):
                    # Persist fallback into legacy `settings` table
                    await cur.execute(
                        "REPLACE INTO settings (`setting_key`, `value`) VALUES (%s, %s)",
                        ("base_cortex", name),
                    )
                    await conn.commit()
                    from core.logging_utils import log_warning

                    log_warning(
                        f"[config] ⚠️ Fallback: persisted BASE_CORTEX='{name}' into settings.base_cortex because `config` write wasn't present"
                    )
    except Exception as _exc:  # pragma: no cover - best-effort fallback
        log_warning(f"[config] Failed to apply legacy fallback for BASE_CORTEX: {_exc}")


async def set_scope_cortex(scope: str, name: str) -> None:
    """Persist a scope-specific cortex override.

    If DB persist fails for the modern `config` table, also write a legacy
    copy into `settings` (where applicable) so the selection survives restarts
    and will be migrated on next boot.
    """
    key = "GRILLO_CORTEX" if scope == "grillo" else "TRAINER_CORTEX"
    try:
        await config_registry.set_value(key, name)
        log_info(f"[config] 💾 Saved {key} to database: {name}")
    except Exception as e:
        log_error(f"[config] ❌ Error saving {key} to database: {repr(e)}")
        raise

    # Fallback -> legacy settings (grillo_cortex / trainer_cortex)
    legacy_map = {"GRILLO_CORTEX": "grillo_cortex", "TRAINER_CORTEX": "trainer_cortex"}
    legacy_key = legacy_map.get(key)
    if legacy_key:
        try:
            from core.db import ensure_core_tables

            await ensure_core_tables()
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT value FROM config WHERE config_key = %s",
                        (key,),
                    )
                    row = await cur.fetchone()
                    if not row or (row and row[0] != name):
                        await cur.execute(
                            "REPLACE INTO settings (`setting_key`, `value`) VALUES (%s, %s)",
                            (legacy_key, name),
                        )
                        await conn.commit()
                        from core.logging_utils import log_warning

                        log_warning(
                            f"[config] ⚠️ Fallback: persisted {key}='{name}' into settings.{legacy_key}"
                        )
        except Exception as _exc:  # pragma: no cover - best-effort fallback
            log_warning(f"[config] Failed to apply legacy fallback for {key}: {_exc}")


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
