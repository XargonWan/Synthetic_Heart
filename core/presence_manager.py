import asyncio
from datetime import datetime, timezone
from core.db import get_recent_responses, insert_memory
from core.db import (
    get_active_emotions,
    update_emotion_intensity,
    mark_emotion_resolved,
    crystallize_emotion,
)
from core.trigger_processor import process_triggers_for_emotion
from core.logging_utils import log_debug, log_info

# ⚙️ Behaviour configuration
presence_config = {
    "normal_interval": 1800,  # 30 minutes
    "cooldown_per_user": {"replies": 3, "per_minutes": 10},
    "jay_override": True,
}


# 🧠 Emotion → re-evaluation + crystallization
async def evaluate_emotions():
    emotions = await get_active_emotions()

    for em in emotions:
        log_debug(f"[PresenceManager] Reassessing emotion {em['id']} ({em['emotion']})")

        delta = await process_triggers_for_emotion(em)  # returns +1, -1, 0, etc.
        await update_emotion_intensity(em["id"], delta)

        log_debug(f"[PresenceManager] Valutazione emozione: {em}")
        log_debug(f"[PresenceManager] Delta calcolato: {delta}")
        log_debug(f"[PresenceManager] Intensità aggiornata: {em['intensity'] + delta}")
        log_debug(
            f"[PresenceManager] Stato emozione: {'risolta' if em['intensity'] + delta <= 0 else 'cristallizzata' if em['intensity'] + delta >= 10 else 'attiva'}"
        )

        # 💀 If intensity reaches zero → resolved
        if em["intensity"] + delta <= 0:
            await mark_emotion_resolved(em["id"])
            log_debug(f"[PresenceManager] Emotion resolved: {em['id']}")

        # 💎 Automatic crystallization if intensity is high
        elif em["intensity"] + delta >= 10:
            await crystallize_emotion(em["id"])
            log_debug(
                f"[PresenceManager] 💎 Emotion crystallized: {em['emotion']} ({em['id']})"
            )

        await apply_emotion_decay(em)


# ♻️ Main loop
async def presence_loop():
    while True:
        log_debug("[PresenceManager] Cyclic check running...")
        await evaluate_emotions()
        await asyncio.sleep(presence_config["normal_interval"])


# 💭 Transformative reflection
async def reflect_on_recent_responses():
    # Assumption: function defined elsewhere in the core
    from core.llm_logic import evaluate_transformative_by_llm

    responses = await get_recent_responses(limit=10)
    for resp in responses:
        if await evaluate_transformative_by_llm(resp["content"]):
            meta = get_transformative_metadata(resp["content"])
            await insert_memory(
                content=resp["content"],
                author="synth",
                source=meta["source"],
                tags=meta["tags"],
                scope=meta["scope"],
                emotion=meta["emotion"],
                intensity=meta["intensity"],
                emotion_state=meta["emotion_state"],
            )
            log_info("[synth] 💭 Transformative reflection saved.")


def get_transformative_metadata(response_text: str) -> dict:
    """
    synth evaluates which metadata to assign to a transformative response.
    In the future this might be delegated to the LLM or loaded from a file.
    """
    return {
        "tags": "transformative,internal",
        "scope": "self",
        "emotion": "reflection",
        "intensity": 9,
        "emotion_state": "crystallized",
        "source": "reflection",
    }


async def apply_emotion_decay(emotion: dict):
    """
    Slowly reduces the intensity if the emotion is not reinforced.
    Only works if `decay_enabled` is on.
    """
    if emotion.get("state") != "active":
        return
    if emotion.get("decay_enabled", 1) != 1:
        return

    intensity = emotion.get("intensity", 0)
    if intensity > 0:
        await update_emotion_intensity(emotion["id"], delta=-1)
        log_debug(
            f"[Decay] 🕯️ Emotion {emotion['id']} decreasing: {intensity} → {intensity - 1}"
        )

        if intensity - 1 <= 0:
            await mark_emotion_resolved(emotion["id"])
            log_debug(f"[Decay] 💤 Emotion resolved due to depletion: {emotion['id']}")
