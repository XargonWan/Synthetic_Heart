# Grillo — Curiosity Beat

Part of the [G.R.I.L.L.O.](../guide.md) background subsystem.

## Purpose

Lets Synth explore emergent questions on her own — things she became curious
about during recent interactions — and capture the exploration as a diary entry.

## Beat

- **Beat type:** `curiosity`
- **Selection:** weighted-selected by the Grillo scheduler on a normal tick.
- **Output:** a curiosity-driven prompt enqueued as a low-priority beat; the
  reply is written to the diary.

## How it works

The plugin builds a prompt that invites Synth to follow up on an open question
and hands it to the active Grillo cortex. Discovery is automatic via the plugin
registry using its `BEAT_TYPE` attribute.

## Configuration

Uses the shared Grillo settings — `GRILLO_BEAT_INTERVAL`, `GRILLO_CORTEX`,
`GRILLO_ALLOWED_ACTIONS` / `GRILLO_ALLOWED_SECURITY_LEVEL`. See the
[G.R.I.L.L.O. guide](../guide.md) for the full list.
