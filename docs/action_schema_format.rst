Action Schema Format
====================

.. versionadded:: 1.0
   New optimized action schema format for reduced prompt sizes and improved LLM efficiency.

Overview
--------

Synthetic Heart uses a structured three-tier action schema format designed to optimize LLM prompt size while maintaining full functionality and comprehensive documentation. This format separates concerns between prompt efficiency, validation, and detailed guidance.

The three tiers work together:

1. **Schema Tier**: JSON Schema for structure and validation
2. **Brief Tier**: Concise description for LLM prompts
3. **Examples Tier**: Comprehensive documentation for error correction

Why Three Tiers?
----------------

**Problem**: Traditional action descriptions included everything (schema, examples, instructions) in LLM prompts, leading to excessive token usage and potential context limits.

**Solution**: Separate what the LLM needs for generation (schema + brief) from what it needs for error correction (full examples).

.. code-block:: none

   LLM Prompt → Schema + Brief (minimal, efficient)
   LLM Error → Corrector uses Schema + Brief + Examples (comprehensive)

Schema Tier
-----------

The schema tier defines the action's structure using JSON Schema standards. This is used for:

- **Validation**: Automatic payload validation before execution
- **LLM Guidance**: Provides structure information in prompts
- **Type Safety**: Ensures correct data types and required fields

.. code-block:: python

   "schema": {
       "type": "object",
       "properties": {
           "message": {
               "type": "string",
               "description": "The message content to send"
           },
           "priority": {
               "type": "string",
               "enum": ["low", "normal", "high"],
               "description": "Message priority level"
           },
           "interface_path": {
               "type": "string",
               "description": "Hierarchical interface path (e.g., 'telegram_bot/chat_id/thread_id')"
           }
       },
       "required": ["message"]
   }

**Supported Field Types:**

+------------------+--------------------------------------------------+
| Type             | Description                                      |
+==================+==================================================+
| ``"string"``     | Text values                                      |
+------------------+--------------------------------------------------+
| ``"number"``     | Integer or floating point numbers                |
+------------------+--------------------------------------------------+
| ``"boolean"``    | True/false values                                |
+------------------+--------------------------------------------------+
| ``"array"``      | Lists or arrays of values                        |
+------------------+--------------------------------------------------+
| ``"object"``     | Nested objects with their own properties         |
+------------------+--------------------------------------------------+

**Schema Validation Rules:**

- ``required``: Array of field names that must be present
- ``enum``: Restricts values to specific options
- ``description``: Human-readable explanation of the field
- ``type``: JSON Schema type validation

Brief Tier
----------

The brief tier provides a concise, one-line description of what the action does. This appears in LLM prompts and should be:

- **Under 100 characters**
- **Action-oriented** (what it does, not how)
- **Clear and specific**

.. code-block:: python

   "brief": "Send a message to a Discord channel"

**Good Examples:**

.. code-block:: python

   "brief": "Schedule a reminder for future delivery"
   "brief": "Add an entry to the personal diary"
   "brief": "Retrieve weather information for a location"

**Poor Examples:**

.. code-block:: python

   "brief": "This action allows you to send messages through the Discord interface by providing message content and optionally specifying priority and recipient"  # Too long
   "brief": "Message sender"  # Too vague

Examples Tier
-------------

The examples tier contains comprehensive documentation used only when the LLM makes mistakes. This includes:

- **Detailed descriptions** with context and edge cases
- **Usage instructions** and best practices
- **Multiple examples** covering different scenarios
- **Common pitfalls** and error scenarios

