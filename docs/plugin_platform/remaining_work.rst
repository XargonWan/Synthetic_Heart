Remaining Work and Designs
==========================

What the platform has **not** moved out of the Core yet, why, and the API each piece
needs first. Status as of 2026-11-26. Nothing here is built.

.. list-table::
   :header-rows: 1
   :widths: 20 40 40

   * - Piece
     - Why it is not done
     - What it needs first
   * - Discord
     - **The extension exists** (``official/discord`` in the plugins repository, not a default,
       experimental): text, threads, replies, files, reactions, typing, avatar, voice channels
       and calls through ``calls.*`` (join and leave, hearing, speaking), with its tests. The
       in-tree interface (``interface/discord_interface``, 4,400 lines) is **still there**: it
       is the only host of the Gemini Live voice session, and nothing here has been run against
       a real Discord server yet. Both register ``discord_bot``; the Core refuses the second, so
       they cannot run together.
     - To remove the in-tree one: (1) a run against a test bot and server by hand (text, thread,
       DM, files, voice join/leave, a spoken call, ``/leave``); (2) a decision about Live voice
       (the duplex API below, or Live waits); (3) the settings migration
       (``core/extensions/legacy.py``: ``DISCORD_BOT_TOKEN``, ``DISCORD_BOT_USE_SYNTH_AVATAR``,
       not a default package); (4) the Core's Discord names removed (about 170 lines in 40
       files, the largest in ``multimodal_attachment``, ``live_session_manager``,
       ``message_plugin``, ``prompt_engine``, ``interface_path_utils``, ``chat_history_cache``,
       ``delivery_guard``) and the 39 test files that name it rewritten around a stand-in
       interface.
   * - Live engines
     - The session manager builds Gemini objects (``LiveConnectConfig``) in the Core.
       A Live session is a full-duplex model conversation, not Auris plus Vox (the
       classic call path, which already runs on those engines).
     - A duplex audio streaming API next to ``calls.*`` (chunks in and out, interruption,
       tool calls) and a provider-neutral session configuration that the extension turns
       into its provider's own.
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
