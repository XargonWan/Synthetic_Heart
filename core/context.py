import json
import os
from core.logging_utils import log_debug, log_info

from core.abstract_context import AbstractContext
from typing import Optional, Callable

CONFIG_PATH = "config/synth_config.json"

# Injection priority for context information
INJECTION_PRIORITY = 1  # Highest priority - context is essential


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def save_config(config: dict):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def get_context_state() -> bool:
    return load_config().get("context_mode", False)


def set_context_state(state: bool):
    config = load_config()
    config["context_mode"] = state
    save_config(config)


async def context_command(
    abstract_context: AbstractContext, reply_fn: Optional[Callable] = None
):
    """Context command that works with any interface."""
    if not abstract_context.is_trainer():
        return

    current = get_context_state()
    new_state = not current
    set_context_state(new_state)

    state_str = "enabled" if new_state else "disabled"
    response_message = f"🧠 Context mode {state_str}."

    if reply_fn:
        await reply_fn(response_message)

    log_debug(f"Context mode {state_str}.")


def register_injection_priority():
    """Register this component's injection priority."""
    log_info(f"[context] Registered injection priority: {INJECTION_PRIORITY}")
    return INJECTION_PRIORITY


# Register priority when module is loaded
register_injection_priority()
