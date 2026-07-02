# Grok Voice Agent API

Build powerful real-time voice applications with the Grok Voice Agent API. Create interactive voice conversations with Grok models via WebSocket.

**WebSocket Endpoint:** `wss://api.x.ai/v1/realtime`

> **Note:** The Voice Agent API is only available in the `us-east-1` region.

## Overview

The Grok Voice Agent API enables:
- Voice assistants for web and mobile
- Phone agents with Twilio integration
- Interactive voice applications
- Customer support automation
- Real-time voice conversations
- AI-powered phone systems (IVR)

Optimized for enterprise use cases across Customer Support, Medical, Legal, Finance, Insurance, and more.

## Key Features

### Real-time Performance
- Optimized for minimal response times
- Natural back-and-forth dialogue without awkward pauses
- Streams audio bidirectionally over WebSocket
- Instant voice-to-voice interactions that feel like talking to a human

### Multilingual Support
- Speaks over **100 languages** with native-quality accents
- Automatically detects input language
- Responds naturally in the same language
- No configuration required for language switching

### Supported Languages
English, Spanish, French, German, Italian, Portuguese, Dutch, Russian, Chinese (Mandarin), Japanese, Korean, Arabic, Hindi, Turkish, Polish, Swedish, Danish, Norwegian, Finnish, Czech, and many more.

Each language features natural pronunciation, appropriate intonation patterns, and culturally-aware speech rhythms.

## Voice Options

Choose from 5 distinct voices, each with unique characteristics:

| Voice | Type | Tone | Description |
|-------|------|------|-------------|
| **Ara** | Female | Warm, friendly | Default voice, balanced and conversational |
| **Rex** | Male | Confident, clear | Professional and articulate, ideal for business |
| **Sal** | Neutral | Smooth, balanced | Versatile voice suitable for various contexts |
| **Eve** | Female | Energetic, upbeat | Engaging and enthusiastic, great for interactive experiences |
| **Leo** | Male | Authoritative, strong | Decisive and commanding, suitable for instructional content |

### Selecting a Voice

```python
session_config = {
    "type": "session.update",
    "session": {
        "voice": "Ara",  # Choose from: Ara, Rex, Sal, Eve, Leo
        "instructions": "You are a helpful assistant."
    }
}
await ws.send(json.dumps(session_config))
```

## Audio Formats

### Supported Format Types

| Format | Encoding | Container Types | Sample Rate |
|--------|----------|-----------------|-------------|
| `audio/pcm` | Linear16, Little-endian | Raw, WAV, AIFF | Configurable |
| `audio/pcmu` | G.711 μ-law (Mulaw) | Raw | 8000 Hz |
| `audio/pcma` | G.711 A-law | Raw | 8000 Hz |

### Supported Sample Rates (PCM only)

| Sample Rate | Quality | Description |
|-------------|---------|-------------|
| 8000 Hz | Telephone | Narrowband, suitable for voice calls |
| 16000 Hz | Wideband | Good for speech recognition |
| 21050 Hz | Standard | Balanced quality and bandwidth |
| 24000 Hz | High (Default) | Recommended for most use cases |
| 32000 Hz | Very High | Enhanced audio clarity |
| 44100 Hz | CD Quality | Standard for music/media |
| 48000 Hz | Professional | Studio-grade audio / Web Browser |

### Audio Specifications

| Property | Value | Description |
|----------|-------|-------------|
| Sample Rate | Configurable (PCM only) | Sample rate in Hz |
| Default Sample Rate | 24kHz | 24,000 samples per second |
| Channels | Mono | Single channel audio |
| Encoding | Base64 | Audio bytes encoded as base64 string |
| Byte Order | Little-endian | 16-bit samples (for PCM) |

### Configuring Audio Format

```python
session_config = {
    "type": "session.update",
    "session": {
        "audio": {
            "input": {
                "format": {
                    "type": "audio/pcm",  # or "audio/pcmu" or "audio/pcma"
                    "rate": 24000  # Only applicable for audio/pcm
                }
            },
            "output": {
                "format": {
                    "type": "audio/pcm",
                    "rate": 24000
                }
            }
        },
        "instructions": "You are a helpful assistant."
    }
}
await ws.send(json.dumps(session_config))
```

## Authentication

See the Voice Agent Authentication guide for details on:
- API Key authentication (server-side)
- Ephemeral Token authentication (client-side, recommended for browsers)

## Basic Usage

### Python WebSocket Connection

