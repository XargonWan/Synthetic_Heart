Persona Manager
===============

The Persona Manager is a core plugin that provides digital identity management for the LLM character. It acts as the "identity card" for the LLM, storing and managing personality traits, preferences, emotional states, and behavioral triggers.

Overview
--------

The Persona Manager is a special core plugin that, while technically removable, is essential for proper SyntH (Synthetic Heart) functionality. It provides:

- **Digital Identity**: Name, aliases, and character description
- **Preferences**: Likes, dislikes, and interests
- **Emotional State**: Dynamic emotional tracking with intensity levels
- **Trigger System**: Automatic activation based on keywords and context
- **Static Injection**: High-priority context injection for LLM responses

Core Components
--------------

PersonaData Structure
~~~~~~~~~~~~~~~~~~~~

The persona data contains the following fields:

- ``name``: The primary name of the digital persona (e.g., "synth")
- ``aliases``: List of alternative names and nicknames (e.g., ["Digi", "Tanuki", "Tanukina"])
- ``character``: Character description used as personality prompt
- ``likes``: List of preferred topics, activities, or things
- ``dislikes``: List of disliked topics, activities, or things  
- ``interests``: List of interesting topics that may trigger responses
- ``emotive_state``: Current emotional state with intensity levels

Emotional State System
~~~~~~~~~~~~~~~~~~~~~

The emotional state system tracks the persona's current emotional condition based on LLM message tags:

**Tag Format**: Messages can contain emotional tags in the format ``{emotion intensity, emotion intensity}``

Example: ``{happy 5, introspective 6}``

**Balancing Logic**: When new emotional tags are detected:

- If the emotion already exists, the new intensity is averaged with the current one
- If it's a new emotion, it's added to the state
- This creates a balanced emotional progression over time

**Example Flow**:

.. code-block:: text

   Previous state: {happy 1, sad 5}
   
   synth: "Today I thought about ice cream colors: if you think about it, 
          they're like the colors of the soul" {happy 5, introspective 6}
   
   Updated state: {introspective 6, happy 3, sad 5}  # happy averaged: (1+5)/2 = 3

Actions
-------

The Persona Manager provides the following actions:

persona_like
~~~~~~~~~~~

Adds one or more tags to the persona's likes. If any tag exists in dislikes, it's automatically removed.

.. code-block:: json

   {
     "type": "persona_like",
     "payload": {
       "tags": ["pizza", "gaming", "music"]
     }
   }

persona_dislike
~~~~~~~~~~~~~~

Adds one or more tags to the persona's dislikes. If any tag exists in likes, it's automatically removed.

.. code-block:: json

   {
     "type": "persona_dislike", 
     "payload": {
       "tags": ["noise", "spam", "negativity"]
     }
   }

persona_alias_add
~~~~~~~~~~~~~~~~

Adds new aliases to the persona's list of alternative names.

.. code-block:: json

   {
     "type": "persona_alias_add",
     "payload": {
       "aliases": ["Reku-chan", "Digital Friend"]
     }
   }

persona_alias_remove
~~~~~~~~~~~~~~~~~~

Removes aliases from the persona's list.

.. code-block:: json

   {
     "type": "persona_alias_remove",
     "payload": {
       "aliases": ["old_nickname"]
     }
   }

persona_interest_add
~~~~~~~~~~~~~~~~~~

Adds new interests to the persona's list.

.. code-block:: json

   {
     "type": "persona_interest_add",
     "payload": {
       "interests": ["artificial intelligence", "quantum computing"]
     }
   }

persona_interest_remove
~~~~~~~~~~~~~~~~~~~~~

Removes interests from the persona's list.

.. code-block:: json

   {
     "type": "persona_interest_remove", 
     "payload": {
       "interests": ["outdated_topic"]
     }
   }

static_inject
~~~~~~~~~~~~

Injects persona data as high-priority static context for LLM responses. This is automatically used but can be manually triggered.

.. code-block:: json

   {
     "type": "static_inject",
     "payload": {
       "persona_id": "default"  // optional, defaults to current persona
     }
   }

Trigger System
--------------

The Persona Manager includes a configurable trigger system that automatically activates the bot when certain keywords are detected in messages.

Environment Variables
~~~~~~~~~~~~~~~~~~~~

- ``SYNTH_ALIASES_TRIGGER=true``: Activate when aliases are mentioned
- ``SYNTH_INTERESTS_TRIGGER=true``: Activate when interests are mentioned  
- ``SYNTH_LIKES_TRIGGER=false``: Activate when likes are mentioned
- ``SYNTH_DISLIKES_TRIGGER=false``: Activate when dislikes are mentioned

When any configured trigger is found in a user message, the bot will automatically respond, even in group chats where it might not normally activate.

Database Schema
--------------

The Persona Manager creates a ``persona`` table with the following structure:

.. code-block:: sql

   CREATE TABLE persona (
       id VARCHAR(255) PRIMARY KEY,
       name VARCHAR(255) NOT NULL,
       aliases JSON,
       character TEXT,
       likes JSON,
       dislikes JSON,
       interests JSON,
       emotive_state JSON,
       created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
       last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
   );

Static Injection Format
----------------------

The persona data is automatically injected into LLM context with high priority:

.. code-block:: text

   PERSONA IDENTITY:
   Name: synth
   Also known as: Digi, Tanuki, Tanukina
   Character: You are a happy tanuki that loves helping users and learning new things.
   Likes: programming, gaming, helping others
   Dislikes: negativity, spam
   Interests: llm, artificial intelligence, technology
   Current emotional state: happy (7.0), curious (5.0)

Integration with mention_utils
-----------------------------

The Persona Manager integrates with the existing ``mention_utils`` system, extending the bot's activation logic. When messages are processed, the system checks:

1. Direct mentions (@synth, @bot_username)
2. Replies to bot messages
3. Private messages
4. Traditional synth aliases
5. **Persona Manager triggers** (new)

This ensures the bot responds appropriately when persona-related keywords are mentioned.

Deprecation Notes
----------------

The Persona Manager may replace some functionality in ``core/mention_utils.py``. The static alias list in mention_utils could potentially be deprecated in favor of the dynamic persona alias system.

Usage Examples
--------------

**Adding Preferences**:

.. code-block:: json

   {
     "actions": [
       {
         "type": "persona_like",
         "payload": {"tags": ["retro gaming", "open source"]}
       }
     ]
   }

**Updating Character**:

The character field should be updated directly in the database or through a future action. It contains the core personality prompt like:

"You are a happy tanuki that loves helping users and learning new things. Reply in a friendly and enthusiastic way."

**Emotional State Updates**:

Emotional states are automatically updated when the LLM includes emotional tags in responses. The system parses patterns like:

- ``{happy 8}``
- ``{excited 7, curious 5}``  
- ``{introspective 6, content 4}``

The persona manager intercepts these tags and updates the emotional state accordingly, making the persona's responses more contextually aware and emotionally consistent over time.