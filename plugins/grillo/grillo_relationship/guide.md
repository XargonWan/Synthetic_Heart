# Grillo — Relationship Beat

Part of the [G.R.I.L.L.O.](../guide.md) background subsystem.

## Purpose

Reflects on recent interaction patterns — who Synth has been talking to, how
those exchanges felt, and how the relationships are evolving.

## Beat

- **Beat type:** `relationship`
- **Selection:** weighted-selected by the Grillo scheduler on a normal tick.
- **Output:** a relationship-oriented prompt enqueued as a low-priority beat;
  the reply is typically written to the diary.

## How it works

The plugin builds a prompt from recent interaction history and passes it to the
active Grillo cortex. Discovery is automatic via the plugin registry using its
`BEAT_TYPE` attribute.

## Configuration

Uses the shared Grillo settings — `GRILLO_BEAT_INTERVAL`, `GRILLO_CORTEX`,
`GRILLO_ALLOWED_ACTIONS` / `GRILLO_ALLOWED_SECURITY_LEVEL`. See the
[G.R.I.L.L.O. guide](../guide.md) for the full list.
