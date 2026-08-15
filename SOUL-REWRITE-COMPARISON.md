# SOUL Rewrite — Local Pipeline vs. Plan Comparison

> Analysis date: 2026-08-05
> Status: **Analysis only — no code was changed.**
> Scope: Compares the currently running local pipeline against the design in
> `SOUL-REWRITE-TASK.md` (v3.0, 2026-04-17), with evidence from the live
> PostgreSQL store, runtime config, and a real (non-recon) Langfuse chat trace.

---

## 1. What is actually running

- **Runtime DB is PostgreSQL** (`soul@192.168.1.13:5432/soul`), so
  `SoulPlugin._get_repository_backend()` → `postgres` → `PostgresSoulRepository`.
  Persistence is real and live.
- **SOUL plugin is enabled** (global plugin toggle `PLUGIN_ENABLED__soul_plugin`), `SOUL_COMPILE_IDLE_SECONDS=300`,
  `SOUL_SCHEDULER_INTERVAL_SECONDS=60`.
- **Compiler is producing data** (live store counts):

  | Table | Rows |
  |-------|------|
  | `mem_cells` | 22 |
  | `mem_cell_vectors` | 22 |
  | `mem_scenes` | 418 |
  | `kg_triples` | 427 |
  | `dsp_extractions` | 105 |
  | `dsp_versions` | 46 (active DSP exists, `<user_profile>` wrapped) |
  | `foresight_signals` | 17 |

- Langfuse traces show `post_session_compile` and `async_consolidate` firing on
  the 30-minute schedule (e.g. 18:15 and 15:25 on 2026-08-05).

So the **write side** — store, compile, consolidate, DSP build, curator, foresight —
is largely built out and running against Postgres + pgvector, matching the plan's
backend decision.

### Runtime config snapshot (relevant keys)

| Key | Value |
|-----|-------|
| `BASE_CORTEX` | `Venice2` |
| `GRILLO_CORTEX` | `Venice2` |
| `PROJECT_DEFAULT_LANGUAGE` | `en` |
| `ENABLE_MEMORIES` | `1` |
| `ENABLE_RECON` | `false` |
| `SOUL_COMPILE_IDLE_SECONDS` | `300` |
| `SOUL_SCHEDULER_INTERVAL_SECONDS` | `60` |

---

## 2. The critical gap — the Context Tower (prompt assembly)

The plan (Part 3) specifies a **layered Context Tower**:

> Tool schemas → System → **DSP (User, `<user_profile>`)** → **Session State
> (foresight + session-start emotion)** → **Layered Memory** → day-long info →
> **Cross-interface content** → **Active context** with a **per-turn
> `{"e": {...}}` mood delta** prepended to each User message.

The **last non-recon user-chat Langfuse trace** (`a39d6406`, Telegram 18:10,
"*Yeah i see that boo, what did you do while i was out?*") shows the actual
assembly:

1. System: persona + JSON instructions + action catalog (`9671` tool chars, 20 tools)
2. `[SYSTEM: REALITY ANCHOR]`
3. `[Recent context from other conversations]` (cross-interface — **present**)
4. `[Memory honesty notice]` + `[Relevant memories]` (SOUL recalled memories —
   **present**)
5. Active conversation turns

**Missing from the live trace:**

- ❌ **No `<user_profile>` / DSP block** — even though the DB has an active DSP.
- ❌ **No `<session_state>` / foresight** — even though 17 foresight signals exist.
- ❌ **No per-turn `{"e": {"joy":..,"fear":..,"sad":..,"anger":..}}` mood delta** on the user message.
- ❌ **No Layered Memory** (daily/weekly/monthly summaries; the "9/7 rule").

### Root cause (code)

`SoulPlugin.get_static_injection()` correctly **computes** all four blocks and
returns keys `soul_user_profile`, `soul_session_state`, `soul_turn_emotion_delta`,
`soul_active_foresight` — see `plugins/soul_plugin/soul_plugin.py:293-308`.

But in `core/prompt_engine.py`:

- **Line 1838:** only `soul_recalled_memories` is popped and merged into
  `context_section["memories"]`.
- **Line 1843:** the **remaining `soul_*` keys** are lumped into
  `context_section.update(injections)`.
- The renderer `_build_context_summary()` (`core/prompt_engine.py:700-866`) only
  emits: `persona_preferences`, `self_growth`, `history_recent`, `thoughts`,
  `memories`, `participants`. **It never reads `soul_user_profile`,
  `soul_session_state`, `soul_turn_emotion_delta`, or `soul_active_foresight`.**

So the DSP, session state, foresight, and the 4-dim emotion delta are computed,
persisted, and passed up — then **silently dropped at render**. A repo-wide grep
confirms these keys are referenced *only* in the plugin and its tests, never in
any renderer.

**This is the integration point the plan's Part 3/Part 4 described, and it isn't wired.**

---

## 3. Second gap — two parallel emotion systems

The plan's emotion engine is the **4-dim leaky integrator**
(`{"e":{"joy","fear","sad","anger"}}`, `core/soul/emotion_engine.py`). The **live
trace shows the legacy system**, not SOUL's:

- The `[lang:... | emotions:neutral (5.0 - moderate), relaxed (1.0 - trace),
  devotion...]` prefix and the emitted `update_emotion_state` action (with a
  Korean label `쑥스러움`) come from `plugins/emotion_manager/emotion_manager.py`
  (named-emotion set: happy / sad / love / etc. + intensity), not from
  `core/soul/emotion_engine.py`.
- `core/prompt_engine.py:3461-3465` even explicitly **discards** the `emotion_state`
  injection key and prefers `current_emotions_nl` from the legacy manager.
