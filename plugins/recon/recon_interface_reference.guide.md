# Recon — Interface Reference

A **Recon contributor**: when the user's message talks about a chat platform as
a place to write or be reached ("scrivimi su telegram"), it adds a short snippet
listing the matching **active** interfaces, the `interface_path` format of each
and the paths already known for it.

The prompt always carries a one-line `[Interfaces]` list of the active interface
ids; this plugin supplies the detail only when it is wanted, so the cost is paid
on the turns that need it. An interface that is switched off disappears from the
snippet on the next turn, with no restart.

Recon contributors expose no model-facing actions.

## Configuration

| Key | Purpose |
|-----|---------|
| `RECON_INTERFACE_REFERENCE_RECON_ENABLED` | Enable this Recon contributor. |
