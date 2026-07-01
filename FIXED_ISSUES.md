# FIXED_ISSUES.md — Synthetic Heart (SyntH)

> Companion to `AGENTS.md` §12. All entries here have **Status: fixed** and are archived for reference only.
> Active issues, known limitations, and partial fixes remain in `AGENTS.md` §12.

---

### Local tool-calling models drop the chat reply → "missing reply" correction loop  <!-- 2026-06-20 -->
**Symptom:** With native tool-calling models on llama.cpp (Qwen3.5, Gemma) the chat works for a turn or two, then every turn gets caught by the corrector (`⚠️ LLM generated no outbound message action … triggering corrector for missing reply`, `message_chain.py:2298`) and sometimes echoes the user's own message / ends in the 😵 fallback. Cloud models and non-tool-calling local quants are unaffected.
**Location:** `core/prompt_renderers.py` (`OpenAIRenderer.parse_tool_call_response`), `core/external_endpoints/adapters/openai_compat.py` (`_extract_tool_call_actions`), `core/prompt_engine.py` (`_derive_default_prompt_action_types`).
**Status:** fixed (2026-06-20).
**Notes:** Root cause — `parse_tool_call_response` discarded the model's natural-language `content` whenever any `tool_calls` were present, keeping only the structured calls. Tool-trained local models write the reply in `content` and use tool_calls for side-effects (and `create_personal_diary_entry` is prompted as *"REQUIRED in every response"*, so they almost always emit at least one non-message tool call), so the reply was lost → no `message_*` action → corrector storm; the corrector embeds the original user message, which small quants then parrot back (the "echo"). Fix: when tool_calls are present, surface leftover `content` under the top-level `message` key (the message chain already maps that to `message_<interface>` and dedupes), unless a `message_*` tool call already carries the reply; `<think>` blocks are stripped first.

**Related — disabled plugins still injected their tools (token bloat for small LLMs):** `core_initializer` skips action registration for plugins reporting `is_enabled() == False`, but `radio_host` and `agent_plugin` stored their toggle in `self._enabled` (`RADIO_HOST_ENABLED` / `AGENT_ENABLED`) **without overriding `is_enabled()`** — and `RadioHostPlugin` isn't even an `AIPluginBase` subclass — so they always counted as enabled and dumped `radio_speak`/`radio_update_metadata` and `agent_execute`/`propose_action`/`approve_action` into *every* prompt. Fix: both now override `is_enabled()` to return `bool(self._enabled)`. General rule for new plugins: gate tool exposure with `is_enabled()`, not a private flag. (Note: actions are scoped at registration/startup, so a runtime toggle flip needs a component reload to take effect.)

**Related — corrector dropped the persona (model improvised likes/dislikes on corrected turns):** `run_corrector_middleware` (`core/transport_layer.py`) sends a fresh single-message correction prompt with no `system` role and no history, so the persona (identity + `persona_preferences` likes/dislikes) was absent and the model improvised off-character on every corrected turn. Because corrections were firing constantly (the tool-call bug above), it looked like likes/dislikes were never injected — but on *normal* turns they are (`_build_context_summary` → `[Persona background]`). Fix: the corrector now prepends the active persona (`get_static_identity_content()` + `get_static_preference_content()`) to `correction_message_text`. Note: likes/dislikes are read from the **Postgres `runtime` DB only** (`SYNTH_LIKES`/`SYNTH_DISLIKES`), never from `skins/*/persona.json`; the MariaDB `source` DB is not read for config and can diverge. Latent wipe risk: `save_persona`/`_update_persona_configs` write `persona.likes` back to config, so a persona that loads with empty likes (config DB not ready at startup) could overwrite the stored list with `[]`.

**Related — diary consolidation ran on a broken base cortex:** the diary-merge `Exhausted 4 attempts … chat_id=-1` errors happened because diary consolidation was resolving to `BASE_CORTEX` (which was set to a keyless `anthropic`) instead of the local grillo engine. `ai_diary` re-dispatches the merge as its own `diary_merge` interface with `diary_merge_beat` but **no `grillo_beat`**, and `derive_cortex_scope` (`core/config.py`) only mapped `is_trainer`/`grillo_beat` to a scope — so it fell through to base. Fix: `derive_cortex_scope` now routes `diary_merge_beat` to the `grillo` scope, so diary consolidation follows `GRILLO_CORTEX`. (If `GRILLO_CORTEX` is `Default` it still falls back to `BASE_CORTEX`, so a usable `BASE_CORTEX` is still required.) This is near-undebuggable from the UI — the only signal was the `ANTHROPIC_API_KEY not configured` warning line.

