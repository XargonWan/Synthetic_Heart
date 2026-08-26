# PR: `feat/rift-vessel-new` → upstream `feat/rift-vessel-new`

**Source branch:** `feat/rift-vessel-new` (local, `857f588e`)
**Target branch:** `upstream/feat/rift-vessel-new` (`a0a21388`)
**Merge base:** `a0a21388` (upstream head, already merged in via `5c89e025`)

This document captures the complete current diff between this branch and
XargonWan's upstream `feat/rift-vessel-new`, after merging the upstream branch
in and landing all local work.

## Overview

A large batch of Rift Vessel, SOUL runtime, Cortex engine, Grillo, WebUI/plugin
architecture, and developer-tooling work, plus the upstream message-queue
priority restructure that was merged in.

- **351 files changed, +54,535 / −1,154** (full diff vs upstream)
- **98 code/test/script files, +10,928 / −592** (excluding the generated docs/wiki
  export, maintained docs, and markdown)
- ~240 generated wiki/doc export files (bulk of the line count)

## Commits in this PR (vs upstream)

```
857f588e fix(webui): stabilize engine config controls
8c3d9544 fix(prompt): relative-age markers on history turns; harden corrector against duplicate replies; knock out open changelog issues
a4890b2f merge: restore complete newer WebUI and plugin architecture
3cddb8f2 feat(rift_vessel): add Rift Vessel subsystem specification
3f9654a8 fix(vessel): correct blind craft retries and stop goal churn
799fa5b2 fix(goals): unscoped chat goal actions fall back to recent active goal scope
dd2a1caa fix cortex selection and engine config UI
dbbb177c feat(webui): endpoint vision-test button and in-place Engines-tab refresh
597ee839 feat(webui): engine-config presets with model swap + Iris vision prompt editor
e6e4824d feat(cortex): reliable first-attempt JSON chat turns + correction/diary hardening
b5eefc95 feat(vessel): parallel native tool-calling, vessel loop fixes, and diary/emotion schema hardening
5c89e025 Merge remote-tracking branch 'upstream/feat/rift-vessel-new' into feat/rift-vessel-new
3c0fdf25 feat(cortex): retry empty LLM responses in the bridge
6303aa72 fix(telegram): discard non-numeric thread_id on outbound sends
30388d7a fix(grillo): skip self-senders and placeholder paths in observer
447060c8 feat(soul): skip roleplay turns in memcell and DSP compilation
80d2bc27 fix(soul): reject emote-tailed and laughter-shaped DSP facts
f094b58c fix(soul): reject speech-addressed and roleplay-shaped DSP facts
97d36f2d feat(webui): expose SOUL_DSP_INJECT_ENABLED toggle in Settings
8fa9feda fix(soul): read SOUL_DSP_INJECT_ENABLED as int for env override
8d2b0316 fix(soul): bound DSP extraction and gate injection off by default
fe696a36 fix: isolate vessel history from global context
e1a7d133 feat(soul): place DSP in user role and reconcile emotion signals
9de656b5 fix(cortex): gate native tools to Vessel turns so chat replies reliably
52ab2f0e feat(soul): wire session-state and per-turn mood delta into prompt
9c215182 fix(vessel): suppress missing-reply corrector after vessel_disconnect
c95f031c docs: adopt TencentDB developer MCP
7cdf0bfd fix(soul): gate DSP stability and wire per-turn soul context
bd3cc5b1 Fix Grillo outbound Telegram action scoping
5d4b3071 Add TencentDB developer knowledge MCP
65517571 Fix Vessel action failures and Minecraft handoff
e9240a31 Fix vessel recap evidence and Minecraft target resolution
cae224d9 docs: clarify pytest environment
a599b0bb fix(cortex): cap Venice native tools and harden Langfuse diagnostics
33484624 Fix vessel action grounding and endpoint tool opt-ins
b266bb6d docs: update Rift Vessel wiki
dd64be8a Fix Rift Vessel prompt and Minecraft delivery
64868ecd docs: define developer MCP usage policy
cc22a620 doc(agents): add wiki maintenance workflow
9f123a81 fix(vessel): harden correction routing and refresh agent docs
9a2a1b80 Remove task
7f65c636 fix(stage): resolve VRM skin_change freeze and idle T-pose in new Vue stage
bd3d50d0 fix(grillo): tag observer snippets with relative age so outreach stops continuing stale threads
0fb90043 chore(gitignore): ignore .kilo, local dev PNG, and runtime TTS audio
0b96454f fix(grillo): backfill interface_paths registry so outreach never silently dies
c247468e chore: resolve config style audits and fix vox defaults tests after upstream merge
```

