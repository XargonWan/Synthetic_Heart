# Grillo — Self-Reflection Beat

Part of the [G.R.I.L.L.O.](../guide.md) background subsystem.

## Purpose

An emotional self check-in: Synth pauses to notice how she is feeling and why,
and records the reflection in her diary. Keeps her inner life continuous even
when no one is talking to her.

## Beat

- **Beat type:** `self_reflection`
- **Selection:** weighted-selected by the Grillo scheduler on a normal tick.
- **Output:** a reflective prompt enqueued as a low-priority beat; the resulting
  reply is written to the diary.

## How it works

The plugin composes a self-reflection prompt (grounded in the current emotion
state) and passes it to the active Grillo cortex. Discovery is automatic via the
plugin registry using its `BEAT_TYPE` attribute.

## Configuration

Uses the shared Grillo settings — `GRILLO_BEAT_INTERVAL`, `GRILLO_CORTEX`,
`GRILLO_ALLOWED_ACTIONS` / `GRILLO_ALLOWED_SECURITY_LEVEL`. See the
[G.R.I.L.L.O. guide](../guide.md) for the full list.
