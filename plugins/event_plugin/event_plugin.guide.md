# Events & Scheduled Messages

Lets Synth **schedule things for the future**. She can register an event to act
on at a given date/time, or queue a delayed message. A scheduler fires due
events back into the message chain, and upcoming events are injected into the
prompt context so Synth stays aware of what's coming.

Backed by the `scheduled_events` table.

## How the block reaches the model

`static_inject` contributes the `upcoming_events` block ("upcoming events (next N
days) (informational only, do not act unless relevant)") for the configured
lookahead window, and the core renderer prints it under an `[Upcoming events]`
heading on the ordinary chat and beat prompts (`_PLUGIN_CONTEXT_BLOCKS` in
`core/prompt_engine.py`). The block was built on every turn and dropped until it
was declared there, so this is the first time it reaches the model; the plugin's
own scheduling and firing path is unaffected. Vessel turns deliberately drop it
(`core/prompt_engine.py::_compact_prompt_for_vessel`).

## Actions

| Action | Purpose |
|--------|---------|
| `event` | Register a future event for Synth to act on. |
| `schedule_message` | Queue a message to be delivered later. |
| `static_inject` | Inject upcoming events into the prompt. |
