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

Because every memory costs a model call, the pass passes over the ones a call
could never earn back. A memory that recall would never inject anyway (in-character
roleplay, explicit exchanges, housekeeping sessions such as nightly or diary-merge
work) is skipped without reaching the model and counted separately as
`skipped_unusable`, so a store full of roleplay costs nothing to clear. The panel
therefore reports two numbers: how many memories are unstamped, and how many a
press would actually distil, so the cost is known before it is paid. A memory the
model will not paraphrase stays unstamped and can be retried later.

Engines are not equally fast, so each memory's rewrite is also bounded by
`SOUL_REDISTIL_TIMEOUT_SEC` (default 300 s, `0` removes the bound). A memory that
exceeds it is counted as `timed_out` rather than `failed`, is left untouched and
unstamped, and the panel says so in the status line. Raise the value for a slow
engine (one driving a browser, or a large local model) and press again: the pass
skips what it already rewrote, so only the timed-out ones are retried.

| Key | Purpose |
|-----|---------|
| `SOUL_REDISTIL_LIMIT` | How many memories one press may rewrite (default 5000, hard ceiling 20000). Each one costs a model call, so a large store is caught up over several presses. |
| `SOUL_REDISTIL_TIMEOUT_SEC` | How long one memory's rewrite may take before the pass counts it as timed out and moves on (default 300, `0` removes the bound). Raise it for a slow engine; a timed-out memory is left unstamped and is retried by the next press. |

## What recall shows

Three structural rules decide which memories reach a prompt.

- **Rotation.** A memory that was just injected steps aside for the recall
  cooldown (`SOUL_RECALL_COOLDOWN_SEC`, default 15 minutes) so the
  next-most-similar memories are shown instead. Semantic similarity is 58% of the
  recall score and it does not move while the same person keeps talking about the
  same things, so without this the identical handful of memories is injected turn
  after turn for days. When nothing else is left to show, the held-back memories
  fall back in, so the block is never empty. The store is untouched: only what is
  shown rotates.
- **Raw transcript is not recalled.** A memory carrying no distillation stamp was
  written before the distilling extractor existed, so its trace is the verbatim
  session text and it stays out of the recalled set until the re-distil pass
  rewrites it. This rule is skipped when the active extractor does not distil at
  all: the rule-based fallback stamps nothing, so the stamp would describe every
  memory and the block would go empty.
- **Everything says where it came from.** Each injected entry carries its date and
  its origin: `Recalled memory from <date> (same chat)` or `(other chat: <path>)`
  for a memory cell, and `(diary)`, `(long-term memory)`, `(<chat path>, chat
  history)` for the other tiers. Without the label, a raw line lifted from another
  conversation reads exactly like the model's own recollection.

## Configuration

| Key | Purpose |
|-----|---------|
| `SOUL_RECALL_COOLDOWN_SEC` | How long a memory stays out of the recalled set after it has been injected into a prompt (default 900 = 15 minutes, `0` disables). Rotation only: it changes which memories are shown, never what is stored. |
| `SOUL_COMPILE_IDLE_SECONDS` | Idle seconds before compiling buffered transcript. |
| `SOUL_SCHEDULER_INTERVAL_SECONDS` | Scheduler tick interval. |
| `SOUL_MEMCELL_LLM_ENABLED` | Distil each MemCell with an LLM (`DSP_CORTEX` scope) instead of storing the conversation text verbatim: recall returns paraphrased knowledge with `subject\|predicate\|object` facts, and a corrected statement says so in its own trace. Default on; the rule-based extractor is the fallback on any failure. |
| `SOUL_CURATOR_MIN_AGE_HOURS` | Grace period (default 168 = one week) during which a freshly compiled memory is never removed by the curator's low-salience pass. Without it a calm new cell scores 0.2 against a 0.4 threshold and is deleted the night it is compiled. Set 0 to restore the old behaviour. |
| `SOUL_TEMPORAL_INJECT_LIMIT` | Maximum active situational notes rendered into one prompt (default 8), ranked by priority then confidence and deduplicated by subject tokens. The store keeps every note; this only bounds what the model sees. |
| `SOUL_REPOSITORY_BACKEND` | Persistence backend (`memory` or `postgres`). |
| `SOUL_POSTGRES_DSN` | PostgreSQL DSN when the backend is `postgres`. |
