# OpenAI API Server — Setup

This interface exposes SyntH through an **OpenAI-compatible HTTP API** (it also
speaks the legacy Ollama protocol), so any client that talks to the OpenAI or
Ollama endpoints can chat with the Synth. It runs on port **11435**.

## 1. Enable the server

The server starts automatically on boot. To toggle it explicitly, set:

- **`OLLAMA_SERVER_ENABLED`** — `true` (default) to start the HTTP server,
  `false` to keep the interface actions available without opening the port.

## 2. Optional configuration

- **`OLLAMA_DEFAULT_MODEL`** — the model name advertised on `/api/tags` and
  `/v1/models` (defaults to `SyntH`).
- **`OLLAMA_MAX_HISTORY`** — how many past messages to include as context.
- **`OLLAMA_STREAM_TIMEOUT`** — seconds to wait while streaming a response.
- **`OLLAMA_COMPLETION_TIMEOUT`** — seconds to wait for a full completion
  (`0` disables the timeout).

## 3. Talk to the Synth

Point any OpenAI/Ollama-compatible client at the server:

```bash
curl -X POST http://localhost:11435/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "default",
    "messages": [
      { "role": "user", "content": "Hello!" }
    ],
    "stream": false
  }'
```

The OpenAI-style routes (`/v1/models`, `/v1/chat/completions`) and the Ollama
routes (`/api/tags`, `/api/chat`) are both served.

## Tips

- The Synth exposes itself as a single logical model — the `model` field in the
  request is largely cosmetic.
- Replies flow through the normal Synth message chain, so plugins, memory, and
  persona all apply just like on any other interface.