```python
import asyncio
import json
import os
import websockets

XAI_API_KEY = os.getenv("XAI_API_KEY")
base_url = "wss://api.x.ai/v1/realtime"

async def on_message(ws, message):
    data = json.loads(message)
    print("Received event:", json.dumps(data, indent=2))

async def send_message(ws, event):
    await ws.send(json.dumps(event))

async def on_open(ws):
    print("Connected to server.")

    # Configure the session
    session_config = {
        "type": "session.update",
        "session": {
            "voice": "Ara",
            "instructions": "You are a helpful assistant.",
            "turn_detection": {"type": "server_vad"},
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": 24000}},
                "output": {"format": {"type": "audio/pcm", "rate": 24000}}
            }
        }
    }
    await send_message(ws, session_config)

    # Send a user text message
    event = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        },
    }
    await send_message(ws, event)

    # Request a response
    event = {
        "type": "response.create",
        "response": {
            "modalities": ["text", "audio"],
        },
    }
    await send_message(ws, event)

async def main():
    async with websockets.connect(
        uri=base_url,
        ssl=True,
        additional_headers={"Authorization": f"Bearer {XAI_API_KEY}"}
    ) as websocket:
        await on_open(ws=websocket)

        while True:
            try:
                message = await websocket.recv()
                await on_message(websocket, message)
            except websockets.exceptions.ConnectionClosed:
                print("Connection Closed")
                break

asyncio.run(main())
```

## Message Types

### Client Events

| Event | Description |
|-------|-------------|
| `session.update` | Update session configuration (system prompt, voice, audio format, tools) |
| `input_audio_buffer.append` | Append chunks of audio data (base64-encoded). No server response. |
| `input_audio_buffer.commit` | Commit audio buffer as user message (manual VAD only) |
| `input_audio_buffer.clear` | Clear the input audio buffer |
| `conversation.item.create` | Create a new user message with text |
| `conversation.item.commit` | Commit audio buffer to conversation (manual VAD only) |
| `response.create` | Request assistant response (automatic with server_vad) |

### Server Events

| Event | Description |
|-------|-------------|
| `session.updated` | Confirms session configuration updated |
| `conversation.created` | First message on connection - session created |
| `input_audio_buffer.speech_started` | VAD detected speech start (server_vad only) |
| `input_audio_buffer.speech_stopped` | VAD detected speech end (server_vad only) |
| `input_audio_buffer.committed` | Audio buffer committed |
| `input_audio_buffer.cleared` | Audio buffer cleared |
| `conversation.item.added` | User/assistant message added to history |
| `conversation.item.input_audio_transcription.completed` | Input audio transcription complete |
| `response.created` | Assistant response in progress |
| `response.output_item.added` | Assistant response added to history |
| `response.done` | Assistant response completed |
| `response.output_audio_transcript.delta` | Audio transcript chunk |
| `response.output_audio_transcript.done` | Audio transcript complete |
| `response.output_audio.delta` | Audio stream chunk |
| `response.output_audio.done` | Audio stream complete |
| `response.function_call_arguments.done` | Function call triggered |

## Session Configuration

### Session Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `instructions` | string | System prompt |
| `voice` | string | Voice: `Ara`, `Rex`, `Sal`, `Eve`, `Leo` |
| `turn_detection.type` | string/null | `"server_vad"` for automatic, `null` for manual |
| `audio.input.format.type` | string | `"audio/pcm"`, `"audio/pcmu"`, or `"audio/pcma"` |
| `audio.input.format.rate` | number | Input sample rate (PCM only) |
| `audio.output.format.type` | string | Output format |
| `audio.output.format.rate` | number | Output sample rate (PCM only) |

### Example Session Update

```json
{
    "type": "session.update",
    "session": {
        "instructions": "You are a helpful assistant.",
        "voice": "Ara",
        "turn_detection": {
            "type": "server_vad"
        },
        "audio": {
            "input": {"format": {"type": "audio/pcm", "rate": 24000}},
            "output": {"format": {"type": "audio/pcm", "rate": 24000}}
        }
    }
}
```

## Tool Calling

The Voice Agent API supports various tools to enhance capabilities.

### Available Tool Types

- **Collections Search (`file_search`)** - Search uploaded document collections
- **Web Search (`web_search`)** - Search the web for current information
- **X Search (`x_search`)** - Search X (Twitter) for posts and information
- **Custom Functions** - Define your own function tools with JSON schemas

### Collections Search (file_search)

```python
session_config = {
    "type": "session.update",
    "session": {
        "tools": [
            {
                "type": "file_search",
                "vector_store_ids": ["your-collection-id"],
                "max_num_results": 10,
            },
        ],
    },
}
```

