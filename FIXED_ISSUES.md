# FIXED_ISSUES.md — Synthetic Heart (SyntH)

> Companion to `AGENTS.md` §12. All entries here have **Status: fixed** and are archived for reference only.
> Active issues, known limitations, and partial fixes remain in `AGENTS.md` §12.

---

### Peer SyntH lines bled into the human's own turn, and a thread-suffixed reply could vanish from history entirely  <!-- 2026-07-02 -->
**Symptom:** Found via Langfuse traces `ff4e648f-fb13-47f9-b7ca-85828d987832` and `893661fb-2a23-4f7e-a0e0-ef634f7ad8a6` (two-SyntH Telegram group, 2B + 2D). A peer SyntH's `[2B]: ...`-tagged lines and a genuine human line landed in the *same* `role: "user"` message, joined by `\n\n`, with no way for the model to tell where the human's actual words ended and the peer's began — degrading memory coherence right after Grillo beats fired. Separately, Grillo's outreach prompt (`_build_outreach_prompt`) re-embedded the same recent turns a second time as a text recap, duplicating what the standard history pipeline already injected.
**Location:** `core/prompt_engine.py::_history_to_turns` (coalescing), `plugins/grillo/grillo_outreach.py::_build_outreach_prompt` (duplicate recap), `core/chat_history_cache.py::load_chat_history` (root cause of the missing turn that let the coalescing bridge peer+human).
**Status:** fixed (2026-07-02).
**Notes:** Three layered issues:
1. **Coalescing blended peer and human turns.** `_history_to_turns`'s "merge consecutive same-role turns" pass only checked `role` ("user"/"assistant") — since a peer SyntH's lines get `role="user"` too (no third role exists in the chat protocol; peer attribution lives in a `[PeerName]: ` content prefix, see `core/peer_policy.py::get_peer_names`), an adjacent peer line and human line silently merged into one block. Fixed by tracking an `is_peer` marker per turn and only coalescing when both `role` *and* `is_peer` match — peer-to-peer and human-to-human still coalesce, cross-category adjacency no longer does.
2. **Grillo outreach duplicated the thread.** Outreach beats are deliberately excluded from the "grillo internal" bucket (`is_grillo_internal = _is_grillo_beat and _beat_type != "outreach"`, `core/prompt_engine.py`) specifically so real conversation history still attaches — but `_build_outreach_prompt`'s `thread_section` *also* re-fetched and re-rendered the same recent turns via its own `_get_context_snippets` query, under a different labeling scheme (`2B:`/`You:`/`Scar:` vs. the real `[2B]:`-tagged turns), showing the model the same messages twice. Replaced the recap with a short pointer back to the history already in context; kept the diary-based "what's on your mind" section since that's genuinely unique (sourced from `ai_diary`, not `chat_history_cache`).
3. **Root cause of *why* peer+human ever looked adjacent: a real assistant turn can silently vanish from history.** `chat_history_cache.load_chat_history` queried `WHERE interface_path = %s` — an **exact match**. A message can be persisted with a Telegram thread-ID suffix on `interface_path` (`telegram_bot/-5408266521/237093590` vs. the chat's bare `telegram_bot/-5408266521`) — e.g. a reply-in-thread. The exact-match query silently drops such rows from both the DB-fallback fetch (`core/history_engine.py::build_context`) *and* the in-memory rehydration on startup/reconnect (`core/chat_context_manager.py::load_chat_history`), even though the message is fully legitimate and present in the table (confirmed live: `chat_history_cache` id 1847 had exactly this thread-suffixed path and was invisible everywhere except Grillo's own ad-hoc `LIKE`-based snippet query). Losing that assistant turn is what let an adjacent human line and peer line end up next to each other with nothing between them. Fixed with an **opt-in** `match_chat_level: bool = False` parameter on `load_chat_history` — default preserves the exact-match query byte-for-byte (13 other call sites across WebUI archive/restore, recon plugins, and Grillo dream/observer were left untouched; `gitnexus_impact` on this function came back **CRITICAL**, 27 impacted symbols, so the fix was deliberately scoped rather than changed globally). Wired `match_chat_level=True` into exactly the two callers that assemble conversation history: `history_engine.py`'s `cache_load` call and `chat_context_manager.py`'s rehydration call. Uses the same first-two-`/`-segments "chat key" convention already established in `core/peer_policy.py::_chat_key` ("so thread IDs don't fragment results").
**Related:** wiring `match_chat_level=True` into `history_engine.py` surfaced 3 tests in `tests/test_current_chat_history.py` that mocked `load_chat_history` with a single-positional-arg fake (`async def _fake_cache_load(ip)`), which raised `TypeError` inside a broad `except Exception` and silently zeroed out history in those tests without failing them — a pre-existing test-mocking gap unrelated to this bug, fixed alongside it.

