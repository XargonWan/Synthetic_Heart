# Grillo — Chat Observer Beat

Part of the [G.R.I.L.L.O.](../guide.md) background subsystem.

## Purpose

Passively watches recent conversations. It samples chat snippets, optionally
stores passive memories from them, and — within strict anti-spam guards — can
propose proactive outreach so Synth can gently re-engage a quiet conversation
instead of only ever replying.

## Beat

- **Trigger:** periodic (its own interval, `GRILLO_OBSERVER_INTERVAL`).
- **Output:** proposed chat snippets for processing and/or passive memories.

## How it works

On each run the plugin looks at conversations active within
`GRILLO_OBSERVER_ACTIVITY_WINDOW_DAYS`. Proactive messaging is heavily guarded:
if the synth was the last to speak in a conversation, outreach to it is blocked
for `GRILLO_OBSERVER_SELF_COOLDOWN_DAYS` (or, when that is 0, for
`GRILLO_OBSERVER_SELF_COOLDOWN_MINUTES`), and quiet hours are respected via
`GRILLO_OUTREACH_QUIET_MINUTES`. The last run is tracked in
`GRILLO_OBSERVER_LAST_RUN_TS`. Discovery is automatic via the plugin registry.

## Configuration

| Key | Purpose |
|-----|---------|
| `GRILLO_OBSERVER_ENABLED` | Enable/disable the observer. |
| `GRILLO_OBSERVER_INTERVAL` | Seconds between observer runs. |
| `GRILLO_OBSERVER_STORE_MEMORIES` | Whether to store passive memories from snippets. |
| `GRILLO_OBSERVER_ACTIVITY_WINDOW_DAYS` | How recent a conversation must be to consider. |
| `GRILLO_OBSERVER_SELF_WINDOW` | Window used when evaluating the synth's own recent messages. |
| `GRILLO_OBSERVER_SELF_COOLDOWN_DAYS` | Anti-spam: days to forbid outreach after the synth spoke last (0 = use minutes). |
| `GRILLO_OBSERVER_SELF_COOLDOWN_MINUTES` | Fine-grained anti-spam cooldown when the days cooldown is 0. |
| `GRILLO_OUTREACH_QUIET_MINUTES` | Quiet period during which proactive outreach is suppressed. |

Plus the shared Grillo settings. See the [G.R.I.L.L.O. guide](../guide.md).
