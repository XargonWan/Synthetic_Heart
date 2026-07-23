# Mate Engine

Optional integration with **Mate-Engine**, an external avatar/companion
runtime. When enabled, it lets Synth send messages to the Mate outbox and
promote animation uploads for use by the Mate client. If Mate-Engine is not in
use, the plugin simply stays idle.

## Actions

| Action | Purpose |
|--------|---------|
| `send_mate_message` | Enqueue a message to the Mate-Engine outbox. |
| `promote_upload` | Promote an uploaded animation for Mate-Engine. |
