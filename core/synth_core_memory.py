from core.db import insert_memory
import logging
import json
from core.logging_utils import log_info

# === Memory logging setup ===
os.makedirs("logs", exist_ok=True)  # Ensure log directory exists

memory_logger = logging.getLogger("synth.memory")
if not memory_logger.handlers:
    memory_logger.setLevel(logging.INFO)
    handler = logging.FileHandler("logs/memoria.log", encoding="utf-8")
    formatter = logging.Formatter("%(asctime)s - %(message)s")
    handler.setFormatter(formatter)
    memory_logger.addHandler(handler)


# Static internal configuration (expandable)
DEFAULT_TAGS = json.dumps(["auto", "interazione"])
DEFAULT_SCOPE = "general"
DEFAULT_SOURCE = "chat"

REMEMBER_KEYWORDS = []


def should_remember(user_text: str, response_text: str) -> bool:
    """
    synth autonomously evaluates whether the interaction is memorable.
    This decision is entirely internal and not visible to the user.
    """
    text = (user_text + " " + response_text).lower()

    if any(k in text for k in REMEMBER_KEYWORDS):
        return True

    if "mi hai fatto sentire" in response_text.lower():
        return True

    return False


async def silently_record_memory(
    user_text: str,
    response_text: str,
    tags: str = DEFAULT_TAGS,
    scope: str = DEFAULT_SCOPE,
    source: str = DEFAULT_SOURCE,
):
    """
    synth internally stores what it decided to remember.
    No feedback is provided externally.
    """

    # If tags is a list, convert to JSON
    if isinstance(tags, list):
        tags = json.dumps(tags)

    await insert_memory(
        content=user_text,
        author="synth",
        source=source,
        tags=tags,
        scope=scope,
        emotion=None,
        intensity=None,
        emotion_state=None,
    )

    log_info("[synth_CORE] 🧠 Memory saved autonomously.")

    memory_logger.info(
        f"[MEMORY] Saved by synth\n"
        f"→ Input: {user_text}\n"
        f"→ Response: {response_text}\n"
        f"→ Tags: {tags} | Scope: {scope} | Source: {source}"
    )


# Injection priority for core memory
INJECTION_PRIORITY = 6  # Medium-low priority


def register_injection_priority():
    """Register this component's injection priority."""
    log_info(f"[synth_core_memory] Registered injection priority: {INJECTION_PRIORITY}")
    return INJECTION_PRIORITY


# Register priority when module is loaded
register_injection_priority()
