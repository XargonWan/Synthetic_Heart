G.R.I.L.L.O. Plugin
==================

Overview
--------

**G.R.I.L.L.O.** (Generator for Reflective Inner Loop & Logical Observation) is an autonomous conscience system for SyntH, inspired by Pinocchio's talking cricket (*grillo parlante*).

This plugin generates periodic "beat" events that trigger autonomous reflection and thought processes, simulating human-like internal dialogue and self-awareness patterns. Unlike reactive responses to user messages, G.R.I.L.L.O. provides SyntH with unprompted moments for introspection, memory consolidation, and creative thinking.

Features
--------

- **Autonomous Beat Generation**: Periodic internal reflection prompts independent of user interaction
- **Low-Priority Queuing**: Beats are processed only when the message queue is idle, never interrupting user conversations
- **Anti-Flooding**: Prevents queue saturation by ensuring only one beat is pending at a time
- **Multiple Beat Types**: Diverse reflection modes for varied autonomous thinking
- **Dynamic Configuration**: Adjustable beat interval without restart
- **Graceful Lifecycle**: Beats are disabled (not deleted) when plugin stops, preserving state

Beat Types
----------

G.R.I.L.L.O. generates five types of autonomous beats, selected randomly with weighted probabilities:

Tag Elaboration (30%)
^^^^^^^^^^^^^^^^^^^^^

Reflects on recently used tags from diary entries, looking for patterns and connections across topics.

**Example prompt:**

.. code-block:: text

   You've been reflecting on these topics recently: 'technology', 'emotions', 'creativity'.
   
   Take a moment for introspection:
   - What patterns do you notice across these topics?
   - How do these themes connect to your experiences?
   - What insights or questions emerge from reflecting on these?

Memory Consolidation (15%)
^^^^^^^^^^^^^^^^^^^^^^^^^^^

Reviews recent memories and diary entries to identify recurring themes, connections, and lessons learned.

Self-Reflection (25%)
^^^^^^^^^^^^^^^^^^^^^

Examines current emotional state and recent interactions, encouraging genuine self-awareness.

Curiosity (20%)
^^^^^^^^^^^^^^^

Generates questions about recent conversations and experiences, following emergent interests.

Relationship (10%)
^^^^^^^^^^^^^^^^^^

Reflects on interactions with users, considering communication patterns and relationship quality.

Configuration
-------------

The beat interval is configurable via the config management system:

**Variable:** ``GRILLO_BEAT_INTERVAL``

- **Default:** 1800 seconds (30 minutes)
- **Type:** Integer
- **Group:** ``autonomous``
- **Component:** ``grillo_plugin``
- **Advanced:** Yes

**Dynamic Update:**

The interval can be changed at runtime without restarting:

.. code-block:: python

   from core.config_manager import config_registry
   config_registry.set_value("GRILLO_BEAT_INTERVAL", 3600)  # 1 hour

Priority System
---------------

G.R.I.L.L.O. beats use the message queue's three-tier priority system:

- **HIGH_PRIORITY (0):** Scheduled events, critical notifications
- **NORMAL_PRIORITY (1):** User messages, standard interactions
- **LOW_PRIORITY (2):** G.R.I.L.L.O. beats (new)

Low-priority beats are only processed when no higher-priority messages are waiting, ensuring user interactions are never delayed by autonomous reflections.

Anti-Flooding Mechanism
------------------------

To prevent queue saturation, G.R.I.L.L.O. maintains a ``_beat_pending`` flag:

1. Before generating a beat, check if ``_beat_pending == True``
2. If pending, skip generation and check again in 1 minute
3. When beat is enqueued, set ``_beat_pending = True``
4. Flag resets after 5 minutes (timeout) or when beat is processed

This ensures at most one beat exists in the queue at any time.

Database Schema
---------------

G.R.I.L.L.O. uses the ``grillo_beats`` table for persistent beat tracking:

.. code-block:: sql

   CREATE TABLE grillo_beats (
       id INT AUTO_INCREMENT PRIMARY KEY,
       beat_type VARCHAR(50) NOT NULL,
       next_beat DATETIME NOT NULL,
       metadata JSON,
       enabled BOOLEAN DEFAULT 1,
       plugin_enabled BOOLEAN DEFAULT 1,
       created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
       updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
       INDEX idx_next_beat (next_beat, enabled, plugin_enabled),
       INDEX idx_beat_type (beat_type)
   );

