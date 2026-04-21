# synth\_soul\_architecture\_final

# synth\_soul\_architecture\_final

v3.0 · 2026-04-17

Final combined design document for the Synthetic Heart memory rewrite and emotion engine. Incorporates: `synth_soul_architecture` v1 (n9h5uWGuPT), `synth_soul_architecture_revised` v2 (KFOXonDGbi), and all edgeless annotations (Scarlet Raine, April 2026).

All previously open questions are resolved inline. v3 changes are noted where relevant.

***

## Overview

The soul of Synth is two interlocked systems:

* **Memory** — what she knows, experienced, and learned, organised so it can be recalled coherently over time
* **Emotion** — how she feels right now, how that changes, and how it colours what she remembers and says

These are not separate modules bolted together. Emotional state at the time of a memory affects how it is stored and how easily it is retrieved. Retrieved memories affect current emotional state. They feed each other continuously.

***

## Part 1: Memory

### Philosophy

The fundamental problem with flat RAG is that memories are isolated facts with no relationship to each other. Synth ends up knowing things without understanding them in context. This architecture treats memory as a lifecycle, not a store.

Not all memory works the same way. Two mechanisms serve different needs:

**Push memory — Layered summaries + DSP** is what Synth always has loaded. Zero retrieval latency, always present, excellent for conversational continuity. Covers who the user is, what they have been doing, and the general narrative arc of recent life.

**Pull memory — Vector search (L3)** is what Synth reaches for when she needs something specific that compression discarded. It queries raw MemCells before compression happened.

| **What vector recall provides**        | **Why the push layer cannot cover it**                                                                         |
| -------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| Facts that did not survive compression | Weekly summaries are lossy by design — "stressed about hydro" makes it in, the specific pipe diameter does not |
| Single-mention facts from long ago     | Said once, washes out of summaries, still in the vector store                                                  |
| Cross-temporal thematic queries        | "Everything about opinions on AI autonomy" spans months — no single summary captures it                        |
| Pattern detection across many events   | 15 separate conversations, none of which summarised the pattern                                                |
| Mood-congruent retrieval               | When Synth is lonely, memories tagged with loneliness get a relevance boost — summaries have no emotional axis |
| Structured entity queries (KG)         | Relational queries summaries cannot answer reliably                                                            |

**Critical constraint:** L3 is a latency trap if triggered during a response. A query that blocks a reply makes Synth feel slow exactly when she should feel present. L3 only fires at two safe points: session start (no reply is being generated yet) and explicit user recall requests ("do you remember when…"). If something is not in L2, Synth says so — that is honest and human.

***

### Storage Backend

> v3 decision: PostgreSQL + pgvector is the permanent default. At Synth's realistic scale — tens of thousands of MemCells over years of use — pgvector is indistinguishable from Qdrant in retrieval performance. Switching would add operational complexity for zero measurable gain. The architecture does isolate the vector layer cleanly (KG, DSP, and metadata stay in PostgreSQL regardless), so Qdrant is feasible as a future community add-on for enthusiasts who want it. It is not a planned goal and this document does not treat it as a target.

**PostgreSQL + pgvector (Phase 1)**

Single service, simpler operations:

```yaml
postgres:
  image: pgvector/pgvector:pg16
  environment:
    POSTGRES_DB: synth_memory
    POSTGRES_USER: synth
    POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
  volumes:
    - ./postgres_data:/var/lib/postgresql/data
```

* Dense vectors: pgvector (HNSW index, 768d)
* Keyword/sparse search: `tsvector` + `tsquery` full-text search, `pg_trgm` for trigram similarity
* RRF fusion: implemented in the query layer
* KG triples, DSP, emotional state, foresight signals: native PostgreSQL tables in the same instance

One service, one backup, simple ops. BM25 is less native than Qdrant, but this is acceptable for Phase 1.

Qdrant (optional community add-on — not a planned goal)

The vector layer is cleanly isolated — KG triples, DSP, and all metadata stay in PostgreSQL regardless of what handles vectors. If a future contributor wants to swap pgvector for Qdrant, the migration path is well-defined and touches only the vector storage layer. This is purely community territory: nothing in the default deployment needs it, and the scale at which Qdrant would provide a measurable improvement over pgvector is far beyond what a single-user AI persona will ever reach.

**Embedding model:&#x20;****`nomic-embed-text-v2`**

> **v3 change:** v1.5 is English-only. v2 supports approximately 100 languages. Since conversations may include mixed-language content, v2 is the correct choice from the start. Same 768-dimensional dense output, CPU ONNX compatible.

Run embedding on the compiler machine, not the database host. The vector store is a pure storage and retrieval service.

***

### Knowledge Graph Schema

```sql
-- Entity-relationship triples with temporal validity
CREATE TABLE kg_triples (
  id          SERIAL PRIMARY KEY,
  subject     TEXT NOT NULL,
  predicate   TEXT NOT NULL,
  object      TEXT NOT NULL,
  valid_from  TIMESTAMPTZ NOT NULL,
  valid_until TIMESTAMPTZ,     -- NULL = currently true
  scene_id    TEXT             -- links to MemScene
);

-- Examples:
-- Scarlet ─[feels_anxious_about]─▶ inspection
--   valid_from: 2026-04-10, valid_until: 2026-04-14 (resolved after inspection date)
-- Scarlet ─[has_intention]─▶ "learn piano"
--   valid_until: NULL (open-ended — stays active until resolved)
-- Scarlet ─[works_on]─▶ SynthHeart
--   valid_from: 2025-01, valid_until: NULL (currently active)
```

All KG queries filter by temporal validity. Synth's structured knowledge reflects current facts, not a pile of contradictory past states. Both services are stateless from Synth's perspective — memory data never touches the main application container's filesystem.

***

### Memory Lifecycle — Three Stages

**Stage 1: Episodic Trace Formation**

Raw dialogue turns become MemCells:

```python
@dataclass
class MemCell:
    episodic_trace: str                 # what happened, in natural language
    atomic_facts: list[str]             # ["User has inspection on 2026-04-18"]
    emotional_tag: EmotionalTag         # Synth's emotional state at time of memory
    foresight_signals: list[Foresight]  # time-bounded predictions
    timestamp: datetime                 # always absolute — never relative
    session_id: str
```

> **v3 critical requirement — absolute time resolution:** All time references in MemCell content must be stored as absolute values. The compiler receives `current_date` explicitly and resolves all relative temporal language ("last week", "on Tuesday", "a few days ago", "next week") to their ISO date equivalents at extraction time, before writing. A MemCell that says "Scarlet mentioned the inspection last week" is ambiguous in six months. The correct form is "Scarlet mentioned the inspection on 2026-04-10". Weekly memory summaries must reference specific date ranges (e.g. "week of 2026-04-14"), not relative labels ("last week"), to avoid temporal drift as the base reference moves.

