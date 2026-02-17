# plugins/emotion_manager.py
"""
Emotion Manager - Centralized Emotional State Management for SyntH

This core plugin manages the digital persona's emotional state with:
- Persistent emotional state storage in DB with timestamps
- Exponential decay of emotions over time
- Plutchik's wheel for opposite emotion balancing
- Dynamic emotion evaluation from LLM message tags
- Global readable state for WebUI, animations, and other plugins
"""

import json
import math
import asyncio
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from dataclasses import dataclass
import inspect

from core.plugin_base import PluginBase
from core.db import get_conn_ctx
from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.config_manager import config_registry

# Canonical emotion set used for LLM prompting and validation (Ekman6 + neutral + relaxed)
CANONICAL_EMOTIONS = {
    "happy",  # happiness
    "sad",  # sadness
    "angry",  # anger
    "fear",  # fear
    "disgust",  # disgust
    "surprised",  # surprise
    "neutral",
    "relaxed",
    "love",
    "arousal",
    "devotion",
}

# VALID_EMOTIONS now equals the canonical set used for LLM prompting and validation.
# We intentionally **do not** maintain a large legacy compatibility list in dev.
VALID_EMOTIONS = set(CANONICAL_EMOTIONS)

# Basic synonym map to normalize LLM-provided emotion names to canonical ones
EMOTION_SYNONYMS = {
    "happiness": "happy",
    "joy": "happy",
    "joyful": "happy",
    "smiling": "happy",
    "sadness": "sad",
    "anger": "angry",
    "furious": "angry",
    "rage": "angry",
    "scared": "fear",
    "frightened": "fear",
    "terror": "fear",
    "disgusted": "disgust",
    "surprise": "surprised",
    "surprisedness": "surprised",
    "calm": "relaxed",
    "serenity": "relaxed",
    "relaxed": "relaxed",
    "neutral": "neutral",
    "engaged": "neutral",
    "affection": "love",
    "adoaration": "love",
    "infatuation": "love",
    "lust": "arousal",
    "horny": "arousal",
    "desire": "arousal",
    "worship": "devotion",
    "dedication": "devotion",
}


def normalize_emotion_name(name: str) -> str | None:
    """Normalize a possibly-nonstandard emotion name to a canonical emotion.

    Returns canonical emotion string (from CANONICAL_EMOTIONS) or None if it
    cannot be normalized.
    """
    if not name or not isinstance(name, str):
        return None
    n = name.strip().lower()
    if not n:
        return None
    # direct canonical match
    if n in CANONICAL_EMOTIONS:
        return n
    # synonyms
    if n in EMOTION_SYNONYMS:
        mapped = EMOTION_SYNONYMS[n]
        return mapped if mapped in CANONICAL_EMOTIONS else None
    # plural/simple heuristics: trailing 'ness' -> remove
    if n.endswith("ness"):
        cand = n[:-4]
        if cand in EMOTION_SYNONYMS:
            mapped = EMOTION_SYNONYMS[cand]
            return mapped if mapped in CANONICAL_EMOTIONS else None
    return None


# Plutchik's wheel - opposite emotions for balancing
# When a new emotion is triggered, opposites are reduced
PLUTCHIK_OPPOSITES = {
    # Opposites expressed within the canonical emotion vocabulary
    "happy": "sad",
    "sad": "happy",
    "angry": "fear",
    "fear": "angry",
    "neutral": "disgust",
    "surprised": "relaxed",
    "relaxed": "surprised",
    "love": "disgust",
    "arousal": "disgust",
    # Disgust reduces Love/Arousal
    # Disgust reduces Love/Arousal
    "disgust": "love",  # Primary opposite mapping
}

# Default baseline intensity for emotions (to avoid dropping to absolute zero)
DEFAULT_BASELINE = 0.1

# Specific baselines for certain emotions
EMOTION_BASELINES = {
    "neutral": 5.0,  # Neutral state should be relatively high
    "relaxed": 1.0,  # Always a bit relaxed
}


