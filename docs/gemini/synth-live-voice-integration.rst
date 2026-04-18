Gemini Live API — SyntH Discord Voice Integration
=================================================

This document describes how Synthetic Heart integrates the Gemini Live API
with Discord voice channels to enable real-time bidirectional voice
conversation with the persona.

Overview
--------

The integration follows a **Hybrid Voice** architecture: Discord captures and
plays back PCM audio, while the Gemini Live API handles speech recognition,
reasoning, and speech synthesis over a persistent WebSocket session.

.. code-block:: text

   Discord User
       │
       ▼ (48 kHz stereo PCM)
   ┌──────────────────────────┐
   │  LiveVoiceAudioSink      │ ← discord-ext-voice-recv AudioSink
   │  48 kHz stereo → 16 kHz  │
   │  mono (audioop)          │
   └──────────┬───────────────┘
              │ 16 kHz mono PCM
              ▼
   ┌──────────────────────────┐
   │  LiveSessionManager      │ ← core/live_session_manager.py
   │  WebSocket session       │
   │  send_realtime_input()   │
   │  receive() loop          │
   └──────────┬───────────────┘
              │ 24 kHz mono PCM + tool calls
              ▼
   ┌──────────────────────────┐
   │  LiveAudioBuffer         │ ← interface/discord_interface.py
   │  24 kHz mono → 48 kHz    │
   │  stereo (audioop)        │
   └──────────┬───────────────┘
              │ 48 kHz stereo PCM
              ▼
   ┌──────────────────────────┐
   │  LivePCMAudioSource      │ ← discord.AudioSource
   │  20 ms frames (3840 B)   │
   └──────────┬───────────────┘
              │
              ▼
         Discord User

Key files
---------

.. list-table:: Key files
   :header-rows: 1
   :widths: 20 80

   * - File
     - Purpose
   * - ``core/live_session_manager.py``
     - Session lifecycle, audio I/O, receive loop, tool call dispatch, reconnect logic
   * - ``engines/external_engines/gemini_api.py``
     - ``get_live_session_manager()`` factory, ``start_live_voice_session()`` / ``stop_live_voice_session()`` wrappers
   * - ``core/prompt_engine.py``
     - ``build_live_system_instruction()`` — condensed persona for live sessions
   * - ``interface/discord_interface.py``
     - Discord actions, audio pipeline classes, tool-calling bridge, voice state cleanup

Audio format details
--------------------

- **Discord → Gemini (input)** — 16 kHz; Mono; 16-bit signed LE; MIME: ``audio/pcm;rate=16000``
- **Gemini → Discord (output)** — 24 kHz; Mono; 16-bit signed LE; MIME: ``audio/pcm;rate=24000``
- **Discord voice (native)** — 48 kHz; Stereo; 16-bit signed LE

Resampling is performed with ``audioop.ratecv()``, ``audioop.tostereo()`` and
``audioop.tomono()`` in the ``LiveVoiceAudioSink`` (input) and
``LiveAudioBuffer`` (output) classes.

Resampling is performed with ``audioop.ratecv()``, ``audioop.tostereo()`` and
``audioop.tomono()`` in the ``LiveVoiceAudioSink`` (input) and
``LiveAudioBuffer`` (output) classes.

Session lifecycle
-----------------

Starting a session
~~~~~~~~~~~~~~~~~~

Triggered by the ``start_live_voice_discord`` action (the model may decide to
start it based on conversation context).

1. **Join voice** — connect to the Discord voice channel using
   ``voice_recv.VoiceRecvClient`` (required for receiving user audio).
2. **Build system instruction** — ``build_live_system_instruction()`` creates a
   condensed persona prompt suitable for the live session's context window.
3. **Build tool declarations** — ``_build_gemini_tool_declarations()`` queries
   all plugins and interfaces via ``get_action_plugin_instructions()`` and
   converts each action's payload schema into ``genai.types.FunctionDeclaration``
   objects.
4. **Open WebSocket** — ``LiveSessionManager.start_session()`` opens a
   connection to the Live model with ``response_modalities=["AUDIO"]``, the
   system instruction, and tool declarations.
5. **Start audio pipeline** — the ``LivePCMAudioSource`` begins playing
   buffered model audio, and ``LiveVoiceAudioSink`` begins forwarding user
   audio.

During a session
~~~~~~~~~~~~~~~~~

- **User speaks** → ``LiveVoiceAudioSink.write()`` downsamples and forwards to
  ``send_realtime_input()``.
- **Model speaks** → ``_receive_loop()`` dispatches ``on_audio`` callback →
  ``LiveAudioBuffer.write()`` upsamples → ``LivePCMAudioSource.read()`` feeds
  Discord.