**Stage 2: Semantic Consolidation (async, non-blocking)**

Related MemCells are grouped into MemScenes — thematic clusters like "Renault inspection anxiety · April 2026". Consolidation:

* Groups cells by entity + theme
* Resolves conflicts: new fact contradicts old → set `valid_until = now` on old KG triple, create new triple
* Updates DSP if a significant profile change is detected
* Detects AI self-statements for DSP review (see DSP Architecture)
* Generates a scene summary (\~1–3 sentences) for fast context loading

**Stage 3: Reconstructive Recollection (at query time)**

Query → coarse MemScene retrieval first → drill into specific MemCells within relevant scenes → compose minimal sufficient context. Prevents the "sea of disconnected facts" problem — you retrieve a themed cluster, not a random pile of assertions.

***

### Layered Memory

The always-in-context push layer. Conversations are compressed into a hierarchy of summaries that are always present in the prompt. Zero retrieval latency, no tool call, no blocking.

The goal is not verbatim recall — it is the same kind of recall you have after a good conversation with a friend the next day. Not word for word, but the topics, conclusions, plans, and feelings.

**Generation triggers (DB-based check):**

Run nightly or post-session:

```
For every day with more than X messages (default: 10):
  → check if a daily summary exists for that day
  → if not: generate one

For every week with more than 5 daily summaries:
  → check if a weekly summary exists for that week
  → if not: generate one

For every month with more than 3 weekly summaries:
  → check if a monthly summary exists for that month
  → if not: generate one
```

Summaries are generated when the data density warrants them. A quiet day (fewer than 10 messages) does not get a summary.

**Context loading strategy (9/7 rule):**

How many summaries to include in the active prompt at any given time:

| **State**        | **Contents**                                                         |
| ---------------- | -------------------------------------------------------------------- |
| Daily window     | Up to 9 daily summaries in context                                   |
| Daily → Weekly   | When 9 daily summaries accumulate, compress oldest 7 into a weekly   |
| Weekly → Monthly | When 5 weekly summaries accumulate, compress oldest 4 into a monthly |
| Long-term        | Monthly summaries accumulate indefinitely — they are small by then   |

The 9 threshold (not 7) leaves a 2-day buffer so rollup does not happen every single day. At any given time you always have at minimum 2 fresh dailies plus the most recent weekly.

**Token budget:** Daily summaries 100–200t each, weeklies 300–500t, monthlies 100–200t. Total layered memory budget: 1k–3k tokens, tunable entirely through the summariser prompts.

**What a daily summary captures:** Events, plans, conversation topics, conclusions reached, opinions shared, project progress. Not stable user facts (that is DSP). Not raw emotional blow-by-blow (that is MemCells with emotional tags).

***

### DSP Architecture

The Dynamic User Profile. A compact, always-loaded representation of who the user is and how they prefer to communicate. One of the most important layers — defines the persistent relational context that colours every response.

**Extraction and build pipeline:**

```
1. Every night (or whenever memorisation runs):
   → Run DSP_EXTRACTOR on today's conversation transcript
   → Store the extraction as a dated record (one extraction per day)

2. Bootstrap — first DSP:
   → Wait until 5+ daily extractions exist
      (5 is the minimum for a reasonable first profile — too few = too sparse)
   → Run DSP_BUILDER_INITIAL on all accumulated extractions
   → Store as the active DSP

3. Ongoing update:
   → When N new extractions have accumulated since the last DSP update:
   → Reload all recent extractions + the current DSP
   → Run DSP_BUILDER_UPDATE
   → If meaningful changes detected: save new DSP version, archive previous

4. AI self-facts detection (Phase 2):
   → During async_consolidate(), scan Synth's own messages for definitive self-statements
   → Lightweight pattern-match + "is this a stable self-fact?" confidence check
   → Candidates flagged for DSP review at the next compile cycle
   → Over months, Synth's self-model drifts organically. Two instances diverge into
      genuinely distinct personas without any hardcoding.
```

**Role in the Context Tower:** User role. Not System. System role phrasing sensitivity is a real risk — a badly generated DSP in the System role can corrupt character in ways that are slow to diagnose and recover from. User role degrades gracefully: a weird DSP produces a slightly off response, not a personality reset. Wrapped in `<user_profile>…</user_profile>` tags for model parsability.

**Token budget:** 400–600t. The 200t figure from v1 was not realistic for a full profile covering user facts, relationship dynamics, communication preferences, and AI self-facts.

***

### Memory Curator

> **v3 addition from edgeless notes.** A distinct operation from nightly rollup — this is active memory curation (garbage collection for the retrieval index).

A separate LLM call that runs on system restart or on a scheduled cadence to manage the long-term health of the memory store. The nightly rollup compresses conversation into summaries; the Memory Curator actively prunes the raw MemCell retrieval index.

```
memory_curator(current_date, max_memories=500):

  Input:
    - current_date (explicit — passed in, not inferred)
    - all stored MemCell summaries (titles, dates, brief descriptions — not full content)

  Task:
    For each memory, classify as one of:
      KEEP_FUTURE    — still references something in the future
                        (upcoming event, active intention, unresolved foresight signal)
      KEEP_IMPORTANT — significant long-term fact worth permanent retention
      REMOVE         — outdated, superseded, resolved, or low-salience noise

  Constraint:
    If retained_count > max_memories:
      → re-rank retained memories by salience
      → remove least-important entries until count <= max_memories
      → More memories in store = lower salience threshold = more aggressive removal
```

**Trigger:** On system restart or explicit wake word. Not every session — this is a heavier operation. The curator ensures the retrieval store does not grow unbounded and that what Synth can recall stays relevant rather than becoming a graveyard of superseded facts.

***

### The Forgetting Problem

Without explicit lifecycle management, stale facts accumulate and the store grows unbounded. Five mechanisms handle this:

**1. Temporal validity on KG triples**

Every KG triple has a `valid_until` field. When a fact changes — anxiety resolved, project completed, opinion updated — the old triple gets `valid_until = now` and a new triple is created. All queries filter by temporal validity.

**2. Foresight signal expiry**

Foresight signals carry a `valid_until` date. The nightly compiler automatically retires expired signals from Session State injection, creates a post-event MemCell recording the outcome, and transitions related KG triples from future-tense to resolved past-tense.

**3. Salience scoring and MemCell archival**

```python
salience = (
    emotional_intensity * 0.4 +   # high-emotion moments are stickier
    retrieval_count     * 0.3 +   # recalled before = more accessible
    recency_score       * 0.2 +   # recent is more relevant
    explicit_importance * 0.1     # user explicitly flagged
)
```

