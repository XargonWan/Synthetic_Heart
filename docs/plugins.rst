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
* ``blocklist`` – User blocking/unblocking functionality (no configuration).
* ``chat_link`` – Cross-platform chat linking and message forwarding.
* ``message_map`` – Message threading and conversation tracking.
* ``message_plugin`` – Send text across registered interfaces (no configuration).
* ``recent_chats`` – Access to recent conversation history.
* ``time_plugin`` – Inject current time and location (no configuration).
* ``weather_plugin`` – Provide weather info as static context. Optional ``WEATHER_FETCH_TIME`` sets refresh interval.

**Development Plugins** (in ``plugins_dev/`` directory):

* ``event`` – Schedule and deliver reminders. Requires ``DB_HOST``, ``DB_PORT``, ``DB_USER``, ``DB_PASS``, ``DB_NAME`` and optional ``CORRECTOR_RETRIES``.
* ``reddit_plugin`` – Submit posts and comments to Reddit. Requires ``REDDIT_CLIENT_ID``, ``REDDIT_CLIENT_SECRET``, ``REDDIT_USERNAME``, ``REDDIT_PASSWORD`` and ``REDDIT_USER_AGENT``.
* ``selenium_elevenlabs`` – Generate speech audio with ElevenLabs. Set ``ELEVENLABS_EMAIL`` and ``ELEVENLABS_PASSWORD`` (``synth_SELENIUM_HEADLESS`` controls headless mode).
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
