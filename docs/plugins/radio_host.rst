Radio Host Plugin
=================

The Radio Host plugin turns Synth into an AI radio DJ. It monitors an
AzuraCast radio station, detects track changes, and generates spoken
DJ transitions between songs — all using Synth's existing personality,
emotions, memories, and conversation history.

Unlike static DJ tools that produce pre-recorded shows, the Radio Host
plugin operates live: every banter segment is generated through Synth's
full context pipeline (persona, emotion state, diary entries, SOUL
memories, and recent listener interactions) and injected into the
AzuraCast stream as a short jingle between tracks.

How it works
------------

::

    Track change
    (AzuraCast nowplaying API)
           │
           ▼
    TrackMonitor detects change
           │
           ▼
    Synthetic message enqueued
    (via message_queue.enqueue_low_priority)
           │
           ▼
    Full SyntH prompt pipeline:
      • SYNTH_PROFILE / SYNTH_NAME
      • Emotion state (arousal/valence)
      • Recent diary entries
      • SOUL recalled memories
      • Recent listener chat history
      + Radio-specific system instruction
           │
           ▼
    Cortex LLM → JSON action: ``radio_speak``
           │
           ▼
    Vox TTS renders banter to audio
           │
           ▼
    WAV uploaded to AzuraCast → queued between songs
           │
           ▼
    Logged to ``radio_activity_log``

Setup
-----

Prerequisites
~~~~~~~~~~~~~

- An AzuraCast instance (any version with REST API support)
- An AzuraCast API key with **Manage Station Broadcasting** permissions

Obtain an API key from your AzuraCast admin panel at
``Administration → Users → Edit User → API Keys``.

Configuration
~~~~~~~~~~~~~

All configuration is done through the WebUI **Config** tab under the
**Radio Host** section. No files need to be edited.

+----------------------------+----------+--------------------------------------+
| Variable                   | Type     | Description                          |
+============================+==========+======================================+
| ``RADIO_HOST_ENABLED``     | toggle   | Master switch to enable the plugin.  |
+----------------------------+----------+--------------------------------------+
| ``AZURACAST_BASE_URL``     | string   | Your AzuraCast instance URL, e.g.    |
|                            |          | ``https://radio.example.com``.       |
+----------------------------+----------+--------------------------------------+
| ``AZURACAST_API_KEY``      | password | API key with station management      |
|                            | (masked) | permissions.                          |
+----------------------------+----------+--------------------------------------+
| ``AZURACAST_STATION_ID``   | string   | Station shortcode from AzuraCast,    |
|                            |          | visible in the URL when viewing the  |
|                            |          | station admin page.                  |
+----------------------------+----------+--------------------------------------+

Once the URL, API key, and station ID are filled in, toggle
``RADIO_HOST_ENABLED`` to ``True``. The plugin starts the track monitor
and begins generating transitions on the next song change.

Actions
-------

The plugin exposes two actions that the Cortex LLM can invoke:

``radio_speak``
    Generate and inject a DJ comment into the stream.

    **Required fields:**

    - ``text`` — The spoken comment (1–3 sentences). The LLM generates
      this based on the current and previous song information provided in
      the synthetic message context.

    **Optional fields:**

    - ``style`` — Context hint for the comment type. Allowed values:
      ``transition`` (between songs), ``intro``, ``outro``, ``news``,
      ``shoutout``.

    **Prompt instructions** tell the LLM to be natural, reference the
    current mood and recent context, and keep it concise.

``radio_update_metadata``
    Update the now-playing metadata on the AzuraCast stream.

    **Required fields:**

    - ``artist`` — Artist name.
    - ``title`` — Song title.

    **Optional fields:**

    - ``album`` — Album name.

    This action is called by the LLM when it wants to update the visible
    stream metadata to match the current track.

Architecture
------------

Plugin files
~~~~~~~~~~~~

All plugin code lives in ``plugins/radio_host/``, a self-contained
directory with no modifications to any core files:

.. code-block:: text

    plugins/radio_host/
      __init__.py                  # Empty module marker
      radio_host_plugin.py         # Main plugin class, PLUGIN_CLASS,
                                   #   action registration, config listeners
      azuracast_client.py          # REST API client for AzuraCast
      track_monitor.py             # Nowplaying poll loop + change detection
      jingle_injector.py           # TTS generation → upload → queue
      db.py                        # Table initialization

Context pipeline
~~~~~~~~~~~~~~~~

Radio banter is **not** generated in isolation. The plugin creates a
synthetic internal message and enqueues it through the standard
``message_queue.enqueue_low_priority()`` path. This means the LLM
receives the full context stack:

- **Persona** (``SYNTH_PROFILE``, ``SYNTH_NAME``, ``SYNTH_ALIASES``)
- **Emotion state** — current arousal and valence from ``emotion_manager``
- **Diary entries** — recent reflections from ``ai_diary``
- **SOUL memories** — recalled via SOUL static injection
- **Recent listener chat history** — so Synth can reference recent
  conversations on air (controlled by ``RADIO_HOST_LISTENER_HISTORY``)

The only context components that are **skipped** are:

- **Current chat history** — there is no ongoing conversation to continue;
  the radio host is a one-shot monologue per track change
- **Recon LLM call** — to keep latency low, no additional LLM-based memory
  search is performed

This design ensures the radio voice is **Synth's voice**, not a generic
DJ persona — every comment reflects who she is, how she feels, what she
remembers, and what she has discussed with listeners recently.

Database
--------

.. code-block:: sql

    CREATE TABLE IF NOT EXISTS radio_activity_log (
        id INT AUTO_INCREMENT PRIMARY KEY,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        track_title VARCHAR(512),
        track_artist VARCHAR(512),
        banter_text TEXT,
        banter_audio_file VARCHAR(1024),
        style VARCHAR(50),
        status VARCHAR(50) DEFAULT 'injected'
    )

The table logs every generated banter segment including the track
context, the spoken text, the audio file path, and whether the
injection succeeded.

Troubleshooting
---------------

**No transitions are generated.**
    Check that ``RADIO_HOST_ENABLED`` is ``True`` and that all three
    AzuraCast settings (URL, API key, station ID) are filled in
    correctly. Look for ``[radio_host]`` log entries in ``synth.log``.

**Upload succeeds but nothing plays on the stream.**
    The injected jingle file is queued in AzuraCast's AutoDJ. If the
    AutoDJ is paused or a live DJ is connected, queue items may not
    play. Check the AzuraCast station status.

**TTS fails silently.**
    Verify that the active Vox engine is functional by testing TTS
    through the normal chat interface. The Radio Host plugin uses
    whatever engine ``ACTIVE_VOX_ENGINE`` is set to.

**Synth sounds generic / not like herself.**
    This indicates the context pipeline is not being used. Check that
    ``RADIO_HOST_LISTENER_HISTORY`` is at least ``1`` and that
    ``DIARY_HISTORY_DAYS`` is set. The radio host prompt goes through
    the full ``build_prompt_request()`` pipeline — if context
    components are missing, verify they are enabled globally.

**Radio host stops after a user message.**
    The low-priority queue can cancel background tasks when a user
    message arrives. This is by design — user interactions take
    priority. The next track change will re-trigger the host.

Limitations
-----------

- Only one AzuraCast station is supported per plugin instance.
- The ``RADIO_HOST_VOX_ENGINE`` is not exposed in the WebUI; inherits
  the system-wide ``ACTIVE_VOX_ENGINE``. To use a different voice for
  the radio host, set ``ACTIVE_VOX_ENGINE`` before enabling the plugin.
- Audio is injected as a file upload + queue operation, not as a live
  Icecast source connection. There may be a short delay between the
  track change and the banter being audible.
- The ``RADIO_HOST_INTERMISSION`` setting (songs between comments) is
  not exposed in the WebUI; defaults to every track.