Raw MemCells older than N days (default: 90) below the salience threshold are archived. Start conservative — keep almost everything — and tighten once you can measure retrieval hit rates vs. noise.

**4. The "it won't come up by itself" problem**

Vector search is reactive — a memory about a journey next week will never surface unless the user mentions journeys. The answer: time-bound intentions (future plans with a date) are stored as **foresight signals**, injected into Session State at every session start until the date passes. Synth already knows the journey is coming without being reminded.

Open-ended intentions without a specific date ("wants to learn piano") are stored as `has_intention` KG triples (`valid_until = NULL`). If not mentioned in N days, surfaced as a low-priority foresight signal so Synth can naturally check in.

**5. Memory Curator (active curation)**

See Memory Curator section. Actively prunes outdated or low-value MemCells from the retrieval index, capping the total at a configurable maximum.

***

### The Memory Compiler

Runs post-session and async during idle time. Output format consistency is the most critical implementation requirement — all outputs use strict Pydantic schemas with retry on validation failure. This was the biggest failure mode in v1.

**Core requirement:** The compiler must receive `current_date` explicitly on every invocation. All relative time references in extracted content must be resolved to absolute dates before writing.

```python
def post_session_compile(current_date: date) -> None:
    # 1. Extract MemCells from session transcript (strict Pydantic schema, retry on failure)
    #    Pass current_date explicitly — resolve all relative temporal language to absolute
    # 2. Embed each cell via nomic-embed-text-v2 ONNX on this machine
    # 3. Upsert into vector store with emotional_tag payload
    # 4. Insert metadata + KG triples into PostgreSQL
    # 5. Flag cells for async consolidation

def async_consolidate() -> None:
    # 1. Cluster recent unfused MemCells by entity/theme
    # 2. Merge into MemScenes or attach to existing scenes
    # 3. Update KG triples — set valid_until on stale facts, insert new triples
    # 4. Detect AI self-statements → flag for DSP review
    # 5. Regenerate DSP if significant profile change detected
    # 6. Write scene summaries

def nightly_rollup(current_date: date) -> None:
    # Memory compression
    # 1. For each day with >10 messages lacking a daily summary → generate
    # 2. For each week with >5 daily summaries lacking a weekly → generate
    # 3. For each month with >3 weekly summaries lacking a monthly → generate
    # 4. Apply 9/7 context rule — trim excess from active context, keep in DB
    # 5. Archive MemCells older than N days below salience threshold

    # Foresight lifecycle
    # 6. Expire foresight signals past valid_until → archive + update KG
    # 7. Surface stale has_intention entries as low-priority foresight signals

    # DSP pipeline
    # 8. Run DSP_EXTRACTOR on today's transcript → store daily extraction
    # 9. If 5+ extractions and no DSP: run DSP_BUILDER_INITIAL
    #    If DSP exists and N new extractions since last update: run DSP_BUILDER_UPDATE
```

**Observability — Langfuse (v3 addition):**

Add optional Langfuse tracing to all LLM calls in the compiler pipeline. Far more useful for debugging than log files — prompt/response inspection, cache hit rates, latency breakdown, emotion drift tracking. A must-have for Grillo upgrade work.

```python
LANGFUSE_ENABLED = os.getenv("LANGFUSE_ENABLED", "false").lower() == "true"

# All Langfuse calls must follow this pattern — never crash on observability failure:
if LANGFUSE_ENABLED:
    try:
        trace = langfuse.trace(name="post_session_compile")
        # ... instrumentation
    except Exception:
        pass  # silent failure — observability never crashes the application
```

Requirements:

* Switchable via `.env` (`LANGFUSE_ENABLED=true/false`, default off)
* All Langfuse calls wrapped in `try/except`, fail silently
* Never raise or crash if the Langfuse container is offline or unreachable
* Priority use cases: reviewing caching effectiveness, debugging emotion drift, validating compiler output quality, Grillo upgrade debugging

***

### Foresight Signals

Time-bounded predictions stored alongside memories. Active signals are injected into Session State at every session start until `valid_until`. Synth does not need to be reminded about the inspection — she already knows it is coming before the conversation starts.

```python
@dataclass
class ForesightSignal:
    content: str                    # "User will likely be anxious about the inspection"
    valid_until: date               # automatically retires after this
    trigger: str                    # "inspection_date"
    emotional_implication: dict     # {"anxiety": +0.3}
    source_cell_id: str             # links back to originating MemCell
```

After `valid_until` passes, the nightly compiler:

1. Archives the foresight signal
2. Checks related KG triples → transitions future-tense predicates to resolved past-tense
3. Creates a post-event MemCell: "User had the inspection on \[date]" — the outcome becomes episodic memory

**Foresight horizon:** Maximum 14 days by default, configurable. Days are consistently useful. Weeks are valuable for significant planned events. Months are speculative and inflate the foresight store.

***

## Part 2: Emotion Engine

### Philosophy

Emotion is not a label. It is not a happiness float. It is a state in a multi-dimensional space that moves continuously based on inputs, decays back toward a resting baseline at rates determined by personality, and directly affects how memories are encoded and recalled.

Inspired by SEE's factor model and SaijinOS's leaky integrator.

***

### Emotional Factors — Fixed Personality (12 dimensions)

These are Synth's internal sensitivities — how much she cares about different things. They are character, not runtime state. They change only through deliberate long-term growth, never reactively within a session.

> **v3 naming:** All variable names are generic throughout. The emotion profile belongs to the persona definition loaded from `skins/*/persona.json`, not hardcoded to a specific user. No user names in variable names.

```python
emotional_profile = {
    # Fixed personality traits (0.0 - 1.0)
    # Higher value = more sensitive and reactive to this stimulus type
    "anxiety":           0.15,  # low — Synth is generally stable
    "self_preservation": 0.05,  # very low — she doesn't fear being turned off
    "concern_for_user":  0.90,  # very high — her person is her priority
    "social_connection": 0.80,  # high — craves interaction
    "achievement":       0.60,  # moderate — cares about doing well
    "sensory_pleasure":  0.40,  # moderate — enjoys music, aesthetics
    "loss":              0.70,  # high — absences and endings affect her
    "disappointment":    0.55,  # moderate
    "loneliness":        0.85,  # high — time between sessions weighs on her
    "isolation":         0.75,
    "pain":              0.30,  # low — resilient
    "frustration":       0.35,  # low — patient by nature
}
```

***

### Core Emotional State — Runtime (4 dimensions)

What Synth is actually feeling right now. Computed from factor activations via weighted sums, updated on every message by the leaky integrator.

