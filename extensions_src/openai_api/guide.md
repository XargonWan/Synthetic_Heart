# OpenAI API — Setup

This extension exposes SyntH through an **OpenAI-compatible HTTP API**: any tool,
IDE plugin, app or script built for the OpenAI API can chat with your Synth, and the
persona, memory and emotions ride along. The Synth appears as **one model**, named
after it, whose capabilities are the union of what its engines provide.

## Routes

| Route | What it does |
| --- | --- |
| `GET /v1/models`, `GET /v1/models/{id}` | The Synth as a model |
| `POST /v1/chat/completions` | A chat turn (`messages`) |
| `POST /v1/completions` | A single prompt (`prompt`) |
| `GET /` | Health check (no key needed) |

Add `"stream": true` to receive standard **server-sent events** (`data: {...}`
chunks, ending with `data: [DONE]`). Without it you get one JSON response.

## 1. Settings

- **Port** — default `11435`. Inside Docker, publish the port you choose.
- **Bind address** — leave empty: `0.0.0.0` in a container, `127.0.0.1` otherwise.
- **API key** — when set, clients must send `Authorization: Bearer <key>`. Leave it
  empty only on a trusted network: anyone who can reach the port can talk to your Synth.
- **Messages of history used** — how many earlier messages of a request are used.
- **Answer timeout** — seconds a request waits for the answer (`0` = up to 15 minutes).

## 2. Try it

```bash
curl http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Hello!"}]}'
```

## Conversations

The API is stateless: send the whole conversation in `messages` each time. To keep
one conversation's memory on the Synth side, send a stable `conversation` (or
`session`, `user`) value; without one every request is its own conversation.
Only one request per conversation may be in flight at once (`409` otherwise).

## Tips

- The Synth answers in text; media and voice are not part of this API.
- Slash commands (`/help`, …) work in the last user message.
