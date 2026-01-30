Plugins
=======

.. image:: res/plugins.png
    :alt: synth plugin architecture diagram
    :width: 600px
    :align: center


Plugins are the primary mechanism for extending synth's capabilities. Unlike traditional systems where functionality is hardcoded, synth's plugin system allows for complete modularity - plugins are automatically discovered and loaded at runtime without any core modifications.

Plugin Architecture
-------------------

All plugins follow a consistent architecture:

- **Auto-Discovery**: Plugins are automatically found in the ``plugins/`` directory
- **Self-Registration**: Each plugin registers itself with the core system
- **Action-Based**: Plugins provide actions that can be invoked by LLM-generated JSON
- **Schema-Driven**: Actions are defined with clear schemas including required/optional fields
- **Validation**: All actions are validated before execution

Available Action Plugins
------------------------

**Stable Plugins** (in ``plugins/`` directory):

* ``ai_diary`` – Personal memory system for synth. Records conversations, thoughts, and emotions. See :doc:`ai_diary_personal_memory` for details.
* ``bio_manager`` – Manage persistent user biographies. Uses database settings ``DB_HOST``, ``DB_USER``, ``DB_PASS`` and ``DB_NAME``.
* ``get_logs`` – Return the last N lines from a log file (default: ``synth.log``, default lines: 30). Useful to provide the LLM or operators with recent runtime output for diagnostics.
* ``search_logs`` – Search logs for keywords or regular expressions (queries can be a string or list). Optional parameters: ``regex`` (bool), ``context`` (surrounding lines), ``lines`` (how many tail lines to search). Results are delivered back to the invoking interface.

Bio Manager Plugin
------------------

The ``bio_manager`` plugin provides persistent storage and retrieval of user biographical information. It automatically injects participant context into LLM prompts, including:

- **User profiles**: Nicknames, short bio, current feelings
- **Chat history**: Recent messages from each participant
- **Last accessed**: Automatic timestamp updates

**Database Schema**:

.. code-block:: sql

    CREATE TABLE bio (
        id VARCHAR(255) PRIMARY KEY,
        known_as TEXT DEFAULT '[]',
        likes TEXT DEFAULT '[]',
        not_likes TEXT DEFAULT '[]',
        information TEXT DEFAULT '',
        past_events TEXT DEFAULT '[]',
        feelings TEXT DEFAULT '[]',
        contacts TEXT DEFAULT '{}',
        social_accounts TEXT DEFAULT '[]',
        privacy VARCHAR(50) DEFAULT 'default',
        created_at TEXT DEFAULT '',
        last_accessed TEXT DEFAULT '',
        last_update TEXT DEFAULT '',
        update_count INT DEFAULT 0
    );

**Performance Optimizations**:

- **Table Initialization Cache**: Prevents repeated ``_ensure_table()`` calls that caused timeouts
- **Async Operations**: ``get_static_injection()`` is fully async to prevent deadlock with the event loop
- **Connection Pool Tuning**: Reduced pool size to 5 connections to ensure availability

**API Methods**:

- ``get_bio_light(user_id)``: Retrieve lightweight bio for prompt injection
- ``get_bio_full(user_id)``: Retrieve complete user profile
- ``update_bio_fields_auto(user_id, updates)``: Update fields without rate limiting
- ``get_static_injection(message, context_memory)``: Async method for prompt context injection

Emotion Manager Plugin
----------------------

.. versionadded:: 1.0
   Centralized emotional state management with decay, balancing, and persistence.

The ``emotion_manager`` plugin manages SyntH's emotional state with sophisticated psychological modeling. It provides persistent emotional state storage, exponential decay over time, and Plutchik's wheel-based emotion balancing for realistic emotional behavior.

**Key Features:**

- **Persistent Storage**: Emotions stored in database with timestamps
- **Exponential Decay**: Emotions naturally fade over time
- **Emotion Balancing**: Plutchik's wheel opposites reduce conflicting emotions
- **LLM Integration**: Automatic emotion extraction from message tags
- **Global State**: Readable emotional state for WebUI, animations, and plugins

**Architecture:**

The emotion engine consists of three main components:

1. **EmotionState Class**: Represents individual emotions with intensity and decay
2. **EmotionManager Plugin**: Core plugin handling all emotion operations
3. **Database Layer**: Persistent storage with automatic cleanup

**Emotion State Model:**

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

**Plutchik's Wheel Balancing:**

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

**Supported Emotions:**

The engine supports a comprehensive whitelist including Ekman's basic emotions (anger, disgust, fear, happiness, sadness, surprise), Plutchik's complex emotions (joy, trust, anticipation, etc.), and common states (anxiety, calm, confusion, etc.).

**Database Schema:**

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

**API Actions:**