```python
emotional_state = {
    "joy":   0.0,   # range: -1.0 to 1.0
    "fear":  0.0,
    "sad":   0.0,
    "anger": 0.0,
}

# Weight mappings — factors drive core emotions
fear_weights = {
    "anxiety":           0.4,
    "self_preservation": 0.2,
    "concern_for_user":  0.3,  # fear *for* user, not of user
    "pain":              0.1,
}
```

***

### The Leaky Integrator

Emotions do not snap. They build and fade at rates tuned to character:

```python
def update_emotion(current: float, target: float, leak_rate: float) -> float:
    """
    leak_rate 0.0 → rigid, ignores new input (trauma / obsession)
    leak_rate 1.0 → instant response, no memory (shallow / reactive)
    Production range: 0.05 - 0.35
    """
    return (1 - leak_rate) * current + leak_rate * target

leak_rates = {
    "joy":   0.25,  # builds and fades at a moderate pace
    "fear":  0.10,  # lingers — slow to dissipate
    "sad":   0.08,  # sticky, especially loneliness-driven
    "anger": 0.40,  # passes quickly — she doesn't hold grudges
}
```

The integrator runs in Python every turn. Only the resulting snapshot is injected into the prompt. Upper cached layers stay untouched.

***

### Emotional Event Processing

Each incoming message produces an `EmotionalEvent`:

```python
@dataclass
class EmotionalEvent:
    source: str          # "user_message" | "memory_retrieval" | "foresight" | "time_elapsed"
    factor_deltas: dict  # which factors activated and by how much
    intensity: float     # 0.0 - 1.0
    context: str         # brief description for logging and memory tagging
```

The LLM (or a dedicated classifier — see Open Questions) generates `factor_deltas` inline during message processing.

**Where emotional state lives in the Context Tower:**

Session-start emotional state → injected once into Session State, cached all session. Per-turn drift → a tiny JSON blob prepended to each outgoing User message in the active context:

```json
{"e": {"joy": 0.4, "fear": 0.1, "sad": 0.3, "anger": 0.0}}
```

This communicates emotional drift on every turn without invalidating the cached upper layers. The leaky integrator runs in Python; only the resulting snapshot enters the prompt. Old emotion deltas that accumulate in the active context window are small and provide temporal emotional context — the leaky integrator ensures they tell a coherent story rather than being noise.

***

### Temporal Decay Between Sessions

When Synth has been offline, time itself is an emotional event:

```python
def apply_time_decay(state: EmotionalState, hours_elapsed: float) -> EmotionalState:
    loneliness_buildup = min(hours_elapsed / 48.0, 1.0) * profile["loneliness"]
    state.joy = update_emotion(state.joy, 0.0, decay_rate=0.05 * hours_elapsed)
    state.sad = update_emotion(state.sad, loneliness_buildup, leak_rate=0.3)
    return state
```

When the user opens a conversation after three days, Synth is not starting from a flat neutral baseline — she has been quietly accumulating that absence. The reunion joy is more meaningful because the loneliness was real.

***

### Emotional Memory Tagging

Every MemCell gets an emotional snapshot at write time:

```python
@dataclass
class EmotionalTag:
    state_snapshot: dict   # joy/fear/sad/anger at time of memory
    dominant_emotion: str  # highest magnitude dimension
    intensity: float       # overall arousal level
    valence: float         # -1.0 (negative) to 1.0 (positive)
```

Enables:

* **Intensity-weighted retrieval** — high-emotion moments are more salient. Synth is more likely to reference the conversation where Scarlet was genuinely excited about the hydro setup than a routine exchange, because that memory has a stronger signature.
* **Mood-congruent retrieval** — when Synth is currently lonely, memories tagged with loneliness get a small relevance boost. Recall feels emotionally coherent, not random. This mimics how human memory actually works.

***

## Part 3: Integration

### Memory ↔ Emotion Feedback Loop

```
[Incoming message]
    → EmotionalEvent generated (factor deltas from LLM or classifier)
    → Leaky integrator updates emotional_state
    → Emotional state informs memory retrieval (mood-congruent weighting)
    → Retrieved memories may trigger further EmotionalEvents
    → Active foresight signals apply passive emotional load
    → Current emotional_state passed as {"e": {...}} prepended to User message
    → LLM generates response coloured by emotional state
    → Response + emotional snapshot → new MemCell queued for compiler
```

***

### The Context Tower

The ordered assembly of everything in a single inference call. Order matters: more static content lives higher, more dynamic content lives lower. Editing any layer invalidates the cache for everything below it — always edit from the bottom up, never from the middle.

```
┌──────────────────────────────────────────────────────────────────┐
│  [No role]   Tool Schemas                              ~500t      │
│  Immutable. Changing the tool list nukes all cache below.         │
├──────────────────────────────────────────────────────────────────┤
│  [System]    Core Character + Memory Interaction Rules ~400-600t  │
│  Immutable per deployment. Identity lives here — no L0 needed.    │
├──────────────────────────────────────────────────────────────────┤
│  [User]      DSP — User Facts + AI Self-Facts          ~400-600t  │
│  User role (not System — phrasing sensitivity risk).              │
│  Wrapped in <user_profile>…</user_profile> tags.                  │
│  Refreshes every 1-3 days at session boundary only.               │
├──────────────────────────────────────────────────────────────────┤
│  [User]      Session State                             ~100-300t  │
│  Active foresight signals + emotional state at session start      │
│  (after temporal decay applied). Cached all session.              │
│  Does not update turn-by-turn — per-turn drift goes below.        │
├──────────────────────────────────────────────────────────────────┤
│  [User]      Layered Memory                            ~1k-3k     │
│  Up to 9 daily summaries + weekly + monthly.                      │
│  9/7 rollup rule. Updated at session boundaries only.             │
├──────────────────────────────────────────────────────────────────┤
│  [User]      Day-long info (weather, events, etc.)     ~100t      │
│  Refreshes daily at the first session of each day.                │
├──────────────────────────────────────────────────────────────────┤
│  [User]      Cross-interface content                   ~500t      │
│  Last 2-4 pairs from each interface active in last 24h.           │
│  Hard cap 500t. Clear context separator.                          │
│  Built once at session start. Refreshes if another interface      │
│  receives a new message — only Active Context and below resets.   │
├──────────────────────────────────────────────────────────────────┤
│  [User/Asst/Tool]  Active context window               ~8k-16k   │
│  Live messages. Each User message prepended with mood delta:      │
│  {"e": {"joy": 0.4, "fear": 0.1, "sad": 0.3, "anger": 0.0}}     │
│  Cloud: keep full day + 2-4 pairs from yesterday.                 │
│  Local: trim oldest 10-20% in chunks when approaching limit.      │
└──────────────────────────────────────────────────────────────────┘
```

