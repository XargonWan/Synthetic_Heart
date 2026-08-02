# Recon — Memory Recollector

A **Recon contributor**: during pre-processing it extracts tags/keywords from
the incoming message, searches the memory store, and surfaces the most relevant
memories as a `memory_search` recon hint — so Synth recalls the right context
before composing a reply.

Recon contributors expose no model-facing actions.

## Configuration

| Key | Purpose |
|-----|---------|
| `RECON_MEMORY_RECOLLECTOR_RECON_ENABLED` | Enable this Recon contributor. |
