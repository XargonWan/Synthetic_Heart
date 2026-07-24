# Message Map

Maps forwarded/relayed messages back to their **original chat and message ID**.
When a message is forwarded to the trainer (or across interfaces), this plugin
keeps the link so Synth can reference or reply to the source message correctly.

Backed by the `message_map` table.

## Actions

| Action | Purpose |
|--------|---------|
| `get_original_message` | Resolve a mapped message back to its origin. |
| `get_mapping_stats` | Report mapping statistics. |
