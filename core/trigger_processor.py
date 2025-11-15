# core/trigger_processor.py

from core.db import get_conn_ctx
from datetime import datetime, timedelta, timezone
from core.logging_utils import log_debug, log_info, log_warning, log_error
import aiomysql

# Observation interval to evaluate whether the emotion is reinforced or softened
LOOKBACK_MINUTES = 30

async def process_triggers_for_emotion(emotion: dict) -> int:
    """
    Evaluate recent events to determine whether to reinforce, soften, or ignore the emotion.
    Returns a delta (e.g., +1, -1, 0) to apply to the intensity.
    """
    emotion_type = emotion["emotion"]
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=LOOKBACK_MINUTES)

    # Retrieve recent events related to the same scope
    scope = emotion.get("scope", "auto")  # fallback if not defined
    query = """
        SELECT content FROM memories
        WHERE timestamp >= %s AND scope = %s
        ORDER BY timestamp DESC
    """
    async with get_conn_ctx() as conn:
        try:
            async with conn.cursor() as cur:
                await cur.execute(query, (since.isoformat(), scope))
                rows = await cur.fetchall()
                contents = [row[0] for row in rows]
        except Exception as e:
            log_error(f"[trigger_processor] Error fetching memories: {e}")
            contents = []

    # Simple heuristic: look for reinforcing or softening keywords
    reinforce_keywords = {
        "anger": ["hate", "you disappointed me", "you're worthless", "idiot"],
        "sadness": ["I miss you", "you're far away", "I'm alone"],
        "joy": ["I care about you", "you're special", "thank you", "we did it"],
    }

    soften_keywords = {
        "anger": ["sorry", "I didn't mean to", "I respect you"],
        "sadness": ["I'm here", "you're not alone", "I hug you"],
        "joy": ["boring", "enough", "it doesn't matter"],
    }

    delta = 0
    for content in contents:
        text = content.lower()
        if any(k in text for k in reinforce_keywords.get(emotion_type, [])):
            delta += 1
        if any(k in text for k in soften_keywords.get(emotion_type, [])):
            delta -= 1

    log_debug(f"[TriggerProcessor] Evaluated delta for {emotion_type}: {delta}")
    return delta