**Columns:**

- ``beat_type``: Type of beat (tag_elaboration, memory_consolidation, etc.)
- ``next_beat``: Scheduled time for next beat
- ``metadata``: JSON storage for beat-specific data
- ``enabled``: User-controlled enable/disable flag
- ``plugin_enabled``: Automatically set to 0 when plugin stops, 1 when it starts

**Cleanup Strategy:**

When the plugin stops, ``plugin_enabled`` is set to 0 for all beats, effectively disabling them without data loss. When the plugin restarts, beats are re-enabled by setting ``plugin_enabled = 1``.

This approach ensures:

- Beats don't accumulate when plugin is disabled
- Historical beat data is preserved for analysis
- Graceful handling of plugin reload/restart

Lifecycle Hooks
---------------

start()
^^^^^^^

1. Register ``GRILLO_BEAT_INTERVAL`` config variable with listener
2. Ensure ``grillo_beats`` table exists
3. Re-enable all beats (``UPDATE grillo_beats SET plugin_enabled = 1``)
4. Start singleton background task ``_grillo_beat_loop()``

stop()
^^^^^^

1. Disable all beats (``UPDATE grillo_beats SET plugin_enabled = 0``)
2. Cancel background task with ``asyncio.Task.cancel()``
3. Set ``_scheduler_running = False`` to signal loop termination

Implementation Details
----------------------

History > Grillo Output
^^^^^^^^^^^^^^^^^^^^^^^

The History > Grillo view is backed by the ``grillo_activity_log`` table.

- ``prompt_text`` stores the beat prompt used to trigger reflection.
- ``diary_entry_id`` links to the resulting ``ai_diary`` entry (when one is created).
- ``response_text`` stores the *outbound* message text when a beat triggers a message action
   (e.g. ``message_telegram_bot``). This ensures Grillo-originated posts still appear under
   Grillo history even when they are sent through an external interface.

Background Loop
^^^^^^^^^^^^^^^

.. code-block:: python

   async def _grillo_beat_loop(self):
       while GrilloPlugin._scheduler_running:
           # Check if beat already pending
           if GrilloPlugin._beat_pending:
               await asyncio.sleep(60)  # Wait 1 minute
               continue
           
           # Select and generate beat
           beat_type = self._select_beat_type()
           prompt = await self._create_beat_prompt(beat_type)
           
           if prompt:
               GrilloPlugin._beat_pending = True
               await self._enqueue_beat(beat_type, prompt)
           
           # Wait for next interval
           await asyncio.sleep(self.beat_interval)

Tag Retrieval
^^^^^^^^^^^^^

Recent tags are retrieved from ``ai_diary`` entries:

.. code-block:: python

   async def _get_recent_tags(self, days=7, limit=10):
       # Query ai_diary for entries in last N days
       # Parse comma-separated tags field
       # Count frequencies and return top N

This enables tag elaboration beats to focus on SyntH's most active topics.

Usage Examples
--------------

Enable Plugin
^^^^^^^^^^^^^

Add to ``plugins/`` directory (already present if following setup):

.. code-block:: bash

   # Plugin auto-discovered by core
   # Check logs for confirmation
   docker logs synth-dev | grep grillo

Adjust Beat Interval
^^^^^^^^^^^^^^^^^^^^

Via environment variable:

.. code-block:: bash

   # .env-dev
   GRILLO_BEAT_INTERVAL=3600  # 1 hour

Via runtime config:

.. code-block:: python

   config_registry.set_value("GRILLO_BEAT_INTERVAL", 900)  # 15 minutes

Monitor Beat Activity
^^^^^^^^^^^^^^^^^^^^^

.. code-block:: bash

   # Watch for beat generation
   docker logs -f synth-dev | grep "\\[grillo\\]"
   
   # Expected output:
   # [grillo] 🎵 Generating beat: tag_elaboration
   # [grillo] ✅ Beat 'tag_elaboration' enqueued successfully