- **Model calls a function** → ``_receive_loop()`` dispatches ``on_tool_call`` →
  ``_handle_live_tool_call()`` → ``core.action_parser.run_action()`` → result
  sent back via ``send_tool_response()``.

Stopping a session
~~~~~~~~~~~~~~~~~~

Triggered by:

- ``stop_live_voice_discord`` action
- Bot kicked or disconnected from voice (``on_voice_state_update``)
- Bot moved to a different channel (``on_voice_state_update``)
- All human users leave the voice channel (``on_voice_state_update``)

Cleanup cancels the receive task, closes the WebSocket context, stops Discord
audio playback and listening, and closes the audio buffer.

Automatic reconnection
~~~~~~~~~~~~~~~~~~~~~~

Sessions have a **15-minute limit** (audio-only). The manager checks
``should_reconnect`` on every ``send_audio()`` call and triggers ``_reconnect()``
30 seconds before the limit.

Reconnection steps:

1. Stop the current session.
2. Rebuild the system instruction from the current persona state.
3. Re-discover tool declarations (so function calling persists).
4. Open a new session.

.. note::

   Conversation context is not preserved across reconnections yet. Future work
   could inject a conversation summary via ``send_client_content()`` or use the
   Live API session resumption feature.

Tool / function calling
-----------------------

The Live API supports function calling, allowing the persona to execute SyntH
actions (diary entries, emotion updates, sending messages to other
interfaces, etc.) during a voice conversation.

How it works
~~~~~~~~~~~~

1. **At session start**, ``_build_gemini_tool_declarations()`` iterates all
   plugins and interfaces that implement ``get_prompt_instructions()``.
2. Each action's payload schema is converted to a ``genai.types.FunctionDeclaration``
   with:
   - ``name`` = action name (e.g., ``update_diary``, ``message_discord_bot``)
   - ``description`` = from ``get_prompt_instructions()["description"]``
   - ``parameters`` = JSON Schema built from the payload field definitions
3. Declarations are wrapped in a ``genai.types.Tool`` and passed to
   ``LiveConnectConfig.tools``.
4. When the model emits a ``tool_call`` message, the receive loop calls
   ``_handle_live_tool_call()`` which:
   - Wraps the call as ``{"type": action_name, "payload": args}``
   - Routes it through ``core.action_parser.run_action()``
   - Returns the result dict to Gemini via ``send_tool_response()``

Limitations
~~~~~~~~~~~

- Tool declarations are a snapshot at session start. If plugins are
  loaded/unloaded mid-session, the declarations won't update until
  reconnection.
- The Live API may not support all JSON Schema features; complex nested
  schemas may need simplification.

Voice state cleanup
-------------------

The ``on_voice_state_update`` event handler in ``DiscordInterface`` monitors
three scenarios:

- Bot disconnected/kicked from voice — ``_stop_live_voice()``
- Bot moved to a different channel — ``_stop_live_voice()``
- All human users leave the bot's channel — ``_stop_live_voice()``

This ensures Live API sessions are never left orphaned.

Dependencies
------------

- ``google-genai`` — Google GenAI SDK (WebSocket client, types)
- ``discord.py`` — Discord bot framework
- ``discord-ext-voice-recv`` — Audio reception from Discord voice channels

Install with::

   uv sync

Configuration
-------------

- ``GEMINI_API_KEY`` — Google AI API key (required)
- ``DISCORD_BOT_TOKEN`` — Discord bot token (required)

The Live API model is hardcoded as ``gemini-2.5-flash-native-audio-preview-12-2025``
in ``core/live_session_manager.py:LIVE_MODEL``.

Troubleshooting
---------------

- ``_HAS_VOICE_RECV is False`` — ``discord-ext-voice-recv`` not installed. Run ``uv add discord-ext-voice-recv``
- ``Live session manager unavailable`` — ``google-genai`` not installed or ``GEMINI_API_KEY`` not set
- No audio from model — Check ``LIVE_OUTPUT_SAMPLE_RATE`` matches actual model output; inspect ``on_audio_from_model`` logs
- WebSocket disconnects — 15-minute session limit hit; reconnection should fire automatically
- Tool calls not working — Check ``_build_gemini_tool_declarations()`` log output for declaration count
- Bot stays in voice after session ends — ``on_voice_state_update`` handler should clean up; check for exceptions in logs

Remaining work
--------------

- **Integration testing** against the real Gemini Live API (audio format
  verification, auth, WebSocket stability).
- **Audio output sample rate verification** — the assumed 24 kHz may differ;
  inspect ``mime_type`` of received audio blobs during testing.
- **Session resumption** — use ``send_client_content()`` to inject conversation
  summaries on reconnect, or implement Google's ``SessionResumptionConfig``.
- **Speech config** — add ``speech_config`` to ``LiveConnectConfig`` to
  explicitly request a voice and output format.
