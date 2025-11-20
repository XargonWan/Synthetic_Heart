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
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict

from core.plugin_base import PluginBase
from core.db import get_conn_ctx
from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.config_manager import config_registry

# Canonical whitelist of valid emotions - moved from persona_manager
VALID_EMOTIONS = {
    # Basic emotions (Ekman)
    'anger', 'disgust', 'fear', 'happiness', 'sadness', 'surprise',
    
    # Complex emotions (Plutchik & extensions)
    'joy', 'trust', 'anticipation', 'acceptance', 'serenity', 'interest',
    'boredom', 'annoyance', 'apprehension', 'pensiveness', 'fatigue', 'vigilance',
    'rage', 'loathing', 'terror', 'amazement', 'grief', 'optimism', 'love',
    'submission', 'awe', 'disapproval', 'remorse', 'contempt', 'aggressiveness', 'ecstasy',
    
    # Common emotional states
    'anxiety', 'calm', 'confusion', 'contentment', 'curiosity', 'despair',
    'determination', 'disappointment', 'doubt', 'embarrassment', 'enthusiasm', 'envy',
    'excitement', 'frustration', 'gratitude', 'guilt', 'hope', 'humiliation',
    'impatience', 'indifference', 'jealousy', 'loneliness', 'nervousness',
    'outrage', 'panic', 'patience', 'pride', 'regret', 'relief', 'resentment',
    'satisfaction', 'shame', 'shock', 'sympathy', 'tenderness', 'triumph', 'worry',
    
    # Social/relational emotions
    'admiration', 'affection', 'arrogance', 'compassion', 'empathy', 'hatred',
    'kindness', 'pity', 'respect', 'scorn',
    
    # Moods
    'amused', 'apathetic', 'bitter', 'cheerful', 'depressed', 'eager',
    'gloomy', 'irritated', 'melancholy', 'miserable', 'playful', 'restless',
    'silly', 'sombre', 'tense', 'thoughtful', 'weary',
    
    # Intensive emotions
    'agony', 'bliss', 'delight', 'desire', 'horror', 'lust', 'passion', 'pleasure', 'rapture'
}

