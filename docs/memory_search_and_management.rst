Memory Search and Management
============================

This page describes how Synthetic Heart looks up stored memories, prepares them
for the prompt, and manages memory-related context during prompt construction.

This is a developer-focused explanation of the internal memory pipeline, not the
public `memory_search` action plugin.

How memory search is triggered
-----------------------------

Memory search in the JSON prompt builder is triggered by tags extracted from the
incoming message text.

- `core.prompt_engine.build_json_prompt()` extracts tags from `message.text`.
- Tags are expanded with `core.synth_tagging.expand_tags()`.
- If expanded tags exist, `core.synth_core_memory.search_memories()` is called.

The prompt builder passes:

- `tags=expanded_tags`
- `limit=max(1, mem_limit)` where `mem_limit` is derived from
  `CONTEXT_VERBOSITY`.
- `include_chat=True` so the search may also include recent chat history hits.

Search pipeline and query behavior
----------------------------------

`core.synth_core_memory.search_memories()` performs a unified search across three
sources:

1. `memories` table
2. `ai_diary` table
3. `chat_history_cache` table (only when `include_chat=True`)

Search rules:

- Tag searches use `JSON_CONTAINS(tags, %s)` for the `memories` and
  `ai_diary` tables.
- Keyword searches use SQL `LIKE` on text fields.
- Chat history search also uses `LIKE` on cached message text.
- A large internal `pool_limit` is used to prefetch extra results before
  deduplication.

Result normalization:

- Each hit is returned as a normalized dictionary:
  `{"source", "id", "timestamp", "snippet", "tags"}`.
- Snippets are truncated to 400 characters for prompt efficiency.
- Tags are deserialized from stored JSON where available.
- Chat history hits use an empty `tags` list.

Deduplication and ordering
--------------------------

After collecting hits from all sources, `search_memories()` deduplicates them by
`source`, `id`, and snippet prefix.

The remaining results are sorted by timestamp descending and trimmed to the
requested `limit`.

How memories are injected into the prompt
-----------------------------------------

Search results are passed back into `build_json_prompt()` as the `memories`
argument.

`core.history_engine.HistoryEngine.build_context()` is responsible for adding
those memory hits into the final `context` section:

- `context["memories"]` is included only when
  `ENABLE_MEMORIES` is enabled.
- In normal mode, the number of memories included is capped by
  `CONTEXT_VERBOSITY`.
- In `PROMPT_LITE_MODE`, the cap is lower: `min(verbosity, 2)`.

This means the prompt contains a small, curated list of memory recall results
alongside chat history and other context contributions.

Memory config flags and behavior toggles
----------------------------------------

Key configuration flags affecting memory and history:

- `ENABLE_MEMORIES` — whether memory search results are included in prompt
  context.
- `CONTEXT_VERBOSITY` — how many history-like items are allowed in prompt
  context, including memories.
- `UNIFIED_HISTORY` — whether chat streams are merged across interfaces.
- `PROMPT_LITE_MODE` — aggressively minimizes prompt size and reduces memory
  counts.
- `ENABLE_HISTORY_CURRENT_CHAT` / `ENABLE_HISTORY_RECENT` — include current
  chat or recent chat history.
- `ENABLE_AI_DIARY` / `AI_DIARY_FULL` — include AI diary entries and control
  whether full diary content is preserved.
- `ENABLE_THOUGHTS` — include separate diary thought summaries.
- `ENABLE_TAGS_PLACEHOLDER` — include an empty `tags_placeholder` list for
  future multi-step or tag-based flows.

Memory management internals
---------------------------

Synthetic Heart also contains an internal memory save pipeline in
`core.synth_core_memory.py`:

- `should_remember(user_text, response_text)` decides whether an interaction is
  worth storing.
- `silently_record_memory()` saves the interaction via `core.db.insert_memory()`.
- These stored memories are not exposed directly to users; they are an internal
  recall mechanism.

The memory storage path is separate from the prompt search path, but the same
`memories` table is used for recall results.

Developer notes
---------------

- The prompt-side memory search is part of the JSON prompt construction flow,
  not the same as the `memory_search` plugin action.
- The relevant sources are:
  - `core.prompt_engine.build_json_prompt`
  - `core.synth_core_memory.search_memories`
  - `core.history_engine.HistoryEngine.build_context`
- Any changes to `context.memories` or memory result normalization should be
  coordinated with prompt reduction logic in
  `core.prompt_engine.reduce_prompt_for_llm_limit()`.

See also
--------

- `docs/prompt_engine_json_prompt.rst`
- `core.synth_core_memory.py`
- `core.history_engine.py`
- `core.prompt_engine.py`