.. code-block:: python

   "examples": {
       "description": "Send a message through Discord with optional priority and recipient targeting. Messages are delivered asynchronously and support rich text formatting.",
       "instructions": {
           "when_to_use": "Use this action to communicate with users through Discord channels or direct messages.",
           "common_pitfalls": [
               "Ensure recipient exists and bot has permission to message them",
               "High priority messages may be rate-limited",
               "Empty messages will be rejected"
           ],
           "notes": [
               "Messages support Discord markdown formatting",
               "Bot must be in the target server/channel",
               "Rate limits apply based on bot permissions"
           ]
       },
       "examples": [
           {
               "scenario": "Send a simple message to a channel",
               "payload": {
                   "message": "Hello everyone!"
               }
           },
           {
               "scenario": "Send high-priority message to specific user",
               "payload": {
                   "message": "Urgent: System maintenance in 5 minutes",
                   "priority": "high",
                   "interface_path": "discord_interface/guild123/channel456"
               }
           },
           {
               "scenario": "Send formatted message with priority",
               "payload": {
                   "message": "**Important Update:** New features available",
                   "priority": "normal"
               }
           }
       ]
   }

Complete Action Definition
--------------------------

Here's a complete action definition combining all three tiers:

.. code-block:: python

   def get_supported_actions(self) -> dict:
       return {
           "message_discord": {
               "schema": {
                   "type": "object",
                   "properties": {
                       "message": {
                           "type": "string",
                           "description": "The message content to send"
                       },
                       "priority": {
                           "type": "string",
                           "enum": ["low", "normal", "high"],
                           "description": "Message priority level"
                       },
                       "recipient": {
                           "type": "string",
                           "description": "Target recipient (@user or #channel)"
                       }
                   },
                   "required": ["message"]
               },
               "brief": "Send a message to a Discord channel",
               "examples": {
                   "description": "Send a message through Discord with optional priority and recipient targeting.",
                   "instructions": {
                       "when_to_use": "Use to communicate with Discord users and channels.",
                       "common_pitfalls": [
                           "Bot must have send permissions in target channel",
                           "Rate limits may delay high-priority messages"
                       ]
                   },
                   "examples": [
                       {
                           "scenario": "Basic channel message",
                           "payload": {"message": "Hello world!"}
                       },
                       {
                           "scenario": "Priority message to user",
                           "payload": {
                               "message": "Meeting in 10 minutes",
                               "priority": "high",
                               "interface_path": "discord_interface/guild123/user456"
                           }
                       }
                   ]
               }
           }
       }

Migration Guide
---------------

**From Old Format:**

.. code-block:: python

   # Old format (still supported)
   "my_action": {
       "description": "Do something with a value",
       "required_fields": ["value"],
       "optional_fields": ["option"],
       "instructions": {...}
   }

**To New Format:**

.. code-block:: python

   # New optimized format
   "my_action": {
       "schema": {
           "type": "object",
           "properties": {
               "value": {"type": "string", "description": "The value to process"},
               "option": {"type": "boolean", "description": "Optional flag"}
           },
           "required": ["value"]
       },
       "brief": "Do something with a value",
       "examples": {
           "description": "Do something with a value and optional configuration",
           "instructions": {...},
           "examples": [...]
       }
   }

**Automatic Conversion:**

The system automatically converts old format actions to new format. No immediate migration required, but new plugins should use the new format for optimal performance.

Best Practices
--------------

**Schema Design:**

- Use appropriate JSON Schema types for all fields
- Include ``enum`` constraints where applicable
- Provide clear, concise field descriptions
- Mark truly required fields in the ``required`` array

**Brief Descriptions:**

- Keep under 100 characters
- Start with action verb (Send, Get, Create, etc.)
- Focus on outcome, not implementation
- Be specific about the action's purpose

**Examples Documentation:**

- Cover primary use cases first
- Include edge cases and error scenarios
- Document rate limits, permissions, and constraints
- Provide realistic payload examples

**Performance Considerations:**

- The ``brief`` field appears in every LLM prompt - keep it minimal
- The ``examples`` tier is only loaded during error correction
- Well-designed schemas reduce validation errors and corrector usage

**Validation:**

- Test schemas with various payloads
- Ensure ``required`` fields are actually required for the action
- Validate enum values are appropriate and complete

For implementation examples, see ``plugins/ai_diary.py`` (new format) or ``plugins/terminal.py`` (legacy format automatically converted).