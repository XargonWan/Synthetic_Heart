# KittenTTS voice

Local, offline text-to-speech with the [KittenTTS](https://github.com/KittenML/KittenTTS)
models: eight voices (Bella, Jasper, Luna, Bruno, Rosie, Hugo, Kiki, Leo) that work for any
language, ONNX on the CPU, no account and no cloud.

## Use it

1. Switch the extension on and grant what it asks for. It creates a voice engine called
   **kitten** (Engines > Voice).
2. Pick **kitten** as the active voice engine and a voice in the voice picker. The model is
   downloaded from HuggingFace the first time it speaks.

## Settings

- **Model**: `kitten-tts-mini-0.8` (default), `kitten-tts-nano-0.8`, `kitten-tts-micro-0.8`.
- **Speech speed**, and a **language to model map** for a model per language.

## Needs

`espeak-ng` on the machine (the phonemizer), and the `kittentts` package with its CPU torch,
which makes this a large extension: that is why it is **not installed by default**.

## What changed from the built-in engine

It is now an endpoint-backed engine like any other, so the "Manage Models" download list no
longer shows the KittenTTS models; the extension downloads the one it needs by itself. The
voice key `KITTEN_VOICE` and the active-engine name `kitten` are the same, so an existing
setup keeps working.
