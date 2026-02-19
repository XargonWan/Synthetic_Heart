# Gemini Live API — SyntH Discord Voice Integration

This document describes how Synthetic Heart integrates the Gemini Live API
with Discord voice channels to enable real-time bidirectional voice
conversation with the persona.

## Overview

The integration follows a **Hybrid Voice** architecture: Discord captures and
plays back PCM audio, while the Gemini Live API handles speech recognition,
reasoning, and speech synthesis over a persistent WebSocket session.

```
Discord User
    │
    ▼ (48 kHz stereo PCM)
┌──────────────────────────┐
│  LiveVoiceAudioSink      │ ← discord-ext-voice-recv AudioSink
│  bot/None filter          │   (skips bot & unmapped SSRCs)
│  48 kHz stereo → 16 kHz  │
│  mono (audioop)           │
└──────────┬───────────────┘
           │ 16 kHz mono PCM
           ▼
┌──────────────────────────┐
│  LiveSessionManager      │ ← core/live_session_manager.py
│  WebSocket session        │
│  send_realtime_input()    │
│  receive() loop           │
│  _on_turn_complete()      │
└──────────┬───────────────┘
           │ 24 kHz mono PCM + tool calls + transcripts
           ▼
┌──────────────────────────┐
│  LiveAudioBuffer          │ ← interface/discord_interface.py
│  24 kHz mono → 48 kHz    │
│  stereo (audioop)         │
└──────────┬───────────────┘
           │ 48 kHz stereo PCM
           ▼
┌──────────────────────────┐
│  LivePCMAudioSource       │ ← discord.AudioSource
│  20 ms frames (3840 B)   │
└──────────┬───────────────┘
           │
           ▼
      Discord User
```

