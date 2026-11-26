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
     - The adapter contract has about 25 methods, many synchronous; the Minecraft adapter
       alone is about 6,000 lines.
     - A state-mirror connector API: the extension pushes the world state and receives
       Vessel verbs, instead of the Core calling methods in-process.
   * - VRM renderer, format-agnostic Karada (M8c)
     - The Karada payloads (``vrm_model``, ``vrm_animation_v2``, ``vrm_face``,
       ``vrm_expression_*``) are modelled on VRM.
     - Split the logical state (animation, expression, audio, presence) from renderer
       data, then rename the payload types once, with a data-free migration.
   * - Vosk and native Kitten
     - Both are installed and managed from the Manage Models panel.
     - A models API (list, download, remove, report status) callable by an extension.
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

#. Models API (unblocks Vosk and Kitten).
#. Duplex streaming API, then Live, then Discord (text, actions, calls, Live).
#. Vessel connector API, then the adapters one at a time (retroxr, vrchat, skyrim,
   minecraft).
#. Karada logical state, then the VRM renderer.
#. Retire the legacy LLM engines and the old ``plugins/``/``interface/`` scans.
