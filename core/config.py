# core/config.py

import os
import json
import asyncio
try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover - fallback when dotenv not installed
    def load_dotenv(*args, **kwargs):
        return False
from core.db import get_conn
import aiomysql
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

_active_llm = None  # local global variable

async def get_active_llm():
    global _active_llm
    if _active_llm is None:
        conn = await get_conn()
        try:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("SELECT value FROM settings WHERE `setting_key` = 'active_llm'")
                row = await cur.fetchone()
                if row:
                    _active_llm = row["value"]
                    log_debug(f"[config] 🧠 Active LLM plugin loaded from DB: {_active_llm}")
                else:
                    _active_llm = "manual"
        except Exception as e:
            log_error(f"[config] ❌ Error in get_active_llm(): {repr(e)}")
        finally:
            conn.close()
    return _active_llm

async def set_active_llm(name: str):
    global _active_llm
    if name == _active_llm:
        log_debug(f"[config] 🔄 LLM already set: {name}, no update needed.")
        return
    _active_llm = name
    from core.db import ensure_core_tables
    await ensure_core_tables()
    conn = await get_conn()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "REPLACE INTO settings (`setting_key`, value) VALUES (%s, %s)",
                ("active_llm", name),
            )
            await conn.commit()
            log_debug(f"[config] 💾 Saved active plugin in DB: {name}")
    except Exception as e:
        log_error(f"[config] ❌ Error in set_active_llm(): {repr(e)}")
    finally:
        conn.close()

_log_chat_id: int | None = None  # cached log chat ID
_log_chat_thread_id: int | None = None  # cached log chat thread ID
_log_chat_interface: str | None = None  # cached log chat interface

async def get_log_chat_id() -> int | None:
    """Return the configured log chat ID, if any."""
    global _log_chat_id
    if _log_chat_id is None:
        conn = await get_conn()
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
        finally:
            conn.close()
    return _log_chat_id


async def get_log_chat_interface() -> str | None:
    """Return the configured log chat interface, if any."""
    global _log_chat_interface
    if _log_chat_interface is None:
        conn = await get_conn()
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
        finally:
            conn.close()
    return _log_chat_interface

async def set_log_chat_id(chat_id: int) -> None:
    """Persist and cache the log chat ID."""
    global _log_chat_id
    _log_chat_id = chat_id
    from core.db import ensure_core_tables
    await ensure_core_tables()
    conn = await get_conn()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "REPLACE INTO settings (`setting_key`, `value`) VALUES (%s, %s)",
                ("log_chat", str(chat_id)),
            )
            await conn.commit()
            log_debug(
                f"[config] 💾 Saved log_chat in DB: {chat_id}"
            )
    except Exception as e:
        log_error(f"[config] ❌ Error in set_log_chat_id(): {repr(e)}")
    finally:
        conn.close()

async def get_log_chat_thread_id() -> int | None:
    """Return the configured log chat thread ID, if any."""
    global _log_chat_thread_id
    if _log_chat_thread_id is None:
        conn = await get_conn()
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
        finally:
            conn.close()
    return _log_chat_thread_id

async def set_log_chat_id_and_thread(chat_id: int, thread_id: int | None = None, interface: str = "webui") -> None:
    """Persist and cache the log chat ID, thread ID, and interface."""
    global _log_chat_id, _log_chat_thread_id, _log_chat_interface
    _log_chat_id = chat_id
    _log_chat_thread_id = thread_id
    _log_chat_interface = interface
    from core.db import ensure_core_tables
    await ensure_core_tables()
    conn = await get_conn()
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
    finally:
        conn.close()

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

# === Global model management ===
MODEL_FILE = os.path.join(os.path.dirname(__file__), "model_config.json")

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