After each model turn, `_on_turn_complete` fires and persists both sides of
the conversation across all context subsystems (see
[Context Ingestion](#context-ingestion)).

## Key Files

| File | Purpose |
|------|---------|
| `core/live_session_manager.py` | Session lifecycle, audio I/O, receive loop, transcript accumulation, tool call dispatch, reconnect logic, voice config |
| `llm_engines/gemini_api.py` | `get_live_session_manager()` factory, `start_live_voice_session()` / `stop_live_voice_session()` wrappers |
| `core/prompt_engine.py` | `build_live_system_instruction()` — condensed persona for live sessions |
| `interface/discord_interface.py` | Discord actions, audio pipeline classes, tool-calling bridge, `on_turn_complete` callback, voice state cleanup |
| `core/chat_history_cache.py` | Per-interface and global chat history persistence |
| `plugins/ai_diary.py` | Diary entry persistence |
| `core/synth_core_memory.py` | Semantic memory persistence (`memories` table) |

## Audio Format Details

| Direction | Sample Rate | Channels | Bit Depth | MIME |
|-----------|-------------|----------|-----------|------|
| Discord → Gemini (input) | 16 kHz | Mono | 16-bit signed LE | `audio/pcm;rate=16000` |
| Gemini → Discord (output) | 24 kHz | Mono | 16-bit signed LE | `audio/pcm;rate=24000` |
| Discord voice (native) | 48 kHz | Stereo | 16-bit signed LE | — |

Resampling is performed with `audioop.ratecv()` and `audioop.tostereo()` /
`audioop.tomono()` in the `LiveVoiceAudioSink` (input) and `LiveAudioBuffer`
(output) classes.

## Session Lifecycle

### Starting a Session

Triggered by the `start_live_voice_discord` action (the LLM decides to start
it based on conversation context).

1. **Join voice** — Connect to the Discord voice channel using
   `voice_recv.VoiceRecvClient` (required for receiving user audio).
2. **Build system instruction** — `build_live_system_instruction()` creates a
   condensed persona prompt suitable for the live session's context window.
3. **Build tool declarations** — `_build_gemini_tool_declarations()` queries
   all plugins and interfaces via `get_action_plugin_instructions()` and
   converts each action's payload schema into `genai.types.FunctionDeclaration`
   objects.
4. **Open WebSocket** — `LiveSessionManager.start_session()` opens a
   connection to `gemini-2.5-flash-native-audio-preview-12-2025` with
   `response_modalities=["AUDIO"]`, bidirectional transcription, the configured
   voice, the system instruction, and tool declarations.
5. **Start audio pipeline** — The `LivePCMAudioSource` begins playing buffered
   model audio, and `LiveVoiceAudioSink` begins forwarding user audio.
6. **Initial kick** — A `[Session started. Greet naturally.]` text turn is sent
   so the persona greets without waiting for the user to speak first.

### During a Session

- **User speaks** → `LiveVoiceAudioSink.write()` filters out bot/unmapped
  SSRCs, downsamples, and forwards to `send_realtime_input()`.
- **Model speaks** → `_receive_loop()` dispatches `on_audio` callback →
  `LiveAudioBuffer.write()` upsamples → `LivePCMAudioSource.read()` feeds
  Discord.
- **Model calls a function** → `_receive_loop()` dispatches `on_tool_call` →
  `_handle_live_tool_call()` → `core.action_parser.run_action()` → result
  sent back via `send_tool_response()`.
- **Turn ends** → `_receive_loop()` fires `_on_turn_complete(guild_id,
  user_transcript, model_transcript)` with cleaned, normalised transcripts from
  both sides. See [Context Ingestion](#context-ingestion).

### Stopping a Session

Triggered by:
- `stop_live_voice_discord` action
- Bot kicked or disconnected from voice (`on_voice_state_update`)
- Bot moved to a different channel (`on_voice_state_update`)
- All human users leave the voice channel (`on_voice_state_update`)

Cleanup: cancels the receive task, closes the WebSocket context, stops
Discord audio playback and listening, closes the audio buffer.

### Automatic Reconnection

Sessions have a **15-minute limit** (audio-only). The manager checks
`should_reconnect` on every `send_audio()` call and triggers `_reconnect()`
30 seconds before the limit.

Reconnection:
1. Stops the current session.
2. Rebuilds the system instruction from the current persona state.
3. Re-discovers tool declarations (so function calling persists).
4. Opens a new session.

**Note:** Conversation context is not preserved across reconnections yet.
Future work: inject a conversation summary via `send_client_content()` or
use Google's session resumption feature.

## Audio Feedback Prevention

`LiveVoiceAudioSink.write()` drops packets before processing if:

```python
if user is None or getattr(user, "bot", False):
    return
```

- `user is None` — SSRC not yet mapped to a Discord user. This typically
  includes the bot's own audio stream as reflected by Discord's voice server,
  as well as the first few packets of any participant before mapping completes.
- `user.bot` — Any bot user, including the bot itself if Discord has resolved
  its SSRC.

Without this guard, the bot's spoken audio is echoed back to Gemini, which
hears its own voice and loops on it (most visibly as infinite chuckling).

## Transcription and Transcript Cleaning

Both `input_audio_transcription` and `output_audio_transcription` are enabled
in `LiveConnectConfig` via `types.AudioTranscriptionConfig()`. Fragments arrive
as streaming chunks inside `server_content.input_transcription.text` and
`server_content.output_transcription.text` respectively.

Fragments are processed by `_clean_transcript()` in
`core/live_session_manager.py`:

1. **Concatenate** with `"".join(parts)` — fragments already carry correct
   surrounding whitespace; inserting an extra separator produces double spaces.
2. **Strip `{...}` tags** — persona-engine emotion/state tags
   (e.g. `{emotion neutral  intensity  3.0}`) appear in the model's output
   audio transcription and must be removed before persistence.
3. **Normalise whitespace** — collapse any remaining runs of two or more
   spaces to a single space.

> **Note on ASR quality:** Gemini's speech-to-text occasionally splits words
> at sub-word boundaries (e.g. `wo ndering`). This is a model artifact; the
> cleaning step does not attempt to rejoin split words.

## Speaker Attribution

`LiveVoiceAudioSink` tracks the last human speaker:

```python
self._last_speaker_name = str(
    getattr(user, "display_name", None) or getattr(user, "name", None)
)
self._last_speaker_id = str(getattr(user, "id", ""))
```

`on_turn_complete` reads these from `_live_voice_state[gid]["sink"]` and
passes them to `save_chat_message()` as `sender_name` / `sender_id`, so the
`chat_history_cache` table records the real Discord display name and ID rather
than the placeholder `[voice_user]`.

## Context Ingestion

After each model turn completes, `on_turn_complete` in
`interface/discord_interface.py` persists the conversation to all four context
subsystems.  The `interface_path` key used for voice conversations is
`discord_live_{guild_id}`.

| Subsystem | Table | How |
|-----------|-------|-----|
| `chat_history_cache` | `chat_history_cache` | `save_chat_message()` called for both sides; user entry uses real Discord name/ID from speaker attribution |
| `ai_diary` | `ai_diary` | `add_diary_entry()` called via `run_in_executor` on the configured cadence (`_LIVE_DIARY_EVERY_N_TURNS`, default 1) |
| `synth_core_memory` | `memories` | `silently_record_memory()` called as `asyncio.create_task` when both sides have content; tagged `["voice", "auto"]`, `source="voice"` |
| Grillo introspection | — | Passive — Grillo's beat-based introspection reads `load_global_chat_history()` which queries all `interface_path` values including `discord_live_*` |

The `chat_history_cache` load query uses `ORDER BY timestamp ASC, id ASC` so
that user/model pairs saved within the same second always appear in insertion
order (user before model).

`discord_live_{guild_id}` entries are automatically included when
`HistoryEngine.build_context()` calls `load_global_chat_history()`, so the
full voice conversation history flows into future text-channel interactions and
`build_live_system_instruction()` on reconnect.

## Voice Configuration

The output voice is configurable without code changes via the `LIVE_VOICE_NAME`
config key (`.env` or persona config):

```env
LIVE_VOICE_NAME=Aoede
```

The key is read at `start_session()` time, so changing it and restarting the
voice session (without a full process restart) picks up the new value.

### Available Prebuilt Voices

| Voice | Character |
|-------|-----------|
| **Aoede** | Breezy, natural *(default)* |
| **Puck** | Upbeat, playful |
| **Charon** | Informational, calm |
| **Kore** | Firm, clear |
| **Fenrir** | Excitable |
| **Orbit** | Easy-going |
| **Zephyr** | Bright |
| **Leda** | Youthful |
| **Orus** | Firm, deep |
| **Autonoe** | Bright, warm |

See [Google's documentation](https://ai.google.dev/gemini-api/docs/live) for
the authoritative and up-to-date list.

## Tool / Function Calling

The Live API supports function calling, allowing the persona to execute SyntH
actions (diary entries, emotion updates, sending messages to other interfaces,
etc.) during a voice conversation.

### How It Works

1. **At session start**, `_build_gemini_tool_declarations()` iterates all
   plugins and interfaces that implement `get_prompt_instructions()`.
2. Each action's payload schema is converted to a
   `genai.types.FunctionDeclaration` with:
   - `name` = action name (e.g., `update_diary`, `message_discord_bot`)
   - `description` = from `get_prompt_instructions()["description"]`
   - `parameters` = JSON Schema built from the payload field definitions
3. Declarations are wrapped in a `genai.types.Tool` and passed to
   `LiveConnectConfig.tools`.
4. When the model emits a `tool_call` message, the receive loop calls
   `_handle_live_tool_call()` which:
   - Wraps the call as `{"type": action_name, "payload": args}`
   - Routes it through `core.action_parser.run_action()`
   - Returns the result dict to Gemini via `send_tool_response()`

### Limitations

- Tool declarations are a snapshot at session start. If plugins are
  loaded/unloaded mid-session, the declarations won't update until
  reconnection.
- The Live API may not support all JSON Schema features; complex nested
  schemas may need simplification.

## Voice State Cleanup

The `on_voice_state_update` event handler in `DiscordInterface` monitors
three scenarios:

| Scenario | Action |
|----------|--------|
| Bot disconnected/kicked from voice | `_stop_live_voice()` |
| Bot moved to a different channel | `_stop_live_voice()` |
| All human users leave the bot's channel | `_stop_live_voice()` |

This ensures Live API sessions are never left orphaned.

## Dependencies

| Package | Purpose |
|---------|---------|
| `google-genai` | Google GenAI SDK (WebSocket client, types) |
| `discord.py` | Discord bot framework |
| `discord-ext-voice-recv` | Audio reception from Discord voice channels |

Install all with:
```bash
uv sync
```

## Configuration

| Variable | Description | Required |
|----------|-------------|----------|
| `GEMINI_API_KEY` | Google AI API key | Yes |
| `DISCORD_BOT_TOKEN` | Discord bot token | Yes |
| `LIVE_VOICE_NAME` | Prebuilt voice name (see [Voice Configuration](#voice-configuration)) | No (default: `Aoede`) |
| `CONTEXT_VERBOSITY` / `CHAT_HISTORY` | History depth for `load_chat_history()` | No |

The Live API model is hardcoded as `gemini-2.5-flash-native-audio-preview-12-2025`
in `core/live_session_manager.py:LIVE_MODEL`.

## Troubleshooting

| Symptom | Likely Cause |
|---------|-------------|
| `_HAS_VOICE_RECV is False` | `discord-ext-voice-recv` not installed. Run `uv add discord-ext-voice-recv` |
| `Live session manager unavailable` | `google-genai` not installed or `GEMINI_API_KEY` not set |
| No audio from model | Check `LIVE_OUTPUT_SAMPLE_RATE` matches actual model output; inspect `on_audio_from_model` logs |
| WebSocket disconnects | 15-minute session limit hit; reconnection should fire automatically |
| Tool calls not working | Check `_build_gemini_tool_declarations()` log output for declaration count |
| Bot stays in voice after session ends | `on_voice_state_update` handler should clean up; check for exceptions in logs |
| Infinite audio loop (bot laughs or repeats itself) | Audio feedback — ensure `LiveVoiceAudioSink` bot/None filter is in place |
| `sender_name` shows `[voice_user]` in DB | SSRC not mapped before turn ended; check `voice_recv` version and that the user was speaking before the turn fired |
| Emotion tags in model transcript | Check `_clean_transcript()` regex; tags must be `{...}` delimited |

## Remaining Work

- **Session resumption** — use `send_client_content()` to inject conversation
  summaries on reconnect, or implement Google's `SessionResumptionConfig`.
- **Integration testing** against the real Gemini Live API (audio format
  verification, auth, WebSocket stability under load).
- **Multi-speaker attribution** — currently only the *last* speaker before
  turn-complete is attributed. Mixed-speaker turns lose earlier speaker info.
- **VAD tuning** — Gemini's built-in VAD ends turns on short pauses, splitting
  single utterances into multiple turns. Custom turn detection would require
  buffering audio and implementing silence thresholds on our side.