### Web Search and X Search

```python
session_config = {
    "type": "session.update",
    "session": {
        "tools": [
            {"type": "web_search"},
            {
                "type": "x_search",
                "allowed_x_handles": ["elonmusk", "xai"],
            },
        ],
    },
}
```

### Custom Function Tools

```python
session_config = {
    "type": "session.update",
    "session": {
        "tools": [
            {
                "type": "function",
                "name": "get_weather",
                "description": "Get current weather for a location",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "City name",
                        },
                    },
                    "required": ["location"],
                },
            },
        ],
    },
}
```

### Handling Function Calls

```python
async def handle_function_call(ws, event):
    function_name = event["name"]
    call_id = event["call_id"]
    arguments = json.loads(event["arguments"])

    # Execute the function
    result = execute_function(function_name, arguments)

    # Send result back
    await ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(result)
        }
    }))

    # Request agent to continue
    await ws.send(json.dumps({"type": "response.create"}))

# In message handler
async def on_message(ws, message):
    event = json.loads(message)
    if event["type"] == "response.function_call_arguments.done":
        await handle_function_call(ws, event)
```

### Function Call Events

| Event | Direction | Description |
|-------|-----------|-------------|
| `response.function_call_arguments.done` | Server → Client | Function call with arguments |
| `conversation.item.create` (function_call_output) | Client → Server | Send function result |
| `response.create` | Client → Server | Request agent to continue |

### Combining Multiple Tools

```python
session_config = {
    "type": "session.update",
    "session": {
        "tools": [
            {
                "type": "file_search",
                "vector_store_ids": ["your-collection-id"],
                "max_num_results": 10,
            },
            {"type": "web_search"},
            {"type": "x_search"},
            {
                "type": "function",
                "name": "get_weather",
                "description": "Get current weather",
                "parameters": {
                    "type": "object",
                    "properties": {"location": {"type": "string"}},
                    "required": ["location"],
                },
            },
        ],
    },
}
```

### Complete Function Call Example

```python
import json
import websockets

# Define function implementations
def get_weather(location: str, units: str = "celsius"):
    """Get current weather for a location"""
    return {
        "location": location,
        "temperature": 22,
        "units": units,
        "condition": "Sunny",
        "humidity": 45
    }

def book_appointment(date: str, time: str, service: str):
    """Book an appointment"""
    import random
    confirmation = f"CONF{random.randint(1000, 9999)}"
    return {
        "status": "confirmed",
        "confirmation_code": confirmation,
        "date": date,
        "time": time,
        "service": service
    }

# Map function names to implementations
FUNCTION_HANDLERS = {
    "get_weather": get_weather,
    "book_appointment": book_appointment
}

async def handle_function_call(ws, event):
    """Handle function call from the voice agent"""
    function_name = event["name"]
    call_id = event["call_id"]
    arguments = json.loads(event["arguments"])

    print(f"Function called: {function_name} with args: {arguments}")

    if function_name in FUNCTION_HANDLERS:
        result = FUNCTION_HANDLERS[function_name](**arguments)

        # Send result back to agent
        await ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(result)
            }
        }))

        # Request agent to continue
        await ws.send(json.dumps({"type": "response.create"}))
    else:
        print(f"Unknown function: {function_name}")

async def on_message(ws, message):
    event = json.loads(message)
    if event["type"] == "response.function_call_arguments.done":
        await handle_function_call(ws, event)
```

### Real-World Example: Weather Query Flow

When a user asks "What's the weather in San Francisco?":

| Step | Direction | Event | Description |
|------|-----------|-------|-------------|
| 1 | Client → Server | `input_audio_buffer.append` | User speaks: "What's the weather in San Francisco?" |
| 2 | Server → Client | `response.function_call_arguments.done` | Agent calls `get_weather` with `location: "San Francisco"` |
| 3 | Client → Server | `conversation.item.create` | Code executes and sends result: `{temperature: 68, condition: "Sunny"}` |
| 4 | Client → Server | `response.create` | Request agent to continue with function result |
| 5 | Server → Client | `response.output_audio.delta` | Agent responds: "The weather in San Francisco is currently 68°F and sunny." |

## Detailed Message Reference

### Session Messages

#### session.update (Client → Server)

```json
{
    "type": "session.update",
    "session": {
        "instructions": "You are a helpful assistant.",
        "voice": "Ara",
        "turn_detection": {"type": "server_vad"},
        "audio": {
            "input": {"format": {"type": "audio/pcm", "rate": 24000}},
            "output": {"format": {"type": "audio/pcm", "rate": 24000}}
        }
    }
}
```

