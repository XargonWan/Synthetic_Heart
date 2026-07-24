# G.R.I.L.L.O. — Background Autonomous Agent

G.R.I.L.L.O. is Synth's background "inner life" subsystem. Instead of only
reacting to incoming messages, Grillo periodically generates its own
introspective prompts — called **beats** — that let Synth reflect, consolidate
memories, get curious, and keep evolving even when nobody is talking to her.

Each beat is enqueued as a low-priority message and flows through the normal
message chain, so it uses Synth's full context pipeline (persona, emotions,
memories, diary). Beats never interrupt a real conversation.

## How it works

The Grillo core (`grillo_impl.py`, wrapped by `grillo_plugin.py`) runs a
scheduler on a fixed interval (`GRILLO_BEAT_INTERVAL`). On each tick it
weighted-selects a beat type and asks the matching beat sub-plugin to build a
prompt. Beat sub-plugins are discovered dynamically through the plugin
registry — adding a new one is enough to make it available.

## Beat types

Each beat sub-plugin lives in its own sub-folder under `plugins/grillo/` with a
dedicated guide (linked below).

| Beat sub-plugin | Beat type | What it does |
|-----------------|-----------|--------------|
| [`grillo_tag`](grillo_tag/guide.md) | `tag_elaboration` | Reflect on recent conversation topics/tags. |
| [`grillo_compactor`](grillo_compactor/guide.md) | `memory_consolidation` | Group old memories by tag and synthesize a compacted memory (nightly). |
| [`grillo_diary_consolidator`](grillo_diary_consolidator/guide.md) | `diary_consolidation` | Merge fragmented diary rows of a past day into one coherent entry. |
| [`grillo_self_reflection`](grillo_self_reflection/guide.md) | `self_reflection` | Emotional self check-in, written to the diary. |
| [`grillo_curiosity`](grillo_curiosity/guide.md) | `curiosity` | Explore emergent questions and write a diary entry. |
| [`grillo_relationship`](grillo_relationship/guide.md) | `relationship` | Reflect on recent interaction patterns. |
| [`grillo_temporal_reflection`](grillo_temporal_reflection/guide.md) | `temporal_reflection` | Notice the time gap since the last user message (ignores routine gaps < 12h). |
| [`grillo_dream`](grillo_dream/guide.md) | (daily, ~05:00) | Generate a "dream" diary entry from recent fragments; also a Recon contributor. |
| [`grillo_growth`](grillo_growth/guide.md) | (weekly) | Reflect on the week and rewrite Synth's self-growth reflection and likes/dislikes. |
| [`grillo_chat_observer`](grillo_chat_observer/guide.md) | (periodic) | Sample recent chat snippets and propose them for processing. |
| [`grillo_llm_failure_recovery`](grillo_llm_failure_recovery/guide.md) | (periodic) | Detect recent LLM fallback failures and regenerate the proper reply. |

[`history_evaluator`](history_evaluator/guide.md) is a non-LLM helper that
formats the last N chat messages into a concise reflection prompt used by
several beats.

## Configuration

| Key | Purpose |
|-----|---------|
| `GRILLO_BEAT_INTERVAL` | Seconds between scheduler ticks. |
| `GRILLO_CORTEX` | LLM engine used by Grillo beats. |
| `GRILLO_ALLOWED_ACTIONS` / `GRILLO_ALLOWED_SECURITY_LEVEL` | Which actions beats may execute. |
| `GRILLO_COMPACT_ENABLED` / `GRILLO_COMPACT_AGE_DAYS` | Memory-compaction beat toggles. |
| `GRILLO_OBSERVER_STORE_MEMORIES` | Whether the chat observer stores passive memories. |
| `GROWTH_MODE` | Self-growth approval mode (`off` / `on` / `request`). |

Beats are logged to `grillo_activity_log`, `grillo_beats`, and
`grillo_action_execs`.