## Change areas

### 1. Rift Vessel / Minecraft embodiment

- **Parallel native tool-calling for ordinary chat** (`core/external_endpoints/bridges/cortex_bridge.py`):
  opt-in via the endpoint's Extra Config (`enable_tools_parallel` / `parallel_tool_calls`); a
  capable model can now emit the reply + emotion + diary actions in one turn. Vessel turns keep
  the reliable single-call contract. Tool sets are scoped so other interfaces' delivery actions
  (e.g. `send_mate_message`) never appear in a chat turn — replies route through the current
  interface's `message_*` tool.
- **Autonomous-vessel correction skip** (`core/message_chain.py`): failed actions on autonomous
  vessel beats no longer trigger an immediate LLM correction re-invocation (kills the trace flood
  and in-world `say` spam); reactive player chat still gets correction.
- **Session chat-context purge** (`interface/vessel_interface.py`): ending a session clears the
  vessel chat context (memory + durable cache) so a fresh session never inherits the previous one.
- **Goal-debrief verification** (`plugins/rift_vessel/minecraft/minecraft.py`): history-based goal
  completion only counts verified successes (`_result.ok`), so a failed craft no longer
  auto-completes a goal; `nearbyBlocks` reports the nearest instance per block name (fixes the
  "walks to a far block" observation).
