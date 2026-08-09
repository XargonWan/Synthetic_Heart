# Recent Chats

Tracks which conversations have been **recently active**. Interfaces call in to
record activity, and Synth (or background agents like Grillo) can retrieve the
list of recently active chats — useful for deciding where to proactively reach
out.

Backed by the `recent_chats` table.

## Actions

| Action | Purpose |
|--------|---------|
| `get_recent_chats` | List recently active chats. |
