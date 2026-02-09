Emotion Engine
=============

.. versionadded:: 1.0
   Centralized emotional state management with decay, balancing, and persistence.

Overview
--------

The Emotion Engine is a core plugin that manages SyntH's emotional state with sophisticated psychological modeling. It provides persistent emotional state storage, exponential decay over time, and Plutchik's wheel-based emotion balancing for realistic emotional behavior.

**Key Features:**

- **Persistent Storage**: Emotions stored in database with timestamps
- **Exponential Decay**: Emotions naturally fade over time
- **Emotion Balancing**: Plutchik's wheel opposites reduce conflicting emotions
- **LLM Integration**: Automatic emotion extraction from message tags
- **Global State**: Readable emotional state for WebUI, animations, and plugins

Architecture
------------

The emotion engine consists of three main components:

1. **EmotionState Class**: Represents individual emotions with intensity and decay
2. **EmotionManager Plugin**: Core plugin handling all emotion operations
3. **Database Layer**: Persistent storage with automatic cleanup

Emotion State Model
-------------------

**Emotion Representation:**

.. code-block:: python

   @dataclass
   class EmotionState:
       emotion_name: str      # Emotion identifier
       intensity: float       # 0.0-10.0 scale
       timestamp: datetime    # Creation/update time

**Decay Calculation:**

Emotions decay exponentially over time using the formula:

.. math::

   I(t) = I_0 \cdot e^{-\frac{t}{\tau}}

Where:
- :math:`I(t)` = Current intensity
- :math:`I_0` = Initial intensity
- :math:`t` = Time elapsed (seconds)
- :math:`\tau` = Decay half-life (configurable, default 3600s = 1 hour)

**Configuration:**

- ``EMOTION_DECAY_TAU``: Decay half-life in seconds (default: 3600)
- ``EMOTION_DECAY_THRESHOLD``: Minimum intensity before removal (default: 0.1)

Plutchik's Wheel Balancing
--------------------------

The engine implements Plutchik's circumplex model of emotion, where opposite emotions on the wheel naturally suppress each other:

.. code-block:: python

   PLUTCHIK_OPPOSITES = {
       'joy': 'sadness',
       'trust': 'disgust',
       'fear': 'anger',
       'anticipation': 'surprise',
       'happiness': 'sadness',
       'excitement': 'calm',
       # ... extended opposites
   }

**Balancing Logic:**

When a new emotion is triggered, its opposite emotion is reduced by the same intensity amount. This creates realistic emotional dynamics where conflicting emotions cannot coexist at high intensity.

Supported Emotions
------------------

The engine supports a comprehensive whitelist of emotions:

**Basic Emotions (Ekman):**
   anger, disgust, fear, happiness, sadness, surprise

**Complex Emotions (Plutchik):**
   joy, trust, anticipation, acceptance, serenity, interest, boredom, annoyance, apprehension, pensiveness, fatigue, vigilance, rage, loathing, terror, amazement, grief, optimism, love, submission, awe, disapproval, remorse, contempt, aggressiveness, ecstasy

**Common States:**
   anxiety, calm, confusion, contentment, curiosity, despair, determination, disappointment, doubt, embarrassment, enthusiasm, envy, excitement, frustration, gratitude, guilt, hope, humiliation, impatience, indifference, jealousy, loneliness, nervousness, outrage, panic, patience, pride, regret, relief, resentment, satisfaction, shame, shock, sympathy, tenderness, triumph, worry

**Social/Relational:**
   admiration, affection, arrogance, compassion, empathy, hatred, kindness, pity, respect, scorn

**Moods:**
   amused, apathetic, bitter, cheerful, depressed, eager, gloomy, irritated, melancholy, miserable, playful, restless, silly, sombre, tense, thoughtful, weary

Database Schema
---------------

**emotion_state Table:**