@dataclass
class EmotionState:
    """Represents an emotion with intensity and timestamp."""

    emotion_name: str
    intensity: float  # 0.0-10.0
    timestamp: datetime  # When this emotion was created/updated

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "emotion": self.emotion_name,
            "intensity": self.intensity,
            "timestamp": self.timestamp.isoformat(),
        }

    def get_decayed_intensity(self, current_time: Optional[datetime] = None) -> float:
        """Calculate intensity with exponential decay over time.

        Args:
            current_time: Current time (default: now). Used for testing.

        Returns:
            Decayed intensity (0.0-10.0)
        """
        if current_time is None:
            current_time = datetime.now()

        # Get decay half-life from config (in seconds, default 1 hour = 3600s)
        tau = config_registry.get_value("EMOTION_DECAY_TAU", 3600)

        # Calculate time delta in seconds
        delta_t = (current_time - self.timestamp).total_seconds()

        # Exponential decay: intensity * e^(-delta_t / tau)
        decayed = self.intensity * math.exp(-delta_t / tau)

        # Get baseline for this emotion
        baseline = EMOTION_BASELINES.get(self.emotion_name, DEFAULT_BASELINE)

        # Exponential decay towards baseline: B + (I - B) * e^(-delta_t / tau)
        # decayed = self.intensity * math.exp(-delta_t / tau)
        decayed = baseline + (self.intensity - baseline) * math.exp(-delta_t / tau)

        # Return clamped value
        return max(0.0, min(10.0, decayed))