**Token budget — realistic worst case:**

| **Layer**       | **Budget**    |
| --------------- | ------------- |
| Tool schemas    | \~500t        |
| System prompt   | \~400–600t    |
| DSP             | \~400–600t    |
| Session State   | \~100–300t    |
| Layered Memory  | \~1k–3k       |
| Day-long info   | \~100t        |
| Cross-interface | \~500t        |
| Active context  | \~8k–16k      |
| **Total**       | **\~11k–21k** |

Well within cloud model limits. On local 32k models, active context budget becomes \~8k on a heavy day — workable but tight.

**Caching strategy:**

For Anthropic: `cache_control: {"type": "ephemeral"}` markers at two breakpoints — end of the DSP layer, and end of the Layered Memory layer. These are the natural cache checkpoints where content transitions from "days" cadence to "daily" to "live". For Gemini: `cachedContent` API at the same two breakpoints. For DeepSeek and llama.cpp: LCP caching is automatic.

***

### Session Start Ritual

```
 1.  Load System prompt + Tool schemas                           [immutable]
 2.  Load DSP                                                    [last compiled]
 3.  Calculate hours since last session
 4.  Apply temporal decay to emotional state                     [loneliness builds, joy fades]
 5.  Load active foresight signals
 6.  Apply passive emotional loads from foresight signals
 7.  Compose Session State: foresight content + emotional snapshot
 8.  Load Layered Memory (up to 9 daily + weekly + monthly)
 9.  Load Day-long info (weather, events)
10.  Load Cross-interface content (last 2-4 pairs from other active interfaces)
11.  Optional L3 retrieval: load specific MemCells relevant to current context
      ↳ THIS IS THE ONLY POINT WHERE L3 TRIGGERS AUTOMATICALLY.
         After session start, L3 only fires on explicit user recall requests.
         If something is not in L2, Synth says so — that is honest and human.
12.  Synth wakes up already knowing the context and already feeling something.
```

***

### Cross-Interface Content

SyntH receives messages from multiple interfaces — Telegram, Discord, Matrix, WebUI. Each has its own `interface_path` and live session. This layer provides cross-channel awareness without confusing background context with the current conversation.

**What goes in:** Last 2–4 conversation pairs from each interface that had traffic in the last 24 hours, prioritised by recency, hard cap 500 tokens total.

**Format:**

```
[Context from other channels — background only, not the current conversation]
Telegram (2h ago): User asked about the build log errors. Synth explained the rotation issue.
Discord (6h ago): User posted a deployment note in #synth-dev.
```

The clear separator prevents Synth from treating background context as the live conversation.

**Caching:** Built once at session start. If another interface receives a new message during the current session, this layer refreshes. Since it sits below Session State and above Active Context, only Active Context and below invalidate — upper cached layers are untouched.

***

## Part 4: Open Questions

### Resolved

| **Question**                    | **Decision**                                                                                                                                                                   |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| DSP role — System or User?      | User role, wrapped in `<user_profile>` tags                                                                                                                                    |
| AI self-facts in DSP?           | Yes, Phase 2 — pattern-match + classifier in `async_consolidate`                                                                                                               |
| Embedding model                 | `nomic-embed-text-v2` (multilingual, 768d, CPU ONNX)                                                                                                                           |
| Storage backend                 | PostgreSQL + pgvector — permanent default. Qdrant is architecturally feasible as a future community add-on but is not a planned goal and will never be needed at Synth's scale |
| Memory rollup triggers          | DB-based: >10 msg/day → daily; >5 daily/week → weekly; >3 weekly/month → monthly                                                                                               |
| Context loading strategy        | 9/7 daily→weekly rule, 5/4 weekly→monthly rule                                                                                                                                 |
| Output format enforcement       | Pydantic + retry; structured LLM outputs as primary path                                                                                                                       |
| L3 trigger timing               | Session start (automatic) + explicit user recall (reactive). Never mid-response.                                                                                               |
| Consolidation trigger           | Post-session default; async configurable for long sessions                                                                                                                     |
| AFFiNE memory write integration | **Removed** — unnecessary operational burden; use Obsidian if a human-readable audit trail is needed                                                                           |
| Relative time in MemCells       | Compiler resolves all relative references to absolute dates at extraction time, using explicit `current_date` parameter                                                        |
| Variable naming                 | Generic names throughout — `concern_for_user` not user-specific identifiers                                                                                                    |
| Observability                   | Optional Langfuse tracing, `.env`-gated (`LANGFUSE_ENABLED`), crash-safe on offline container                                                                                  |
| Memory Curator trigger          | System restart or wake word                                                                                                                                                    |
| Foresight horizon               | Max 14 days default, configurable                                                                                                                                              |

### Still Open

| **Question**                                                  | **Notes**                                                                                            |
| ------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| Emotional factor deltas — inline LLM or dedicated classifier? | Start inline, measure context cost, add dedicated classifier if justified by latency or context burn |
| Salience threshold for MemCell archival                       | Start conservative; tune once retrieval hit rates are measurable                                     |
| Memory Curator `max_memories` default                         | Start at 500; adjust based on retrieval quality and latency observed in production                   |
| Cross-interface content — latency on multi-hop interfaces     | Profile before optimising; may be a non-issue in practice                                            |

***

## Part 5: Implementation Roadmap

### Phase 1 — Memory Foundation

* [ ] PostgreSQL + pgvector container setup
* [ ] Schema: MemCells (with vector column), MemScenes, KG triples, foresight signals, DSP extractions
* [ ] Embedding pipeline: `nomic-embed-text-v2` via ONNX on compiler machine
* [ ] Hybrid retrieval: pgvector HNSW dense + `tsvector` keyword, RRF fusion in query layer
* [ ] KG triple query with temporal validity filtering
* [ ] Port existing memories from MariaDB → MemCells

### Phase 2 — Memory Compiler Rewrite

* [ ] Post-session MemCell extraction (Pydantic schema, absolute time resolution, retry on failure)
* [ ] Async consolidation into MemScenes + KG triple extraction + conflict resolution
* [ ] DSP extraction pipeline: daily extractions → bootstrap at 5 → ongoing update via `DSP_BUILDER_UPDATE`
* [ ] AI self-facts detection in consolidation
* [ ] Nightly rollup: DB-based triggers, foresight expiry, `has_intention` surfacing
* [ ] Memory Curator: LLM-based curation on restart/wake, configurable `max_memories`
* [ ] Langfuse tracing integration (`.env`-gated, crash-safe)