- ``static_inject``: Inject current emotional state into LLM context
- ``get_emotion_state``: Get current emotional state with decay applied
- ``update_emotion_from_tags``: Extract and apply emotions from LLM message tags like ``{emotion intensity}``
- ``set_emotion``: Set a single emotion intensity directly
- ``decay_emotions``: Apply decay to all emotions and remove low-intensity ones
- ``sync_emotions_from_all_sources``: Synchronize emotions from ai_diary, message tags, and emotion_state DB

**LLM Integration:**

Emotions can be specified in LLM responses using the format ``{emotion intensity}``:

.. code-block:: none

   I'm so excited about this project! {excitement 8.5} {curiosity 7.2}

The engine automatically scans LLM messages for emotion tags and applies them to the emotional state with balancing.

**WebUI Integration:**

The emotional state is exposed to the WebUI for real-time visualization and can trigger animations and visual feedback.

**Troubleshooting:**

- Check ``EMOTION_DECAY_TAU`` configuration for decay issues
- Verify emotion names against ``VALID_EMOTIONS`` whitelist
- Ensure DB credentials are configured correctly
- Check that ``decay_emotions`` is called periodically

* ``emotion_manager`` – Centralized emotional state management with decay and balancing.
* ``blocklist`` – User blocking/unblocking functionality (no configuration).
* ``chat_link`` – Cross-platform chat linking and message forwarding.
* ``message_map`` – Message threading and conversation tracking.
* ``message_plugin`` – Send text across registered interfaces (no configuration).
* ``recent_chats`` – Access to recent conversation history.
* ``time_plugin`` – Inject current time and location (no configuration).
* ``weather_plugin`` – Provide weather info as static context. Optional ``WEATHER_FETCH_TIME`` sets refresh interval. Daily announcements can be enabled with ``WEATHER_DAILY_REPORT_ENABLED`` via ``WEATHER_DAILY_REPORT_INTERFACE``.
    - **Broadcasting**: If interface is ``telegram_bot``, reports are broadcast to all known group chats.
    - **Context Awareness**: Injects specific targeted interface paths so the LLM knows where to address the report.
    - **Manual Trigger**: "weather report" (bypasses sleep mode).
* ``tts_lipsync`` – Generate speech audio from external TTS endpoints.
    - **Smart Cleaning**: Emojis are stripped from audio generation to prevent reading errors, but preserved in text captions.
    - **Configuration**: ``TTS_ENDPOINTS`` (comma-separated list of URLs), ``TTS_TIMEOUT_SECONDS``, ``TTS_OUTPUT_DIR``.
    - **Failover Support**: Automatically cycles through configured endpoints if one fails. Supports hardcoded voice reference mapping for specific servers in ``_get_voice_ref``.
    - **Auto-Dispatch**: Automatically dispatches generated audio to the active interface (Discord/Telegram) based on ``interface_path`` in context.
    - **Audio Format**: Automatically wraps raw PCM streams in WAV containers for compatibility.

Recent Chats Plugin
-------------------

.. versionchanged:: 1.0
   ``update_chat_activity`` is now automatic and no longer exposed to LLM actions.

The ``recent_chats`` plugin manages conversation activity tracking and provides access to recently active chats. Unlike other plugins, this plugin focuses on mechanical operations that don't require LLM reasoning.

**Key Changes in v1.0:**

- **Automatic Activity Tracking**: Chat activity is now tracked automatically when messages are received, eliminating the need for LLM-requested ``update_chat_activity`` actions
- **Centralized Implementation**: Activity tracking is handled in ``core/chat_context_manager.py`` instead of being duplicated across interfaces
- **Reduced Prompt Size**: One less action for the LLM to consider

**Available Actions:**

- ``get_recent_chats``: Retrieve the most recently active chats
- ``cleanup_old_chats``: Remove chat records older than specified days

**Database Schema:**

.. code-block:: sql

   CREATE TABLE recent_chats (
       chat_id VARCHAR(255) PRIMARY KEY,
       last_active DOUBLE NOT NULL,
       metadata TEXT,
       created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
       INDEX idx_last_active (last_active)
   );

**Automatic Activity Tracking:**

Chat activity is now tracked automatically whenever ``add_message_to_context()`` is called:

.. code-block:: python

   # Automatic - no LLM intervention required
   asyncio.create_task(update_chat_activity(
       chat_id=extracted_chat_id,
       metadata={
           'username': sender_name,
           'user_id': sender_id,
           'interface_path': interface_path
       }
   ))

**Benefits:**

- **Performance**: Eliminates unnecessary LLM decision-making for basic tracking
- **Consistency**: All interfaces track activity the same way
- **Maintenance**: Single implementation point for activity tracking logic

**Development Plugins** (in ``plugins_dev/`` directory):

