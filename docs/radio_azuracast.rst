Radio — AzuraCast Integration
================================

.. versionadded:: 2.0

Overview
--------

The **Radio** subsystem turns Synthetic Heart into an AI radio DJ that can
monitor an AzuraCast station, announce track changes, and inject spoken
commentary between songs via the AzuraCast WebDJ API.

Components:

* **TrackMonitor** — polls AzuraCast ``/api/nowplaying`` and ``/api/queue``,
  detects track changes, crossfade glitches, and end-of-song events.
* **JingleInjector** — generates TTS audio (via the Vox subsystem), converts
  it to WebM with ffmpeg, and broadcasts it through AzuraCast's WebDJ
  websocket.
* **AzuraCastClient** — thin HTTP/WebSocket client for the AzuraCast REST API
  and WebDJ broadcast.

Architecture
------------

.. code-block:: text

   ┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
   │  AzuraCast API  │────▶│   TrackMonitor   │────▶│ RadioHostPlugin │
   │  /nowplaying    │     │  _check_track()  │     │  _on_track_change│
   │  /queue         │     │  _verify_stable()│     │  _on_winding_down│
   └─────────────────┘     └─────────────────┘     └────────┬────────┘
                                                          │
                                                          ▼
                                                ┌──────────────────┐
                                                │  JingleInjector  │
                                                │  generate_tts()  │
                                                │  inject_banter() │
                                                └────────┬─────────┘
                                                          │
                                                          ▼
                                                ┌──────────────────┐
                                                │  AzuraCastClient │
                                                │  broadcast_banter│
                                                │  WebDJ websocket │
                                                └──────────────────┘

Track change flow
-----------------

1. **Polling** — ``TrackMonitor._poll_loop`` calls ``_check_track`` every
   ``RADIO_HOST_POLL_INTERVAL_S`` seconds (default 15 s).

2. **Stabilisation** — AzuraCast can briefly report the wrong track during
   crossfades. ``_verify_track_stable`` sleeps 3 s and re-fetches nowplaying;
   if the metadata settled back to the previous song the change is discarded.

3. **De-announce vs. full announcement** — controlled by
   ``RADIO_HOST_NEXT_SONG_ANNOUNCEMENT``:

   * ``false`` (default) — only the song that just finished is mentioned
     ("Avete ascoltato X"). No reference to the next track.
   * ``true`` — full transition: "Avete ascoltato X, ora Y".

4. **Injection timing** — the primary injection point is ``_on_winding_down``,
   which fires when the current track has ~45 s remaining. It schedules the
   WebDJ broadcast to start exactly at song end so the announcement plays in
   the clean gap between tracks.

5. **Fallback** — if winding-down was skipped (short jingle, bumper, or
   crossfade), ``_inject_at_track_change`` triggers an immediate injection
   from ``_on_track_change``.

Pre-generation
--------------

When ``RADIO_HOST_NEXT_SONG_ANNOUNCEMENT`` is ``true``, the plugin pre-generates
banter for upcoming transitions using the AzuraCast queue:

* ``queue_ahead[0]`` is the **next** track, not the current one.
* Transitions are queued as ``current → next``, ``next → next+1``, etc.
* Pre-generation uses both a fast template (for immediate availability) and
  an LLM-generated variant (richer commentary).

Configuration
-------------

All variables are WebUI-configurable under **Settings → Plugins → Radio Host**.

.. list-table::
   :header-rows: 1
   :widths: 30 10 60

   * - Variable
     - Default
     - Description
   * - ``RADIO_HOST_ENABLED``
     - ``false``
     - Master switch for the radio host plugin.
   * - ``AZURACAST_BASE_URL``
     - ``""``
     - AzuraCast instance URL (e.g. ``https://radio.example.com``).
   * - ``AZURACAST_API_KEY``
     - ``""``
     - AzuraCast API key with station management permissions.
   * - ``AZURACAST_STATION_ID``
     - ``""``
     - Station shortcode from AzuraCast (e.g. ``main``).
   * - ``RADIO_HOST_LANGUAGE``
     - ``English``
     - Language for DJ comments (``English``, ``Italian``, ``Spanish`` …).
   * - ``RADIO_HOST_POLL_INTERVAL_S``
     - ``15``
     - How often to poll AzuraCast for track changes.
   * - ``RADIO_HOST_INTERMISSION``
     - ``1``
     - Number of songs to play before Synth speaks (``1`` = every song).
   * - ``RADIO_HOST_LISTENER_HISTORY``
     - ``5``
     - How many recent listener messages to include in the prompt context.
   * - ``RADIO_HOST_VOX_ENGINE``
     - ``""``
     - Override the default TTS engine for radio host (blank = system default).
   * - ``RADIO_HOST_GAIN_DB``
     - ``4.0``
     - Volume boost for banter audio in dB (applied via ffmpeg ``volume`` filter).
   * - ``RADIO_HOST_NEXT_SONG_ANNOUNCEMENT``
     - ``false``
     - (EXPERIMENTAL) When ``true``, announces the next song ("Avete ascoltato X, ora Y"). When ``false``, only de-announces the song that just finished.
   * - ``AZURACAST_STREAMER_USERNAME``
     - ``SyntH``
     - WebDJ streamer account username used to broadcast banter.
   * - ``AZURACAST_STREAMER_PASSWORD``
     - ``synthradio``
     - WebDJ streamer account password.

WebUI
-----

The plugin registers a **Radio** sub-tab under **History** in the WebUI.

Endpoints:

* ``GET /api/radio/data`` — returns plugin status, station info, and recent
  activity log (up to 50 entries).
* ``GET /api/radio/audio?id=<row_id>`` — serves a banter audio file by DB row.
* ``GET /api/radio/audio?path=<abs_path>`` — serves audio by absolute path
  (restricted to known radio audio directories).

Troubleshooting
---------------

**Volume too low**

* Increase ``RADIO_HOST_GAIN_DB`` (default ``4.0``). The value is passed to
  ffmpeg as ``volume={gain}dB`` and applied during WebM conversion.
* Verify that the AzuraCast streamer account has broadcast permissions; a
  misconfigured WebDJ login can cause silent drops.

**Wrong song announced**

* The off-by-one bug in queue pre-generation has been fixed: ``queue_ahead[0]``
  is now correctly treated as the *next* track, not the current one.
* If AzuraCast metadata glitches during crossfades, the 3-second verification
  delay should discard false transitions.

**WebDJ broadcast fails**

* Check ``AZURACAST_STREAMER_USERNAME`` / ``AZURACAST_STREAMER_PASSWORD`` —
  the account must be enabled for WebDJ in AzuraCast.
* Ensure the station is not already live with another broadcaster; WebDJ
  connections are exclusive.

**No de-announce when ``RADIO_HOST_NEXT_SONG_ANNOUNCEMENT`` is off**

* The plugin still speaks — it uses ``_build_deannounce_template`` which
  produces short "That was X by Y" lines without mentioning the next track.
* If you want complete silence between songs, disable ``RADIO_HOST_ENABLED``
  instead.

Testing
-------

.. code-block:: bash

   uv run pytest tests/plugins/test_radio_host_plugin.py -v

Key test scenarios:

* Track change detection with queue pre-generation.
* De-announce-only injection when ``RADIO_HOST_NEXT_SONG_ANNOUNCEMENT`` is off.
* Gain propagation through ffmpeg conversion.
* Postgres / MariaDB schema dialect selection.