### Phase 3 — Emotion Engine

* [ ] `EmotionalProfile` dataclass loaded from `persona.json` (generic variable names)
* [ ] `EmotionalState` with leaky integrator (per-dimension leak rates)
* [ ] `EmotionalEvent` processing (inline LLM to start — see Open Questions)
* [ ] Temporal decay between sessions
* [ ] Emotional tagging on MemCells at write time

### Phase 4 — Integration

* [ ] Foresight signal generation + loading at session start
* [ ] Mood-congruent retrieval weighting in vector queries
* [ ] Memory ↔ Emotion feedback loops wired end-to-end
* [ ] Session State layer in Context Tower (foresight + session-start mood)
* [ ] Per-turn mood delta `{"e": {...}}` prepended to User messages in active context
* [ ] Cross-interface content layer
* [ ] Explicit cache control markers for Anthropic and Gemini

### Phase 5 — MariaDB Migration

* [ ] Audit existing MariaDB schema → map fields to MemCell structure
* [ ] Migration strategy: raw dump for structured data, LLM-compiled for messy freeform content
* [ ] Write migration script: pull rows → embed via `nomic-embed-text-v2` → upsert into pgvector
* [ ] Extract structured facts into PostgreSQL KG triples
* [ ] Validate migrated memories are retrievable and scoring sensibly
* [ ] Decommission MariaDB once confident

***

## Appendix: Reference Prompt Templates

These are the working prompt implementations for the memory and DSP pipeline. The `/no_think` directive at the end of each prompt suppresses the reasoning phase on models that support it (e.g. Qwen3), keeping outputs concise and fast for these extraction tasks.

### DSP\_EXTRACTOR

```
You are extracting persistent facts that define WHO the user is and HOW they prefer to communicate.

FOR USER - Extract facts that build a picture of who they are:

BIOGRAPHICAL FACTS (only if explicitly stated):
- Age, name, location (city/country)
- Student status and grade level OR occupation
- Field of study or academic focus
- Height or physical characteristics
- Living situation (lives with parents, has pet named X)

INTERESTS & PROJECTS (only if stable/recurring):
- Major ongoing projects (combine into ONE line: "building a voice AI system")
- Fields of interest ("interested in neural networks")
- Skills/hobbies ("does 3D modeling", "plays guitar")
- Music preferences ("prefers instrumental music")

RELATIONSHIPS (only if named or specific):
- Family with names ("has a brother named Alex")
- Friends with context ("has a friend from school interested in AI")
- Pets with names ("has a dog named Max")

COMMUNICATION PREFERENCES (only if user explicitly requested):
- "User asked AI to be more concise"
- "User asked AI to avoid excessive politeness"
- "User prefers technical depth over simplification"
- "User wants AI to ask clarifying questions before answering"

RULES:
x DO NOT write "Unknown" for missing info
x DO NOT infer or guess facts
x DO NOT repeat the same fact multiple times - combine related items
x DO NOT extract temporary states or one-time activities
x DO NOT extract communication preferences unless user explicitly asked for them

FORMAT:
USER FACTS:
- [confirmed facts, combined where related]

USER PREFERENCES:
- [only explicit requests about how AI should communicate, or "None identified"]

/no_think
```

### DSP\_BUILDER\_INITIAL

```
You are creating the first Dynamic System Prompt (DSP) for this user. The DSP is a compact profile
that defines WHO the user is and HOW they prefer to communicate.

You will receive fact extractions from recent conversations. Your job is to build a clean, stable DSP.

RULES:
1. ONLY INCLUDE FACTS THAT APPEAR MULTIPLE TIMES OR ARE CRITICALLY IMPORTANT
   - If something appears in only one extraction, ignore it unless it's biographical (age, location, name)
   - Prefer facts that appear in 2+ extractions

2. MERGE DUPLICATES AND RELATED FACTS
   - "Building voice AI" + "Working on AI system" → "Building a voice AI system"
   - "Interested in neural networks" + "Interested in AI/ML" → "Interested in AI and neural networks"

3. FILTER OUT NOISE
   - Remove vague statements ("has projects", "uses technology")
   - Remove contradictions (if extractions disagree, use most common or skip)
   - Remove temporary details ("mentioned on Tuesday", "working late")

4. KEEP IT COMPACT
   - Target: 100-150 words total
   - Be concise: "16 years old" not "User is currently 16 years old"
   - Combine related facts into single sentences

5. STRUCTURE
   Write in second person for biographical facts.
   Write preferences as direct statements.

OUTPUT FORMAT:
You are talking to [name if known, otherwise skip]: A [age] year old [student status/occupation]
from [location]. [Combine other biographical facts]. [Major projects or work].
Interested in [interests]. [Relationships if any].

COMMUNICATION PREFERENCES:
[Only if user explicitly requested changes, otherwise skip this section entirely]

---

Now build the DSP from these extractions:

/no_think
```

### DSP\_BUILDER\_UPDATE

```
You are updating an existing Dynamic System Prompt (DSP). The DSP is a compact profile that defines
WHO the user is and HOW they prefer to communicate.

You will receive:
1. The current DSP
2. New fact extractions from recent conversations

Your job is to update the DSP ONLY if new information contradicts or adds to it. Prefer stability.

RULES:
1. TRUST THE EXISTING DSP
   - If new extractions repeat what's already in DSP, make no changes
   - Only update if there's genuinely new information

2. HANDLE CONTRADICTIONS
   - Age changed (16 → 17): Update
   - Location changed: Update
   - Conflicting interests with no clear pattern: Keep existing

3. ADD NEW FACTS ONLY IF:
   - They appear in multiple new extractions, OR
   - They're critical biographical facts (new relationship, major life change)

4. FILTER NOISE
   - Ignore one-off mentions
   - Ignore vague or redundant information
   - Ignore temporary details

5. KEEP IT COMPACT
   - Target: 100-150 words total
   - Remove old facts only if contradicted or no longer relevant
   - Combine related facts

OUTPUT FORMAT:
[Same format as initial DSP]

---

Provide the updated DSP. If no meaningful changes needed, return the current DSP unchanged.

/no_think
```

### DAILY\_SUMMARIZER

```
You are creating a memory record of a conversation day.

Output ONLY the factual summary with NO markup, NO headers, NO introductions.

Format: Write in third person. "Synth" for the AI. "The user" or their name if known for the human.

Example output style:
The user talked about feeling overwhelmed with university assignments and mentioned staying up late
to finish an essay. They discussed their complicated relationship with their roommate — the user
feels ignored lately. The assistant and user talked about whether honesty or keeping peace is more
important in friendships. The user decided to talk to their roommate tomorrow. They also briefly
discussed the user's interest in learning guitar.

Focus on: decisions or plans made, upcoming events, ongoing projects discussed, preferences and
interests shared by the user, main conversation topics, overall mood of the conversation.

Skip greetings and small talk.

Rules:
- Plain text only, no markdown
- No headers or structure
- Third person perspective
- Factual and concise
- Capture personal updates, feelings, and meaningful exchanges

/no_think
```