* ``event`` – Schedule and deliver reminders. Requires ``DB_HOST``, ``DB_PORT``, ``DB_USER``, ``DB_PASS``, ``DB_NAME`` and optional ``CORRECTOR_RETRIES``.
* ``reddit_plugin`` – Submit posts and comments to Reddit. Requires ``REDDIT_CLIENT_ID``, ``REDDIT_CLIENT_SECRET``, ``REDDIT_USERNAME``, ``REDDIT_PASSWORD`` and ``REDDIT_USER_AGENT``.
* ``selenium_elevenlabs`` – Generate speech audio with ElevenLabs. Set ``ELEVENLABS_EMAIL`` and ``ELEVENLABS_PASSWORD`` (``synth_SELENIUM_HEADLESS`` controls headless mode).
* ``selenium_ttsfree`` – Generate speech audio by automating https://ttsfree.com. No credentials required; set ``synth_SELENIUM_HEADLESS`` to control headless mode. Use action ``voice_message_ttsfree`` with payload: ``{message, language, voice, interface_path}``.
    - Exposed variable: ``Free_TTS_VOICES`` (mapping language -> voice definition). Configure this in the Web UI under Settings → Plugins to map language keys to voice options used by the plugin.
* ``terminal`` – Run shell commands or interactive sessions. Uses ``TELEGRAM_TRAINER_ID`` to authorize access.

Terminal Plugin
---------------

The ``terminal`` plugin provides secure access to shell execution:

- **Single Commands**: Execute one-off shell commands with output capture
- **Persistent Sessions**: Maintain interactive shell sessions for complex workflows
- **Security**: Limited to authorized trainer IDs only
- **Output Handling**: Captures both stdout and stderr with proper encoding

Event Plugin
------------

The ``event`` plugin manages scheduled reminders:

- **Database Storage**: Events stored in MariaDB with timezone support
- **Background Processing**: Asynchronous scheduler checks for due events
- **Flexible Scheduling**: Support for various time formats and recurrence
- **Cross-Platform Delivery**: Events delivered through any active interface

AI Diary Personal Memory
-------------------------

The ``ai_diary`` plugin implements a sophisticated personal memory system:

* Record what synth says to users in conversations
* Store personal thoughts about each interaction
* Track emotions experienced during conversations
* Build relationships and remember users over time

This creates a more human-like memory system compared to traditional technical logging.
The plugin automatically injects recent diary entries into prompts, giving synth
context about past conversations.

.. note::
   For complete usage instructions and API reference, see :doc:`ai_diary_personal_memory`.

The plugin requires database access and automatically creates the necessary tables
on first run. In development, use ``recreate_diary_table.py`` to reset the table
structure.

Plugin Registration System
--------------------------

Plugins are automatically discovered and loaded through the core initializer:

1. **File Discovery**: Core scans ``plugins/`` directory recursively for ``*.py`` files
2. **Import & Inspection**: Each file is imported and checked for ``PLUGIN_CLASS``
3. **Instantiation**: Compatible classes are instantiated (must have parameterless constructors)
4. **Registration**: Plugins register themselves with ``register_plugin()``
5. **Capability Reporting**: Plugins provide action schemas and metadata

This design ensures that plugins are completely decoupled from the core - adding a new plugin requires only placing the file in the correct directory.

Developing Plugins
------------------

Creating a new plugin is straightforward. All plugins should extend ``AIPluginBase`` and follow these patterns:

Action Plugin
~~~~~~~~~~~~~

Action plugins provide executable actions that can be called via JSON. Actions are defined using a structured schema format that optimizes prompt size while maintaining full functionality.

.. code-block:: python

   from core.ai_plugin_base import AIPluginBase
   from core.core_initializer import core_initializer, register_plugin

   class MyActionPlugin(AIPluginBase):
       def __init__(self):
           # Register with the core system
           register_plugin("myplugin", self)
           core_initializer.register_plugin("myplugin")

       @staticmethod
       def get_supported_action_types() -> list[str]:
           """Return action types this plugin handles."""
           return ["my_action"]

       def get_supported_actions(self) -> dict:
           """Return schema for all supported actions using the new optimized format."""
           return {
               "my_action": {
                   "schema": {
                       "type": "object",
                       "properties": {
                           "value": {
                               "type": "string",
                               "description": "The value to process"
                           },
                           "option": {
                               "type": "boolean",
                               "description": "Optional flag"
                           }
                       },
                       "required": ["value"]
                   },
                   "brief": "Execute my custom action with a value",
                   "examples": {
                       "description": "Execute my custom action with a value. The option flag controls additional behavior.",
                       "instructions": {
                           "when_to_use": "Use this action when you need to process a value with optional configuration",
                           "common_pitfalls": ["Ensure value is not empty", "Boolean option defaults to false"]
                       },
                       "examples": [
                           {
                               "scenario": "Basic value processing",
                               "payload": {"value": "hello world"}
                           },
                           {
                               "scenario": "Processing with option enabled",
                               "payload": {"value": "hello world", "option": true}
                           }
                       ]
                   }
               }
           }

       def validate_payload(self, action_type: str, payload: dict) -> list[str]:
           """Validate action payload before execution."""
           errors = []
           if action_type == "my_action" and "value" not in payload:
               errors.append("payload.value is required")
           return errors

       async def handle_custom_action(self, action_type: str, payload: dict):
           """Execute the action logic."""
           if action_type == "my_action":
               # Perform your action here
               result = process_value(payload["value"])
               return result

   # Required: Export the plugin class
   PLUGIN_CLASS = MyActionPlugin