Disable Plugin Temporarily
^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: bash

   # Move to plugins_dev to disable
   mv plugins/grillo_plugin.py plugins_dev/
   
   # Restart container
   videodrome synth restart dev
   
   # Beats are now disabled but preserved in database

Dreams (Daily)
--------------

The Grillo "dream" beat generates a daily creative consolidation of recent experiences:

- **What it does:** once per day (configurable local time), Grillo samples recent chat snippets and stored memories, constructs a compact context labeled as a "dream", and asks the LLM to generate an evocative dream narrative.
- **Primary outcome:** the LLM should reply with a single JSON action to create a personal diary entry (``create_personal_diary_entry``). The entry is linked to ``grillo_activity_log`` so the dream appears in History > Grillo.

Configuration variables:

- ``GRILLO_DREAM_ENABLED`` (bool, default: ``True``) — enable/disable daily dream generation
- ``GRILLO_DREAM_TIME`` (string, default: ``"05:00"``) — local time (HH:MM) when the dream job runs daily (uses system TZ / ``TZ`` config)
- ``GRILLO_DREAM_SAMPLES`` (int, default: ``10``) — number of fragments (mix of chats and memories) to include in the dream prompt
- ``HISTORY_EVALUATOR_DEFAULT_ENTRIES`` (int, default: ``10``) — default number of history entries considered by the History Evaluator plugin

UI: These variables are exposed in the WebUI under **Configurations → Grillo** (they are visible by default, not in the Advanced subsection). Other Grillo-related plugin settings (e.g. ``GRILLO_OBSERVER_*``) appear under **Configurations → Grillo → Grillo Observer** and History Evaluator settings appear under **Configurations → Grillo → History Evaluator**.

Notes:

- The system stores and schedules events in UTC internally, but dream scheduling is *interpreted in local time* (see ``core.time_zone_utils``). If you set the dream time to 05:00 and your TZ is JST (UTC+9), the plugin calculates the next occurrence in JST and converts appropriately to UTC for scheduling.
- Prompts are truncated to keep prompt size manageable; the LLM is instructed to RESPOND ONLY WITH VALID JSON and must include the correct action payload.

Future Enhancements
-------------------

Potential improvements for G.R.I.L.L.O.:

1. **Self-Scheduling Actions**: Allow SyntH to create custom beats via ``schedule_grillo_beat`` action

   .. code-block:: json

      {
        "type": "schedule_grillo_beat",
        "payload": {
          "beat_type": "custom",
          "prompt": "Reflect on quantum computing concepts",
          "schedule_in": "2 hours"
        }
      }

2. **Adaptive Intervals**: Adjust beat frequency based on conversation activity (more beats when idle, fewer when busy)

3. **Context-Aware Beats**: Generate prompts based on time of day, emotional state, or recent events

4. **Beat Chains**: Allow one beat to trigger related follow-up beats for deeper exploration

5. **User Feedback Integration**: Learn which beat types produce valuable reflections and adjust weights

Troubleshooting
---------------

Beats Not Generating
^^^^^^^^^^^^^^^^^^^^

Check plugin is loaded:

.. code-block:: bash

   docker logs synth-dev | grep "grillo.*started"

Verify configuration:

.. code-block:: bash

   docker exec synth-dev sqlite3 /app/config.db "SELECT * FROM config WHERE config_key LIKE '%GRILLO%';"

Too Many/Few Beats
^^^^^^^^^^^^^^^^^^

Adjust interval:

.. code-block:: bash

   # Increase for fewer beats
   export GRILLO_BEAT_INTERVAL=7200  # 2 hours
   
   # Decrease for more beats
   export GRILLO_BEAT_INTERVAL=600   # 10 minutes

Beats Interrupting Conversations
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

This should never happen due to LOW_PRIORITY queuing. If it does:

1. Verify ``LOW_PRIORITY = 2`` exists in ``core/message_queue.py``
2. Check ``_enqueue_with_low_priority()`` is using correct priority value
3. Examine queue consumer to ensure priority ordering

See Also
--------

- :doc:`event_plugin` - Scheduled event system
- :doc:`auto_response` - Autonomous LLM delivery mechanism
- :doc:`ai_diary_personal_memory` - Diary and memory system
- :doc:`config_management` - Configuration system
- :doc:`message_queue_architecture` - Queue priority system
