# Events & Scheduled Messages

Lets Synth **schedule things for the future**. She can register an event to act
on at a given date/time, or queue a delayed message. A scheduler fires due
events back into the message chain, and upcoming events are injected into the
prompt context so Synth stays aware of what's coming.

Backed by the `scheduled_events` table.

## Actions

| Action | Purpose |
|--------|---------|
| `event` | Register a future event for Synth to act on. |
| `schedule_message` | Queue a message to be delivered later. |
| `static_inject` | Inject upcoming events into the prompt. |
