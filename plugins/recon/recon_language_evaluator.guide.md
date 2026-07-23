# Recon — Language Evaluator

A **Recon contributor**: during pre-processing it detects the primary language
of the incoming message and produces a `language_hint`, so Synth can reply in
the right language without any keyword matching.

Recon contributors expose no model-facing actions.

## Configuration

| Key | Purpose |
|-----|---------|
| `RECON_LANGUAGE_EVALUATOR_RECON_ENABLED` | Enable this Recon contributor. |
