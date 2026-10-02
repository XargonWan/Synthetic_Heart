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
is guarded so only one instance runs at a time, and the guard is **process-wide,
not per instance**: the loader starts the Grillo plugin several times during boot
and each start used to build its own copy of this plugin, each copy starting its
own loop. Four loops then scanned the same failures at the same moment and each
recovered them, so a single failed turn produced up to four recovery messages
(observed live: four `recovery loop started` and four `recovery delivered` for one
chat). The first starter now owns the loop; later instances log that a loop is
already running and stay idle, and stopping a non-owner leaves the owner's loop
alone. `GrilloPlugin.start()` also returns before doing any of its work once a
scheduler is running, so the later starts no longer build a copy at all.

Only real failures are considered: writes the test suite makes are flagged
`is_test` and excluded from every scan, and a startup migration flags the fixture
rows that were stored before that marker was written.

Failures are only recovered within `GRILLO_FAILURE_RECOVERY_WINDOW_MIN` (default
30) minutes of being recorded, at most once per interface path per window.
Anything older, unroutable, or already handled is marked processed in its
`metadata` so it is never revisited. Every processed failure is marked in a
`finally` block even when regeneration fails — without that, the same failure
would reappear on every scan and the plugin would keep generating messages for a
turn that is already over.

## Configuration

Uses the shared Grillo settings — `GRILLO_BEAT_INTERVAL`, `GRILLO_CORTEX`,
`GRILLO_ALLOWED_ACTIONS` / `GRILLO_ALLOWED_SECURITY_LEVEL` — plus:

- `GRILLO_FAILURE_RECOVERY_INTERVAL` (seconds between scans, default 120)
- `GRILLO_FAILURE_RECOVERY_WINDOW_MIN` (how recent a failure must be, default 30)

See the [G.R.I.L.L.O. guide](../guide.md) for the full list.
