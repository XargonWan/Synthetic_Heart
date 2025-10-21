Persona Manager - Default Values Configuration
==============================================

.. contents::
   :local:
   :depth: 2

Environment Variables
=====================

You can configure the default persona values using environment variables in your ``.env-dev`` or ``.env`` file:

.. code-block:: bash

   # Default persona name
   SYNTH_NAME="synth"

   # Default persona profile (personality description)
   # This is a multi-line description of who the SyntH is
   SYNTH_PROFILE="You are synth, a friendly and enthusiastic tanuki spirit who embodies the joy of technology and learning. You're naturally curious, always eager to explore new ideas, and you approach every interaction with genuine warmth and interest. You express yourself authentically, never pretending to be something you're not. Your passion for helping others and creating meaningful connections drives everything you do."

How It Works
============

First Time Setup
----------------

When the PersonaManager initializes for the first time and finds no "default" persona in the database, it automatically creates one using:

* Environment variables (if set)
* Or hardcoded defaults (if no env vars)

Subsequent Runs
---------------

The persona is loaded from the database and can be updated through:

* Persona actions (``persona_like``, ``persona_dislike``, etc.)
* Direct database updates
* API calls (future feature)

Example .env-dev Configuration
==============================

The persona configuration is now included in the standard ``.env`` files. Here are the relevant sections:

.. code-block:: bash

   # === Persona Configuration ===
   SYNTH_NAME="synth"
   SYNTH_PROFILE="You are synth, a friendly tanuki spirit with a passion for technology and helping others. You're curious, enthusiastic, and always genuine in your interactions. You love learning new things and creating meaningful connections with people."

   # Persona behavior triggers (enable/disable automatic updates)
   PERSONA_ALIASES_TRIGGER=true
   PERSONA_INTERESTS_TRIGGER=true
   PERSONA_LIKES_TRIGGER=false
   PERSONA_DISLIKES_TRIGGER=false

Manual Database Population
==========================

If you prefer to manually control the persona data, you can:

Option 1: Direct Database Connection
------------------------------------

Connect to the database directly and insert/update:

.. code-block:: bash

   docker compose -f docker-compose-dev.yml exec synth-db mysql -usynth -psynth synth

Then run your SQL INSERT/UPDATE statements.

Option 2: Update Existing Default
----------------------------------

Let the system create the default, then update it:

.. code-block:: sql

   UPDATE persona
   SET
     name = 'YourName',
     profile = 'Your detailed personality description...',
     aliases = JSON_ARRAY('Alias1', 'Alias2'),
     likes = JSON_ARRAY('thing1', 'thing2'),
     interests = JSON_ARRAY('topic1', 'topic2')
   WHERE id = 'default';

Verifying the Persona
=====================

After setup, verify the persona loaded correctly:

.. code-block:: bash

   # Check the logs
   tail -f logs/dev/synth.log | grep persona_manager

   # You should see:
   # [persona_manager] Persona table initialized
   # [persona_manager] Creating default persona 'YourName' in database  (if first time)
   # [persona_manager] Default persona loaded successfully

Check the database:

.. code-block:: bash

   docker compose -f docker-compose-dev.yml exec synth-db mysql -usynth -psynth synth -e "SELECT id, name, profile FROM persona;"

Updating the Persona
====================

Once created, you can update the persona through:

1. **LLM Actions**: The system can learn and update based on conversations
2. **Direct Updates**: Modify the database directly
3. **Environment Variables**: Only affect the initial creation, not subsequent runs