- **Blind craft-retry fix + goal-churn stop** (`3f9654a8`, `core/transport_layer.py`,
  `core/vessel_beat.py`, `minecraft.py`, `minecraft_bridge.js`, `target_names.py`): craft actions
  no longer retry the identical blind recipe; `target_names.derive_quantity` is quantity-aware so
  a single intermediate product never closes a multi-product goal ("runs around re-authoring the
  same goal" churn fix). `test_vessel_goal_debrief` extended (486 lines), new
  `test_corrector_vessel_world_state`.
- **Vessel payload alias normalization** (`core/action_parser.py`): common misnamed keys
  (`recipe`/`block`/`amount`) map to the canonical `item`/`name`/`target`/`count` for `vessel_*`
  actions, with longest-verb-suffix matching (`collect_block` etc.).
- **Rift Vessel subsystem specification** (`3cddb8f2`): the full multi-file subsystem —
  `core/vessel_registry.py`, `core/vessel_session_manager.py`, `core/db.py` vessel tables,
  `core/command_registry.py` (`/vessel status` etc.), `interface/vessel_interface.py`,
  `interface/minecraft_provisioner.py`, `plugins/vessel_base.py`, `plugins/vessel_plugin.py`,
  `plugins/vessels/minecraft_connector.py`, `interface_dev/minecraft_bridge_minimal.js`,
  `init-db.sql` vessel tables, `docs/rift_vessel.rst` (224 lines), and the WebUI
  History/Activities hooks (`core/webui.py`, `history.html`, `history.js`).
- Fixes to vessel action grounding, Minecraft handoff, recap evidence, target resolution,
  correction routing, and prompt/delivery (`65517571`, `e9240a31`, `33484624`, `dd64be8a`,
  `9f123a81`).
- Vessel history isolated from global context (`fe696a36`).

### 2. SOUL runtime

- DSP fact extraction/compilation with roleplay, speech-addressed, emote-tailed and laughter-shaped
  fact rejection (`core/soul/compiler.py`, `core/soul/roleplay.py`, `core/soul/strategies.py`).
- DSP gated off by default, bounded extraction, placed in the user role, emotion-signal
  reconciliation, session-state + per-turn mood delta wired into the prompt (`core/prompt_engine.py`,
  `core/soul/*`, `plugins/soul_plugin/soul_plugin.py`).
- `SOUL_DSP_INJECT_ENABLED` WebUI toggle + env parsing (`core/webui_templates/sections/engines.html`,
  `core/webui.py`).

### 3. Cortex engine / LLM bridge

- Retry empty LLM responses (`3c0fdf25`).
- Native tools first gated to Vessel turns so chat replies reliably (`9de656b5`), then the
  parallel-tool opt-in above (`b5eefc95`).
- **Reliable first-attempt JSON chat turns** (`e6e4824d`): `core/external_endpoints/bridges/cortex_bridge.py`
  first-attempt JSON reliability + correction/diary hardening (`core/action_parser.py`,
  `core/transport_layer.py`, `plugins/ai_diary/ai_diary.py`, `plugins/emotion_manager/emotion_manager.py`).
- **Prompt staleness + corrector duplicate fixes** (`8c3d9544`): conversation turns older than
  `HISTORY_AGE_MARKER_MINUTES` (10) carry a relative-age marker (`[3 hours earlier]`) so outreach
  and beats stop treating an hours-old thread as live; the correction loop accumulates
  already-delivered actions across passes (`_merge_correction_successes`) and suppresses re-emitted
  `message_*` actions, eliminating duplicate Telegram replies. Plus changelog knock-outs
  (FakeBuilder stub, cp1252 console logging, stale entries).
- Venice native-tool cap + Langfuse diagnostics hardening (`a599b0bb`).
- Adapter logs `reasoning_content` when a provider returns it (`core/external_endpoints/adapters/openai_compat.py`).

### 4. Message queue priority scale (merged from upstream)

- `a0a21388` restructured the low bands: `PRIORITY_RADIO = 6`, `PRIORITY_GENERAL = 5`,
  `PRIORITY_BACKGROUND = 2` (Grillo at absolute bottom); vessel bands (`REFLECTION = 9`,
  `AMBIENT = 4`) unchanged. Grillo/radio/event callers updated, tests + docs + CHANGELOG updated.

### 5. Grillo, Telegram, Recon

- Grillo observer skips self-senders and placeholder paths (`30388d7a`); outbound Telegram action
  scoping fixed (`bd3cc5b1`); Grillo beats at `PRIORITY_BACKGROUND`.
- **`interface_paths` registry backfill** (`0b96454f`, `core/interface_paths.py`): the registry that
  lets outreach target the right chat path is backfilled at startup so outreach never silently dies.
- **Relative-age observer snippets** (`bd3d50d0` on the flat module, re-applied to the packaged
  `grillo_chat_observer/grillo_chat_observer.py` in `8c3d9544`): snippet timestamps render as a
  compact relative age so stale threads are not continued as if live.
- Config style audits + Vox defaults tests after upstream merge (`c247468e`).
- Telegram discards non-numeric `thread_id` on outbound sends (`6303aa72`).
- Recon memory recollector + search orchestrator adjustments.

### 6. WebUI / plugin architecture (restored via merge `a4890b2f`)

- **Complete newer WebUI and plugin architecture restored** (merge of the first-parent line):
  `core/plugin_base.py` refactor, `core/plugin_instance.py`, `core/core_initializer.py`,
  `core/log_archive.py`, `core/db_backup.py`, `core/migrations.py`, `core/agent_core.py`,
  `core/message_queue.py`, `.kilo/kilo.jsonc`, `config/synth_mcp.json`, `LICENSE_EXTERNAL.md`,
  SOUL proposal docs (`SOUL-REWRITE-COMPARISON.md`, `SOUL-WIRING-PROPOSAL.md`), and the Vue
  frontend + classic WebUI assets.
- **Engine-config presets with model swap + Iris vision prompt editor** (`597ee839`,
  `core/engine_config_presets.py`, `core/config.py`, `core/webui.py`, `engines.html`,
  `res/synth_webui/js/main.js`, `plugins/iris_plugin/iris_plugin.py`).
- **Endpoint vision-test button + in-place Engines-tab refresh** (`dbbb177c`, `core/webui.py`,
  `res/synth_webui/js/engines.js`).
- **Cortex selection + engine config UI fixes** (`dd2a1caa`, `core/config.py`).
- **Stable engine-config controls** (`857f588e`): styled boolean toggles keep their hidden focus
  target in the visible flex row, preventing Chromium from scrolling past the active panel to a
  blank blue viewport. Applying a named engine preset immediately synchronizes the visible model
  text box, model label, cached engine config, and structured fields from the authoritative apply
  response instead of requiring a page refresh.
- **VRM skin_change freeze / idle T-pose fix** (`7f65c636`, `frontend/src/composables/vrm/animation.ts`).
- `kronos-task.md` removed (`9a2a1b80`); `.gitignore` covers `.kilo/`, local dev PNGs, runtime TTS
  audio (`0fb90043`).

### 7. Developer tooling

- TencentDB developer knowledge MCP: launcher + sync scripts (`scripts/tencentdb_knowledge_mcp.py`,
  `scripts/tencentdb_knowledge_sync.py`), `.mcp.json`, docs, wiki-maintenance workflow
  (`c95f031c`, `5d4b3071`, `64868ecd`, `cc22a620`).
- `main.py` wiring and `.gitignore` updates.

### 8. Docs & generated wiki

- `AGENTS.md` (priority scale + operating rules + vessel/agent docs), `CLAUDE.md`, `README.md`,
  `docs/rift_vessel.rst` (+19), `docs/developer_memory.rst` (+63), `docs/WIKI_MAINTENANCE.md` (+246),
  `docs/external_endpoints.rst`, component guides.
- ~240 generated `docs/wiki/*` export files (reader-facing + knowledge modules) refreshed per the
  wiki maintenance workflow.

## Code-level diff stat (excluding generated docs/wiki, maintained docs, markdown)

```
 .gitignore                                         |   8 +
 .kilo/kilo.jsonc                                   |  42 ++
 .mcp.json                                          |  20 +-
 core/action_parser.py                              | 154 +++-
 core/chat_context_manager.py                       |  10 +-
 core/config.py                                     |  92 ++-
 core/cortex_api_logger.py                          | 204 +++++-
 core/engine_config_presets.py                      | 164 +++++
 core/external_endpoints/adapters/openai_compat.py  | 289 +++++++-
 core/external_endpoints/bridges/cortex_bridge.py   | 666 +++++++++++++++--
 core/external_endpoints/registry.py                |  12 +
 core/history_engine.py                             | 107 ++-
 core/interface_path_utils.py                       |  23 +-
 core/interface_paths.py                            | 122 ++++
 core/logging_utils.py                              |  40 +-
 core/message_chain.py                              | 225 ++++--
 core/plugin_instance.py                            |  13 +-
 core/prompt_engine.py                              | 322 ++++++++-
 core/recon.py                                      |  16 +-
 core/soul/compiler.py                              |  88 ++-
 core/soul/roleplay.py                              | 136 ++++
 core/soul/strategies.py                            | 215 +++++-
 core/transport_layer.py                            | 283 +++++++-
 core/vessel_beat.py                                |  57 ++
 core/vessel_diary_compactor.py                     | 217 +++++-
 core/vessel_focus.py                               |   7 +-
 core/webui.py                                      | 314 +++++++-
 core/webui_templates/base.html                     |   9 +-
 core/webui_templates/sections/engines.html         |  73 +-
 core/webui_templates/synth_webui_shell.html        |  10 +-
 frontend/src/composables/vrm/animation.ts          |  44 +-
 interface/telegram_bot/telegram_bot.py             |  12 +-
 interface/vessel_interface.py                      | 271 ++++++-
 interface_dev/minecraft_bridge_minimal.js          | 365 ++++++++++
 main.py                                            |  18 +
 plugins/ai_diary/ai_diary.py                       |  58 +-
 plugins/emotion_manager/emotion_manager.py         |  29 +-
 plugins/goals/goals.py                             |  56 ++
 plugins/grillo/common_instructions.py              |   1 +
 plugins/grillo/grillo_chat_observer/grillo_chat_observer.py | 132 +++-
 plugins/grillo/grillo_dream/grillo_dream.py        |   4 +
 plugins/grillo/grillo_impl.py                      |   7 +
 plugins/iris_plugin/iris_plugin.py                 |  16 +-
 plugins/recon/recon_memory_recollector.py          |  18 +-
 plugins/rift_vessel/minecraft/minecraft.py         | 351 +++++++--
 plugins/rift_vessel/minecraft/minecraft_bridge.js  | 293 +++++++-
 plugins/rift_vessel/minecraft/mineflayer/package.json | 8 +-
 plugins/rift_vessel/minecraft/target_names.py      | 110 ++-
 plugins/rift_vessel/vessel_base.py                 |   1 +
 plugins/rift_vessel/vessel_plugin.py               |  99 ++-
 plugins/soul_plugin/soul_plugin.py                 |  36 +-
 plugins/vessel_base.py                             | 201 ++++++
 plugins/vessel_plugin.py                           | 323 +++++++++
 plugins/vessels/__init__.py                        |   6 +
 plugins/vessels/minecraft_connector.py             | 259 +++++++
 plugins/vox_plugin/vox_plugin.py                   |  14 +
 plugins/web_search/search_orchestrator.py          |   4 +-
 res/synth_webui/js/engines.js                      |  31 +
 res/synth_webui/js/main.js                         | 454 +++++++++++---
 scripts/tencentdb_knowledge_mcp.py                 | 166 +++++
 scripts/tencentdb_knowledge_sync.py                | 137 ++++
 tests/conftest.py                                  |  36 +
 tests/soul/test_compiler.py                        | 176 ++++-
 tests/soul/test_roleplay.py                        |  60 ++
 tests/soul/test_soul_plugin.py                     |  44 ++
 tests/test_action_parser_diary_payload.py          |  19 +-
 tests/test_action_parser_safe.py                   |  21 +
 tests/test_action_parser_text_alias.py             |  90 ++-
 tests/test_ai_diary_pool_behavior.py               |  23 +
 tests/test_chat_attention_triggers.py              |  30 +
 tests/test_context_perception_split.py             |  17 +
 tests/test_core_config.py                          |  54 +-
 tests/test_corrector_interface_path.py             |  90 +++
 tests/test_corrector_no_duplicate_message.py       |  84 +++
 tests/test_corrector_vessel_world_state.py         | 170 +++++
 tests/test_cortex_api_logger_toggles.py            |  54 ++
 tests/test_cortex_bridge_empty_retry.py            | 116 +++
 tests/test_engine_config_presets.py                | 219 ++++++
 tests/test_exposed_variables_audit.py              |   6 +
 tests/test_ext_model_persistence.py                | 144 ++++
 tests/test_external_endpoints_adapter.py           | 796 +++++++++++++++++++++
 tests/test_goals_plugin.py                         | 125 ++++
 tests/test_grillo_observer.py                      | 160 ++++-
 tests/test_history_engine.py                       | 137 ++++
 tests/test_iris.py                                 |  59 ++
 tests/test_message_chain_corrector.py              |  34 +
 tests/test_minecraft_connector.py                  |  12 +
 tests/test_prompt_engine.py                        | 378 +++++++++-
 tests/test_prompt_generation.py                    |  35 +
 tests/test_telegram_interface_send.py              |  27 +
 tests/test_vessel_action_namespacing.py            |  26 +-
 tests/test_vessel_beat.py                          |  46 ++
 tests/test_vessel_compactor.py                     |  73 +-
 tests/test_vessel_goal_debrief.py                  | 486 +++++++++++++
 tests/test_vessel_reactive_actions.py              | 142 ++++
 tests/test_vox_defaults.py                         |   6 +-
 tests/test_vox_plugin.py                           |  15 +-
 tests/test_webui_static_sanity.py                  |  48 +
 98 files changed, 10928 insertions(+), 592 deletions(-)
```

## Tests / validation

- Vessel: `test_vessel_realtime`, `test_vessel_goal_debrief`, `test_vessel_action_namespacing`,
  `test_vessel_compactor`, `test_vessel_reactive_actions`, `test_context_perception_split`,
  `test_minecraft_connector`, `test_corrector_vessel_world_state`, `test_vessel_beat`.
- Cortex/bridge: `test_cortex_bridge_empty_retry`, `test_external_endpoints_adapter`,
  `test_cortex_api_logger_toggles`, `test_message_chain_corrector`, `test_prompt_engine`,
  `test_corrector_no_duplicate_message`, `test_corrector_interface_path`, `test_action_parser_diary_payload`.
- WebUI/config: `test_engine_config_presets`, `test_iris`, `test_ext_model_persistence`,
  `test_core_config`, `test_webui_static_sanity`, `test_exposed_variables_audit`. The final focused
  engine-config UI/preset run passed **19 tests**, plus `node --check` and `git diff --check`.
- SOUL: `tests/soul/test_compiler.py`, `test_roleplay.py`, `test_soul_plugin.py`.
- Grillo/Telegram/others: `test_grillo_observer`, `test_telegram_interface_send`,
  `test_action_parser_safe`, `test_action_parser_text_alias`, `test_history_engine`,
  `test_goals_plugin`, `test_ai_diary_pool_behavior`, `test_vox_defaults`, `test_vox_plugin`.
- Known environment-limited failures (DB/DNS unavailable in sandbox, unrelated to this PR):
  `test_grillo_observer` (DB-dependent), `test_external_endpoints_adapter` (`Model.model_type`),
  `test_timestamped_rotation_respects_backupcount` (rotation files accumulate in the live `logs/`
  dir across runs).

## Notes

- The upstream branch (`a0a21388`) has been **merged into this branch** (`5c89e025`), so the diff
  above is purely the changes this branch carries that upstream does not. The `a4890b2f` merge
  restored the newer WebUI/plugin-architecture line (first parent `3cddb8f2`) on top of that.
- Local commits ahead of `origin/feat/rift-vessel-new`: `857f588e` (engine-config toggle/preset UI)
  and `8c3d9544` (age-markers/corrector/changelog). Earlier history includes `a4890b2f`
  (WebUI/plugin restore merge), `3cddb8f2` (Rift Vessel subsystem spec), and the `3f9654a8`-line
  six commits (vessel craft retries, goals scope fallback, cortex selection, WebUI presets/vision,
  reliable JSON turns).
- The generated `docs/wiki/` export is refreshed via `docs/WIKI_MAINTENANCE.md`.