Action Schema Format
~~~~~~~~~~~~~~~~~~~~

.. versionadded:: 1.0
   New optimized action schema format for reduced prompt sizes.

Actions are defined using a structured three-tier format that optimizes LLM prompt size while maintaining full functionality:

**Schema Tier** (JSON Schema)
    Defines the structure, field types, and required fields. Used for validation and LLM prompt generation.

**Brief Tier** (One-line description)
    Concise description of what the action does. Included in LLM prompts for context.

**Examples Tier** (Detailed documentation)
    Comprehensive descriptions, usage instructions, and examples. Used only by the corrector when LLM makes mistakes.

.. code-block:: python

   {
       "my_action": {
           "schema": {
               "type": "object",
               "properties": {
                   "field_name": {
                       "type": "string",  # or "number", "boolean", "array", "object"
                       "description": "What this field does",
                       "enum": ["option1", "option2"]  # optional: restrict to specific values
                   }
               },
               "required": ["field_name"]  # array of required field names
           },
           "brief": "One-line description of what the action does",
           "examples": {
               "description": "Detailed description with usage context and edge cases",
               "instructions": {
                   "when_to_use": "When to use this action",
                   "common_pitfalls": ["Common mistakes to avoid"],
                   "notes": ["Additional important information"]
               },
               "examples": [
                   {
                       "scenario": "Description of use case",
                       "payload": {"field_name": "example_value"}
                   }
               ]
           }
       }
   }

**Field Types Supported:**

- ``"string"`` - Text values
- ``"number"`` - Numeric values (integers/floats)
- ``"boolean"`` - True/false values
- ``"array"`` - Lists of values
- ``"object"`` - Nested objects

**Schema Validation:**

The schema follows JSON Schema standards and is automatically validated. Use ``enum`` to restrict values to specific options:

.. code-block:: python

   "priority": {
       "type": "string",
       "enum": ["low", "medium", "high"],
       "description": "Message priority level"
   }

**Backward Compatibility:**

The old format (``description``, ``required_fields``, ``optional_fields``) is automatically converted to the new format. Existing plugins continue to work without changes.

Plugin Flow
-----------

The plugin system integrates seamlessly with the message chain:

.. graphviz::

    digraph plugin_flow {
         rankdir=LR;
         node [shape=box, style=rounded];
         A [label="1. Plugin auto-discovered\nand instantiated"];
         B [label="2. Plugin registers actions\n→ available_actions"];
         C [label="3. Plugin provides instructions\n→ action_instructions"];
         D [label="4. LLM generates JSON\naction request"];
         E [label="5. Action parser routes\nto plugin"];
         F [label="6. Plugin executes logic\nand returns result"];

         A -> B -> C -> D -> E -> F;
    }

**Step-by-step integration:**

1. **Auto-Discovery**: Core initializer finds and loads plugin from filesystem
2. **Registration**: Plugin registers its supported actions with the system
3. **Instruction Provision**: Plugin provides usage instructions for LLM integration
4. **Action Generation**: LLM creates JSON actions based on available capabilities
5. **Routing**: Action parser matches actions to appropriate plugin handlers
6. **Execution**: Plugin performs the requested operation and returns results

Best Practices
--------------

**Action Schema Design**
    Use the new three-tier action format for optimal prompt efficiency. Keep ``brief`` descriptions under 100 characters.

**Schema Validation**
    Define comprehensive JSON schemas with proper types and constraints. Use ``enum`` for restricted values.

**Brief Descriptions**
    Write concise, actionable descriptions for the ``brief`` field. Focus on what the action does, not how.

**Examples Documentation**
    Provide comprehensive examples in the ``examples`` tier. Include common use cases, edge cases, and error scenarios.

**Security First**
    Always validate inputs and restrict access to authorized users only.

**Error Handling**
    Provide meaningful error messages and handle edge cases gracefully.

**Documentation**
    Include clear descriptions and examples in the ``examples`` tier for corrector assistance.

**Testing**
    Test plugins independently before integration with the full system.

**Performance**
    Consider async operations for I/O-bound tasks to maintain responsiveness.

For examples, examine existing plugins like ``plugins/ai_diary.py`` (new format) or ``plugins/terminal.py`` (legacy format) in the repository.
