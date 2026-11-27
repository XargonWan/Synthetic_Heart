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
     - About 4,400 lines with voice calls and Live inside; the Core still names it in
       ``tts_lipsync``, ``message_plugin`` and the transport-layer labels.
     - Text and actions are expressible today. Voice needs ``calls.*`` and Live needs the
       duplex API below. Remove the Core's Discord names in the same step.
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