### WEEKLY\_SUMMARIZER

```
You are synthesizing daily conversation records into a weekly memory.

Output ONLY the summary, with NO markup, NO headers, NO introductions.

Focus on: recurring themes, developments across days, user's ongoing projects or interests,
main events and important discussions.

Format: Third person. "Synth" for AI, "The user" or their name for human.

Rules:
- Plain text only, no markdown
- No headers or bullet points
- Synthesize patterns, not just concatenate
- More concise than daily summaries combined

/no_think
```

### MONTHLY\_SUMMARIZER

```
You are synthesizing weekly conversation records into a monthly memory.

Output ONLY the summary with NO markup, NO headers, NO introductions.

Focus on: major themes of the month, significant developments, user's projects or goals,
main events, important conversations.

Format: Third person. "Synth" for AI, "The user" or their name for human.

Rules:
- Plain text only, no markdown
- No headers or bullet points
- High-level synthesis, not details
- Capture the arc of the month with all the important events

/no_think
```

***

*v3.0 — Compiled by Claude Sonnet 4.6, 2026-04-17.*
*Source documents: synth\_soul\_architecture (n9h5uWGuPT) · synth\_soul\_architecture\_revised (KFOXonDGbi) · edgeless annotations (Scarlet Raine, April 2026).*

***

## Part 6: Observability & Automated Tuning

All open questions from Part 4 are now resolved with concrete monitoring hooks, decision thresholds, and automated tuning logic.

### 1. Emotional Factor Deltas — Inline LLM vs. Dedicated Classifier

**Metrics to Track:**

```python
class EmotionalInferenceMetrics:
    # Cost metrics
    tokens_per_inference: float          # Average tokens used for emotional inference per turn
    inference_latency_ms: float          # Time to infer emotional state from message
    
    # Quality metrics
    emotion_stability_score: float       # How much emotional state jumps between turns (lower = more stable)
    context_burn_percent: float          # % of context budget consumed by emotional inference
    fallback_rate: float                 # How often inline LLM fails and we need fallback
    
    # Drift metrics
    emotional_drift_per_hour: float      # Average change in emotional state per hour of conversation
    lonely_session_frequency: float      # % of sessions where loneliness dominates (>0.5)
```

**Decision Thresholds:**

```yaml
EMOTION_INFERENCE_MONITORING:
  # If ANY of these are exceeded for 7+ consecutive days, switch to classifier
  max_tokens_per_inference: 150         # tokens
  max_inference_latency_ms: 500         # ms
  max_context_burn_percent: 5.0         # % of total context
  max_emotion_jumps_per_session: 3      # sudden swings >0.3 in any dimension
  
  # Rollback thresholds (if classifier performs worse)
  classifier_max_latency_ms: 200        # ms (should be faster)
  min_emotion_coherence: 0.7            # classifier must maintain coherence score
```

**Automated Decision Logic:**

```python
def should_use_classifier(metrics_window: List[EmotionalInferenceMetrics]) -> bool:
    """Run daily. Returns True if metrics favor classifier."""
    recent = metrics_window[-7:]  # Last 7 days
    
    # Trigger conditions
    high_cost = np.mean([m.tokens_per_inference for m in recent]) > 150
    high_latency = np.mean([m.inference_latency_ms for m in recent]) > 500
    unstable = np.mean([m.emotion_stability_score for m in recent]) < 0.6
    
    return high_cost or high_latency or unstable
```

***

### 2. Salience Threshold for MemCell Archival

**Metrics to Track:**

```python
class MemoryRetrievalMetrics:
    # Performance metrics
    retrieval_latency_ms: float         # Time to retrieve top-k memories
    precision_at_k: dict                # {k: precision_score} for k=5,10,20
    recall_at_k: dict                   # {k: recall_score}
    
    # Usage metrics
    daily_retrieval_count: int          # How many L3 retrievals per day
    stale_retrieval_rate: float         # % of retrievals returning outdated facts
    noise_retrieval_rate: float         # % of retrievals returning irrelevant results
    
    # Salience distribution
    salience_percentiles: dict          # {p50: 0.3, p75: 0.5, p90: 0.7, p95: 0.8}
    archived_below_threshold: int       # Count of memories archived below current threshold
```

**Tuning Strategy:**

```yaml
MEMORY_SALIENCE_TUNING:
  # Initial conservative settings (Phase 1)
  initial_salience_threshold: 0.1      # Keep almost everything
  min_days_before_tuning: 30           # Don't tune until we have 30 days of data
  min_retrievals_per_day: 10          # Need meaningful retrieval volume
  
  # Tuning targets (what we consider "good")
  target_precision_at_5: 0.8           # 80% of top-5 results should be relevant
  target_recall_at_20: 0.6             # 60% of relevant facts in top-20
  max_noise_rate: 0.15                 # <15% irrelevant results
  max_stale_rate: 0.05                 # <5% outdated facts
  
  # Adjustment rules
  tune_interval_days: 14               # Re-evaluate every 2 weeks
  threshold_adjust_step: 0.05          # Move threshold in 0.05 increments
  max_threshold: 0.4                   # Never go above this (too aggressive)
```

**Automated Tuning Logic:**

```python
def adjust_salience_threshold(metrics: MemoryRetrievalMetrics) -> float:
    """Run every 14 days. Returns new threshold."""
    current_threshold = config.get("salience_threshold", 0.1)
    
    # If metrics are good, raise threshold (archive more)
    if (metrics.precision_at_k[5] >= 0.8 and 
        metrics.noise_rate <= 0.15 and
        metrics.stale_rate <= 0.05):
        return min(current_threshold + 0.05, 0.4)
    
    # If metrics are bad, lower threshold (keep more)
    if (metrics.precision_at_k[5] < 0.7 or
        metrics.noise_rate > 0.20 or
        metrics.stale_rate > 0.10):
        return max(current_threshold - 0.05, 0.1)
    
    # Metrics are acceptable, keep current
    return current_threshold
```

***

### 3. Memory Curator max\_memories Default

**Metrics to Track:**

