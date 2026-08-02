# Grillo — History Evaluator (helper)

Part of the [G.R.I.L.L.O.](../guide.md) background subsystem.

## Purpose

A non-LLM helper — **not** a beat. It formats the last N chat messages into a
concise reflection prompt that several beats reuse, so each beat doesn't have to
reimplement history formatting.

## What it is

- A `PluginBase` (no LLM) with its own `get_metadata()` for the WebUI.
- Has **no** `BEAT_TYPE`: it is never weighted-selected by the scheduler; the
  Grillo core (`grillo_impl.py`) fetches it explicitly from the plugin registry
  and calls it while building beat prompts.

## How it works

Given an `interface_path` and a message count, it reads recent chat history and
returns a compact, prompt-ready summary of the conversation. Purely
deterministic formatting — no model call.

## Configuration

No dedicated config keys. Behaviour is driven entirely by the caller (which beat
requested the reflection and for how many messages). See the
[G.R.I.L.L.O. guide](../guide.md).
