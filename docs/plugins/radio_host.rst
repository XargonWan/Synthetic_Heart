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

    Track A starts playing
           │
           ├─► Pre-generate banter for "A → B"
           │    (enqueued as low-priority message)
           │
           ▼
    Cortex LLM → JSON ``radio_speak``
           │
           ▼
    Banter text stored in ``_pending_banter``
           │
           │   ... time passes, Track A finishes ...
           │
           ▼
    Track B starts playing
           │
           ├─► Inject stored banter "A → B" immediately
           │    (avoids the 30-120s LLM+TTS delay)
           │
           ├─► Pre-generate banter for "B → C"
           │
           ▼
    JingleInjector:
      1. TTS renders banter to WAV
      2. Uploads to AzuraCast ``_banter/`` directory
      3. Queues for immediate playback
         (``POST /api/station/{id}/queue``)
      4. Schedules cleanup after 120s
         (``DELETE /api/station/{id}/files/{id}``)

    +----+----+----+----+
    | Logged to ``radio_activity_log`` |
    +----+----+----+----+

  If AzuraCast does not expose ``playing_next`` metadata for the station,
  the plugin falls back to generating the transition live on the current
  track change. That path has higher latency than the pre-generated flow,
  but it avoids silent no-op behavior on stations where the next song is
  not available through the API.

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
| ``RADIO_HOST_LANGUAGE``    | string   | Language for DJ comments, e.g.       |
|                            |          | ``English``, ``Italian``, ``Spanish``|
|                            |          | (default: ``English``).              |
+----------------------------+----------+--------------------------------------+

The WebDJ streamer account used to broadcast banter is configured with
``AZURACAST_STREAMER_USERNAME`` (default ``SyntH``) and
``AZURACAST_STREAMER_PASSWORD`` (masked, default ``synthradio``); they
must match a streamer account created on the AzuraCast station.

Once the URL, API key, and station ID are filled in, toggle
``RADIO_HOST_ENABLED`` to ``True``. The plugin starts the track monitor
and begins generating transitions on the next song change.

The WebDJ websocket connection follows the base URL's scheme: an
``https://`` AzuraCast instance is reached over ``wss://``, a plain
``http://`` instance over ``ws://``.

When AzuraCast exposes station metadata, the plugin also reads the
station name and current schedule description directly from the
AzuraCast APIs at runtime. Those values are no longer configured
manually in Synth.

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
context, the spoken text, and whether the injection succeeded.

WebUI
-----

When the plugin is loaded, it augments the existing **Activity** page
with a plugin-owned ``Radio`` sub-tab. This keeps the plugin fully
removable: if the Radio Host plugin is disabled or removed, the core
Activity UI goes back to normal with no Radio-specific core changes.

Troubleshooting
---------------

**No transitions are generated.**
    Check that ``RADIO_HOST_ENABLED`` is ``True`` and that all three
    AzuraCast settings (URL, API key, station ID) are filled in
  correctly. Look for ``[radio_host]`` log entries in ``synth.log``
  such as ``RadioHostPlugin initialized``, ``Radio host started``,
  and ``Track monitor started``. If startup stops at configuration,
  the plugin now logs explicit warnings when the base URL, API key,
  or station ID is missing.

**The Radio sub-tab does not appear in Activity.**
  Refresh the WebUI after enabling the plugin, then check
  ``synth.log`` for ``[radio_host] Radio Activity integration registered``
  or warnings about deferred or failed Activity integration.

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

**No banter is heard on air even though logs show success.**
    Check that AzuraCast's AutoDJ is running and not paused. The
    banter is explicitly queued via the station queue API — if a
    live DJ is connected or AutoDJ is suspended, queue items are
    skipped. Monitor the AzuraCast station queue view to verify.

Limitations
-----------

- Only one AzuraCast station is supported per plugin instance.
- The radio host follows the **same voice as normal chat**. It routes
  through the standard Vox flow (``vox.speak()``), so the configured
  voice of the active engine (the ``<ENGINE>_VOICE`` config key, e.g.
  ``FISH-AUDIO_VOICE``) is used automatically — no radio-specific voice
  setting exists.
- ``RADIO_HOST_VOX_ENGINE`` is an optional **engine** override only. It
  is not exposed in the WebUI and defaults to empty, in which case the
  system-wide ``ACTIVE_VOX_ENGINE`` is used. If you set it to a
  different engine, you must also select that engine's voice in the
  WebUI (which persists ``<ENGINE>_VOICE``); otherwise the engine falls
  back to its own default and, for engines like Fish Audio, will pick a
  random voice.
- Audio is injected as a file upload + queue operation, not as a live
  Icecast source connection. Because banter is **pre-generated** during
  the previous song, it is ready the moment a track change fires and
  injected immediately. If the station API does not provide the next
  song, the plugin falls back to generating the comment live when the
  track actually changes.
- Banter files are uploaded to the ``_banter/`` directory (hidden from
  normal playlist rotation), explicitly queued via AzuraCast's AutoDJ
  queue API, and automatically deleted 120 seconds after upload. Under
  normal operation no banter files persist in the media library.
- The ``RADIO_HOST_INTERMISSION`` setting (songs between comments) is
  not exposed in the WebUI; defaults to every track.
