# Grillo — LLM Failure Recovery Beat

Part of the [G.R.I.L.L.O.](../guide.md) background subsystem.

## Purpose

A safety-net loop. It detects recent turns where the LLM fell back to a generic
failure reply and regenerates the proper response, so a transient model error
doesn't leave a user with a broken or empty answer.

## Beat

- **Trigger:** periodic recovery loop.
- **Output:** a regenerated, corrected reply delivered in place of the failed
  fallback.

## How it works

The plugin scans recent activity for fallback-failure markers and, when it finds
one, re-runs the turn through the active cortex to produce a real reply. The loop
is guarded so only one instance runs at a time. Discovery is automatic via the
plugin registry.

## Configuration

Uses the shared Grillo settings — `GRILLO_BEAT_INTERVAL`, `GRILLO_CORTEX`,
`GRILLO_ALLOWED_ACTIONS` / `GRILLO_ALLOWED_SECURITY_LEVEL`. See the
[G.R.I.L.L.O. guide](../guide.md) for the full list.