---

### GitNexus index pointed at a different clone of this repo — tools silently ran against the wrong workspace  <!-- 2026-07-02, fixed 2026-07-03 -->
**Symptom:** From `D:\dev\D15\synthetic_heart`, `gitnexus_detect_changes` returned "No changes detected" despite real edits, and every gitnexus tool actually queried another clone's index. `mcp__gitnexus__list_repos()` showed the indexed `synthetic_heart` repos at `D:\dev\13` and `D:\dev\B15` — never D15.
**Location:** GitNexus global registry (`~/.gitnexus/registry.json`), not this repo's code.
**Status:** fixed (2026-07-03).
**Notes:** Root cause: GitNexus keys repos by **folder basename** and its MCP resolves the `repo` name parameter by first match — with multiple clones all named `synthetic_heart` in the shared global registry, whichever clone registered first won, and D15 had never been indexed at all. Fix: per-workspace isolation via `GITNEXUS_HOME` (the registry-path env override in gitnexus's `repo-manager.js`). `.mcp.json` now launches the gitnexus MCP with `GITNEXUS_HOME=.gitnexus-home` (gitignored, one registry per workspace containing exactly that clone, which also makes single-repo auto-resolution work — no `repo` param needed), the stale `D:\dev\13` entry was removed from the global registry, and AGENTS.md §8 documents the per-workspace analyze command (`GITNEXUS_HOME=.gitnexus-home npx gitnexus analyze --skip-agents-md`). Each workspace must run that once — until it does, gitnexus tools error with "No indexed repositories" instead of silently using the wrong clone, which is the intended failure mode.

---

### Postgres DDL translator silently renamed the `timestamp` column itself, not just its type  <!-- 2026-07-01 -->
**Symptom:** `column "timestamp" does not exist … HINT: Perhaps you meant to reference the column "emotion_state.timestamptz"` recurring across `emotion_manager`, `ai_diary`, `chat_history_cache`, `grillo_outreach`, `webui`, etc., even after the source SQL/Python were confirmed to say `timestamp` everywhere (not `timestamptz`).
**Location:** `core/db_backends.py` `_translate_create_table()` — the `\bDATETIME\b`/`\bTIMESTAMP\b` → `TIMESTAMPTZ` regexes ran with `re.IGNORECASE`. Every column literally named `timestamp` (lowercase, as this codebase always writes identifiers) sits at a word boundary just like the uppercase `TIMESTAMP`/`DATETIME` type keyword it follows, so the case-insensitive match renamed the **column itself** to `timestamptz` on every `CREATE TABLE` that went through `translate_postgres_sql()` on the Postgres ("soul") backend — including `core/db.py:init_db()`'s own native-Postgres schema load from `scripts/sql/app_main_postgres.sql`, which already spelled the column correctly.
**Status:** fixed (2026-07-01).
**Notes:** Fix had two parts: (1) made the two regexes case-sensitive (codebase convention: SQL type keywords are always UPPERCASE, identifiers always lowercase — verified no collisions exist for `LONGTEXT`/`DOUBLE`/`JSON`/etc.), so the type gets translated but a same-spelled lowercase column name doesn't. (2) Fixing the translator does **not** repair tables already created with the bad name (`CREATE TABLE IF NOT EXISTS` is a no-op against an existing table) — added `core/db.py:_heal_legacy_timestamptz_columns()`, called from `init_db()`'s Postgres branch, which does an idempotent `ALTER TABLE … RENAME COLUMN "timestamptz" TO "timestamp"` for the 7 affected tables (`ai_diary`, `ai_diary_archive`, `chat_history_cache`, `emotion_diary`, `emotion_state`, `memories`, `message_map`), skipping any table that already has a proper `timestamp` column. **Requires an app restart to take effect** (runs once at `init_db()` time). `radio_activity_log` was deliberately excluded from the heal list — it independently self-healed via `plugins/radio_host/db.py`'s own schema guard (`ADD COLUMN IF NOT EXISTS timestamp …`), so it now has *both* a stale unused `timestamptz` column and a working `timestamp` column; the dead column is harmless and was left alone (cleanup is a separate, not-yet-requested task).

---

### `BOTFATHER_TOKEN` silently disabled when `load_all_from_db` runs before ConfigVar is evaluated  <!-- 2026-06-30 -->
**Symptom:** `[telegram_interface] Interface loaded in disabled state: BOTFATHER_TOKEN not configured` at startup even though the token is present in `.env`. Bot never starts; recovery path also reports "BOTFATHER_TOKEN not configured".
**Location:** `interface/telegram_bot.py` module-level autostart block (~line 2893); `core/config_manager.py` `load_all_from_db` / `_load_definition_sync`.
**Status:** fixed (2026-06-30) — see `interface/telegram_bot.py` legacy autostart block.
**Root cause:** `ConfigVar` is lazy — it only reads `os.getenv` the FIRST time `bool()` is called on it (`_load_definition_sync`). If something blocks that first call until AFTER `load_all_from_db` has run, `load_all_from_db` marks the definition `loaded=True, value=None` (not in DB → default=None). Subsequent `_load_definition_sync` calls then return early and never read env. Concretely: the legacy autostart condition previously evaluated `BOTFATHER_TOKEN` as part of its `if` clause, which forced env-loading before `load_all_from_db`. Adding any short-circuit before that `BOTFATHER_TOKEN` check (e.g. `_under_pytest` guard) can prevent the early eval, letting `load_all_from_db` eat the definition first.
**Fix:** Evaluate `_botfather_configured = bool(BOTFATHER_TOKEN)` unconditionally at module level before the autostart `if` block. This forces `env_override=True` onto the definition, causing `load_all_from_db` to skip it. The `_under_pytest` guard (to prevent `initialize_interface()` from touching a live token during tests) must come AFTER this early eval — not before it. Also: only check `"pytest" in sys.modules` for the pytest guard, NOT `"unittest"` — the latter can appear in production if any dep imports `unittest.mock`.

---

### `timestamptz`-as-column-name "fix" (ff9c4d7) was based on a false premise — reverted  <!-- 2026-06-30 -->
**Symptom:** Commit `ff9c4d7` (2026-06-29) rewrote all SQL in `emotion_manager.py`, `ai_diary.py`, `grillo_outreach.py`, `chat_update_checker.py`, `chat_history_cache.py` to reference a column literally named `timestamptz`, on the claim that "the live DB uses `timestamptz` as the column identifier." This was wrong: every affected table (`emotion_state`, `ai_diary`, `ai_diary_archive`, `chat_history_cache`, `emotion_diary`) actually has a column named `timestamp` of type `timestamp with time zone` (i.e. the Postgres *type* is TIMESTAMPTZ, the *column* is `timestamp`) — confirmed live via `describe_table`. The commit conflated the type name with the column name. Result: constant `column "timestamptz" does not exist` errors across emotion state, diary, and chat history reads/writes — including history injection silently failing.
**Location:** same files as above.
**Status:** fixed (2026-06-30) — reverted `ff9c4d7`'s column renames back to `timestamp`. The MariaDB→Postgres syntax conversions bundled into that commit (`ON DUPLICATE KEY UPDATE`→`ON CONFLICT`, `UTC_TIMESTAMP()`→`NOW()`, `CURDATE()`→`CURRENT_DATE`) were also reverted because they're redundant: `core/db_backends.py` `translate_postgres_sql()` already auto-translates this MySQL syntax for every query that goes through `PostgresCompatCursor`, and `DATE(col)` is valid Postgres syntax natively.
**Notes:** Before assuming a live schema differs from the code, verify with `describe_table` (synth-db MCP) rather than trusting an error-hint string or a guess — and check whether the existing DB compat layer already handles the apparent mismatch before hand-editing SQL strings across multiple files.

---

### `grillo_activity_log` table — previously reported missing, now exists  <!-- 2026-06-30 -->
**Status:** resolved/stale — `describe_table`/`list_tables` confirm `grillo_activity_log` exists on the live DB (7474+ rows as of 2026-06-30). The 2026-06-29 entry claiming it was missing no longer applies; table was presumably created by a subsequent migration.
**Note:** a separate, newer 2026-07-01 entry in `AGENTS.md` §12 reports `grillo_activity_log` absent on the **Postgres "soul"** backend specifically — that is a different DB target and remains open.

---

### Recon `parse_recon_response` missing `_raw_llm_text` on 4 plugins  <!-- 2026-06-27 -->
**Symptom:** `Recon plugin ReconMemoryRecollectorPlugin parse failed: parse_recon_response() got an unexpected keyword argument '_raw_llm_text'` — and same for log_reader, tone_evaluator, language_evaluator. Crashes the entire recon dispatch for that plugin group, producing zero recon contributions.
**Location:** `plugins/recon_memory_recollector.py`, `plugins/recon_log_reader.py`, `plugins/recon_tone_evaluator.py`, `plugins/recon_language_evaluator.py` — all `parse_recon_response()` signatures.
**Status:** fixed (2026-06-27).
**Notes:** `core/recon.py:747` passes `_raw_llm_text=llm_text` as a keyword arg to all recon plugins. Four plugins didn't accept it. `recon_web_search.py` already had it (was fixed earlier). The fix adds `_raw_llm_text: str | None = None` as the last keyword parameter.

---

### Server-side LLM errors skip correction loop  <!-- 2026-06-27 -->
**Symptom:** When the LLM engine returns a non-recoverable error (e.g. `Logprobs not supported` from selenium-llm-engine proxying Gemini), the correction loop in `message_chain.py` would call the corrector 2+ times, each hitting the same dead engine and waiting for timeout (~120s each), before finally sending a fallback message.
**Location:** `core/message_chain.py` (correction loop, line ~2365).
**Status:** fixed (2026-06-27).
**Notes:** The fix adds a pre-check in the correction loop: if the LLM return text contains a known server-error marker (`logprobs not supported`, `internal server error`, `service unavailable`, `5xx` gateway errors), skip directly to the fallback message. The `Logprobs not supported` error itself comes from the selenium-llm-engine (not SyntH) — the OpenAI SDK it uses internally sends `logprobs` to Gemini, which rejects it. Fix the selenium-llm-engine to strip/not-set `logprobs` in its OpenAI-compatible adapter.

---

### Gemma-4 missing closing `}` for action dict → diary payload selected as `parsed`  <!-- 2026-06-26 -->
**Symptom:** message_chain logs `Normalizing action-key dictionary format` + `Added 6 synthetic action(s) for unregistered top-level key(s): interaction_summary, content, personal_thought, emotions, context_tags, involved_users` (the diary payload's own fields). Immediately followed by 12 unsupported action types, a correction, and eventual delivery. Trace: `ab6930e3-1689-48c6-a284-ea927c31695a`.
**Location:** `core/transport_layer.py` `extract_json_from_text`; triggered by gemma-4-uncensored (Venice) output.
**Root cause:** Gemma-4 sometimes emits the diary action dict without its closing `}`, so the outer `{"actions": [...], "type": "message_telegram_bot", ...}` is also malformed. The raw_decode scan falls back to the diary *payload* dict (minimum extra-chars parseable candidate). That dict has no `actions` key → message_chain normalizes its 6 fields as fake action types, then the unregistered-top-level-keys block doubles them to 12 → correction fires.
**Status:** fixed (2026-06-26) — `json_repair` now also runs when `found_json` is a dict without `"actions"` (not just when nothing is found). It fixes the missing brace, json_repair returns a list `[outer_with_actions, use_animation]`, the list is detected and merged back into a single dict with `"actions"` containing all recovered actions. `syntax_repaired=True` in metadata; `had_errors=False`; no correction needed.
**Notes:** The fix is in `extract_json_from_text` — the `_json_repair_needed` condition block at the bottom of the outer scan loop (outside all `if not found_json:` guards). Also: `json_repair` was NOT the cause of this trace (confirmed — no `json_repair` log entry exists; the bug predates the json_repair integration).

---

### Local model 20-min runaway + leaked `<thought>` (json_object not enforced)  <!-- 2026-06-21 -->
**Symptom:** A single chat turn took ~20 min and logged a malformed thinking tag plus cascading repeated `message_telegram_bot` outputs; only the first message was delivered. Trace: 1240s elapsed, `prompt 4887 + completion 27881 ≈ 32768` — the model generated until it **filled its entire 32k context window**.
**Location:** `core/external_endpoints/adapters/openai_compat.py` (`_strip_thinking`, `chat_completion`); `core/external_endpoints/bridges/cortex_bridge.py` (`_extra_api_kwargs`).
**Status:** mitigated (2026-06-21) — both causes addressed; archived here since nothing actionable remains.
**Notes:** Two independent causes. (1) The model ignored `enable_thinking=False` and emitted reasoning terminated by `</thought>`; `_strip_thinking` only matched `<think>`/`<thinking>`, not `<thought>`, nor a dangling closing tag (open tag dropped), so it leaked into content (JSON was still extracted after it, so the first reply went out). Fixed: regex now covers `thought` and a leading `^.*?</…>` dangling close. (2) **`response_format: json_object` is NOT enforced by this llama.cpp/model** — the output contained reasoning + prose + repeated JSON objects, i.e. free-form, so `force_json_object` is effectively a no-op here. With **no `max_tokens`**, a repetition loop ran to the context limit. Fixed: a default `max_tokens` (4096) is applied by `cortex_bridge._extra_api_kwargs()` — **only for local-model endpoints** (`disable_tools` / `force_action_grammar`); an explicit `extra_config.max_tokens` always wins, and cloud openai endpoints (xai, openrouter) stay uncapped (scoping tightened 2026-06-21 — every endpoint here is `protocol: openai`, so the blanket adapter default was wrong). The only *hard* JSON constraint for this server remains a GBNF `grammar` (already forwardable via `extra_config.grammar`); `json_object` should be treated as best-effort on local backends.

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

### `schedule_message send_at` path imported missing `get_local_tz` helper  <!-- 2026-04-18, fixed 2026-06-12 -->
**Symptom:** Absolute-time reminders could fail before scheduling with an import error when `schedule_message.payload.send_at` was used (`from core.time_zone_utils import get_local_tz` — a symbol that never existed). Relative-delay scheduling (`send_in`) was unaffected.
**Location:** `plugins/event_plugin.py` (`_handle_schedule_message_payload`).
**Status:** fixed (2026-06-12 audit) — verified 2026-07-03: the phantom import is gone from `plugins/event_plugin.py`.

---

### `event_plugin` interface-path reminder delivery called stale `run_action` signature  <!-- 2026-04-18, fixed 2026-06-12 -->
**Symptom:** Reminder delivery via `interface_path` could log a `run_action()` argument error instead of sending the message — the call site still used the old two-argument form (`run_action(action, message)`).
**Location:** `plugins/event_plugin.py` (`_send_via_interface_path`) vs `core/action_parser.py` (`run_action(action, context, bot, original_message)`).
**Status:** fixed (2026-06-12 audit) — verified 2026-07-03: the call site now uses `run_action(action, context, None, message)`.

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

---

### `BASE_CORTEX` silently and repeatedly reverted to `anthropic` (keyless, breaking every non-trainer/non-grillo turn)  <!-- 2026-07-01 -->
**Symptom:** User had `BASE_CORTEX` set to `Venice2` (the working external endpoint) via the WebUI, but it kept reverting back to `anthropic` — which has no `ANTHROPIC_API_KEY` configured in this environment — breaking every turn that didn't route through a scope override (`TRAINER_CORTEX`/`GRILLO_CORTEX`, both correctly pinned to `Venice2` and thus unaffected). Visible failure mode: a turn would resolve `is_trainer=True` (or `grillo_beat`) and succeed via `Venice2`, and a sibling/adjacent turn on the same chat that resolved to the *base* scope would immediately fail — 4 corrector retries against `anthropic` (`ANTHROPIC_API_KEY not configured` x4), then `message_chain.py` logs "Correction loop detected - same text repeated" and sends the 😵 fallback. Confirmed via Langfuse trace `af4096b6-638e-47f6-a937-b1e261204041` (the successful `Venice2` half) cross-referenced with `synth.log` at `16:17:23-29` (the failing `anthropic` half, same chat, ~5s later) — a peer-bot (2B) message to 2D's telegram_bot triggered the base-scope path and hit this.
**Location:** `core/config.py::get_active_cortex_engine()` (the self-heal branch when `chosen not in available`) + `core/cortex_registry.py::CortexRegistry.get_default_engine()`.
**Status:** fixed (2026-07-01).
**Notes:** Root cause was a two-part trap:
1. `get_default_engine()` has no real "default" concept — it returns `available[0]`, i.e. whichever built-in engine module `pkgutil.iter_modules()` happens to discover first under `engines/external_engines/`. That directory sorts `anthropic.py` before `gemini_api.py`/`openai_compat.py`/etc., so "the default" is really just "alphabetically first on disk" — this is the "Anthropic being first in the list" the user correctly suspected.
2. `get_active_cortex_engine()` calls this fallback — and **persists it back to the DB** (`config_registry.set_value("BASE_CORTEX", fallback)`) — any time the currently-configured engine name isn't in the *live in-memory* `CortexRegistry` at that exact instant. `Venice2` is registered dynamically by the external-endpoints subsystem (`core/external_endpoints/registry.py`), not a static `engines/external_engines/*.py` module, so there's a real window (startup ordering, or any transient re-sync of the endpoint) where `Venice2 not in available` evaluates true even though it's a perfectly valid, enabled, healthy endpoint (`probe_status: success`). Once that fires, `BASE_CORTEX` gets permanently overwritten to `anthropic` in the DB — and since `anthropic` **is** a validly-registered engine (just non-functional, no API key), the self-heal never fires again to notice anything is wrong. Nothing ever un-does it; the user has to manually reselect `Venice2` in the WebUI, and the next transient registry gap silently undoes that again — hence "we keep fixing this but it keeps popping back up." This function has a CRITICAL gitnexus blast radius (66 impacted symbols, 28 direct callers across recon plugins, debrief, prompt_engine, message_queue, core_initializer, webui, etc.) — used by nearly every turn — so the trap was hit constantly.
**Fix:** `get_active_cortex_engine()` now checks, before treating `chosen not in available` as genuinely stale, whether `chosen` matches a currently-enabled external endpoint's `engine_name()` (`get_external_endpoint_registry().list_endpoints(enabled_only=True)`). If it does, the function returns `chosen` unchanged (with a warning log) instead of silently switching away and persisting the change — the transient/startup-lag case now degrades to "this one call may error until the endpoint finishes registering" instead of "permanently corrupt the user's config." The existing self-heal-and-persist behavior for **genuinely** removed/typo'd engine names (e.g. a name that matches no built-in module and no configured external endpoint) is unchanged and still covered by `tests/test_core_config.py::test_get_active_cortex_engine_repairs_stale_base_for_scope` / `test_get_active_cortex_engine_resets_bad_scope_override_to_base`. Added `test_get_active_cortex_engine_keeps_pending_external_endpoint` as a regression test for this specific trap. Also manually corrected the already-corrupted DB value (`BASE_CORTEX` → `Venice2`) to unblock immediately.
**Open (not fixed):** `get_default_engine()`'s `available[0]` fallback is still filesystem-order-dependent for the case where the fallback genuinely is needed (no external endpoint match) — it happens to prefer `"manual"` if present (the intended neutral default, and the module-level `BASE_CORTEX` config var's own default value), but silently falls through to whatever built-in sorts first once `"manual"` isn't registered. Not fixed now since it's a much rarer path (only hit for truly-removed/typo'd names) and changing it risks the two existing tests' exact fallback-target assertions; flagging in case a future agent wants a more deterministic tiebreaker (e.g. explicit priority list) instead of directory order.

---

### `BASE_CORTEX` stuck at keyless `anthropic` — 2nd bot2bot mention in a turn still failed after the fix above  <!-- 2026-07-01 -->
**Symptom:** Follow-up to the incident directly above. In a shared Telegram group with two peer SyntHs (2B, 2D) plus the human trainer (Scar), the trainer-directed turn in a mixed message always succeeded, but the turn generated in response to the *peer's* message (e.g. 2B replying after 2D's message lands, mention-order relay) kept failing with the same "4 corrector retries against anthropic" → "Correction loop detected - same text repeated" → 😵 fallback pattern, timestamps landing 5-15s after the peer message's `chat_history_cache` row. Confirmed via `get_recent_errors`/`get_chat_history` on `telegram_bot/-5408266521`: `synth.log` failures at `19:45:33`/`19:47:57` local line up almost exactly with peer (2D) messages recorded at `17:45:27`/`17:47:52` UTC.
**Location:** `core/config.py::get_active_cortex_engine()` — same function as above, different gap.
**Status:** fixed (2026-07-01).
**Notes:** The prior fix only guards against *future* incorrect reversion away from a still-valid, not-yet-(re)registered external endpoint. It does nothing for a `BASE_CORTEX` value that is *already* `anthropic`: `anthropic` is a real, always-registered built-in engine module, so `chosen not in available` is `False` for it and the self-heal branch never runs at all — the value just sits there. Live `get_config` confirmed `BASE_CORTEX = anthropic` with `updated_at = 2026-04-06`, i.e. it had been broken since well before today's fix and nothing had ever revisited it (the base-scope path is only exercised by peer-message turns and a few background tasks, so a human trainer working the group would never notice — every trainer-scope turn correctly uses `TRAINER_CORTEX = Venice`). Separately, `engines/external_engines/anthropic.py::generate_response()` doesn't raise when the key is missing — it returns the literal string `"Anthropic API Key not configured. Please set ANTHROPIC_API_KEY in settings."` as if it were a real completion, which is exactly why the corrector's `tried_texts` loop sees the identical string every retry and calls it a "correction loop" rather than a hard failure.
**Fix:** `get_active_cortex_engine()` now discards `"anthropic"` from the locally-computed `available` set whenever `ANTHROPIC_API_KEY` is empty, *before* the `chosen not in available` check — so a `BASE_CORTEX`/override value of `anthropic` with no key is now treated exactly like a genuinely-stale engine name and runs the existing self-heal path, instead of being silently accepted as "available" forever. In the final fallback branch (`reg.get_default_engine()`, still filesystem-order-dependent per the "Open" note above and thus still liable to hand back `"anthropic"`), added a small correction: if the picked fallback is `"anthropic"`, prefer whichever of `TRAINER_CORTEX`/`GRILLO_CORTEX` is already validly configured to a currently-available engine on this same instance (here, `Venice`) rather than guessing at an arbitrary external endpoint — both scopes are proven-working on this exact instance already, so reusing one is safe. A real, deliberately-configured `anthropic` (key present) is left untouched. Tests: `tests/test_core_config.py::test_get_active_cortex_engine_avoids_keyless_anthropic_fallback` and `::test_get_active_cortex_engine_allows_anthropic_when_key_configured`. Also corrected the live corrupted value directly (`BASE_CORTEX: anthropic → Venice` via the `synth-db` MCP `set_config`) to unblock immediately, since the code fix only takes effect on the process's next restart.
**Open (not fixed):** `get_default_engine()`'s own directory-order fallback (see "Open" note above) is unchanged — this fix only special-cases the one credential-gated built-in (`anthropic`) known to cause silent, self-reinforcing breakage. A different keyless built-in engine picked as a bare `get_default_engine()` fallback (with no trainer/grillo sibling override to borrow from) would still be returned as-is.