- `emotion_state` table has 11 rows; `emotion_diary` has 9827 — the legacy engine
  fully owns the runtime emotion path.

Meanwhile SOUL's `EmotionalEngine` (leaky integrator, mood-congruent boost) is
instantiated and *does* drive `soul_recalled_memories` mood-congruent ranking and
the `soul_turn_emotion_delta`, but that delta never surfaces. So memory recall
honors SOUL emotioning, while the **visible / LLM-facing emotion is the separate
legacy system** — they don't share a single feedback loop as the plan intended.

---

## 4. Third gap — embedding model & extraction quality

| Concern | Plan | Current code |
|---------|------|--------------|
| Embedding model | `nomic-embed-text-v2` (multilingual, 768d) | `BAAI/bge-base-en-v1.5` via fastembed, falling back to `NoopEmbedder` |
| MemCell extraction | Per-day **LLM** extraction, Pydantic schemas + retry | `RuleBasedMemCellExtractor` (regex) |
| DSP extraction | `DSP_EXTRACTOR` prompt, Pydantic | `RuleBasedDspExtractor` (regex) |
| Summary building | named summariser prompts | `RuleBasedSummaryBuilder` (concatenation) |

- Code: `_build_embedder()` in `plugins/soul_plugin/soul_plugin.py`; extractors in
  `core/soul/strategies.py`.
- The plan's **"Output format enforcement: Pydantic + retry"** is not present.
- Only 22 memcells exist after ~105 DSP extractions — the rule-based (vs. LLM)
  extractor is thin relative to conversation volume (as of the analysis date).

> These are noted as **differences**, not necessarily defects — a rule-based
> fallback is a reasonable lightweight default. Flagged because they diverge from
> the plan's explicit choices.

---

## 5. Nightly / Layered Memory

- `SoulCompiler.nightly_rollup()` (`core/soul/compiler.py:311`) only performs
  foresight expiration + DSP extraction/bootstrap/update.
- It explicitly **does not implement** the daily/weekly/monthly summary rollups
  (comment: "expected to be implemented by repository-backed analytics queries").
- Therefore the **entire Layered Memory tower + 9/7 rule** from the plan is
  unimplemented, and nothing injects summaries into the prompt.

---

## 6. Summary table

| Plan (SOUL-REWRITE-TASK) | Current local | Status |
|---|---|---|
| Postgres + pgvector store | Postgres `soul` DB, pgvector HNSW | ✅ live |
| Post-session compile / consolidate / curator / Langfuse | Present, scheduled, traced | ✅ built |
| nomic-embed-text-v2 embedding | bge-base-en-v1.5 / NoopEmbedder | ⚠️ different |
| Pydantic LLM MemCell extraction | Rule-based regex extractor | ⚠️ not as planned |
| **DSP `<user_profile>` injected (User role)** | Computed by plugin, **dropped at render** | ❌ not in prompt |
| **Session State: foresight + session-start emotion** | Computed, **dropped at render** | ❌ not in prompt |
| **Per-turn `{"e":{...}}` mood delta** | Computed, **dropped at render**; legacy emotions used instead | ❌ not in prompt |
| **Layered Memory (daily/weekly/monthly, 9/7)** | Not implemented | ❌ absent |
| Cross-interface content layer | Present in trace | ✅ works |
| Emotion = 4-dim leaky integrator | Legacy named-emotion system drives prompt; SOUL engine runs recall-side only | ❌ split |

---

## 7. Bottom line

The entire **write side** (store, compile, consolidate, DSP build, curator,
foresight) is built and running on Postgres — but the **read side** (Context
Tower assembly) was never connected. The DSP/session-state/emotion-delta that
`SoulPlugin` produces are silently discarded by `_build_context_summary`, so a
real user turn gets only persona + action catalog + cross-chat + SOUL-recalled
memories. The emotion that actually reaches the model is the separate legacy
engine.

That is why it "feels off": a lot was built, but the main integration point the
plan described (injecting DSP, session state, and per-turn emotion into the
Context Tower) isn't wired.

---

## 8. Key files

| File | Role |
|------|------|
| `core/soul/compiler.py` | Store / compile / consolidate / rollup / curator |
| `core/soul/repository.py` | `InMemorySoulRepository` / `PostgresSoulRepository` |
| `core/soul/emotion_engine.py` | 4-dim leaky integrator (plan's engine) |
| `core/soul/strategies.py` | Rule-based extractors (current, not LLM/Pydantic) |
| `core/soul/models.py` | MemCell / DSP / Emotion dataclasses |
| `plugins/soul_plugin/soul_plugin.py` | Runtime plugin; **computes** `soul_*` context blocks |
| `core/prompt_engine.py:1838-1843` | Only `soul_recalled_memories` merged; rest added but unrendered |
| `core/prompt_engine.py:700-866` | `_build_context_summary` — **the renderer that drops the SOUL blocks** |
| `plugins/emotion_manager/emotion_manager.py` | Legacy named-emotion engine that actually drives the prompt |

---

## 9. Possible next steps (not yet approved / not done)

- Wire the missing injector blocks into `_build_context_summary`: DSP
  (`soul_user_profile`), Session State (`soul_session_state`), and per-turn mood
  delta (`soul_turn_emotion_delta`) — plus surface `soul_active_foresight`.
- Reconcile the two emotion systems (legacy `emotion_manager` vs. SOUL's 4-dim
  engine) so the LLM-facing emotion and memory-recall emotion share one loop.
- Consider implementing the Layered Memory daily/weekly/monthly rollups + 9/7 rule.
- Optionally align embedding model and LLM/Pydantic extraction with the plan.

> No code, config, or data was changed while producing this document.