**Related — small local cortex writes `meta.rationale` / diary fields in detached 3rd-person "the user" instead of persona-voice "Scarlet":** in Langfuse / `cortex_api.log` the `meta.rationale` (and the grillo action-checker `rationale` + sometimes `interaction_summary`) come out as *"The user gave permission to escalate…"* / *"User asked X"* — generic assistant register, 2nd/3rd person, no trainer name — while a stronger cortex on the same code writes them in first person using "Scarlet". Not a per-persona config bug: `TRAINER_NAME` is set (`Scarlet, Zahej`) and the trainer bio *is* injected, but it's a single buried line in the persona profile. Root cause is prompt framing + weak model: (1) `load_json_instructions()` (`core/prompt_engine.py:2078+`) and the whole instruction block refer to the human only as *"the user"*, never the trainer name; the `AUTONOMY GUIDELINES` ask for a *"rationale explaining why the action is taken"*, which reads as a system/meta justification and nudges the model into assistant register. (2) The `create_personal_diary_entry` examples/notes (`plugins/ai_diary.py:1535-1564`) literally model 3rd-person *"User asked about weather conditions and I provided…"*. Small quants (Qwen3.5-9B-Q4, Gemma-4B at `1070ti` = current `BASE_CORTEX`) parrot that literal framing; frontier models resolve "the user" → the named trainer and write in-voice, which is why another instance's output "looks fine". `IDENTITY INTEGRITY: Stay inside the active persona in first person` (`prompt_engine.py:2093`) governs the persona's self-reference, not how the trainer is named, so it doesn't catch this. **Not** caught by the corrector — `run_corrector_middleware` (`core/transport_layer.py`) is JSON-recovery only and never rewrites voice/perspective.
**Status:** fixed (2026-06-20) for the autonomy `rationale` + diary fields (options a+b). The grillo action-checker `rationale` (safe/confidence triad) was left as-is — it is an internal safety justification where system register is acceptable.
**Notes:** Fix — added `core/config.get_trainer_display_name()` (reads `TRAINER_NAME` live from the config registry, returns `""` when unset; **never hardcoded**, multi-trainer comma values preserved). `load_json_instructions()` (`core/prompt_engine.py`) now resolves it and the `AUTONOMY GUIDELINES` line asks for a *first-person `rationale` (your own voice)* plus a dynamic `Name people, not 'the user' (your trainer: <name>)` hint. `ai_diary.get_supported_actions()` resolves the same name and its `interaction_summary`/`personal_thought` descriptions + examples/notes are now first-person and named (e.g. *"{trainer} asked me about the weather, so I shared the forecast"*; placeholder `"my trainer"` when unset). Watch the `load_json_instructions()` size guard (`tests/test_prompt_minification.py::test_instructions_size_reasonable`, <5000 chars) — the trainer name is variable-length; current no-name build ≈4966, with `Scarlet, Zahej` ≈4997, so a very long multi-trainer value could trip it. Remaining option if small-model output still drifts: option (c) — point `GRILLO_CORTEX`/`BASE_CORTEX` at a stronger model for the meta/diary work.

