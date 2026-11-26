# Harmony AI protocol

Adds the **Harmony AI Cloud** protocol to SyntH's external endpoints, so an endpoint whose
protocol is `harmony` (the "Harmony AI" preset) can list its rich model catalogue and use its
text-to-speech and speech-to-text.

## What it does

- **Models**: reads `GET /v1/models`, whose entries carry `model_type`, input/output
  modalities and the speech options (languages, voices, cloning), and maps them to SyntH's
  subsystems (chat, voice, hearing, vision).
- **Text-to-speech** (`/v1/audio/speech`) and **speech-to-text** (`/v1/audio/transcriptions`),
  both JSON with base64 audio, unlike OpenAI's. When a model rejects the language, TTS retries
  once in a language it supports (English if possible).
- Chat, embeddings and vision are plain OpenAI-compatible: the Core handles them.

## Permission

It asks for **endpoints.protocol**. That gives it the API key of the endpoints that use the
`harmony` protocol, and of no others: it needs the key to call Harmony. Grant it only if you use
Harmony.

Without this extension a Harmony endpoint stays in the list but cannot be probed or used;
install and switch it on and its engines appear again at once.
