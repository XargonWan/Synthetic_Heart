Radio — AzuraCast Integration
=============================

.. versionadded:: 2.0

Overview
--------

The **Radio Host** turns Synthetic Heart into an AI radio DJ. It is an extension
(``official/radio_host``, sources in ``<plugins repo>/extensions/radio_host/``) that monitors an
`AzuraCast <https://www.azuracast.com/>`_ station, decides when the synth should speak,
asks the model for a short remark, has the synth's voice say it and streams it into the
station through the WebDJ connection, timed to land in the gap between songs. The same
clip plays on the avatar's stage, so anyone watching sees the synth speak.

It is not installed by default: get it from the official repository (Plugins > Store).
An installation that had the old in-tree plugin switched on gets it fetched, with its
settings, the first time it starts after the update. It needs ``ffmpeg`` on the machine.

Components (all inside the extension)
-------------------------------------

* **TrackMonitor** (``track_monitor.py``) — polls AzuraCast ``/api/nowplaying`` and the
  queue, detects track changes, crossfade glitches and the end of a song.
* **AzuraCastClient** (``azuracast_client.py``) — the REST client (``httpx``) and the WebDJ
  broadcast (``websockets``).
* **JingleInjector** (``jingle.py``) — asks the Core's voice for the clip, converts it for
  the WebDJ with ffmpeg, keeps the replay copy, broadcasts.
* **RadioHost** (``radio.py``) — the rules: when to speak, what to ask, what to do with
  the answer.

How it reaches the Core
-----------------------

