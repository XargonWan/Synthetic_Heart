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
in them (the synth's own lines are never surfaced as reply targets). A human line
is offered as a **reply target** only while the conversation is still pending: a
chat that is *live* (some message younger than `GRILLO_OUTREACH_QUIET_MINUTES`)
**and** already answered (the synth's own newest line is newer than the newest
line from anyone else) contributes its lines as **context** instead, tagged
`you already answered this, not a reply target`. Those lines are rendered into
the prompt but never enter `grillo_snippets`, so the routing guard cannot route a
reply into a conversation that is already up to date — the failure mode being a
beat "answering" a line the synth had just replied to and, in the reported case,
re-sending its own previous reply verbatim. An answered but *idle* chat still
offers its human lines: reaching out there later with something new is what the
run is for, and delivery is protected separately (below).

Proactive outreach is governed by exactly one gate: **a live conversation**. A
chat whose most recent message — from the human *or* from the synth — is younger
than `GRILLO_OUTREACH_QUIET_MINUTES` is marked `LIVE-CONVERSATION` and skipped for
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

Independently of where a snippet came from, `message_plugin` refuses at delivery
any beat message whose text repeats the synth's own recent line in that chat
(`GRILLO_DUP_SIMILARITY_THRESHOLD`), for every beat type and every chat type,
private DMs included. Outbound beats stay exempt from the *public-chat* gates —
reaching out is their purpose — but never from that repeat gate: a duplicate is a
duplicate wherever it lands.

Freshness is judged in wall-clock terms, not against the last-run cursor. A run
that finds a message newer than its cursor but older than one full interval is
treated as decay-driven, exactly as if there had been no message at all, so the
synth is told the run exists to reach out instead of being left with only stale
snippets it has been told not to answer. This is what keeps outreach happening
after a restart: an outage longer than the interval would otherwise leave the
cursor behind a message that is now hours old, and the run would go quiet.

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
