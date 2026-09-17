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
`GRILLO_OBSERVER_ACTIVITY_WINDOW_DAYS` and builds snippets from the human lines
in them (the synth's own lines are never surfaced as snippets). Proactive
outreach is then governed by exactly one gate: **a live conversation**. A chat
whose most recent message — from the human *or* from the synth — is younger than
`GRILLO_OUTREACH_QUIET_MINUTES` is marked `LIVE-CONVERSATION` and skipped for
that run. Every other conversation is offered to the model, and reaching out to
one of them is what the run is for: what has to be genuine is the *content*
(a new, grounded message — never a canned opener, never a restatement of its own
last message), not the fact of speaking. "I have nothing new to say here" is a
reason to write something else, not a reason to stay silent while a target is
offered. Who spoke last is deliberately irrelevant: a synth that answers
everything is the newest speaker in every chat it takes part in, so any "you
spoke last, stay away" rule would silence outreach permanently. Cadence belongs
to `GRILLO_OBSERVER_INTERVAL`; the last run is tracked in
`GRILLO_OBSERVER_LAST_RUN_TS`. Discovery is automatic via the plugin registry.

## Configuration

| Key | Purpose |
|-----|---------|
| `GRILLO_OBSERVER_ENABLED` | Enable/disable the observer. |
| `GRILLO_OBSERVER_INTERVAL` | Seconds between observer runs (the outreach cadence). |
| `GRILLO_OBSERVER_STORE_MEMORIES` | Whether to store passive memories from snippets. |
| `GRILLO_OBSERVER_ACTIVITY_WINDOW_DAYS` | How recent a conversation must be to consider. |
| `GRILLO_OBSERVER_SELF_WINDOW` | Duplicate-suppression window for identical outbound messages. Does not gate outreach. |
| `GRILLO_OUTREACH_QUIET_MINUTES` | Live-conversation guard: a chat with a message (either side) younger than this is skipped for that run. |

Plus the shared Grillo settings. See the [G.R.I.L.L.O. guide](../guide.md).