The extension never imports the Core. Everything goes through the gateway
(:doc:`plugin_platform/extension_runtime`, "Turns of its own, the synth's voice, and a
Recon question"):

.. code-block:: text

   AzuraCast ──▶ TrackMonitor ──▶ RadioHost ──turns.request──▶ Core (model, full context)
                                      ▲                              │
                                      └── interface.action ◀─────────┘   radio_speak
                                      │
                                      ├──voice.synthesize──▶ Core voice ──▶ a clip of its own
                                      ├──WebDJ──▶ AzuraCast (timed to the gap between songs)
                                      └──voice.play──▶ the stage (lip-sync, for spectators)

* The extension registers one interface, ``radio_host`` (not user facing: nobody is
  chatting), with two actions, ``radio_speak`` (its ``message_action``) and
  ``radio_update_metadata``.
* ``turns.request`` runs the remark as a **background turn** on that interface: scoped to
  ``radio_speak`` (the model is told to use only that), never written to the chat history,
  at priority ``elevated`` (above ordinary chat, below the trainer, calls and people). The
  turn goes through the synth's whole context (persona, emotions, diary, memories) and sees
  as many recent listener messages as ``RADIO_HOST_LISTENER_HISTORY`` says.
* The turn's ``context`` (the transition it was asked for) comes back with the model's
  ``radio_speak`` action, which is how a pre-generated remark is matched to its
  transition.
* A Recon question (``radio_status``) lets the radio add the live status (station, now
  playing, next, listeners, schedule) to the prompt when someone asks about it.

Track change flow
-----------------

1. **Polling** — every ``RADIO_HOST_POLL_INTERVAL_S`` seconds (default 15).
2. **Stabilisation** — AzuraCast can briefly report the wrong track during crossfades;
   after 3 s the monitor re-fetches and discards a change that settled back.
3. **De-announce vs. full announcement** — ``RADIO_HOST_NEXT_SONG_ANNOUNCEMENT``:
   ``false`` (default) mentions only the song that just finished; ``true`` announces the
   next one too (experimental).
4. **Injection timing** — the main injection point is the song's last ~45 seconds: the
   remark is generated, converted, and the WebDJ connects shortly before the song ends so
   the speech lands in the clean gap (after any bumper). A remark that arrives after its
   gap has passed is skipped rather than aired over the wrong song.
5. **Fallback** — when winding down was skipped (a jingle, a bumper), the remark is made at
   the next real song.
6. **No listeners** — with ``RADIO_HOST_ANNOUNCE_IF_NO_LISTENERS`` on, it stays silent
   while nobody listens.

Pre-generation
--------------

With the next-song announcement on, banter for upcoming transitions is requested ahead from
the station's queue (``queue_ahead[0]`` is the *next* track): ``current → next``,
``next → next+1`` and so on, kept (at most eight) until its transition comes.

Settings
--------

Set in the extension's settings (Plugins). Changes apply within about twenty seconds. The
names are the old plugin's, so existing values carry over.

.. list-table::
   :header-rows: 1
   :widths: 34 12 54

   * - Setting
     - Default
     - Description
   * - ``AZURACAST_BASE_URL``
     - empty
     - AzuraCast instance URL (e.g. ``https://radio.example.com``). Required.
   * - ``AZURACAST_API_KEY``
     - empty
     - API key with station management permissions (hidden). Required.
   * - ``AZURACAST_STATION_ID``
     - empty
     - Station shortcode (e.g. ``main``). Required.
   * - ``AZURACAST_STREAMER_USERNAME`` / ``_PASSWORD``
     - ``SyntH`` / ``synthradio``
     - The WebDJ streamer account the remarks are broadcast with; it must exist on the
       station.
   * - ``RADIO_HOST_LANGUAGE``
     - ``English``
     - Language of the remarks.
   * - ``RADIO_HOST_ANNOUNCE_IF_NO_LISTENERS``
     - ``true``
     - Speak only when at least one person listens.
   * - ``RADIO_HOST_POLL_INTERVAL_S``
     - ``15``
     - How often the station is asked what is playing.
   * - ``RADIO_HOST_INTERMISSION``
     - ``1``
     - Songs to play before the synth speaks (``1`` = every song).
   * - ``RADIO_HOST_LISTENER_HISTORY``
     - ``5``
     - Recent listener messages the model sees.
   * - ``RADIO_HOST_VOX_ENGINE``
     - empty
     - Use a different speech engine for the radio (blank: the active one). When
       overriding the engine, also select that engine's voice in the WebUI.
   * - ``RADIO_HOST_GAIN_DB``
     - ``4.0``
     - Volume boost for the remarks, in dB (ffmpeg ``volume`` filter).
   * - ``RADIO_HOST_NEXT_SONG_ANNOUNCEMENT``
     - ``false``
     - (Experimental) also announce the next song.
   * - ``RADIO_HOST_ON_THE_FLY_TIMEOUT_S``
     - ``120``
     - How long to wait for a slow model before skipping a remark.

The old ``RADIO_HOST_ENABLED`` switch is gone: the extension is on or off like any other.

The Radio window
----------------

The extension puts a **Radio** window in the *activity* slot: station status and the last
remarks, each with a replay clip. It is served from the extension's own routes
(``/x/official/radio_host/api/data`` and ``…/api/audio?id=<n>``); a clip is served only by
the id of a remark the extension logged, never by a path. The remarks (the last 40) and
their replay clips (the last 30) live in the extension's own storage and data directory,
not in the Core's database. The old table ``radio_activity_log`` is not carried over.

Troubleshooting
---------------

**Volume too low** — raise ``RADIO_HOST_GAIN_DB``; check that the streamer account is
allowed to broadcast.

**Wrong song announced** — the 3 s verification discards most crossfade glitches; the
remark is also dropped when its song is no longer the one on air.

**WebDJ broadcast fails** — check the streamer username and password (the account must be
enabled for WebDJ) and that nobody else is already live on the station.

**It never speaks** — the extension must be on and granted what it asks for; all three
station settings must be filled in; ffmpeg must be installed; and with
``RADIO_HOST_ANNOUNCE_IF_NO_LISTENERS`` on, someone must be listening. Look for
``[ext:official/radio_host]`` lines in the log.

Testing
-------

.. code-block:: bash

   uv run pytest tests/test_extension_radio_host.py tests/test_extension_radio_host_process.py \
       tests/test_extensions_turns_voice_recon.py tests/test_extensions_core_turns_voice.py -v

The first file carries over the old plugin's scheduling tests (suppressed without
listeners, de-announce only, queue pre-generation, a stale de-announce, the clean gap after
a bumper, gain) and adds the extension's own behaviour. The process test runs the real
extension in the sandbox against a fake station (REST and WebDJ) and follows one remark
from the end of a song to the air, the stage and the window.
