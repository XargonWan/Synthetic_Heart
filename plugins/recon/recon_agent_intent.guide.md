# Recon — Agent Intent Evaluator

A **Recon contributor**: during pre-processing it judges whether the request
requires the agentic lane (multi-step tools, files, shell) rather than a plain
reply, producing an `agent_intent` recon hint that informs routing.

Recon contributors expose no model-facing actions.

## Configuration

| Key | Purpose |
|-----|---------|
| `RECON_AGENT_INTENT_RECON_ENABLED` | Enable this Recon contributor. |