#### session.updated (Server → Client)

```json
{
    "event_id": "event_123",
    "type": "session.updated",
    "session": {
        "instructions": "You are a helpful assistant.",
        "voice": "Ara",
        "turn_detection": {"type": "server_vad"}
    }
}
```

### Conversation Messages

#### conversation.created (Server → Client)

First message on connection:

```json
{
    "event_id": "event_9101",
    "type": "conversation.created",
    "conversation": {
        "id": "conv_001",
        "object": "realtime.conversation"
    }
}
```

#### conversation.item.create (Client → Server)

Create a text message:

```json
{
    "type": "conversation.item.create",
    "previous_item_id": "",
    "item": {
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": "Hello, how are you?"}
        ]
    }
}
```

#### conversation.item.added (Server → Client)

```json
{
    "event_id": "event_1920",
    "type": "conversation.item.added",
    "previous_item_id": "msg_002",
    "item": {
        "id": "msg_003",
        "object": "realtime.item",
        "type": "message",
        "status": "completed",
        "role": "user",
        "content": [
            {"type": "input_audio", "transcript": "hello how are you"}
        ]
    }
}
```

#### conversation.item.input_audio_transcription.completed (Server → Client)

```json
{
    "event_id": "event_2122",
    "type": "conversation.item.input_audio_transcription.completed",
    "item_id": "msg_003",
    "transcript": "Hello, how are you?"
}
```

### Input Audio Buffer Messages

#### input_audio_buffer.append (Client → Server)

```json
{
    "type": "input_audio_buffer.append",
    "audio": "<base64-encoded-audio>"
}
```

No server response for this message.

#### input_audio_buffer.clear (Client → Server)

```json
{"type": "input_audio_buffer.clear"}
```

#### input_audio_buffer.commit (Client → Server)

> Only available when `turn_detection.type` is `null` (manual mode).

```json
{"type": "input_audio_buffer.commit"}
```

#### input_audio_buffer.speech_started (Server → Client)

> Only with `server_vad` enabled.

```json
{
    "event_id": "event_1516",
    "type": "input_audio_buffer.speech_started",
    "item_id": "msg_003"
}
```

#### input_audio_buffer.speech_stopped (Server → Client)

> Only with `server_vad` enabled.

```json
{
    "event_id": "event_1516",
    "type": "input_audio_buffer.speech_stopped",
    "item_id": "msg_003"
}
```

#### input_audio_buffer.cleared (Server → Client)

```json
{
    "event_id": "event_1516",
    "type": "input_audio_buffer.cleared"
}
```

#### input_audio_buffer.committed (Server → Client)

```json
{
    "event_id": "event_1121",
    "type": "input_audio_buffer.committed",
    "previous_item_id": "msg_001",
    "item_id": "msg_002"
}
```

### Response Messages

#### response.create (Client → Server)

```json
{"type": "response.create"}
```

Or with modalities:

```json
{
    "type": "response.create",
    "response": {"modalities": ["text", "audio"]}
}
```

#### response.created (Server → Client)

```json
{
    "event_id": "event_2930",
    "type": "response.created",
    "response": {
        "id": "resp_001",
        "object": "realtime.response",
        "status": "in_progress",
        "output": []
    }
}
```

#### response.output_item.added (Server → Client)

```json
{
    "event_id": "event_3334",
    "type": "response.output_item.added",
    "response_id": "resp_001",
    "output_index": 0,
    "item": {
        "id": "msg_007",
        "object": "realtime.item",
        "type": "message",
        "status": "in_progress",
        "role": "assistant",
        "content": []
    }
}
```

#### response.done (Server → Client)

```json
{
    "event_id": "event_3132",
    "type": "response.done",
    "response": {
        "id": "resp_001",
        "object": "realtime.response",
        "status": "completed"
    }
}
```

### Audio and Transcript Messages

#### response.output_audio_transcript.delta (Server → Client)

```json
{
    "event_id": "event_4950",
    "type": "response.output_audio_transcript.delta",
    "response_id": "resp_001",
    "item_id": "msg_008",
    "delta": "Text response..."
}
```

#### response.output_audio_transcript.done (Server → Client)

```json
{
    "event_id": "event_5152",
    "type": "response.output_audio_transcript.done",
    "response_id": "resp_001",
    "item_id": "msg_008"
}
```

#### response.output_audio.delta (Server → Client)