# Plutchik's wheel - opposite emotions for balancing
# When a new emotion is triggered, opposites are reduced
PLUTCHIK_OPPOSITES = {
    # Primary opposites
    'joy': 'sadness',
    'sadness': 'joy',
    'trust': 'disgust',
    'disgust': 'trust',
    'fear': 'anger',
    'anger': 'fear',
    'anticipation': 'surprise',
    'surprise': 'anticipation',
    
    # Extended opposites
    'happiness': 'sadness',
    'excitement': 'calm',
    'calm': 'excitement',
    'anxiety': 'contentment',
    'contentment': 'anxiety',
    'hope': 'despair',
    'despair': 'hope',
    'optimism': 'pessimism',
    'pessimism': 'optimism',
    'love': 'hatred',
    'hatred': 'love',
    'acceptance': 'disapproval',
    'disapproval': 'acceptance',
    'confidence': 'doubt',
    'doubt': 'confidence',
    'serenity': 'apprehension',
    'apprehension': 'serenity',
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
            'emotion': self.emotion_name,
            'intensity': self.intensity,
            'timestamp': self.timestamp.isoformat(),
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
        tau = config_registry.get_value('EMOTION_DECAY_TAU', 3600)
        
        # Calculate time delta in seconds
        delta_t = (current_time - self.timestamp).total_seconds()
        
        # Exponential decay: intensity * e^(-delta_t / tau)
        decayed = self.intensity * math.exp(-delta_t / tau)
        
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
            'name': 'emotion_manager',
            'description': 'Centralized emotional state management with decay and balancing',
            'version': '1.0.0',
            'type': 'core',  # Mark as core plugin
        }
    
    def get_supported_actions(self) -> dict:
        """Register supported actions for the emotion manager."""
        return {
            'static_inject': {
                'description': 'Inject current emotional state into LLM context',
                'required_params': {},
                'optional_params': {},
            },
            'get_emotion_state': {
                'description': 'Get current emotional state with decay applied',
                'required_params': {},
                'optional_params': {
                    'include_raw': 'bool - include raw timestamp data',
                },
            },
            'update_emotion_from_tags': {
                'description': 'Update emotions from LLM message tags like {emotion intensity}',
                'required_params': {
                    'text': 'Message text containing emotion tags',
                },
                'optional_params': {
                    'apply_balancing': 'bool - apply Plutchik opposite balancing (default: true)',
                },
            },
            'set_emotion': {
                'description': 'Set a single emotion intensity directly',
                'required_params': {
                    'emotion': 'Emotion name (must be in whitelist)',
                    'intensity': 'float 0.0-10.0',
                },
                'optional_params': {},
            },
            'decay_emotions': {
                'description': 'Apply decay to all emotions and remove low-intensity ones',
                'required_params': {},
                'optional_params': {
                    'threshold': 'float - remove emotions below this intensity (default 0.1)',
                },
            },
            'sync_emotions_from_all_sources': {
                'description': 'Synchronize emotions from ai_diary, message tags, and emotion_state DB',
                'required_params': {},
                'optional_params': {},
            },
        }
    
    async def start(self):
        """Initialize emotion manager and create DB table if needed."""
        log_info("[emotion_manager] Starting emotion manager")
        try:
            await self._ensure_table_exists()
            log_info("[emotion_manager] Emotion table initialized")
        except Exception as e:
            log_error(f"[emotion_manager] Failed to initialize: {e}")
    
    async def _ensure_table_exists(self):
        """Create emotion_state table if it doesn't exist."""
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
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
    
    async def get_emotion_state(self, include_raw: bool = False) -> Dict[str, float]:
        """Get current emotional state with decay applied.
        
        Args:
            include_raw: If True, return raw intensities without decay
            
        Returns:
            Dictionary mapping emotion names to current intensities
        """
        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
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
                async with conn.cursor() as cur:
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
        emotion = emotion.lower().strip()
        
        # Validate emotion
        if emotion not in VALID_EMOTIONS:
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
                        (emotion,)
                    )
                    existing = await cur.fetchone()
                    
                    if existing:
                        # Update existing
                        await cur.execute(
                            "UPDATE emotion_state SET intensity = %s, timestamp = NOW() WHERE emotion_name = %s",
                            (intensity, emotion)
                        )
                    else:
                        # Insert new
                        await cur.execute(
                            "INSERT INTO emotion_state (emotion_name, intensity) VALUES (%s, %s)",
                            (emotion, intensity)
                        )
                    
                    await conn.commit()
            
            log_debug(f"[emotion_manager] Set {emotion} = {intensity}")
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
        import re
        
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
                    reduction_factor = 0.5  # Configurable: each new emotion reduces opposite by 50%
                    reduced = old_intensity - (new_intensity * reduction_factor)
                    reduced = max(0.0, reduced)
                    
                    await self.set_emotion(opposite, reduced)
                    log_debug(f"[emotion_manager] Reduced opposite {opposite}: {old_intensity} -> {reduced}")
        
        return await self.get_emotion_state()
    
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
        pattern = r'\{([^}]+)\}'
        matches = re.findall(pattern, text, re.IGNORECASE)
        
        for match in matches:
            # Split by comma
            parts = [p.strip() for p in match.split(',')]
            
            for part in parts:
                # Match "emotion_name intensity"
                emotion_match = re.match(r'(\w+)\s+(\d+(?:\.\d+)?)', part)
                if emotion_match:
                    emotion_type = emotion_match.group(1).lower().strip()
                    try:
                        intensity = float(emotion_match.group(2))
                        intensity = max(0.0, min(10.0, intensity))
                        
                        if emotion_type in VALID_EMOTIONS:
                            emotion_tags[emotion_type] = intensity
                            log_debug(f"[emotion_manager] ✅ Extracted: {emotion_type} = {intensity}")
                        else:
                            invalid_emotions[emotion_type] = intensity
                            log_warning(f"[emotion_manager] ❌ Invalid emotion: {emotion_type}")
                            
                    except ValueError:
                        log_warning(f"[emotion_manager] Invalid intensity: {emotion_match.group(2)}")
        
        if invalid_emotions:
            log_warning(f"[emotion_manager] Invalid emotions detected: {invalid_emotions}")
        
        return emotion_tags
    
    async def decay_emotions(self, threshold: Optional[float] = None):
        """Apply decay to all emotions and remove low-intensity ones.
        
        Args:
            threshold: Remove emotions below this intensity (default 0.1)
        """
        if threshold is None:
            threshold = self._decay_threshold
        
        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    # Get all emotions
                    await cur.execute("SELECT emotion_name, intensity, timestamp FROM emotion_state")
                    rows = await cur.fetchall()
                
                now = datetime.now()
                to_remove = []
                
                for emotion_name, intensity, timestamp in rows:
                    state = EmotionState(emotion_name, intensity, timestamp)
                    decayed = state.get_decayed_intensity(now)
                    
                    if decayed < threshold:
                        to_remove.append(emotion_name)
                        log_debug(f"[emotion_manager] Marking for removal: {emotion_name} ({decayed:.2f})")
                    else:
                        # Update with decayed intensity
                        async with get_conn_ctx() as conn2:
                            async with conn2.cursor() as cur2:
                                await cur2.execute(
                                    "UPDATE emotion_state SET intensity = %s WHERE emotion_name = %s",
                                    (decayed, emotion_name)
                                )
                                await conn2.commit()
                
                # Remove low-intensity emotions
                if to_remove:
                    async with get_conn_ctx() as conn3:
                        async with conn3.cursor() as cur3:
                            for emotion_name in to_remove:
                                await cur3.execute(
                                    "DELETE FROM emotion_state WHERE emotion_name = %s",
                                    (emotion_name,)
                                )
                            await conn3.commit()
                    
                    log_info(f"[emotion_manager] Removed {len(to_remove)} decayed emotions")
            
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
            
            # Source 1: Get emotions from ai_diary (latest N entries)
            log_debug("[emotion_manager] Fetching emotions from ai_diary...")
            try:
                async with get_conn_ctx() as conn:
                    async with conn.cursor() as cur:
                        # Get latest 10 diary entries with emotions
                        await cur.execute(
                            """SELECT emotions FROM ai_diary 
                               WHERE emotions IS NOT NULL AND emotions != '[]' 
                               ORDER BY timestamp DESC LIMIT 10"""
                        )
                        rows = await cur.fetchall()
                        
                        for row in rows:
                            if row and row[0]:
                                try:
                                    emotions_data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                                    if isinstance(emotions_data, list):
                                        for emotion_entry in emotions_data:
                                            # Handle both formats: ["emotion"] and [{"type": "emotion", "intensity": 7}]
                                            if isinstance(emotion_entry, str):
                                                # Simple string format
                                                emotion_type = emotion_entry.lower()
                                                intensity = 5.0  # Default intensity
                                            elif isinstance(emotion_entry, dict):
                                                # Object format with type and intensity
                                                emotion_type = emotion_entry.get('type', '').lower()
                                                intensity = float(emotion_entry.get('intensity', 5.0))
                                            else:
                                                continue
                                            
                                            # Favor higher intensities
                                            if emotion_type in VALID_EMOTIONS:
                                                if emotion_type not in merged_emotions or intensity > merged_emotions[emotion_type]:
                                                    merged_emotions[emotion_type] = intensity
                                                    log_debug(f"[emotion_manager] 📔 From diary: {emotion_type} = {intensity}")
                                except (json.JSONDecodeError, ValueError) as e:
                                    log_warning(f"[emotion_manager] Could not parse diary emotions: {e}")
            except Exception as e:
                log_warning(f"[emotion_manager] Error fetching from ai_diary: {e}")
            
            # Source 2: emotions from emotion_state (already decayed and validated)
            log_debug("[emotion_manager] Fetching emotions from emotion_state DB...")
            try:
                current_db_emotions = await self.get_emotion_state()
                # Merge (DB emotions override diary if already present)
                for emotion_type, intensity in current_db_emotions.items():
                    merged_emotions[emotion_type] = intensity
                    log_debug(f"[emotion_manager] 💾 From DB: {emotion_type} = {intensity}")
            except Exception as e:
                log_warning(f"[emotion_manager] Error fetching from emotion_state: {e}")
            
            # Save merged emotions back to DB
            for emotion_type, intensity in merged_emotions.items():
                await self.set_emotion(emotion_type, intensity)
            
            log_info(f"[emotion_manager] 🔄 Synchronized emotions from all sources: {len(merged_emotions)} emotions")
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
        # CRITICAL: Sync emotions from all sources (ai_diary, DB, etc) before reading
        await self.sync_emotions_from_all_sources()
        
        emotions = await self.get_emotion_state()
        
        # Get max emotions to display from config (default 7)
        max_emotions = config_registry.get_value('EMOTION_MAX_DISPLAY', 7)
        
        if not emotions:
            emotion_str = "neutral (no active emotions)"
        else:
            # Sort by intensity (descending) and take top N
            sorted_emotions = sorted(emotions.items(), key=lambda x: x[1], reverse=True)[:max_emotions]
            emotion_lines = [f"{name} ({intensity:.1f})" for name, intensity in sorted_emotions]
            emotion_str = ", ".join(emotion_lines)
        
        log_debug(f"[emotion_manager] Providing static injection: {emotion_str}")
        
        return {
            'emotion_state': f"Current emotional state: {emotion_str}\n\nConsider this emotional state when generating your response. Your emotions influence how you perceive situations and respond to messages.",
        }


# Export for use by other modules
__all__ = [
    'EmotionManager',
    'EmotionState',
    'VALID_EMOTIONS',
    'PLUTCHIK_OPPOSITES',
]

# Export plugin class for auto-discovery by core_initializer
PLUGIN_CLASS = EmotionManager
