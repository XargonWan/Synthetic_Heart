Remaining Work and Designs
==========================

What the platform has **not** moved out of the Core yet, why, and the API each piece
needs first. Status as of 2026-11-28.

.. list-table::
   :header-rows: 1
   :widths: 20 40 40

   * - Piece
     - Why it is not done
     - What it needs first
   * - Discord
     - **Done in the Core**: the in-tree interface and the Core's Discord names are gone;
       ``official/discord`` (plugins repository, not a default) replaces it, and the settings
       migration carries it over. Nothing has been run against a real Discord server.
     - A run against a test bot and server by hand (text, thread, DM, files, voice
       join/leave, a spoken call, ``/leave``), then Gemini Live voice in a channel (the
       extension announces ``stream=True`` and plays ``call.stream`` / drops it on
       ``call.flush``; tested against fakes only).
   * - Live engines
     - **The API and the engine exist** (``live.*``, ``mode: "live"`` calls,
       ``synth_sdk.live``; see :doc:`extension_runtime`; ``official/gemini_live`` in the
       plugins repository, not a default): the model connection, tool declarations,
       reconnection with session resumption and go-away, manual speech detection. The Core's
       ``LiveCallBridge`` holds the call, and ``memory_search`` and ``chat_history_cache``
       already talk to it. **Nothing has been run against the real Gemini Live API**: the
       engine is tested against a fake connection only. The in-tree manager
       (``core/live_session_manager.py``, 2,000 lines) is still there, with
       ``plugins/live_engines/gemini.py`` and ``engines/live/``: nothing calls it any more.
     - A run by hand against Gemini with a test key (a call that hears, answers, calls a
       tool, is interrupted, survives the ten-minute reconnect), then delete the in-tree
       manager in a commit of its own (see ``CLAUDE.md``: leave the hash where the code
       was). The settings migration is done (``core/extensions/legacy.py``): the extension is
       fetched, with ``GEMINI_API_KEY`` and the ``LIVE_*`` options, only when a Live engine was
       **chosen** (``LIVE_CORTEX``), because the key alone also serves the LLM engines. Not carried over from
       the old manager: syncing the chat into the model while a call runs (only new
       messages are forwarded), deferring context while the *interface* is still playing
       (only while the model is still generating), the avatar's mouth.
   * - Rift Vessel adapters (M8b)
     - The connector API and the Karada API for extensions exist (``provides.vessels``,
       ``karada.subscribe``/``karada.presence``). Reading RetroXR for the move found what is
       still missing: (1) the mod's socket (``/api/vessel/retroxr/ws``) has to become an
       extension WebSocket route (``/x/<id>/ws/...``), so the mod (``retroxr-mod-synth``,
       and its copy in the RetroXR checkout) changes its URL; (2) hearing for the player's recorded
       clips (done: ``voice.transcribe``); (3) the connector keeps her goals
       through the Core's Goals plugin (``plugins.goals.goals``): in an extension they go to its
       own storage, and the goals already stored for RetroXR are not carried over; (4) the
       series it records (``body.busy_seconds``, ``vessel.verb.*``) are read back by
       ``core/vessel_stats.py`` under their bare names, but the gateway prefixes an extension's
       series; (5) the session handling of ``VesselInterface`` (``paused``, ``session_token``,
       the disconnect grace) lives half in the connector: it has to be written down as part of
       the contract. Its 2,450 lines of tests move with it. Minecraft additionally needs the
       goals, bases and quests tables through the storage API, the Node bridge as a subprocess
       of the extension and its 6,000 lines.
   * - VRM renderer, format-agnostic Karada (M8c)
     - The Karada payloads (``vrm_model``, ``vrm_animation_v2``, ``vrm_face``,
       ``vrm_expression_*``) are modelled on VRM.
     - Split the logical state (animation, expression, audio, presence) from renderer
       data, then rename the payload types once, with a data-free migration.
   * - ``engines/external_engines/*``
     - Anthropic, Gemini, OpenAI-style, OpenRouter and xAI engines overlap the endpoint
       presets and are used by Discord.
     - Decide per engine whether the endpoint preset fully replaces it; retire after
       Discord has moved.
   * - OpenAI, Gemini, Anthropic, ``custom`` protocols
     - Deliberately kept in the Core: the prompt and tool renderers and the cortex bridge
       are built around them.
     - A decision from the maintainers, not work.

Order
-----

#. Duplex streaming API, then Live, then Discord (text, actions, calls, Live).
#. Vessel adapters, one at a time (retroxr, vrchat, skyrim, minecraft), on the connector API
   that exists now (``provides.vessels``): they also need a Karada API for extensions (frames,
   presence) and the storage API for goals and bases.
#. Karada logical state, then the VRM renderer.
#. Retire the legacy LLM engines and the old ``plugins/``/``interface/`` scans.
