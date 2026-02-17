# TASK: Complete Gemini Live API Discord Voice Integration

## Context

Option A (Hybrid Voice) has been implemented as a first pass. The core plumbing is in place:
- `core/live_session_manager.py` — WebSocket session lifecycle, audio streaming, receive loop
- `llm_engines/gemini_api.py` — `get_live_session_manager()`, `start_live_voice_session()`, `stop_live_voice_session()`
- `core/prompt_engine.py` — `build_live_system_instruction()` for condensed persona
- `interface/discord_interface.py` — `start_live_voice_discord` / `stop_live_voice_discord` actions, `LiveAudioBuffer`, `LivePCMAudioSource`, `LiveVoiceAudioSink`

## Remaining Work

### 1. Install `discord-ext-voice-recv`

```bash
uv add discord-ext-voice-recv
```

Without this, `_HAS_VOICE_RECV` is False and `start_live_voice_discord` returns an error. This is the voice receive extension for discord.py that provides `VoiceRecvClient` and `AudioSink`.

### 2. Wire tool/function calling to the SyntH action parser

**Why**: Without this, the persona can talk in voice but can't *do* anything (diary, emotions, bio updates, send messages to other interfaces).

**What to do**:
- In `_start_live_voice()` in `discord_interface.py`, build a list of Gemini function declarations from the SyntH action registry (`get_supported_actions()` from all plugins/interfaces)
- Pass those as the `tools` parameter to `manager.start_session()`
- Register a `_on_tool_call` callback on the manager that routes incoming function calls to `core.action_parser.execute_action()` or equivalent
- The receive loop in `live_session_manager.py` already handles `tool_call` messages and sends responses back — just need to populate the callback

**Key files**:
- `interface/discord_interface.py` — `_start_live_voice()`, wire up `manager.set_tool_call_callback()`
- `core/action_parser.py` — reference for `gather_static_injections()`, `get_supported_actions()`, action schema format
- `core/live_session_manager.py` — `set_tool_call_callback()` already exists

### 3. Integration test against real Gemini Live API

**Why**: Nothing has hit the real WebSocket API yet. Audio format, session setup, and receive loop are all based on docs.

**What to do**:
- Set a valid `GEMINI_API_KEY`
- Join a Discord voice channel
- Trigger `start_live_voice_discord` with the channel ID
- Speak and verify:
  - Audio reaches Gemini (check logs for `send_realtime_input` calls)
  - Model responds with audio (check `on_audio_from_model` callback)
  - Audio plays back through Discord voice
- Check for: auth errors, audio encoding mismatches, WebSocket disconnects, PCM format issues

### 4. Handle audio output sample rate properly

**Why**: The current code assumes Gemini outputs 24kHz mono PCM. This needs verification. The actual output rate depends on the model and may be configurable via `speech_config` in the `LiveConnectConfig`.

**What to do**:
- During integration testing, inspect the `mime_type` of received audio blobs from the model
- If it's not 24kHz, adjust `LIVE_OUTPUT_SAMPLE_RATE` in `live_session_manager.py` and the resampling in `LiveAudioBuffer.write()` in `discord_interface.py`
- Consider adding `speech_config` to the `LiveConnectConfig` setup to explicitly request a voice and output format

### 5. Session reconnection robustness

**Why**: The 15-minute audio session limit means reconnection is guaranteed for any real conversation. Current auto-reconnect in `_reconnect()` rebuilds persona but doesn't preserve conversation context.

**What to do**:
- Before disconnecting, use `send_client_content()` to inject a summary of the conversation so far into the new session
- Consider using Google's session resumption feature (documented but not yet implemented): [Session Management guide](https://ai.google.dev/gemini-api/docs/live-session#session-resumption)
- Add error handling for mid-conversation disconnects (WebSocket drops, network issues)

### 6. Cleanup on Discord disconnect

**Why**: If the bot is kicked from voice or the user disconnects, the Live API session should be cleaned up.

**What to do**:
- Listen for `on_voice_state_update` events in `discord_interface.py`
- If the bot leaves a voice channel (or is moved), call `_stop_live_voice()` for that guild
- If all users leave the voice channel, optionally auto-stop the session

## File Reference

| File | What's there |
|------|-------------|
| `core/live_session_manager.py` | Session lifecycle, audio I/O, receive loop, reconnect |
| `llm_engines/gemini_api.py` | `get_live_session_manager()`, start/stop wrappers |
| `core/prompt_engine.py` | `build_live_system_instruction()` at bottom of file |
| `interface/discord_interface.py` | Actions, audio pipeline classes (bottom of file), `_start_live_voice()`, `_stop_live_voice()` |

## API Reference

- Live API model: `gemini-2.5-flash-native-audio-preview-12-2025`
- Audio input: PCM 16-bit LE, 16kHz, mono
- Audio output: PCM (rate TBD — assumed 24kHz mono, verify during testing)
- Session limit: 15 minutes audio-only, 2 minutes audio+video
- Docs: https://ai.google.dev/gemini-api/docs/live
- SDK: `client.aio.live.connect(model=..., config=LiveConnectConfig(...))`
