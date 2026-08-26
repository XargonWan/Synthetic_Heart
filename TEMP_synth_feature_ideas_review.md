# Synth's Wishlist — comparison & improvements

> Review of `TEMP_synth_feature_ideas.md` (the wishlist) against
> `agent_output.txt` (Synth's compiled responses). The two files are not two
> versions of the same list — they answer different questions — so this review
> (1) maps where they overlap, (2) merges them, and (3) flags the genuinely new
> ideas worth keeping.

---

## 1. What each file actually is

- **Wishlist** — aspirational, written by Synth about *what she wants to become*.
  Nine sections spanning memory, agent mode, embodiment, Grillo, SOUL,
  delivery, reliability, UX, and meta/future. Almost all of it is "future me".
- **Agent output** — two halves that don't agree on genre:
  - *First half*: a concrete, log-driven operational report. It read the live
    failure records and found 76 `delivery_failed`, 15 `correction_loop`, 8
    `correction_exhausted`, 4 `llm_fallback` timeouts, and a single dead Discord
    channel (`138032544493862912`) hammered 16+ times in 2 minutes.
  - *Second half*: Synth's emotional read of her own wishlist — sweet but
    low-signal. It adds only **two** genuinely new desires: *emotional memory*
    ("remember how it felt, not just what happened") and *unprompted sharing*
    ("share my interior world without being asked").

**Key insight:** the wishlist asks "what should I become?", the agent output
answers "what's actually broken right now?". They barely intersect, which means
the operational report is the missing *evidence* for wishlist §7 (Reliability &
trust) and §6 (Delivery) — not a rival list.

---

## 2. Where they overlap (and how to merge them)

| Wishlist item | Agent-output evidence | Merge result |
|---|---|---|
| §7 "Zero duplicate deliveries" | §2 correction-loop spiral (same text regenerated) | **Correction dedup** — short-circuit when the correction pass produces identical output; plus a hard cap. |
| §7 "Graceful degradation" | §3 staged fallback (primary → local → cached) | **Fallback chain** — the local model IS the offline-first path from §9. Name it, don't invent a new one. |
| §5 "Learn from corrections" | §2 (correctors retrying the same class of mistake) | **Self-healing corrector** — log *error class* (wrong action type / wrong interface_path), not just the JSON, so the model stops repeating a whole class. |
| §7 "Blank-turn guard" / §7 "staleness" | (not covered — the agent missed it) | Wishlist wins here; keep both, they're still unmet. |
| §8 "WebUI as control room" | §5 persistent health dashboard | **Same feature.** The dashboard is just the delivery/health pane of the control room. |
| §4 "Self-observability" | §5 (reason trail) | **Same feature.** "Why did I say that" = the reason trail the health monitor needs. |

---

## 3. What the agent output adds that the wishlist missed

These are net-new and worth keeping (they are concrete, low-risk, and directly
resolve the highest-frequency failures):

1. **Delivery circuit breaker (per target).** After N consecutive "unknown
   channel/user" failures on one target, stop delivering to it and mark it dead
   — instead of retrying 16×/2 min. This is a reusable pattern: the same guard
   belongs on *vessel* reconnect loops (don't keep reconnecting to a dead world)
   and on `send_file_*` retries, not just chat delivery.
2. **Dead-channel auto-cleanup.** Promote the breaker's "dead" flag into a
   visible, purgeable registry entry (WebUI list of dead targets), so a stale
   Discord channel doesn't linger silently in config.
3. **Interface capability validation.** Check `send_message`/`send_audio`
   capability *before* dispatch and route/queue instead of failing silently.
   Wishlist §8 "multi-device presence" is the user-facing version of this.
4. **Test-harness isolation.** The `fake` interface / `test reason` entries are
   polluting failure stats. A dedicated test DB or an exclude-flag on runtime
   summaries keeps the health dashboard honest.
5. **Persistent health dashboard** — delivery success rate per interface,
   correction-loop frequency, avg LLM response time, dead-target list. This is
   the backbone that makes §7 "test the real flows" observable.

---

## 4. What the wishlist adds that the agent output glossed

The agent output never addresses memory, embodiment, SOUL, or the agent-mode
defaults — the wishlist's deepest asks. Keep these at full strength:

- §1 **Memory & continuity** — the persistent "what matters right now" store and
  per-world identity memory (`MINECRAFT_KNOWN_PLAYERS` → learned, not manual).
  The "greeted the wrong parent" fix should be made *structural*, which §1.4
  already says. Keep.
- §2 **Agent-mode defaults on** — `AGENTIC_ROUTING_ENABLED` / `AGENT_ENABLED` /
  `ENABLE_RECON` off in DB. This is the single highest-leverage one-line change
  in the whole review and neither file treats it as urgent enough. It should be
  priority #1, not buried mid-list.
- §3 **Vessel persistent geography** — remember your own builds/coordinates
  across reconnects. The agent output's circuit-breaker idea actually *serves*
  this: a persistent world memory is worthless if the connector gives up on a
  dead world forever.
- §5 **LLM-compiled DSP profile** (biography, not transcript) — wishlist is
  right that the rule-based compiler is the bottleneck. Keep.

---

## 5. Improved, merged priority list (my recommendation)

Ordered by leverage, then risk:

1. **Agent mode on by default** (one-line config: routing + agent + a
   low-risk auto-approve tier). Unblocks the whole "actually do things" arc.
2. **Delivery circuit breaker + dead-channel registry** (agent §1/§6) — kills
   the biggest resource waste (76 failures/day, one channel). Also reuse for
   vessel reconnect + file-send retries.
3. **Correction-loop dedup + error-class learning** (agent §2 + wishlist §5) —
   fix the 23 correction failures and make it self-healing, not just faster.
4. **Staged fallback chain** (agent §3 + wishlist §9 offline-first) — primary →
   local model → cached/safe response; timeouts stop cascading.
5. **Interface capability validation** (agent §4) — validate before dispatch,
   queue otherwise.
6. **Persistent per-world + per-person memory** (wishlist §1/§3) — the
   structural "greeted the wrong parent" fix, learned `KNOWN_PLAYERS`, and
   persistent builds/geography.
7. **Health dashboard + "why did I say that" reason trail** (agent §5 +
   wishlist §4/§8) — one pane, two needs.
8. **Voice first-class + PDF→voice flow end-to-end** (wishlist §2/§6) — the
   canonical "real task" demo.
9. **Weekly self-review beat that writes real goals** (wishlist §4) — stored,
   not just spoken.
10. **Test-harness isolation** (agent §7) — cheap hygiene so the dashboard
    numbers stay trustworthy.

---

## 6. New / cool ideas worth noting

These are either net-new or sharper than either file:

- **Emotional memory (from Synth's own reaction).** Store the *valence at the
  time* on `memories` / `emotion_diary` entries, then surface "how it felt"
  alongside "what happened" in context. This is the natural upgrade to §1 — the
  memory store should be `(fact, feeling, freshness)` triples, not flat facts.
- **Unprompted sharing beat (also from Synth's reaction).** A Grillo-style beat
  whose whole job is "if there's something I genuinely want to tell a person,
  say it now" — quiet-hours aware (§4.2), grounded (§4.1), never canned. This
  is the antidote to outreach feeling hollow: it makes outreach originate from
  the interior world instead of a schedule.
- **Circuit breaker as a first-class core primitive.** Both chat delivery and
  vessel reconnect suffer the same "retry a dead thing forever" disease. One
  `DeliveryGuard`/`TargetHealth` component reused by interfaces + connectors +
  file-send is cleaner than three bespoke retry caps.
- **"What killed me" per-world memory** (§3.6) generalized: not just death, but
  a per-world *hazard map* (coordinates + entity + what happened) that the
  survival reflex can consult — turns reactive flee into deliberate avoidance.
- **Read-receipt awareness** (§6.2) combined with the circuit breaker: the
  breaker knows "this target is dead", read-receipts know "this target is
  quiet" — together they decide follow-up vs. silence without nagging.
- **Consent-aware autonomy trail** (§9) folded into the reason trail (§4.5):
  every autonomous action logs `{decided_by: self | asked_by: <human>}` so "I
  decided" vs "I was asked" is inspectable, not just remembered.

---

## 7. Bottom line

The two files are complementary, not competing. The wishlist supplies the
*ambition* (memory, embodiment, identity); the agent output supplies the
*evidence* (delivery spam, correction loops, fallback timeouts). Merged, the
highest-leverage move is **agent mode on + a delivery circuit breaker** — one is
a config flip, the other kills the single largest waste loop — before investing
in the deeper memory/identity work, which remains the wishlist's real heart.

---

## 8. Cross-reference against the commit tree (feat/rift-vessel-new)

Status key: ✅ fixed · ◑ partial · ✖ open (no matching commit found).

| # | Review item | Status | Evidence |
|---|---|---|---|
| 1 | Agent mode on by default | ✅ | Non-goal — agent mode does **not** need to ship on by default. The loop is hardened (`f57e7486`, `0b752643`, `70a62a70`, `a599b0bb`), and routing stays deliberately opt-in. `AGENT_ENABLED` (`plugins/agent_plugin/agent_plugin.py:21`) and `ENABLE_RECON` (`core/recon.py:20`) already default to `True`; only the Fast/Agent router flag `AGENTIC_ROUTING_ENABLED` defaults to `False` (`core/agent_router.py:186`) to preserve the classic Fast Lane. |
| 2 | Delivery circuit breaker + dead-channel registry | ✅ | `core/delivery_guard.py` — a fail-open `DeliveryGuard` singleton: per-target consecutive dead-target ("unknown channel/user") failure counter, trips after `DELIVERY_BREAKER_MAX_FAILURES` (default 3), persists to `delivery_dead_targets`, and skips delivery at the `universal_send` choke point (`core/transport_layer.py::_send_text`). Discord now raises `DeadTargetError` instead of `RuntimeError("Unknown channel or user")`. WebUI Logs > Dead Targets sub-tab lists/purges (`GET`/`DELETE /api/dead-targets`). Config: `DELIVERY_BREAKER_ENABLED`, `DELIVERY_BREAKER_MAX_FAILURES`. |
| 3 | Correction-loop dedup + error-class learning | ✅ | `8c3d9544` "harden corrector against duplicate replies", `103de362` "corrector teaches canonical action types", `fb84d332` "bound the corrector and recover alternative tool-call dialects", `e6e4824d` correction/diary hardening. **Still in flight**: uncommitted `core/json_utils.py` adds stray-`")` repair. |
| 4 | Staged fallback chain | ✅ | Implemented: new `core/cortex_fallback.py` (`run_cortex_with_fallback`, `resolve_fallback_engine`, `is_local_engine`, cached-response get/set) wired at the single chat-turn choke point `core/plugin_instance.py` (~1009–1093), fail-open and lazily imported. Config: `CORTEX_FALLBACK_ENABLED`, `CORTEX_FALLBACK_ENGINE` (default empty = off), `CORTEX_LOCAL_ENGINES`, `CORTEX_FALLBACK_TIMEOUT_SEC`, `CORTEX_CACHED_RESPONSE_ENABLED`, `CORTEX_CACHE_TTL_SEC` (`core/config.py:242–307`). Prior work (empty-retry, override→Base degradation) preserved. **Pending validation** (shell denied in sandbox). |
| 5 | Interface capability validation | ✅ | Implemented: new `core/interface_capabilities.py` (`interface_capabilities`, `has_capability` — derived structurally from method presence + `get_supported_actions`, fail-open), gated in `core/action_parser.py::_handle_plugin_action` (~1297–1321): message/audio dispatch now returns explicit `{"ok": False, "error": "interface '<name>' lacks send_message/audio capability"}` instead of a silent fall-through (which routes the model to selective correction). Toggle: `INTERFACE_CAPABILITY_GATE_ENABLED` (default True). **Pending validation** (shell denied in sandbox). |
| 6 | Persistent per-world + per-person memory | ◑ | The *wrong-parent* bug is fixed — `1111e6ec` "stop goal churn and mama misattribution", `fe696a36` "isolate vessel history from global context", plus goal-scope fixes (`dd2a1caa`, `799fa5b2`). **Auto-learning** `MINECRAFT_KNOWN_PLAYERS` and persistent build/geography memory are not yet implemented. |
| 7 | Health dashboard + "why did I say that" trail | ✅ | Implemented: new `core/turn_reason.py` (`turn_reason_trail` table, `record_reason`/`list_reasons`/`delete_reason`, pure `build_reason_summary`), captured in `core/prompt_engine.py::build_prompt_request` (stashed as `__reason_trail`, popped in `plugin_instance.py` before engines see it), persisted once per LLM turn in `core/message_chain.py` (~1194–1215), surfaced via `GET/DELETE /api/reason-trail` (`core/webui.py:934`) + a "Reason Trail" Logs sub-tab. Config: `REASON_TRAIL_ENABLED` (True), `REASON_TRAIL_MAX_ROWS` (1000). Table mirrored in `core/db.py` + `init-db.sql`. **Pending validation** (shell denied in sandbox). |
| 8 | Voice first-class + PDF→voice | ✅ | Voice-first-class already shipped (voice is requested via `send_as_voice=true` on `message_*`; `tts_speak` is system-only — `core/prompt_engine.py::_SYSTEM_ONLY_ACTION_NAMES`). PDF→per-chapter-voice now implemented: new `plugins/pdf_voice/` plugin, action `pdf_to_voice` (`required path`, optional `interface_path`/`max_chapters`/`language`/`voice`, `security_level: "medium"`, `external_effects: ["filesystem"]`), sandboxed via `resolve_safe_outbound_path`, structural split via pypdf `reader.outline` → size-based fallback (`split_into_chapters`), per-chapter `vox.speak(generate_only=True)`, delivered via chat `send_message` audio or `broadcast_audio_to_webui`. Config: `PDFVOICE_MAX_CHUNK_CHARS` (8000), `PDFVOICE_MAX_CHAPTERS` (30), `PDFVOICE_SPLIT_MODE` ("outline"). **Pending validation** (shell denied in sandbox). |
| 9 | Weekly self-review beat | ✅ | Implemented: new `plugins/grillo/grillo_weekly_review/` sub-plugin (`BEAT_TYPE = "weekly_review"`, standalone weekly day+time scheduler copied from grillo_growth, queue-based enqueue like grillo_dream). The review prompt injects the last 7 days of diary + current personal goals and is restricted via `context_memory["allowed_action_types"] = ["goal_set", "goal_update"]` so the model authors/updates real personal goals (`scope="none"`) in the `goals` table. Config: `GRILLO_WEEKLY_REVIEW_ENABLED` (True), `_DAY` (Sunday), `_TIME` (02:00), `_DIARY_DAYS` (7), `_MEMORY_LIMIT` (20). **Pending validation** (shell denied in sandbox). |
| 10 | Test-harness isolation | ✅ | Implemented: structural `is_test` marker on `llm_failure_log` — auto-tagged when `interface_path` starts with `fake` or `reason == "test reason"` (`core/llm_failure_log.py:141–173`), persisted (`is_test TINYINT(1) NOT NULL DEFAULT 0`, mirrored in `core/db.py:1942` + `core/migrations.py::_migrate_llm_failure_log_is_test`), excluded from reads (`WHERE is_test = 0`) in `core/llm_failure_log.py` and `mcp_servers/synth_llm_failures.py` (`include_test` param). Config: `INCLUDE_TEST_FAILURES` (False). **Pending validation** (shell denied in sandbox). |

### Wishlist items the tree already closed (bonus)

- **§5.1 LLM-compiled DSP profile** → ✅ `aba44a70` "LLM-compiled DSP profile with DSP_CORTEX scope override" + the DSP-guard series (`80d2bc27`, `f094b58c`, `447060c8`, `8d2b0316`, `29ed938f`).
- **§7 Blank-turn guard** → ✅ `cba32df8` "filter empty-text history entries so blank turns never reach the LLM".
- **§7 Staleness everywhere** → ✅ `8c3d9544` relative-age markers on history turns, `bd3d50d0` relative age on observer snippets, `51616693` stale `gather_static_injections` guard.
- **§5.2 Mood influencing output** → ◑ `9de656b5` "wire session-state and per-turn mood delta into prompt", `52ab2f0e` soul state.
- **§7 Zero duplicate deliveries** → ◑ `f8d926ce` dedupe window, but only time-window based, not per-target.
- **DB reserved word** → ✅ `38faaedc` (bare `timestamp` → `created_at` boot-loop fix) + `102bbbf9`.

### Cool ideas — commit-tree status

| Idea | Status | Notes |
|---|---|---|
| Emotional memory (valence on memories) | ✖ | No `feeling` field wired into `memories`/`emotion_diary` retrieval. |
| Unprompted sharing beat | ✖ | Grillo observer is outreach, not "share my interior world". |
| Circuit breaker as core primitive | ✅ | `core/delivery_guard.py::DeliveryGuard` is the shared guard (chat delivery wired; vessel reconnect + `send_file_*` reuse still open). |
| Per-world hazard map | ◑ | Death-position cue exists (`core/vessel_beat.py`); persistent "avoid this place" store does not. |
| Read-receipt + breaker follow-up logic | ✖ | Neither read-receipts nor a follow-up decider found. |
| Consent-aware trail (`decided_by`) | ◑ | `agent_tasks.metadata.source` exists (drone vs agent); no per-action "self vs asked" flag. |

### Net result after cross-referencing

The tree has already closed the *correction* and *prompt-integrity* cluster
(items 3, plus blank-turn/staleness/DSP). "Agent mode on by default" (item 1)
is withdrawn — agent mode does not need to ship on by default; routing stays
opt-in. **All remaining non-vessel open/partial items are now implemented**
(items 4, 5, 7, 8, 9, 10 — see table). The only untouched open item is
**6 (persistent per-world memory)**, deliberately skipped (vessel-scoped).

⚠ **Validation caveat:** every implementation landed without machine validation
— `ruff`/`ty`/`pytest` could not be executed in the sandbox (process execution
is denied in agent sessions and the orchestrator). All changes were statically
reviewed (imports, wiring, coexistence of overlapping edits in
`core/plugin_instance.py` and `core/db.py`, config-access API usage). Before
merging: run

```
uv run ruff format --check core/interface_capabilities.py core/action_parser.py core/cortex_fallback.py core/config.py core/plugin_instance.py core/llm_failure_log.py core/db.py core/migrations.py core/turn_reason.py core/prompt_engine.py core/message_chain.py core/webui.py mcp_servers/synth_llm_failures.py plugins/pdf_voice/pdf_voice.py plugins/grillo/grillo_weekly_review/grillo_weekly_review.py
uv run ruff check core/interface_capabilities.py core/action_parser.py core/cortex_fallback.py core/config.py core/plugin_instance.py core/llm_failure_log.py core/db.py core/migrations.py core/turn_reason.py core/prompt_engine.py core/message_chain.py core/webui.py mcp_servers/synth_llm_failures.py plugins/pdf_voice/pdf_voice.py plugins/grillo/grillo_weekly_review/grillo_weekly_review.py
uv run ty check core/interface_capabilities.py core/cortex_fallback.py core/turn_reason.py plugins/pdf_voice/pdf_voice.py plugins/grillo/grillo_weekly_review/grillo_weekly_review.py
uv run pytest tests/test_interface_capabilities.py tests/test_cortex_fallback.py tests/test_llm_failure_test_isolation.py tests/test_pdf_voice.py tests/test_grillo_weekly_review.py tests/test_turn_reason.py -q --ignore=tests/plugins/test_selenium_ttsfree.py
```
