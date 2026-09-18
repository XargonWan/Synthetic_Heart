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

## Memory re-distillation

Memories written before the distilling extractor existed hold the session
transcript as their content, so recall can return raw conversation however good
the pipeline is. **Settings → Memory Re-Distillation → Re-distil memories**
rewrites them in place from the WebUI: one model call per memory, run in the
background with live counters, and the rest of the page stays usable.

The pass targets memories that carry no distillation stamp (`mem_cells.distilled_at`,
added automatically when the plugin starts). A memory gets stamped when a
distilling extractor writes it and when the pass rewrites it, so pressing the
button again does nothing rather than paraphrasing good memories a second time.
The panel shows how many memories are still unstamped before anyone presses it.
A memory the model will not paraphrase stays unstamped and can be retried later.

| Key | Purpose |
|-----|---------|
| `SOUL_REDISTIL_LIMIT` | How many memories one press may rewrite (default 5000, hard ceiling 20000). Each one costs a model call, so a large store is caught up over several presses. |

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
