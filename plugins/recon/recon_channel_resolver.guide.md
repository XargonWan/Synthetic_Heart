# Recon — Channel Resolver

A **Recon contributor**: during pre-processing it resolves named channels /
chats mentioned in a request into a concrete `interface_path`, producing a
`channel_reference` recon hint. This lets Synth target the right destination
when a user refers to a channel by name.

Recon contributors expose no model-facing actions.

## Configuration

| Key | Purpose |
|-----|---------|
| `RECON_CHANNEL_RESOLVER_RECON_ENABLED` | Enable this Recon contributor. |
