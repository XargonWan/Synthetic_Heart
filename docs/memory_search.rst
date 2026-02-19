Memory Search Plugin
=====================

Overview
--------

`memory_search` is a core plugin that allows the LLM to query synth's stored memories when it doesn't have enough information to answer a user's question.

Action: `memory_search`
----------------------

Schema (summary):

- mode: "tags" | "free" (required)
- tags: list of strings (required when mode == "tags")
- query: string (required when mode == "free")
- max_results: integer (optional, overrides global limit)
- time_window: string or object (optional) — relative string like "yesterday", "last week", "48 hours", an ISO interval string like "2026-01-10/2026-01-12", or an object with explicit "start"/"end" ISO datetimes or "duration" (see below)

Example payloads:

- Tag search: {"type": "memory_search", "payload": {"mode": "tags", "tags": ["monster", "austria"]}}
- Free search: {"type": "memory_search", "payload": {"mode": "free", "query": "austrian monster"}}

Configuration
-------------

- `ENABLE_MEMORY_SEARCH` (bool, default: True) — Toggle plugin on/off.
- `MEMORY_SEARCH_MAX_RESULTS` (int, default: 10) — Default maximum number of results returned.

Behavior
--------

When the plugin executes it searches both `memories` and `ai_diary` tables (using JSON_CONTAINS for tag searches and LIKE tokens for free searches), orders results by timestamp descending, and returns a list of snippets. Results are also delivered back to the LLM via the core auto-response mechanism so the model can immediately incorporate them in its reply.

Examples and usage notes
------------------------

- The prompt system includes a system instruction (in English) urging the LLM to use `memory_search` when it lacks information. The action should be emitted as a JSON action and will trigger the plugin.

Docs: Where to update
---------------------

- This file: `docs/memory_search.rst`
- Consider adding examples to `AGENTS.md` and the prompt guidance docs if you want more detailed usage examples for specific Cortex engines.
