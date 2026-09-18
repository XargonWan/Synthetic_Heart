# SOUL

Runtime orchestration of the **SOUL** memory/emotion architecture. SOUL
compiles conversation transcripts into structured `MemCells` (and a DSP
representation), rolls them up over time, and injects the resulting SOUL
context into the prompt — a higher-level layer over raw diary/memory storage.

It runs a scheduler that compiles buffered transcript after an idle period and
performs periodic roll-ups. Persistence can be in-memory or PostgreSQL.

## Actions

| Action | Purpose |
|--------|---------|
| `static_inject` | Inject SOUL context into the prompt. |

Enabled/disabled via its global plugin toggle (`PLUGIN_ENABLED__soul_plugin`).

## Configuration

| Key | Purpose |
|-----|---------|
| `SOUL_COMPILE_IDLE_SECONDS` | Idle seconds before compiling buffered transcript. |
| `SOUL_SCHEDULER_INTERVAL_SECONDS` | Scheduler tick interval. |
| `SOUL_MEMCELL_LLM_ENABLED` | Distil each MemCell with an LLM (`DSP_CORTEX` scope) instead of storing the conversation text verbatim: recall returns paraphrased knowledge with `subject\|predicate\|object` facts, and a corrected statement says so in its own trace. Default on; the rule-based extractor is the fallback on any failure. |
| `SOUL_CURATOR_MIN_AGE_HOURS` | Grace period (default 168 = one week) during which a freshly compiled memory is never removed by the curator's low-salience pass. Without it a calm new cell scores 0.2 against a 0.4 threshold and is deleted the night it is compiled. Set 0 to restore the old behaviour. |
| `SOUL_TEMPORAL_INJECT_LIMIT` | Maximum active situational notes rendered into one prompt (default 8), ranked by priority then confidence and deduplicated by subject tokens. The store keeps every note; this only bounds what the model sees. |
| `SOUL_REPOSITORY_BACKEND` | Persistence backend (`memory` or `postgres`). |
| `SOUL_POSTGRES_DSN` | PostgreSQL DSN when the backend is `postgres`. |