**Related — three follow-up fixes (2026-06-21):** the dream-recall failure ("No recent chats found" / corrector storm) decomposed into three independent causes, each fixed separately: (1) **quoted numeric payload fields** — local models behind the grammar emit `{"limit":"5"}` (valid JSON; the grammar doesn't constrain payload value *types*), which broke SQL `LIMIT`; `_normalize_payload` (`core/action_parser.py`) now coerces numeric param fields (limit, offset, older_than_days, intensity, …) to int/float, not just `*_id`. (2) **spurious missing-reply corrector** — self-replying plugin actions (e.g. `get_recent_chats`, which calls `bot.send_message` itself) weren't counted as a reply; added the `USER_OUTPUT_ACTION_TYPES` config + `has_user_output_action` flag (`core/message_chain.py`). (3) **corrector retries lost the grammar** — `run_corrector_middleware` (`core/transport_layer.py`) sends a raw string prompt with no `__prompt_request`, so the bridge couldn't attach the GBNF grammar to retries; `_build_fallback_action_grammar` (`cortex_bridge.py`) now builds it from the full registered action catalog, gated on `force_action_grammar`.

---

### Fetch-only actions don't voice their result (recall_last_dream returns data into the void)  <!-- 2026-06-21 -->
**Symptom:** Asking the persona to recall its last dream (or any fetch-style action whose answer *is* the reply) yields no in-character recount. The model correctly emits `{"type":"recall_last_dream"}`, the action runs and returns the dream content, but the user gets nothing (and the missing-reply corrector fires).
**Location:** `core/action_parser.py` `run_actions` (only `action_type == "terminal"` populates `action_outputs` → `request_llm_delivery`; every other action's `execute_action` return value is returned by `run_action` then dropped); `plugins/grillo/grillo_dream.py` `_recall_last_dream` (returns `{"status","dream_content","message"}`, never sends); `core/auto_response.py` `request_llm_delivery` (already generic — takes any `action_type` + `action_outputs`, JSON-feeds them back, has loop-prevention + message-scoped `build_delivery_request`).
**Status:** fixed (2026-06-21).
**Notes:** Implemented as the planned 3-part change, all additive (no signature/return-shape changes, so none of `run_actions`' direct callers break):
1. **Capture** — `run_actions` (`core/action_parser.py`) gained an `elif isinstance(result, dict) and result.get("deliver_to_llm")` branch next to the terminal branch. It strips the `deliver_to_llm` control flag, stamps `type` = the action type, and appends to `action_outputs`. Crucially it does **not** set `terminal_seen`, so sibling actions in the same batch keep executing. The existing `if action_outputs:` delivery block now derives the `action_type` it passes to `request_llm_delivery` (`next()` over `action_outputs` for the first non-`terminal` type, falling back to `"terminal"`) so the loop-prevention instruction names the real action (`recall_last_dream`), not `terminal`.
2. **Plugin flag** — `grillo_dream._recall_last_dream` now returns `deliver_to_llm: True` on **all four** return paths (success-with-content, no-record, missing-content, DB-error), so the persona always voices *something* in-character.
3. **Corrector reconciliation** — `handle_incoming_message` (`core/message_chain.py`) reads `delivered_to_llm = bool(result.get("action_outputs"))` and adds `and not delivered_to_llm` to **both** missing-reply corrector conditions. Chose the "suppress when `action_outputs` non-empty" option over adding `recall_last_dream` to `USER_OUTPUT_ACTION_TYPES` — the latter is explicitly wrong here (that list is for actions that reply *directly/synchronously*; the in-code comment even calls out `recall_last_dream` as a thing that must NOT be listed). The delivery follow-up *is* the reply, so suppressing the corrector avoids the double follow-up. Generalises to any future `deliver_to_llm` action for free.

To add another fetch-only action: return `deliver_to_llm: True` from its `execute_action` result dict — capture, delivery, and corrector suppression all happen automatically. Tests: `tests/test_action_parser_deliver_to_llm.py` (capture + delivery + the no-flag negative case), `tests/test_grillo_dream.py::test_recall_last_dream_tags_deliver_to_llm`.

---

### llama.cpp generation cancelled mid-stream — layered client timeouts (default was 120s)  <!-- 2026-06-20 -->
**Symptom:** On slow hardware a long local generation is cancelled partway through; the `llama.cpp` server log shows `slot ... n_decoded = N` then `next: stopping wait for next result due to should_stop condition (adjust the --timeout argument if needed)` and `stop: cancel task`. It is *not* the server's `--timeout` — the **client (synth) aborts first and closes the socket**, which llama.cpp detects as `should_stop`.
**Location:** `core/external_endpoints/bridges/cortex_bridge.py` `_get_request_timeout()` (was `return 120.0`), applied both as the OpenAI SDK per-request `timeout` (httpx socket) *and* `asyncio.wait_for` around `chat_completion` (~lines 462/467).
**Status:** fixed (2026-06-20).
**Notes:** The trap that makes this hard to fix once: **multiple independent timeouts wrap the generation and the smallest binds**, so fixing one just exposes the next. Order (innermost→outermost) and the new generous defaults: generation `LLM_GENERATION_TIMEOUT_SEC` **1800** (new `core/config.py` var, `.env`/WebUI tunable, default for the bridge; per-endpoint `extra_config["timeout"]` still overrides) < `RESPONSE_TIMEOUT` 300→**2100** (`core/message_chain.py`) ≤ `AWAIT_RESPONSE_TIMEOUT` 600→**2400** (`core/transport_layer.py`) ≤ `LLM_CHAIN_LEASE_TIMEOUT_SEC` 600→**2400** (`core/plugin_instance.py`, registration + getter; only force-releases the lock, never cancels the gen). The adapter `__init__` default (60s, `openai_compat.py`) is overridden per-request by the bridge, so it doesn't bind on the cortex path. `RESPONSE_TIMEOUT`/`LLM_CHAIN_LEASE_TIMEOUT_SEC` were absent from the runtime DB (code default applies); `AWAIT_RESPONSE_TIMEOUT` was persisted at 600 and was bumped to 2400 in the DB. **External, not in code:** llama.cpp's own `--timeout` server arg — raise it to match for very long gens or the server cancels first. Invariant to preserve if you touch these: keep generation < all outer guards.

---

### `emotion_manager` decay loop double-applies decay (timestamp never refreshed)  <!-- 2026-06-12 -->
**Location:** `plugins/emotion_manager.py`, `decay_emotions()`.
**Status:** fixed.

---

### `ai_diary.get_recent_entries_async` crashes on Postgres and silently disables the diary  <!-- 2026-06-12 -->
**Location:** `plugins/ai_diary.py`, `get_recent_entries_async()` (JSON-field mutation loop) and the broad `except` that sets `PLUGIN_ENABLED = False`.
**Status:** fixed.

---

### `radio_host` track verification can store a track *title* as the last track id  <!-- 2026-06-12 -->
**Location:** `plugins/radio_host/track_monitor.py`, `_verify_track_stable()` (the two fallback paths: fetch-exception and incomplete-data).
**Status:** fixed.

---

### `radio_host` pre-generated banter: six concurrent writers race for one slot  <!-- 2026-06-12 -->
**Location:** `plugins/radio_host/radio_host_plugin.py` — `_pending_banter`, `_store_pending_banter`, `_pop_matching_banter`.
**Status:** fixed.

---

### `radio_host` WebDJ: hardcoded credentials and no `wss://` support  <!-- 2026-06-12 -->
**Location:** `plugins/radio_host/azuracast_client.py`, `plugins/radio_host/radio_host_plugin.py`, `jingle_injector.py`.
**Status:** fixed.

---

### `radio_host` `/api/radio/audio?path=` check allows sibling-directory bypass  <!-- 2026-06-12 -->
**Location:** `plugins/radio_host/radio_host_plugin.py`, `_serve_radio_audio` (direct-path branch).
**Status:** fixed.

---

### `radio_host` `_find_active_schedule` likely never matches AzuraCast's schedule shape  <!-- 2026-06-12 -->
**Location:** `plugins/radio_host/radio_host_plugin.py`, `_find_active_schedule`.
**Status:** fixed — pending live verification: confirm `schedule_description` populates on the next radio run against a real AzuraCast.

---

### `automation_tools/container_synth.sh notify` imports symbols that no longer exist  <!-- 2026-06-12 -->
**Location:** `automation_tools/container_synth.sh`, heredoc in the `notify)` case.
**Status:** fixed.

---

### `mcp_synth-db_get_recent_diary` still queries a stale `created_at` column  <!-- 2026-05-04 -->
**Location:** `synth-db` MCP diary helper / live `ai_diary` schema mismatch.
**Status:** fixed.

---

### `grillo_activity_log` inserts could return `None` ids on Postgres  <!-- 2026-05-04 -->
**Location:** `plugins/grillo/grillo_impl.py`, `GrilloPlugin.create_activity_log`.
**Status:** fixed.

---

### Automatic diary logging could create internal `diary_consolidation` noise rows  <!-- 2026-05-04 -->
**Location:** `core/action_parser.py`, automatic diary hook in `_create_diary_entry_for_actions`.
**Status:** fixed.

---

### `diary_merge` upserts could overwrite a real `ai_diary.interface` origin  <!-- 2026-05-04 -->
**Location:** `plugins/ai_diary.py`, `_merge_diary_interface` during same-day upsert merge.
**Status:** fixed.

---

### Corrector returns empty when `successful_actions = []`  <!-- 2026-04-13 -->
**Location:** `core/transport_layer.py` → `run_corrector_middleware`, the `if correction_context:` block that builds `correction_message_text`.
**Status:** fixed.

---

### `test_openrouter_engine.py` uses stale import patch targets  <!-- 2026-04-17 -->
**Location:** `tests/test_openrouter_engine.py` patch targets; current engine module lives under `engines/external_engines/openrouter.py`.
**Status:** fixed.

---

### `grillo_outreach` may route to invalid chat id `-1`  <!-- 2026-04-17 -->
**Location:** `plugins/grillo/grillo_outreach.py` target resolution in `_get_target_interface_and_chat`; fallback query over `chat_history_cache` can recover stale interface paths.
**Status:** fixed.

---

### google-genai async close can raise `_async_httpx_client` AttributeError  <!-- 2026-04-17 -->
**Location:** google-genai SDK cleanup path (`google/genai/_api_client.py`) triggered from project client instances in `engines/external_engines/gemini_api.py`, `core/live_session_manager.py`, `plugins/live_engines/gemini.py`, and `core/external_endpoints/adapters/gemini_adapter.py`.
**Status:** fixed.

---

### Langfuse response may attach to wrong request when model label drifts  <!-- 2026-04-18 -->
**Location:** `core/cortex_api_logger.py`, `_pop_langfuse_request` fallback behavior.
**Status:** fixed.

---

### `grillo_activity_log.diary_entry_id` may reference missing `ai_diary` rows  <!-- 2026-04-18 -->
**Location:** MariaDB source data in `grillo_activity_log` vs `ai_diary`; migration handling in `core/main_db_migration.py`.
**Status:** fixed.

---

### Postgres compat release path can emit unawaited `Pool.release` warnings  <!-- 2026-04-18 -->
**Location:** `core/db_backends.py` (`PostgresCompatConnection.close`), `core/db.py` (`release_conn`, `_ConnProxy.close`).
**Status:** fixed.

---

### Proxy cursors can break `async with` via delegated `__aenter__` lookup  <!-- 2026-04-18 -->
**Location:** `core/db.py` (`ensure_plugin_tables` local `_cursor_ctx` helper, `_ConnProxy.cursor` proxy wrappers).
**Status:** fixed.

---

### `ai_diary` merge query still uses MySQL `GROUP_CONCAT ... SEPARATOR` syntax  <!-- 2026-04-18 -->
**Location:** `plugins/ai_diary.py` (`DiaryPlugin.on_debrief`, query around `_get_unmerged_entries`).
**Status:** fixed.

---

### `send_message` alias rewrite could trigger avoidable correction on `body` payloads  <!-- 2026-04-18 -->
**Location:** `core/message_chain.py`, LLM-originated action normalization before validation.
**Status:** fixed.

---

### OpenAI-compatible external endpoint probe ignored configured adapter timeout  <!-- 2026-04-19 -->
**Location:** `core/external_endpoints/adapters/openai_compat.py` (`_list_models_via_http`, `ping_test`), `core/external_endpoints/probe.py` timeout plumbing.
**Status:** fixed.

---

### `external_endpoints.updated_at` string writes can fail on Postgres-backed endpoint registry paths  <!-- 2026-04-19 -->
**Location:** `core/external_endpoints/registry.py` (`update_endpoint`, `set_subsystem_map`, `_auto_set_default_model`, `set_default_model`).
**Status:** fixed.

---

### Queued trainer notifications could lose `skip_history` and pollute prompt context  <!-- 2026-04-19 -->
**Location:** `core/notifier.py` (`flush_pending_for_interface`), `core/history_engine.py`.
**Status:** fixed.

---

### `ai_diary` consolidation could recursively re-merge whole days and bloat Gemini prompts  <!-- 2026-04-19 -->
**Location:** `plugins/ai_diary.py` (`DiaryPlugin.on_debrief`, `DiaryPlugin.execute_action` for `update_diary_entry`).
**Status:** fixed.

---

### OpenAI-compatible image turns could be silently downgraded to text after a stale probe  <!-- 2026-04-19 -->
**Location:** `core/external_endpoints/bridges/cortex_bridge.py`, `core/external_endpoints/adapters/openai_compat.py`, `core/external_endpoints/probe.py`.
**Status:** fixed.

---

### OpenAI-compatible image-only turns could hallucinate non-visible details  <!-- 2026-04-19 -->
**Location:** `core/prompt_renderers.py` (`OpenAIRenderer.render_with_multimodal`).
**Status:** fixed.

---

### SOUL `async_consolidate` ran on every idle compile  <!-- 2026-04-19 -->
**Location:** `plugins/soul_plugin.py` (`SoulPlugin._compile_interface`).
**Status:** fixed.

---

### SOUL Postgres recall could full-scan memcells and trip static injection timeouts  <!-- 2026-04-19 -->
**Location:** `core/soul/repository.py` (`PostgresSoulRepository.recall_memories`), SOUL Postgres tables `mem_cells` / `mem_cell_vectors`.
**Status:** fixed.

---

### SOUL recall could inject diary-merge housekeeping into live prompts  <!-- 2026-04-19 -->
**Location:** `plugins/soul_plugin.py` (`SoulPlugin._recall_memories`).
**Status:** fixed.

---

### Selective correction context could store counts instead of action lists  <!-- 2026-04-19 -->
**Location:** `core/action_parser.py` (`_request_selective_correction`), `core/transport_layer.py` (`run_corrector_middleware`).
**Status:** fixed.

---

### Telegram multimodal extraction assumed every optional media attribute exists  <!-- 2026-04-19 -->
**Location:** `core/multimodal_attachment.py` (`extract_multimodal_from_telegram`).
**Status:** fixed.

---

### Async diary commands called sync diary retrieval on the event-loop thread  <!-- 2026-04-19 -->
**Location:** `core/command_registry.py` (`diary_command`, `context_command`), `core/generic_commands.py` (`generic_diary_command`, import of `last_chats_command_generic`).
**Status:** fixed.

---

### Recovered JSON with extra trailing content could drop later actions silently  <!-- 2026-04-20 -->
**Location:** `core/message_chain.py` (`handle_incoming_message` recovery/correction branch), `core/transport_layer.py` (`extract_json_from_text`).
**Status:** fixed.

---

### External cortex bridge dropped PromptRequest and fell back to legacy JSON flattening  <!-- 2026-04-20 -->
**Location:** `core/plugin_instance.py` (`prompt.pop("__prompt_request", None)` handoff), `core/external_endpoints/bridges/cortex_bridge.py` (`ExternalCortexEngine`).
**Status:** fixed.

---

### External OpenAI-compatible PDF attachments were serialized as image parts  <!-- 2026-04-20 -->
**Location:** `core/external_endpoints/bridges/cortex_bridge.py` (`ExternalCortexEngine._format_mm_part`, `_build_mm_parts_from_prompt_request`), `core/prompt_renderers.py` (`_build_multimodal_turn_text`, `OpenAIRenderer.render_with_multimodal`).
**Status:** fixed.

---

### Gemini tool manifests could lose normalized action parameters and yield empty payloads  <!-- 2026-05-07 -->
**Location:** `core/live_tool_registry.py` (`build_manifests_from_actions`, plus shared manifest extraction for action definitions built from normalized `schema` blocks).
**Status:** fixed.

---

### Selective correction retried safety-blocked actions and wasted an extra LLM call  <!-- 2026-04-20 -->
**Location:** `core/action_parser.py` (`_request_selective_correction`).
**Status:** fixed.

---

### Literal newlines inside JSON strings could trigger a spurious corrector round-trip  <!-- 2026-04-20 -->
**Location:** `core/transport_layer.py` (`extract_json_from_text`).
**Status:** fixed.

---

### Legacy `diary_entry` action alias could trigger an avoidable correction hop  <!-- 2026-04-20 -->
**Location:** `core/message_chain.py` (`handle_incoming_message` normalization path before unsupported-action validation).
**Status:** fixed.

---

### Legacy `diary` action and diary payload aliases could still force correction or drop diary metadata  <!-- 2026-04-20 -->
**Location:** `core/message_chain.py` normalization helpers before unsupported-action validation and action execution.
**Status:** fixed.

---

### Standalone `thought` action could still force correction instead of populating diary metadata  <!-- 2026-04-20 -->
**Location:** `core/message_chain.py` diary normalization helpers before unsupported-action validation.
**Status:** fixed.

---

### `chat_history_cache` deduplication query still used MySQL `DATE_SUB(... INTERVAL ...)` syntax  <!-- 2026-04-20 -->
**Location:** `core/chat_history_cache.py` (`save_chat_message`, duplicate-message guard query).
**Status:** fixed.

---

### `chat_update_checker` DB polling still used MySQL `UNIX_TIMESTAMP(...)` syntax  <!-- 2026-04-20 -->
**Location:** `core/chat_update_checker.py` (`ChatUpdateChecker._check_once`).
**Status:** fixed.

---

### `grillo_chat_observer` direct DB probe still uses MySQL `UNIX_TIMESTAMP(...)` syntax  <!-- 2026-05-05 -->
**Location:** `plugins/grillo/grillo_chat_observer.py` (`GrilloChatObserverPlugin._run_observer`).
**Status:** fixed.

---

### `memory_consolidation` beats were using a stale plugin prompt override  <!-- 2026-05-05 -->
**Location:** `plugins/grillo/grillo_impl.py` (`GrilloPlugin._create_beat_prompt`) with the stale override in `plugins/grillo/grillo_memory.py`.
**Status:** fixed.

---

### Tagged memory recall helpers still used MariaDB-only JSON predicates on Postgres  <!-- 2026-05-06 -->
**Location:** `core/synth_core_memory.py` (`search_memories`), `core/prompt_engine.py` (`search_memories`), `plugins/memory_search.py`, `plugins/ai_diary.py` tag/person lookups, and `plugins/grillo/grillo_compactor.py` marker filtering.
**Status:** fixed.

---

### SOUL static injection could time out on internal `grillo/-1` turns  <!-- 2026-04-20 -->
**Location:** `plugins/soul_plugin.py` (`SoulPlugin.get_static_injection`).
**Status:** fixed.

---

### Grillo outreach synthetic message ids could skip PromptRequest assembly  <!-- 2026-04-20 -->
**Location:** `core/prompt_engine.py` (`_assemble_prompt_request`, `RuntimeContext.message_id` assignment).
**Status:** fixed.

---

### Exact runtime timestamps could make trainer replies over-mention time and location  <!-- 2026-04-20 -->
**Location:** `core/prompt_renderers.py` (`_build_runtime_prefix`), `core/prompt_engine.py` (`load_unminified_chat_instruction`).
**Status:** fixed.

---

### Daily diary WebUI used MySQL-only `group_concat_max_len` on Postgres  <!-- 2026-05-19 -->
**Location:** `core/webui.py` (`history_diary`).
**Status:** fixed.

---

### PromptRequest history could replay autonomous outreach as assistant-only monologues  <!-- 2026-05-07 -->
**Location:** `core/prompt_engine.py` (`_history_to_turns`) fed by same-chat `history_current_chat` from `core/history_engine.py` / `chat_history_cache`.
**Status:** fixed.

---

### External Gemini 503 overloads were not considered retryable  <!-- 2026-05-07 -->
**Location:** `core/external_endpoints/bridges/cortex_bridge.py` (`ExternalCortexEngine._is_retryable_exception`).
**Status:** fixed.

---

### WebUI phase logs could hide valid THINKING/WRITING transitions  <!-- 2026-04-22 -->
**Location:** `core/action_state_manager.py`, `res/synth_webui/js/chat-window.mjs`
**Status:** fixed.

---

### `universal_send skip_history=True` was ignored and polluted `chat_history_cache`  <!-- 2026-05-05 -->
**Location:** `core/transport_layer.py` (`universal_send` history-save path), plus tests calling `send_llm_fallback_message()` / `universal_send()` with synthetic interface paths.
**Status:** fixed.

---

### Telegram send failures could mask the real error with undefined `correction_payload`  <!-- 2026-05-05 -->
**Location:** `interface/telegram_bot.py`, `TelegramInterface.send_message` exception handling.
**Status:** fixed.

---

### Telegram startup timeout could leave the interface permanently half-initialized  <!-- 2026-05-06 -->
**Location:** `interface/telegram_bot.py` (`start_bot`, `TelegramInterface.send_message`, `shutdown_interface`) and `plugins/message_plugin.py`.
**Status:** fixed.

---

### Langfuse Gemini generations could keep token summaries empty  <!-- 2026-05-06 -->
**Location:** `core/cortex_api_logger.py` generation logging, `engines/external_engines/gemini_api.py` usageMetadata mapping, and `core/external_endpoints/adapters/gemini_adapter.py` SDK usage-metadata logging.
**Status:** fixed.

---

### External cortex bridge could misreport adapter timeouts as 300s and keep retrying them  <!-- 2026-05-06 -->
**Location:** `core/external_endpoints/bridges/cortex_bridge.py` (`_get_request_timeout`, `generate_response`).
**Status:** fixed.

---

### Langfuse request traces could remain invisible until the response finished  <!-- 2026-05-06 -->
**Location:** `core/cortex_api_logger.py` (`log_cortex_request`).
**Status:** fixed.

---

### Langfuse API-error traces could look half-empty and mislabel Gemini provider failures  <!-- 2026-05-06 -->
**Location:** `core/cortex_api_logger.py` (`log_cortex_response` error-output handling) and `core/external_endpoints/adapters/gemini_adapter.py` exception logging.
**Status:** fixed.

---

### Same-chat user activity could cancel an in-flight Grillo outreach  <!-- 2026-05-06 -->
**Location:** `core/message_queue.py` low-priority background task tracking and cancellation.
**Status:** fixed.

---

### WebUI startup history replay could show the oldest prompt-context window instead of the recent chat  <!-- 2026-05-11 -->
**Location:** `core/chat_history_cache.py` (`load_chat_history` ordering/limit), `core/webui.py` (`_ensure_session_history_loaded`), and `core/chat_context_manager.py` (prompt-context deque size).
**Status:** fixed.

---

### `grillo_activity_log` can show silent blank outreach rows after log rotation  <!-- 2026-05-07 -->
**Location:** Runtime observability split across `logs/synth*`, `grillo_activity_log`, `chat_history_cache`, `core/external_endpoints/adapters/gemini_adapter.py`, and `core/plugin_instance.py` (`_update_grillo_response`).
**Status:** fixed.

---

### Radio host KittenTTS volume — hard clipping distortion fixed with ffmpeg dynaudnorm  <!-- 2026-05-24 -->
**Location:** `plugins/vox_engines/kitten.py` (`generate_tts`), `plugins/radio_host/azuracast_client.py` (`_convert_to_webm`).
**Status:** fixed.

---

### Radio host injection at track_change was too slow for timely announcements  <!-- 2026-05-24 -->
**Location:** `plugins/radio_host/radio_host_plugin.py` (`_on_track_change`, `_inject_banter_now`, `_on_winding_down`).
**Status:** fixed.

---

### Grillo outreach replies *to* "G.R.I.L.L.O." instead of speaking as SyntH (detached current-context outputs)  <!-- 2026-06-21 -->
**Symptom:** Outreach `message_*` actions read clinical/detached — SyntH addresses the recipient as if they were an AI ("how are you *actually* doing underneath all the action? Anything sticking with you that isn't just a subroutine?") rather than reaching out warmly to the trainer. Memory/history/emotion inject fine; only the framing is wrong.
**Location:** `plugins/grillo/grillo_outreach.py` (`_build_outreach_prompt`, `_generate_outreach_beat`, `_get_context_snippets`) feeding `core/prompt_engine.py` `input` block assembly (`get_user_display_name`/`get_user_usertag` from `core/user_utils.py`).
**Status:** fixed (2026-06-21).
**Notes:** Four compounding causes:
1. **Sender identity poisons the "current context."** The synthetic outreach message uses `from_user` with `full_name="G.R.I.L.L.O."`, `username="grillo"`, `is_bot=True`. `prompt_engine` then sets `input.payload.source.username` → `"G.R.I.L.L.O."` and `usertag` → `"@grillo"` (via `get_user_display_name`/`get_user_usertag`). The model is literally told the *current* inbound message is **from a bot named G.R.I.L.L.O.**, so it frames its reply as a response *to* that third entity (hence addressing an AI/"subroutine").
2. **The impulse is injected as a user turn of raw meta-instructions.** `_build_outreach_prompt`'s second-person scaffold ("You feel like reaching out… Return TWO actions… RESPOND ONLY WITH VALID JSON") becomes `input.payload.text`, planted as a *user*-role turn right after genuine intimate conversation. The tonal whiplash pushes the model into a meta/analytical register instead of SyntH's voice.
3. **Context snippets are third-person fragments.** `_get_context_snippets` formats recent context as `[interface] …` / `[memory] …` detached fragments, reinforcing the analytical stance.
4. **Wasted corrector pass (symptom of 1–2, not a model problem).** Because of the bad framing the main pass often returns only a large introspective `update_diary_entry` (no `message_*` action), so the missing-reply corrector fires a second pass that forces the `message_telegram_bot`. This is the *same* local endpoint (`http://127.0.0.1:8081/v1`, one loaded model — the `Huihui-gemma-4-E4B` that voices regular messages fine; the `Qwen3.5-9B` label is a stale WebUI config entry, not a separate model). Fixing the framing makes the first pass emit the message directly and removes the redundant missing-reply corrector round-trip.
**Evidence:** cortex/Langfuse session `2026-06-21 19:18` — main pass → `update_diary_entry` only; corrector pass → outreach text "You're always so busy with simulations… how are you *actually* doing… isn't just a subroutine?" to `telegram_bot/5208932647`. Same shape recurs hourly (cortex_search "OUTREACH": sessions 1, 6, 16, 21, 38, 60, 65, 76, 98, 105).
**Fix (all inside the removable plugin — `build_prompt_request` is CRITICAL-risk so it was left untouched):**
- `_generate_outreach_beat` no longer impersonates a bot sender. The synthetic `from_user`/`chat` now carry the **recipient's** name (`full_name=recipient_name`, `username=None`, `is_bot=False`), so `input.payload.source.username` reads as the person SyntH is reaching out *to*. `id=-1` is retained as the synthetic/internal marker.
- New `_resolve_recipient_name(interface, chat_id)` resolves the recipient only when the target chat matches a configured trainer (`get_trainer_id` == `chat_id`), taking the **primary** name for multi-trainer setups (`TRAINER_NAME` was `"Scarlet, Zahej"`); otherwise returns `""` and callers fall back to a history-grounded generic label (`"Trainer"` for the sender field, `"the person you have been talking with here"` in the prompt body).
- `_build_outreach_prompt` rewritten from `[G.R.I.L.L.O. OUTREACH]` second-person scaffold to `[SELF-INITIATED OUTREACH]`: states plainly "this is NOT a reply — no one has messaged you", clarifies the `source` is the recipient/route, and instructs first-person in-voice writing to `{recipient_label}`. JSON action skeleton unchanged so extraction is unaffected.
- `_get_context_snippets` drops the log-style `[interface]` prefix on diary recollections and tags memories `(memory)` instead of `[memory]`.
- Expected side benefit: the first pass should now emit the `message_*` action directly, eliminating the redundant missing-reply corrector round-trip (cause 4).
- Tests: `tests/test_grillo_beat_system.py::test_grillo_outreach_prompt_generation` (new self-initiated framing assertions), `tests/test_grillo_outreach.py::test_get_context_snippets_pulls_memories` (new `(memory)` tag + raw diary snippet).

---

### Memory-consolidation diary beat read detached/clinical (analyst voice, not journaling)  <!-- 2026-06-21 -->
**Symptom:** The big recurring `update_diary_entry` (canonical day-entry, e.g. id 4311) narrated SyntH in a clinical, self-observing register — "running diagnostics on our recent exchanges", and an `interaction_summary` of *"I responded by providing a structural framework for the memory consolidation process."* This is the "main vs introspection core feels detached" complaint, distinct from (and surfaced while fixing) the outreach detachment above.
**Location:** `plugins/grillo/grillo_impl.py` `_create_memory_consolidation_prompt` (the active builder — `_create_beat_prompt` intercepts `beat_type == "memory_consolidation"` at the top and calls this, **shadowing** `plugins/grillo/grillo_memory.py::GrilloMemoryPlugin.build_prompt`, which is dead for this beat). Amplified by `plugins/grillo/grillo_diary_consolidator.py::build_prompt` (day-merge).
**Status:** fixed (2026-06-21).
**Notes:** Root causes:
1. **Analyst framing.** The prompt asked to *"Synthesize your recent memories and identify recurring patterns"* and to summarise *"the topic, who raised it, and **the assistant's concrete answer**"* (third-person self-reference) with a meeting-minutes example ("We talked about Power Rangers — Jay asked…"). Ask for a pattern-synthesis summary, get clinical note-taking.
2. **No grounding spiral.** Its history lead-in often comes back as the placeholder `[History Evaluator] No recent messages available to evaluate`; with nothing concrete to synthesise, SyntH fell back to abstract self-analysis about her own architecture.
3. **Consolidator preserved the detached tone.** `grillo_diary_consolidator.build_prompt` faithfully *"preserve[s] all fragments"*, re-narrating the already-detached fragments into the canonical entry.
**Fix:** Rewrote `_create_memory_consolidation_prompt` to first-person felt journaling — dropped "synthesize/identify patterns/the assistant's answer" and the Power-Rangers example; added an explicit *"stay in first person, never refer to yourself in the third person, do not describe this as a task/'synthesis'/'process'"* rule; added a `has_history` gate that, when the lead-in is the no-data placeholder, pivots to "just check in with how you're feeling right now" instead of synthesising from nothing. JSON action shape (`create_personal_diary_entry` + `context_tags` + `interaction_summary`) unchanged. Added the same anti-summary rule to `grillo_diary_consolidator.build_prompt`. **Retired the shadow**: deleted the vestigial `plugins/grillo/grillo_memory.py` (no actions, dead `build_prompt` — auto-discovered as `beat_plugins["memory_consolidation"]` but always bypassed by `_create_beat_prompt`'s interception); removed its now-dead reference from `tests/test_grillo_beat_system.py::test_grillo_reflection_prompts_request_introspection_fields`. Plugin removal is safe by the PluginBase contract; `memory_consolidation` is selected from the hardcoded `BEAT_TYPES` dict, not the plugin registry. Tests: `tests/test_grillo_select_active_chats.py::test_memory_consolidation_prompt_is_first_person_journaling` (replaces the old `_instructions_are_specific`).
**Open (not fixed):** entry 4311's `emotions` array is malformed — mixed scales and bare strings (`{"type":"arousal","intensity":7}` alongside `{"type":"love","intensity":0.7}` and bare `"arousal"`). Separate emotion-parsing/data-hygiene bug in the diary write path, not the prompt voice.