.. code-block:: sql

   CREATE TABLE emotion_state (
       id INT AUTO_INCREMENT PRIMARY KEY,
       emotion_name VARCHAR(100) NOT NULL,
       intensity FLOAT NOT NULL DEFAULT 5.0,
       timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
       updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
       INDEX idx_emotion_name (emotion_name),
       INDEX idx_timestamp (timestamp)
   ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

**Fields:**

- ``emotion_name``: Emotion identifier (from whitelist)
- ``intensity``: Raw intensity value (0.0-10.0)
- ``timestamp``: When emotion was created/updated
- ``updated_at``: Automatic update timestamp

API Reference
-------------

**Core Actions:**

``static_inject``
   Inject current emotional state into LLM context

``get_emotion_state``
   Get current emotional state with decay applied

``update_emotion_from_tags``
   Extract and apply emotions from LLM message tags like ``{emotion intensity}``

``set_emotion``
   Set a single emotion intensity directly

``decay_emotions``
   Apply decay to all emotions and remove low-intensity ones

``sync_emotions_from_all_sources``
   Synchronize emotions from ai_diary, message tags, and emotion_state DB

**Usage Examples:**

.. code-block:: python

   from plugins.emotion_manager import EmotionManager

   # Get current emotional state
   mgr = EmotionManager()
   state = await mgr.get_emotion_state()
   # Returns: {'happiness': 7.2, 'curiosity': 3.1, ...}

   # Update from message tags
   await mgr.update_emotion_from_tags("I'm feeling {joy 8.0} and {curiosity 6.5} about this!")

   # Set emotion directly
   await mgr.set_emotion('excitement', 9.0)

LLM Integration
---------------

**Tag Format:**

Emotions can be specified in LLM responses using the format ``{emotion intensity}``:

.. code-block:: none

   I'm so excited about this project! {excitement 8.5} {curiosity 7.2}

**Canonical emotion set:**

To keep behavior consistent the LLM should use the canonical emotion set (Ekman 6 + neutral + relaxed):

- ``happy``, ``sad``, ``angry``, ``fear``, ``disgust``, ``surprised``, ``neutral``, ``relaxed``

If the LLM uses an unknown emotion name, the system will trigger a corrector requesting the LLM to resend using only canonical emotion names (format: ``{emotion intensity}``, intensity 0.0-10.0).

**Automatic Extraction:**

The engine automatically scans LLM messages for emotion tags and applies them to the emotional state with balancing.

**Context Injection:**

Emotional state is automatically injected into LLM prompts for personality-consistent responses.

WebUI Integration
-----------------

The emotional state is exposed to the WebUI for real-time visualization:

.. code-block:: python

   # Get current state for display
   current_emotions = await emotion_manager.get_emotion_state()

   # Filter significant emotions (> 1.0 intensity)
   significant = {k: v for k, v in current_emotions.items() if v > 1.0}

**Animation Triggers:**

Emotional state changes can trigger animations and visual feedback in the WebUI.

Configuration
-------------

**Environment Variables:**

- ``EMOTION_DECAY_TAU``: Decay half-life in seconds (default: 3600)
- ``EMOTION_DECAY_THRESHOLD``: Cleanup threshold (default: 0.1)

**Database:**

- Requires MySQL/MariaDB with utf8mb4 support
- Automatic table creation on startup
- No manual schema management required

Troubleshooting
---------------

**Common Issues:**

**Emotions not decaying:**
   Check ``EMOTION_DECAY_TAU`` configuration value

**Invalid emotions accepted:**
   Verify emotion names against ``VALID_EMOTIONS`` whitelist

**Database connection errors:**
   Ensure DB credentials are configured correctly

**Memory leaks:**
   Check that ``decay_emotions`` is called periodically

**Debug Commands:**

.. code-block:: bash

   # Check current emotional state
   python3 -c "
   import asyncio
   from plugins.emotion_manager import EmotionManager
   mgr = EmotionManager()
   state = asyncio.run(mgr.get_emotion_state())
   print('Current emotions:', state)
   "

   # Force emotion decay
   python3 -c "
   import asyncio
   from plugins.emotion_manager import EmotionManager
   mgr = EmotionManager()
   asyncio.run(mgr.decay_emotions())
   print('Emotions decayed')
   "</content>
<parameter name="filePath">/videodrome/videodrome-deployment/Synthetic_Heart/docs/emotion_engine.rst