class EmotionManager(PluginBase):
    """Centralized emotion manager for SyntH.

    Manages emotional state with persistence, decay, and balancing.
    """

    def __init__(self, config=None):
        super().__init__(config)
        self._decay_threshold = 0.1  # Emotions below this are removed

    def get_metadata(self) -> dict:
        """Return plugin metadata."""
        return {
            "name": "emotion_manager",
            "description": "Centralized emotional state management with decay and balancing",
            "version": "1.0.0",
            "type": "core",  # Mark as core plugin
        }

    def get_supported_actions(self) -> dict:
        """Register supported actions for the emotion manager."""
        return {
            "static_inject": {
                "description": "Inject current emotional state into LLM context",
                "required_params": {},
                "optional_params": {},
            },
            "get_emotion_state": {
                "description": "Get current emotional state with decay applied",
                "required_params": {},
                "optional_params": {
                    "include_raw": "bool - include raw timestamp data",
                },
            },
            "update_emotion_from_tags": {
                "description": "Update emotions from LLM message tags like {emotion intensity}",
                "required_params": {
                    "text": "Message text containing emotion tags",
                },
                "optional_params": {
                    "apply_balancing": "bool - apply Plutchik opposite balancing (default: true)",
                },
            },
            "set_emotion": {
                "description": "Set a single emotion intensity directly",
                "required_params": {
                    "emotion": "Emotion name (must be in whitelist)",
                    "intensity": "float 0.0-10.0",
                },
                "optional_params": {},
            },
            "decay_emotions": {
                "description": "Apply decay to all emotions and remove low-intensity ones",
                "required_params": {},
                "optional_params": {
                    "threshold": "float - remove emotions below this intensity (default 0.1)",
                },
            },
            "sync_emotions_from_all_sources": {
                "description": "Synchronize emotions from ai_diary, message tags, and emotion_state DB",
                "required_params": {},
                "optional_params": {},
            },
        }

    async def execute_action(
        self,
        action: Dict[str, Any],
        context: Dict[str, Any],
        bot: Any,
        original_message: Any,
    ) -> Any:
        """Execute an action."""
        action_type = action.get("type")
        payload = action.get("payload", {})

        # Normalize payload if it's an object/namespace
        if not isinstance(payload, dict):
            try:
                payload = vars(payload)
            except Exception:
                payload = {}

        if action_type == "update_emotion_from_tags":
            text = payload.get("text", "")
            apply_balancing = payload.get("apply_balancing", True)
            return await self.update_emotion_from_tags(text, apply_balancing)

        elif action_type == "set_emotion":
            emotion = payload.get("emotion")
            intensity = payload.get("intensity")
            if emotion is not None and intensity is not None:
                return await self.set_emotion(emotion, float(intensity))
            return False

        elif action_type == "decay_emotions":
            threshold = payload.get("threshold")
            if threshold:
                try:
                    threshold = float(threshold)
                except (ValueError, TypeError):
                    threshold = None
            return await self.decay_emotions(threshold)

        elif action_type == "sync_emotions_from_all_sources":
            return await self.sync_emotions_from_all_sources()

        elif action_type == "get_emotion_state":
            include_raw = payload.get("include_raw", False)
            return await self.get_emotion_state(include_raw)

        elif action_type == "static_inject":
            # Typically handled by gather_static_injections, but if called explicitly:
            return await self.get_static_injection(original_message, context)

        log_warning(f"[emotion_manager] Unknown action type: {action_type}")
        return None

    def get_prompt_instructions(self, action_type: str) -> str:
        """Return instructions for LLM on how to use emotion actions."""
        # Generic instructions for emotion system (used by core_initializer to append to system prompt)
        return """
To express emotions, include tags in your response like {emotion intensity}.
Supported emotions: happy, sad, angry, fear, disgust, surprised, neutral, relaxed, love, arousal, devotion.
Intensity is 0.0 to 10.0.
Example: "I am so happy to see you! {happy 8.5, love 5.0}"
Your emotional state will decay over time, so reinforce it if the feeling persists.
"""

    async def start(self):
        """Initialize emotion manager and create DB table if needed."""
        log_info("[emotion_manager] Starting emotion manager")
        try:
            await self._ensure_table_exists()
            log_info("[emotion_manager] Emotion table initialized")
        except Exception as e:
            log_error(f"[emotion_manager] Failed to initialize: {e}")

        # Sync once at startup
        await self.sync_emotions_from_all_sources()

        # Start background decay loop
        asyncio.create_task(self._decay_loop())

    async def _decay_loop(self):
        """Background loop to decay emotions periodically."""
        log_info("[emotion_manager] Starting decay loop")
        while True:
            try:
                await asyncio.sleep(60)  # Decay every minute
                await self.decay_emotions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_error(f"[emotion_manager] Error in decay loop: {e}")
                await asyncio.sleep(60)

    async def _ensure_table_exists(self):
        """Create emotion_state table if it doesn't exist."""
        async with get_conn_ctx() as conn:
            cm = conn.cursor()
            if inspect.iscoroutine(cm):
                cm = await cm
            async with cm as cur:
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS emotion_state (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        emotion_name VARCHAR(100) NOT NULL,
                        intensity FLOAT NOT NULL DEFAULT 5.0,
                        timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        INDEX idx_emotion_name (emotion_name),
                        INDEX idx_timestamp (timestamp)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                log_debug("[emotion_manager] emotion_state table ensured")

                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS emotion_diary (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        source VARCHAR(100),
                        event VARCHAR(100),
                        emotion VARCHAR(100),
                        intensity FLOAT,
                        state VARCHAR(100),
                        trigger_condition VARCHAR(255),
                        decision_logic TEXT,
                        next_check DATETIME,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_timestamp (timestamp)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                log_debug("[emotion_manager] emotion_diary table ensured")

    async def get_emotion_state(self, include_raw: bool = False) -> Dict[str, float]:
        """Get current emotional state with decay applied.

        Args:
            include_raw: If True, return raw intensities without decay

        Returns:
            Dictionary mapping emotion names to current intensities
        """
        try:
            async with get_conn_ctx() as conn:
                cm = conn.cursor()
                if inspect.iscoroutine(cm):
                    cm = await cm
                async with cm as cur:
                    await cur.execute(
                        "SELECT emotion_name, intensity, timestamp FROM emotion_state ORDER BY timestamp DESC"
                    )
                    rows = await cur.fetchall()

            result = {}
            now = datetime.now()

            for row in rows:
                emotion_name, intensity, timestamp = row

                if include_raw:
                    result[emotion_name] = intensity
                else:
                    # Apply decay
                    state = EmotionState(emotion_name, intensity, timestamp)
                    decayed = state.get_decayed_intensity(now)
                    result[emotion_name] = decayed

            log_debug(f"[emotion_manager] Got emotion state: {result}")
            return result

        except Exception as e:
            log_error(f"[emotion_manager] Error getting emotion state: {e}")
            return {}

    async def _load_emotions_from_db(self) -> Dict[str, Tuple[float, datetime]]:
        """Load current emotions from DB.

        Returns:
            Dict mapping emotion_name -> (intensity, timestamp)
        """
        try:
            async with get_conn_ctx() as conn:
                cm = conn.cursor()
                if inspect.iscoroutine(cm):
                    cm = await cm
                async with cm as cur:
                    await cur.execute(
                        "SELECT emotion_name, intensity, timestamp FROM emotion_state"
                    )
                    rows = await cur.fetchall()

            result = {}
            for row in rows:
                emotion_name, intensity, timestamp = row
                result[emotion_name] = (intensity, timestamp)

            return result

        except Exception as e:
            log_error(f"[emotion_manager] Error loading emotions: {e}")
            return {}

    async def set_emotion(self, emotion: str, intensity: float):
        """Set a single emotion intensity.

        Args:
            emotion: Emotion name (must be in whitelist)
            intensity: Intensity 0.0-10.0

        Returns:
            True if successful, False otherwise
        """
        raw = (emotion or "").lower().strip()
        # Normalize if possible (accept synonyms / variants)
        normalized = normalize_emotion_name(raw)
        if normalized:
            emotion = normalized
        else:
            emotion = raw

        # Validate emotion: accept either the legacy VALID_EMOTIONS or canonical set
        if (emotion not in VALID_EMOTIONS) and (emotion not in CANONICAL_EMOTIONS):
            log_warning(f"[emotion_manager] Invalid emotion: {emotion}")
            return False

        # Clamp intensity
        intensity = max(0.0, min(10.0, float(intensity)))

        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    # Check if emotion exists
                    await cur.execute(
                        "SELECT id FROM emotion_state WHERE emotion_name = %s",
                        (emotion,),
                    )
                    existing = await cur.fetchone()

                    if existing:
                        # Update existing
                        await cur.execute(
                            "UPDATE emotion_state SET intensity = %s, timestamp = NOW() WHERE emotion_name = %s",
                            (intensity, emotion),
                        )
                    else:
                        # Insert new
                        await cur.execute(
                            "INSERT INTO emotion_state (emotion_name, intensity) VALUES (%s, %s)",
                            (emotion, intensity),
                        )

                    # Log to diary within the same transaction
                    await cur.execute(
                        """
                        INSERT INTO emotion_diary 
                        (source, event, emotion, intensity, state, trigger_condition, timestamp)
                        VALUES (%s, %s, %s, %s, %s, %s, NOW())
                        """,
                        (
                            "emotion_manager",
                            "set_emotion",
                            emotion,
                            intensity,
                            "active",
                            "manual_or_tag",
                        ),
                    )

                    await conn.commit()

            log_debug(f"[emotion_manager] Set {emotion} = {intensity}")

            # Notify animation handler
            try:
                from core.animation_handler import get_animation_handler

                handler = get_animation_handler()
                if handler:
                    try:
                        await handler._notify_animation_state_changed(
                            handler.current_state,
                            handler._current_animation_file,
                            handler._current_animation_descriptor,
                        )
                    except Exception:
                        pass
            except Exception:
                pass
            return True

        except Exception as e:
            log_error(f"[emotion_manager] Error setting emotion: {e}")
            return False

    async def update_emotion_from_tags(
        self, text: str, apply_balancing: bool = True
    ) -> Dict[str, float]:
        """Update emotions from LLM message tags.

        Parses tags like {emotion intensity, emotion intensity} and updates DB.
        Applies Plutchik opposite balancing if enabled.

        Args:
            text: Message text with emotion tags
            apply_balancing: Whether to apply opposite emotion reduction

        Returns:
            Updated emotion state
        """

        emotion_tags = self._extract_emotion_tags(text)
        if not emotion_tags:
            log_debug("[emotion_manager] No emotion tags found in text")
            return await self.get_emotion_state()

        log_info(f"[emotion_manager] Extracted emotion tags: {emotion_tags}")

        # Load current emotions
        current = await self._load_emotions_from_db()

        # Update with balancing
        for emotion, new_intensity in emotion_tags.items():
            await self.set_emotion(emotion, new_intensity)

            # Apply balancing: reduce opposite emotions
            if apply_balancing:
                opposite = PLUTCHIK_OPPOSITES.get(emotion)
                if opposite and opposite in current:
                    old_intensity, _ = current[opposite]
                    # Reduce opposite by factor of new emotion
                    reduction_factor = (
                        0.5  # Configurable: each new emotion reduces opposite by 50%
                    )
                    reduced = old_intensity - (new_intensity * reduction_factor)
                    reduced = max(0.0, reduced)

                    await self.set_emotion(opposite, reduced)
                    log_debug(
                        f"[emotion_manager] Reduced opposite {opposite}: {old_intensity} -> {reduced}"
                    )

        # Get final merged state to return
        state = await self.get_emotion_state()
        # After bulk update, try to nudge clients so overlay appears
        try:
            from core.animation_handler import get_animation_handler

            handler = get_animation_handler()
            if handler:
                try:
                    await handler._notify_animation_state_changed(
                        handler.current_state,
                        handler._current_animation_file,
                        handler._current_animation_descriptor,
                    )
                except Exception:
                    pass
        except Exception:
            pass

        return state

    def _extract_emotion_tags(self, text: str) -> Dict[str, float]:
        """Extract emotion tags from text.

        Args:
            text: Text with format {emotion intensity, emotion intensity}

        Returns:
            Dict mapping emotion names to intensities
        """
        import re

        emotion_tags = {}
        invalid_emotions = {}

        # Pattern: {emotion intensity, emotion intensity}
        pattern = r"\{([^}]+)\}"
        matches = re.findall(pattern, text, re.IGNORECASE)

        for match in matches:
            # Split by comma
            parts = [p.strip() for p in match.split(",")]

            for part in parts:
                # Match "emotion_name intensity"
                # Allow negative intensities (clamped later) with optional leading '-'
                emotion_match = re.match(r"(\w+)\s+(-?\d+(?:\.\d+)?)", part)
                if emotion_match:
                    raw_emotion = emotion_match.group(1).lower().strip()
                    # Normalize to canonical emotion name (Ekman6+neutral+relaxed)
                    emotion_type = normalize_emotion_name(raw_emotion)
                    try:
                        intensity = float(emotion_match.group(2))
                        intensity = max(0.0, min(10.0, intensity))
                        if emotion_type:
                            emotion_tags[emotion_type] = intensity
                            log_debug(
                                f"[emotion_manager] ✅ Extracted (normalized): {raw_emotion} -> {emotion_type} = {intensity}"
                            )
                        else:
                            invalid_emotions[raw_emotion] = intensity
                            log_warning(
                                f"[emotion_manager] ❌ Invalid/unrecognized emotion: {raw_emotion}"
                            )

                    except ValueError:
                        log_warning(
                            f"[emotion_manager] Invalid intensity: {emotion_match.group(2)}"
                        )

        if invalid_emotions:
            log_warning(
                f"[emotion_manager] Invalid emotions detected: {invalid_emotions}"
            )
            # persist for potential corrector/orchestration
            try:
                self._last_invalid_emotions = invalid_emotions
            except Exception:
                pass

        return emotion_tags

    async def decay_emotions(self, threshold: Optional[float] = None):
        """Apply decay to all emotions and remove those below baseline (if applicable).

        Note: With baselines, we rarely remove emotions, just clamp them.
        We ensure all canonical emotions exist at least at their baseline.
        """
        # Threshold concept is slightly different now with baselines,
        # but we can still use it to clean up non-canonical noise.
        if threshold is None:
            threshold = self._decay_threshold

        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    # Get all emotions
                    await cur.execute(
                        "SELECT emotion_name, intensity, timestamp FROM emotion_state"
                    )
                    rows = await cur.fetchall()
                    existing_emotions = {row[0] for row in rows}

                    now = datetime.now()
                    to_remove = []
                    to_update = []

                    # 1. Decay existing emotions
                    for emotion_name, intensity, timestamp in rows:
                        state = EmotionState(emotion_name, intensity, timestamp)
                        decayed = state.get_decayed_intensity(now)

                        baseline = EMOTION_BASELINES.get(emotion_name, DEFAULT_BASELINE)

                        # If it's a valid/canonical emotion, we enforce baseline
                        if (
                            emotion_name in VALID_EMOTIONS
                            or emotion_name in CANONICAL_EMOTIONS
                        ):
                            if decayed < baseline:
                                decayed = baseline

                            if abs(decayed - intensity) > 0.01:
                                to_update.append((decayed, emotion_name))
                        else:
                            # Non-canonical garbage -> remove if below threshold/baseline
                            if decayed < max(threshold, baseline):
                                to_remove.append(emotion_name)
                            elif abs(decayed - intensity) > 0.01:
                                to_update.append((decayed, emotion_name))

                    # 2. Ensure all canonical emotions exist
                    to_insert = []
                    for canon in CANONICAL_EMOTIONS:
                        if canon not in existing_emotions:
                            base = EMOTION_BASELINES.get(canon, DEFAULT_BASELINE)
                            # We insert them "fresh" at baseline
                            to_insert.append((canon, base))
                            log_debug(
                                f"[emotion_manager] Seeding missing canonical emotion: {canon} = {base}"
                            )

                    # Perform updates
                    if to_update:
                        await cur.executemany(
                            "UPDATE emotion_state SET intensity = %s WHERE emotion_name = %s",
                            to_update,
                        )
                        # Log significant decay events? Maybe too verbose if we log every minute.
                        # Let's skip detailed diary logging for routine decay to save space/perf,
                        # or only log if change is large.
                        pass

                    if to_remove:
                        await cur.executemany(
                            "DELETE FROM emotion_state WHERE emotion_name = %s",
                            [(name,) for name in to_remove],
                        )

                    if to_insert:
                        await cur.executemany(
                            "INSERT INTO emotion_state (emotion_name, intensity, timestamp) VALUES (%s, %s, NOW())",
                            to_insert,
                        )

                    if to_update or to_remove or to_insert:
                        await conn.commit()
                        if len(to_update) > 0 or len(to_insert) > 0:
                            log_debug(
                                f"[emotion_manager] Decay cycle: updated {len(to_update)}, inserted {len(to_insert)}, removed {len(to_remove)}"
                            )

        except Exception as e:
            log_error(f"[emotion_manager] Error decaying emotions: {e}")

    async def sync_emotions_from_all_sources(self) -> Dict[str, float]:
        """Synchronize emotions from all available sources.

        Aggregates emotions from:
        1. ai_diary latest entries (emotions field)
        2. Message text with {emotion intensity} tags
        3. emotion_state DB (current state)

        Returns:
            Merged emotion state
        """
        try:
            merged_emotions: Dict[str, float] = {}

            # Source 1: Get emotions from ai_diary (latest N entries with timestamps)
            log_debug("[emotion_manager] Fetching emotions from ai_diary...")
            try:
                now = datetime.now()
                async with get_conn_ctx() as conn:
                    cm = conn.cursor()
                    if inspect.iscoroutine(cm):
                        cm = await cm
                    async with cm as cur:
                        # Get latest 20 diary entries with emotions AND timestamp
                        await cur.execute(
                            """SELECT emotions, timestamp FROM ai_diary 
                               WHERE emotions IS NOT NULL AND emotions != '[]' 
                               ORDER BY timestamp DESC LIMIT 20"""
                        )
                        rows = await cur.fetchall()

                        for row in rows:
                            if row and row[0]:  # emotions, timestamp
                                emotions_json = row[0]
                                entry_ts = row[1]

                                # Calculate decay based on diary entry age
                                # Note: We treat the diary entry as a snapshot at that time.
                                # IF the entry is old, its influence should be decayed.
                                if isinstance(entry_ts, str):
                                    # handle potential string format (though db should return datetime)
                                    try:
                                        entry_ts = datetime.fromisoformat(entry_ts)
                                    except (ValueError, TypeError):
                                        entry_ts = now  # fallback

                                try:
                                    emotions_data = (
                                        json.loads(emotions_json)
                                        if isinstance(emotions_json, str)
                                        else emotions_json
                                    )
                                    if isinstance(emotions_data, list):
                                        for emotion_entry in emotions_data:
                                            # Handle both formats: ["emotion"] and [{"type": "emotion", "intensity": 7}]
                                            if isinstance(emotion_entry, str):
                                                # Simple string format
                                                emotion_type = emotion_entry.lower()
                                                raw_intensity = 5.0  # Default intensity
                                            elif isinstance(emotion_entry, dict):
                                                # Object format with type and intensity
                                                emotion_type = emotion_entry.get(
                                                    "type", ""
                                                ).lower()
                                                raw_intensity = float(
                                                    emotion_entry.get("intensity", 5.0)
                                                )
                                            else:
                                                continue

                                            if emotion_type in VALID_EMOTIONS:
                                                # Apply decay to this historical value
                                                # Create temp state to calc decay
                                                temp_state = EmotionState(
                                                    emotion_type,
                                                    raw_intensity,
                                                    entry_ts,
                                                )
                                                decayed_diary_val = (
                                                    temp_state.get_decayed_intensity(
                                                        now
                                                    )
                                                )

                                                if (
                                                    emotion_type not in merged_emotions
                                                    or decayed_diary_val
                                                    > merged_emotions[emotion_type]
                                                ):
                                                    merged_emotions[emotion_type] = (
                                                        decayed_diary_val
                                                    )
                                                    # log_debug(
                                                    #     f"[emotion_manager] 📔 From diary (decayed): {emotion_type} = {decayed_diary_val:.2f} (raw {raw_intensity} from {entry_ts})"
                                                    # )
                                except (json.JSONDecodeError, ValueError) as e:
                                    log_warning(
                                        f"[emotion_manager] Could not parse diary emotions: {e}"
                                    )
            except Exception as e:
                log_warning(f"[emotion_manager] Error fetching from ai_diary: {e}")

            # Source 2: emotions from emotion_state (already decayed and validated)
            log_debug("[emotion_manager] Fetching emotions from emotion_state DB...")
            try:
                current_db_emotions = await self.get_emotion_state()
                # Merge (DB emotions override diary if already present)
                for emotion_type, intensity in current_db_emotions.items():
                    merged_emotions[emotion_type] = intensity
                    log_debug(
                        f"[emotion_manager] 💾 From DB: {emotion_type} = {intensity}"
                    )
            except Exception as e:
                log_warning(f"[emotion_manager] Error fetching from emotion_state: {e}")

            # If no emotions found anywhere, initialize default state
            # Ensure all canonical emotions have at least baseline in merged set
            for canon in CANONICAL_EMOTIONS:
                base = EMOTION_BASELINES.get(canon, DEFAULT_BASELINE)
                if canon not in merged_emotions:
                    merged_emotions[canon] = base
                elif merged_emotions[canon] < base:
                    merged_emotions[canon] = base

            # Save merged emotions back to DB
            # This effectively "re-syncs" the DB to match the diary + decay state
            # NOTE: We must be careful here. set_emotion(..., intensity) updates timestamp to NOW().
            # If we just read a decayed intensity of 5.0 (originally 10.0 from 1hr ago), and set 5.0 now,
            # we effectively "reset" the decay curve starting at 5.0.
            # This is mathematically consistent (5.0 now decays to 2.5 in 1 more hr, just like 10.0(old) would have).
            for emotion_type, intensity in merged_emotions.items():
                await self.set_emotion(emotion_type, intensity)

            log_info(
                f"[emotion_manager] 🔄 Synchronized emotions from all sources: {len(merged_emotions)} emotions"
            )
            return merged_emotions

        except Exception as e:
            log_error(f"[emotion_manager] Error syncing emotions: {e}")
            return {}
            return {}

    async def get_static_injection(self, message=None, context_memory=None) -> dict:
        """Get emotion state formatted for static injection into LLM context.

        This is called by action_parser.gather_static_injections() to inject
        the current emotional state into the LLM prompt.

        Args:
            message: Optional message object (for compatibility with gather_static_injections)
            context_memory: Optional context memory (for compatibility)

        Returns:
            Dict with 'emotion_state' key containing formatted emotion string
        """
        # CRITICAL: Do NOT sync on every read, as it reverts decay by pulling old diary entries.
        # Only read existing state.
        # await self.sync_emotions_from_all_sources()

        emotions = await self.get_emotion_state()

        # Get max emotions to display from config (default 7)
        max_emotions = config_registry.get_value("EMOTION_MAX_DISPLAY", 7)

        if not emotions:
            emotion_str = "Neutral 5.0 (moderate)"
        else:
            # Sort by intensity (max EMOTION_MAX_DISPLAY)
            sorted_emotions = sorted(
                emotions.items(), key=lambda x: x[1], reverse=True
            )[:max_emotions]

            emotion_lines = []
            for name, intensity in sorted_emotions:
                # Add qualitative descriptor
                desc = ""
                if intensity < 2.0:
                    desc = "trace"
                elif intensity < 4.0:
                    desc = "low"
                elif intensity < 7.0:
                    desc = "moderate"
                elif intensity < 9.0:
                    desc = "high"
                else:
                    desc = "intense"

                emotion_lines.append(f"{name} ({intensity:.1f} - {desc})")

            emotion_str = ", ".join(emotion_lines)

        log_debug(f"[emotion_manager] Providing static injection: {emotion_str}")

        # Provide canonical available emotions for the LLM to use
        avail = sorted(list(CANONICAL_EMOTIONS))
        avail_str = ", ".join(avail)

        instruction = (
            f"Current Emotional State: {emotion_str}\n"
            f"Available Emotion Tags: {avail_str}\n"
            "Instruction: To express emotions, include tags in your response like {happy 8.5, surprised 3.0}. "
            "Use only the provided emotion names. Your emotional state decays over time, so reinforce it if feelings persist."
        )

        return {
            "emotion_state": instruction,
            "current_emotions_nl": emotion_str,
            "available_emotions": avail,
        }


# Export for use by other modules
__all__ = [
    "EmotionManager",
    "EmotionState",
    "VALID_EMOTIONS",
    "PLUTCHIK_OPPOSITES",
    "CANONICAL_EMOTIONS",
    "EMOTION_SYNONYMS",
    "normalize_emotion_name",
]

# Export plugin class for auto-discovery by core_initializer
PLUGIN_CLASS = EmotionManager