```json
{
    "event_id": "event_4950",
    "type": "response.output_audio.delta",
    "response_id": "resp_001",
    "item_id": "msg_008",
    "output_index": 0,
    "content_index": 0,
    "delta": "<base64-encoded-audio>"
}
```

#### response.output_audio.done (Server → Client)

```json
{
    "event_id": "event_5152",
    "type": "response.output_audio.done",
    "response_id": "resp_001",
    "item_id": "msg_008"
}
```

## Audio Encoding Utilities

### Receiving and Playing Audio

```python
import base64
import numpy as np

# Configure session with custom sample rate
session_config = {
    "type": "session.update",
    "session": {
        "instructions": "You are a helpful assistant.",
        "voice": "Ara",
        "turn_detection": {"type": "server_vad"},
        "audio": {
            "input": {"format": {"type": "audio/pcm", "rate": 16000}},
            "output": {"format": {"type": "audio/pcm", "rate": 16000}}
        }
    }
}
await ws.send(json.dumps(session_config))

SAMPLE_RATE = 16000

def audio_to_base64(audio_data: np.ndarray) -> str:
    """Convert float32 audio array to base64 PCM16 string."""
    audio_int16 = (audio_data * 32767).astype(np.int16)
    audio_bytes = audio_int16.tobytes()
    return base64.b64encode(audio_bytes).decode('utf-8')

def base64_to_audio(base64_audio: str) -> np.ndarray:
    """Convert base64 PCM16 string to float32 audio array."""
    audio_bytes = base64.b64decode(base64_audio)
    audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)
    return audio_int16.astype(np.float32) / 32768.0
```

## Telephony Integration

### Supported Platforms

- **Twilio** - Full integration support
- **Vonage** - SIP provider support
- **Other SIP providers** - Standard SIP integration

### Architecture (Twilio)

```
Phone Call ←SIP→ Twilio ←WebSocket→ Node.js Server ←WebSocket→ xAI API
```

## Third-Party Framework Integration

### LiveKit

Build real-time voice agents using LiveKit's open-source framework:
- Native Grok Voice Agent API integration
- WebRTC support
- Scalable infrastructure

[LiveKit Docs](https://docs.livekit.io/agents/integrations/xai/) | [GitHub](https://github.com/livekit/agents/tree/main/livekit-plugins/livekit-plugins-xai)

### Voximplant

Build real-time voice agents using Voximplant:
- Native Grok Voice Agent API integration
- SIP support
- Global telephony infrastructure

[Voximplant Docs](https://voximplant.com/products/grok-client) | [GitHub](https://github.com/voximplant/grok-voice-agent-example)

### Pipecat

Build real-time voice agents using Pipecat's open-source framework:
- Native Grok Voice Agent API integration
- Advanced conversation management

[Pipecat Docs](https://docs.pipecat.ai/server/services/s2s/grok) | [GitHub](https://github.com/pipecat-ai/pipecat/blob/main/examples/foundational/51-grok-realtime.py)

## Example Applications

### Web Voice Agent

Real-time voice chat in the browser with React frontend and Python/Node.js backends.

[GitHub](https://github.com/xai-org/xai-cookbook/tree/main/voice-examples/agent/web)

### Phone Voice Agent (Twilio)

AI-powered phone system using Twilio integration.

[GitHub](https://github.com/xai-org/xai-cookbook/tree/main/voice-examples/agent/telephony)

### WebRTC Voice Agent

Browser voice chat using WebRTC for client-side, WebSocket to xAI API.

> **Note:** Direct WebRTC connections to xAI API are not available. Use a WebRTC server that connects to the Grok Voice Agent API.

[GitHub](https://github.com/xai-org/xai-cookbook/tree/main/voice-examples/agent/webrtc)

## Important Notes

- The Voice Agent API is only available in the `us-east-1` region
- Direct WebRTC connections are **not available** - use a WebRTC server as intermediary
- Dedicated speech-to-text and text-to-speech APIs coming soon
- The API handles industry-specific terminology (medical, legal, financial) accurately
- Precise recognition of email addresses, dates, alphanumeric codes, names, addresses, phone numbers

## Best Practices

1. **Handle interruptions**: Users may interrupt mid-response
2. **Buffer audio**: Smooth playback by buffering incoming audio
3. **Error recovery**: Reconnect on WebSocket disconnection
4. **Use server_vad**: Automatic voice activity detection simplifies implementation
5. **Audio quality**: Use 24kHz for most use cases, 8kHz for telephony
6. **Ephemeral tokens**: Use for client-side connections to protect API keys