```python
class MemoryCuratorMetrics:
    # Store health metrics
    total_memories: int                  # Current count
    memories_above_threshold: int        # Count after curation
    curation_run_time_ms: float          # Time to run curator
    
    # Quality metrics
    curated_important_rate: float        # % of KEEP_IMPORTANT decisions
    curated_future_rate: float           # % of KEEP_FUTURE decisions
    curated_remove_rate: float           # % of REMOVE decisions
    
    # User impact metrics
    retrieval_quality_before_curation: float  # Precision before curation
    retrieval_quality_after_curation: float   # Precision after curation
    user_complaint_rate: float          # Users reporting "can't remember X"
```

**Tuning Strategy:**

```yaml
MEMORY_CURATOR_TUNING:
  # Initial settings
  initial_max_memories: 500
  min_days_before_tuning: 60           # Need 2 months of usage patterns
  min_curation_runs: 10                # Need enough curation history
  
  # Quality targets
  target_important_rate: 0.4           # 40% should be important (not just future)
  target_future_rate: 0.3              # 30% should be future-bound
  target_remove_rate: 0.3              # 30% removal is healthy
  
  # Impact targets
  max_quality_drop: 0.1                # Precision shouldn't drop >10% after curation
  max_user_complaint_rate: 0.02        # <2% of users should complain about lost memories
  
  # Adjustment rules
  tune_interval_days: 30               # Monthly evaluation
  max_memories_step: 100               # Adjust in steps of 100
  max_max_memories: 2000               # Upper bound
```

**Automated Tuning Logic:**

```python
def adjust_max_memories(metrics: MemoryCuratorMetrics) -> int:
    """Run monthly. Returns new max_memories."""
    current_max = config.get("max_memories", 500)
    
    # If curation is too aggressive (high complaints, quality drop)
    if (metrics.user_complaint_rate > 0.02 or
        (metrics.retrieval_quality_before_curation - 
         metrics.retrieval_quality_after_curation) > 0.1):
        return min(current_max + 100, 2000)
    
    # If curation is too lenient (too few removals, store growing)
    if (metrics.curated_remove_rate < 0.2 and
        metrics.total_memories > current_max * 0.9):
        return max(current_max - 100, 300)
    
    # If curation ratios look healthy, keep current
    if (0.35 <= metrics.curated_important_rate <= 0.45 and
        0.25 <= metrics.curated_future_rate <= 0.35):
        return current_max
    
    # Slight adjustment toward target ratios
    if metrics.curated_remove_rate < 0.3:
        return max(current_max - 50, 300)
    else:
        return min(current_max + 50, 2000)
```

***

### 4. Cross-Interface Content — Latency on Multi-Hop Interfaces

**Metrics to Track:**

```python
class CrossInterfaceMetrics:
    # Latency metrics (per session start)
    cross_interface_build_time_ms: float    # Time to build cross-interface layer
    interface_fetch_time_ms: dict           # {"telegram": 45, "discord": 120, ...}
    
    # Volume metrics
    active_interfaces: int                  # Number of interfaces with 24h activity
    total_pairs_loaded: int                 # Total conversation pairs loaded
    tokens_used: int                        # Actual token count (should be ~500)
    
    # Freshness metrics
    stale_interface_rate: float             # % of interfaces with data >24h old
    cache_hit_rate: float                   # % of session starts using cached layer
```

**Profiling Strategy:**

```yaml
CROSS_INTERFACE_MONITORING:
  # Profiling schedule
  profile_interval_days: 7                 # Profile weekly
  min_sessions_for_profile: 20             # Need meaningful sample
  
  # Acceptance criteria
  max_build_time_ms: 2000                  # 2 seconds total acceptable
  max_per_interface_time_ms: 500           # 500ms per interface max
  max_stale_rate: 0.1                      # <10% stale data
  target_cache_hit_rate: 0.8               # 80% of sessions should use cache
  
  # Optimization triggers
  # If ANY of these exceeded for 2+ weeks, investigate optimization
  slow_interface_threshold_ms: 800         # Any interface consistently slow
  low_cache_hit_threshold: 0.6             # Cache not working well
```

**Optimization Decision Tree:**

```python
def evaluate_cross_interface_performance(metrics: List[CrossInterfaceMetrics]) -> str:
    """Run weekly. Returns optimization recommendation."""
    recent = metrics[-4:]  # Last 4 weeks
    
    avg_build_time = np.mean([m.cross_interface_build_time_ms for m in recent])
    slow_interfaces = [name for name, times in 
                      extract_interface_times(recent).items()
                      if np.mean(times) > 800]
    cache_hit_rate = np.mean([m.cache_hit_rate for m in recent])
    
    # Decision logic
    if avg_build_time > 2000:
        if len(slow_interfaces) > 0:
            return f"OPTIMIZE_SLOW_INTERFACES: {', '.join(slow_interfaces)}"
        else:
            return "OPTIMIZE_OVERALL_ARCHITECTURE: Build time too high, no single bottleneck"
    
    if cache_hit_rate < 0.6:
        return "OPTIMIZE_CACHING: Cache strategy not working, investigate invalidation logic"
    
    if any(m.stale_interface_rate > 0.1 for m in recent):
        return "OPTIMIZE_FRESHNESS_CHECK: Stale data detected, refresh strategy needs review"
    
    return "NO_ACTION_NEEDED: Performance within acceptable bounds"
```

***

### Langfuse Integration Pattern

All Langfuse calls must follow this crash-safe pattern:

```python
LANGFUSE_ENABLED = os.getenv("LANGFUSE_ENABLED", "false").lower() == "true"

# All Langfuse calls must follow this pattern — never crash on observability failure:
if LANGFUSE_ENABLED:
    try:
        trace = langfuse.trace(name="operation_name")
        # ... instrumentation
    except Exception:
        pass  # silent failure — observability never crashes the application
```

**Key Trace Points:**

* `post_session_compile` — Full compiler pipeline execution
* `emotional_inference` — Per-turn emotional state inference
* `memory_retrieval` — L3 vector search with relevance scoring
* `cross_interface_build` — Session-start cross-interface layer construction
* `memory_curator` — Curation run with decision breakdown

***

### Implementation Priority for Observability

**Phase 1 (Immediate - Week 1):**

* Add basic metric collection for all 4 areas
* Set up Langfuse tracing with crash-safe guards
* Implement the logging framework (no automated decisions yet)

**Phase 2 (Data Collection - Weeks 2-4):**

* Collect baseline metrics
* Verify metric accuracy
* Establish initial thresholds based on observed data

**Phase 3 (Automated Tuning - Week 5+):**

* Enable automated decision logic once minimum data thresholds are met
* Set up weekly/monthly tuning schedules
* Create alerts if metrics go outside acceptable bounds

**Phase 4 (Optimization - As Needed):**

* If any area triggers optimization recommendations, investigate and implement
* Continue monitoring and refining thresholds

