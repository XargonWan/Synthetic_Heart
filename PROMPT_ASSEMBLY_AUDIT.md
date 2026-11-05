# Prompt Assembly Audit — D18 (persona "2D")

**What this is:** a complete map of everything that happens between an inbound message and the
`messages[]` array the LLM receives, measured on the live D18 deployment, plus a ranked list of
places where the final prompt can be shrunk with minimal behavioural impact.

**Evidence basis** (all from D18, 2026-09-22):

| Source | What it gave |
|---|---|
| Langfuse trace `2c6d5617-7a97-4182-84fe-7df6a467c401` (16:58 Telegram turn, post-fix) | the exact system message + history the model saw; dumped to `%LOCALAPPDATA%\Temp\d18prompt\chat1_*` |
| Langfuse trace `922d301f-...` (14:57 turn) | second sample of the same shape |
| Langfuse trace `bc047098-...` (17:00 G.R.I.L.L.O. `tag_elaboration` beat) | internal-beat composition |
| `logs/synth.log` (12 MB, whole day) | every build size, reducer firing, clamp warning, injection key list |
| Runtime config + endpoint rows (D18 DB) | the values in §13 |
| Source at `D:\dev\D18` | the code paths cited inline |

Reproduce: `scripts/prompt_blocks.py <trace_id> --json`; raw dump script used here is
`%LOCALAPPDATA%\Temp\d18prompt\dump_trace.py` (reads `LANGFUSE_*` from `.env`).

---

## 0. Update, 2026-09-24 (revision 2): what changed, and what it measures now

**Everything in §1 to §15 is the 2026-09-22 baseline.** This section carries the changes made since,
with the numbers they moved; the sections below have been corrected wherever a change invalidated them.
Both listed commits are **live** since the 15:13 restart on 2026-09-24 (§0.6 has the verification).

### 0.1 Changes that affect prompt assembly

| Change | Commit | Effect on the prompt |
|---|---|---|
| The active chat's history is counted in **exchanges**, not messages (`CONTEXT_EXCHANGE_WINDOW=5`, `CONTEXT_EXCHANGE_CHAR_CAP=8000`) | `d01936d2`, `2042540c` | the window now reaches five human turns back instead of being eaten by one long monologue (adjacent persona lines also used to merge and orphan-trim). Verified live: the 12:10 turn carried the last five human turns and 8,441 prompt tokens |
| Cross-chat lines carry their speaker (`self (you):`, `said by X`) plus a NOTE rule stating it | `173a6781` | the block grew from 1,927 to **3,730 chars** on a chat turn and **7,965** on a beat (the NOTE alone is 525) |
| The reducer spends the catalogue's redundant detail **before** the conversation | `1fcb3cf2` | order is now `examples` → brief-only → `history_recent` → `history_current_chat` → `memories` → other context → emergency (§10.1 rewritten) |
| The reducer's size report names `instructions` and `input`, and each catalogue step reports the size it produced | `1fcb3cf2` | a failure names its own culprit; §10.1's old report blamed the context for a 143k `input` |
| A diary day is consolidated in parts (`GRILLO_DIARY_CONSOLIDATE_CHUNK_CHARS=25000`) | `30b5171b` | the day that broke sent ~130,000 chars in one call; it is now ~25,000 per call, about six calls, each cheaper than the last |

### 0.2 The measured live chat turn now (13:05 local, trace `7807cfd9`, user Scar)

| Block | 09-22 | now | Note |
|---|---|---|---|
| persona (`=== CRITICAL SYSTEM IDENTITY ===`) | 2,057 | 1,996 | |
| instructions (`=== JSON RESPONSE INSTRUCTIONS ===`) | 4,948 | 4,948 | unchanged |
| `=== AVAILABLE ACTIONS ===` | 8,332 | 8,333 | the scope gate holds it steady (§6) |
| `[SYSTEM: REALITY ANCHOR]` | 422 | 410 | |
| `[Temporal context]` | 1,213 | **0** | absent on this turn |
| `[Persona background]` | 119 | 120 | |
| `[Self-growth]` | 844 | **1,674** | doubled, uncapped |
| `[Home]` / `[Weather]` / `[House]` | 217 / 310 / 62 | **0 / 0 / 0** | absent on this turn |
| `[Recent context from other conversations]` | 1,927 | **3,730** | 17 lines now, plus a 525-char NOTE |
| `[Relevant memories]` | 4,213 | 4,315 | 11 entries, 8+ at the 400-char cap |
| **system message** | 24,748 | **25,601** | |
| history (outside the system message) | 5,331 / 7 msgs | **9,356 / 8 msgs** | exchange-counted window |
| **provider payload** | 30,079 | **34,957** | 8,488 prompt tokens |

### 0.3 The measured live beat now (13:37 local, trace `f5e3bf3b`, `user=G.R.I.L.L.O.`)

System 29,264 + user body 12,411 = **41,675 chars, 10,160 prompt tokens**; blocks: actions 8,310,
`[Recent context from other conversations]` **7,965**, memories 4,416, instructions 4,436, self-growth
1,536, persona 1,996; the user body carries `eligible_targets` 937, `grillo_friendly` 1,636,
`grillo_proactivity` 4,624, `json_format_reminder` 207. The cross-chat block is now the largest
non-catalogue block on both routes.

### 0.4 Firings since the audit (all live, all 2026-09-24, all with the 09-22 code)

| When (local) | Prompt | Limit | What happened |
|---|---|---|---|
| 08:29 | 101,043 | 100,000 | over by 1,043 with a **40,588**-char catalogue in hand |
| 09:29 | 104,641 | 100,000 | over by 4,641, catalogue 40,588 |
| 11:37 (two) | 101,107 / 101,861 | 100,000 | over by 1,107 / 1,861, catalogue 39,051 |
| 02:22 | 143,375 | 100,000 | `diary_consolidation`: the weight was its own `input`, one diary day of ~130,000 chars. The reducer reported a 6,783-char context, deleted every context field for nothing, finished at 136,233, and the bridge logged `budget is unreachable (protected system + current turn alone: 138476)` |

The four beat firings are what `1fcb3cf2` addresses: they were 1,043 to 4,641 chars over, and the
catalogue's `examples`/schema alone can cover that, so under the new order they lose catalogue detail
instead of history lines. The 02:22 case could not be fixed by any ordering, because `input` is
protected; that is what `30b5171b` fixes at the source. (The `Prompt size 99227 exceeds limit 52000`
lines in the log are pytest fixtures, not live.)

### 0.5 New findings, not yet actioned

1. **A duplicated entry in `[Recent context from other conversations]`.** On the measured chat turn the
   11:37 message appears twice (two entries, ≈ 900 chars). The same block also carries age markers
   (`[1 hour earlier]`) inside the quoted text.
2. **`memories` is empty, and that is a bug, not a tidiness detail.** Every "memory" the prompt shows
   comes from `ai_diary.context_tags`, `chat_history` recall or SOUL recall, but `memories` is the store
   the compacted summaries are supposed to land in, and it holds 0 rows while `archived_memories` holds
   14 and the sources of those clusters are already deleted from `ai_diary`. See §0.6 and §16.3.
3. **`[Self-growth]` doubled** (844 → 1,674) with no cap (§11 item 14 is now worth about 800, not 300).
4. **The diary no longer risks the budget** (chunked), but §12.1's rendering gap still stands: the
   diary's own contribution and static injection render nothing on a chat turn, and only the recall
   tier (§8 path C) reaches the model.
5. **Store sizes** (D18, live): `ai_diary` 18 rows / 111,113 chars; `situational_notes` 44 rows / 5,679
   chars, of which 5 are active / 885 chars; `memories` 0 rows.

### 0.6 Post-restart verification (2026-09-24, boot 15:13, manual beat 15:13:47, chat turn 15:27)

Everything below was read from the live process after the restart that loaded `1fcb3cf2` and `30b5171b`.

* **The reducer is live and silent.** No `[reduce_prompt]` firing since the restart, and the whole log
  contains one clamp warning (the 02:22 diary case). The new report format is visible in the earliest
  post-commit test run: `Prompt size 25507 exceeds limit 25407 ... now 5367 chars` after the catalogue
  slim step, i.e. the actions block gives up its detail first and reports the size it produced.
* **The manual beat** (observer route, 15:13:47): `final size: 76050 chars`, `prompt_len=76050`,
  `pre_reduction_size=35418`, completed and sent. Under both thresholds, no reduction.
* **The chat turn at 15:27** (trace `b93e9ea0`): system 24,661 / total 30,662 chars, **7,282 prompt
  tokens**, no reduction, no clamp. Blocks: persona 1,996, instructions 4,948, actions 8,333, reality
  anchor 410, persona background 120, self-growth 1,753, cross-chat 2,563, memories 4,463, history
  4,374 chars over 4 messages.
* **The exchange window is live and the char cap is what binds it.** The 15:27 history holds 2 exchanges,
  not 5, because `CONTEXT_EXCHANGE_CHAR_CAP=8000` drops the oldest whole exchanges and this chat's
  exchanges run 3,000-5,000 chars each. Raising the cap to ~12,000 is what buys the depth the 5-exchange
  setting intends; the current turn only costs 7.3k tokens, so there is room.
* **The diary consolidator's part split works live** (runs at 13:46 and 13:52, before this restart):
  `Day 2026-09-01 is 423 characters: consolidating PART 1 of 2 (211 chars sent, 212 kept for a later
  run)` followed by `Entry 51: part-merge of 16/41 characters, kept 25 for a later run`, and no
  `Beat context handoff skipped` line anywhere.
* **Live chunk value was drifted: 40,000, not the documented 25,000.** Reset to 25,000 on 2026-09-24
  (config table, no restart needed). At 40,000 a part's protected set (system + one part) lands at
  53k+ rendered against the 50k clamp budget, so every part logs the `budget is unreachable` warning and
  costs about 2.5x the tokens of a 25,000 part. Today's row is 46,702 chars, so it is the first day that
  will actually exercise the split.
* **Memory compaction is broken in a way that matters more than any of the above** (§0.5.2 corrected,
  §16 rewritten): the nightly compactor runs at 03:00 and writes its summaries to `archived_memories`
  (14 rows, levels 1, sources 2-6 rows, 19,493 chars compacted to 139) and deletes the sources from
  `ai_diary` (34 rows in `ai_diary_archive`), but `memories` is **empty**: the compacted memory never
  lands. `memories` is what the recall path reads (`core/prompt_engine.py:3533`, `:3652`,
  `plugins/memory_search/memory_search.py:492`); nothing reads `archived_memories`. So the content those
  14 clusters cover is currently unreachable from the prompt even though it was never deleted.

---

## 1. TL;DR

*The block numbers in this section are the 2026-09-22 baseline. §0.2 and §0.3 have the current ones.*

1. **The log's "final size: 64,364 chars" is NOT the prompt.** It is `len(json.dumps(prompt_dict))` —
   the serialized SyntH prompt, which carries the full per-action schema JSON that the renderer never
   emits. The provider payload for that turn was **30,079 chars** (system 24,748 + history 5,331) ≈
   **7.3k tokens**. Ratio measured on live turns: **2.1×**. Any "too massive" read must start from the
   rendered number, not the logged one.
2. **The compression commit worked, but the reducer is what ate her grounding before it.** Pre-fix
   Telegram turns serialized to **100,600–163,492 chars** (most between 157k and 163k) against the
   reducer's 100,000 limit; every one of them over the limit. The reducer's cut list on the 13:38:54
   turn literally reads
   `Removing memories section (11 entries, ~4574 chars)` then `thoughts`, `emotion_state`,
   `current_emotions_nl`, `available_emotions`, `home`, `home_weather`, `home_location`,
   `participants`, `channel_legend` … That is the "missing memories" regression, and it fired on
   **64 live turns today** (07:48–14:10). Post-restart (16:40+) turns serialize to 60k–64k, stay under
   the limit, and **the reducer has not fired once** — memories/emotion/home now survive intact.
3. **Where the rendered system message actually goes** (24,748 chars): instructions 4,948 · action
   catalogue 8,332 · relevant memories 4,213 · persona identity 2,057 · other-chats 1,927 · temporal
   context 1,213 · self-growth 844 · reality anchor 422 · weather 310 · home 217 · persona background
   119 · house 62.
4. **The single largest cuttable block is the action catalogue (8,332 chars).** 34 actions, of which
   1,972 chars are the generic `goal_*` trio, 740 `vessel_connect` (declared `scope: "core"` on
   purpose), 683 `spawn_drone`, 640 `pdf_to_voice`, 542 `apply_growth_proposal`. A per-action brief
   cap (the head/tail compaction already written for vessel briefs in lite mode) cuts ~2.5–3k chars
   with no loss of action availability.
5. **Second largest: blocks that are pure background and uncapped per line** — `[Recent context from
   other conversations]` (1,927 chars, 5 lines, one pair of lines is 1,540 chars) and `[Relevant
   memories]` (4,213 chars, 11 entries, 8 of them truncated at the 400-char cap). A 300-char per-line
   cap on the former (the live route already does exactly this) and a 250-char/8-entry budget on the
   latter remove ~3k chars with no behavioural loss.
6. **Lite mode is OFF in D18** (`PROMPT_LITE_MODE=0`) and, as configured, turning it on would be
   *worse*: `LITE_MODE_HISTORY_LIMIT=8` is larger than `CONTEXT_VERBOSITY=6`, so history would grow
   from 6 to 8 lines. Lite mode is only ever forced on for Vessel turns.
7. **Two independent budget systems overlap.** The up-front reducer compares the *serialized* dict
   against `max_chars` (Venice2: default 100,000) — i.e. it starts deleting grounding at roughly
   45–50k *rendered* chars. The bridge clamp then compares the *rendered* payload against
   `downstream_char_budget` (Venice2 now 50,000; other endpoints 24,000–30,000) and trims **older
   history only**, never the system message. The clamp is the graceful one; the reducer is the
   destructive one. Aligning the two (raise Venice2 `max_chars`; keep the clamp) removes the class of
   failure that caused the regression.
8. **The diary does not reach the chat prompt at all right now** (evidence in §8) — the log says
   *"Capped diary injection: 2 -> 2 entries"* but the rendered system message contains zero `[diary`
   lines, no `[Thoughts and diary entries]` block, while the three diary-sourced entries that DO reach
   the model arrive only via memory recall inside `[Relevant memories]`. Flagged for the parallel
   memory investigation, not fixed here.

**Applying the top cuts (§11, items 1–6) takes the live turn from 30,079 → ≈ 21–22k chars
(−28%), all from background/verbose blocks; nothing in §11 removes a capability without an explicit
design decision.**

---

## 2. The three size currencies — never compare them by eye

| Currency | Where it is produced | Live value (16:58 turn) | What it measures |
|---|---|---|---|
| **Rendered provider payload** | what the engine receives (`_build_messages` → `messages[]`) | **30,079 chars** (system 24,748 + 7 history msgs 5,331) | what the model actually reads |
| **Serialized prompt dict** | `len(json_dumps(prompt_with_instructions))` — logged as `final size:` at `prompt_engine.py:3263` and carried as `prompt_len=` on the handoff line | **64,364 chars** | the dict *including* per-action `schema`/`source` JSON the renderer never prints |
| **Reducer limit** | `model_limits_map` → `extra_config.max_chars` (default `100_000`) — `cortex_bridge.py:1869` | 100,000 (Venice2, default) | compared against the serialized dict |
| **Clamp budget** | `extra_config.downstream_char_budget` (default `24_000`) — `cortex_bridge.py:58,1473` | Venice2 **50,000**; `xtxx` 30,000; everything else 24,000 | compared against the rendered payload |

Practical consequences:

* The `final size:` log line overstates the prompt by ~2.1× on a chat turn (2.0–2.2× measured across
  the three traces; the action catalogue alone serializes to ≈ 39,100 chars while rendering to 8,332).
* `pre_reduction_size` on the handoff line is the serialized **context + input + instructions** before
  the catalogue is added (`prompt_engine.py:3035`); `final size − pre_reduction_size` isolates the
  catalogue's serialized weight. On the live turn: 64,364 − 25,269 = **39,095** (catalogue).
* A reducer limit expressed as "100,000 chars" therefore begins trimming when the *rendered* prompt is
  only ~45–50k. The bridge clamp's 50,000 is in real chars. The two are not the same unit.

---

## 3. End-to-end pipeline (input → Synth output)

```
interface (telegram_bot / discord_bot / synth_webui / vessel / …)
  │  inbound message → core message chain → queue (priority bands) → consumer
  ▼
plugin_instance.handle_incoming_message()                       ← the turn's entry point
  │  1. build_json_prompt(message, context_memory, interface_name)   core/prompt_engine.py:~2250
  │  2. handoff  → engine via cortex_bridge.handle_incoming_message() (–> LLM)
  │  3. LLM text → message_chain_handle (validation, action extraction, dispatch)
  ▼
build_json_prompt() stages (in order):
  A. context_section ← HistoryEngine.build_context()            core/history_engine.py:593
        history_current_chat, history_recent, thoughts, memories,
        history_scope, tags_placeholder, history_interface_paths
  B. gather_static_injections() over ~55 plugins                core/action_parser.py:2996
        20 keys merged into context_section (§5)
  C. plugin-block supersedes (weather/location) + persona extraction
        persona → instructions; drops the persona key from context
  D. instructions ← prompt_instructions.build_instructions(route, …)   core/prompt_instructions/
        route decided structurally (chat / chat_voice / vessel / grillo_internal /
        observer / delivery / live / agent); measured: route=chat 4,694 chars
  E. action catalogue: full registry → scope gate → vessel whitelist → recon drop
        → minify_actions_block(lite=is_lite)                     core/prompt_engine.py:143
  F. lite-mode context stripping (only when is_lite)             core/prompt_engine.py:2138
  G. reduce_prompt_for_llm_limit(prompt, max_chars)              core/prompt_engine.py:~3870
  H. PromptRequest built (typed IR), final size logged
  ▼
renderer  (core/prompt_renderers.py → JsonPromptRenderer)
  system_content = instructions + context blocks (fixed order)   ← the 24.7k system message
  conversation_history = history turns as user/assistant messages
  current user turn = [SYSTEM: REALITY ANCHOR] … + [lang|tone|…] + text
  ▼
cortex_bridge._build_messages()                                  core/external_endpoints/bridges/cortex_bridge.py
  _inject_actions_into_prompt()  → appends the "\n\nAVAILABLE ACTIONS\n- name: brief" block
  _clamp_messages_to_char_budget() → trims OLDER HISTORY only; system + current turn protected
  ▼
provider HTTP call (Venice2 / openai_compat …) → response text
  ▼
message_chain: JSON extract → run_action per action → interface delivery
```

Two size-relevant properties of the renderer worth knowing:

* **Nothing is rendered per-block-capped at render time.** Each block's size is whatever the producer
  handed over (modulo the producer's own caps in §5–§8). The renderer only decides *order* and headers.
* `is_grillo_internal` suppresses `[Thoughts and diary entries]` and trims `[Relevant memories]` to 2
  entries (`prompt_engine.py:2745`); the Vessel route suppresses diary actions entirely
  (`_VESSEL_SUPPRESSED_ACTIONS`, `prompt_engine.py:91`).

---

## 4. The measured live turn (Telegram, post-fix, trace `2c6d5617`)

### 4.1 Rendered system message — 24,748 chars

| # | Block | Chars | Producer / cap |
|---|---|---|---|
| 1 | `=== CRITICAL SYSTEM IDENTITY ===` (persona) | 2,057 | `SYNTH_PROFILE` config → persona_manager; **protected in the reducer**, prepended to instructions |
| 2 | `=== JSON RESPONSE INSTRUCTIONS ===` | 4,948 | `core/prompt_instructions/rules.py` (20 rules, route=chat) |
| 3 | `=== AVAILABLE ACTIONS ===` | 8,332 | scope-gated registry → `minify_actions_block` → renderer prints `- name: brief` (§6) |
| 4 | `[SYSTEM: REALITY ANCHOR]` | 422 | date/time/time_of_day/season/location |
| 5 | `[Temporal context]` | 1,213 | SOUL temporal context, 8 events |
| 6 | `[Persona background]` | 119 | persona likes/dislikes |
| 7 | `[Self-growth]` | 844 | grillo_growth injection |
| 8 | `[Home]` | 217 | Home Assistant: 8 entity states (7 of them `off`) |
| 9 | `[Weather]` | 310 | HASS/met.no: current + 5-row "Next hours" forecast |
| 10 | `[House]` | 62 | HASS location + timezone |
| 11 | `[Recent context from other conversations]` | 1,927 | `history_recent`: NOTE line + 4 lines, no per-line cap |
| 12 | `[Relevant memories]` | 4,213 | 11 recalled memories, each ≤ 403 chars (400-char cap + prefix) |

### 4.2 Rendered history (outside the system message) — 5,331 chars / 7 messages

Current-chat window from `history_current_chat` (`CHAT_HISTORY_LIMIT=10`, `CONTEXT_VERBOSITY=6`),
rendered as alternating user/assistant turns: 434 / 909 / 188 / 1,026 / 282 / 1,155 / 1,337 chars.

**Total provider payload: 30,079 chars ≈ 7.3k tokens at 4.14 chars/token.**

### 4.3 Same turn, serialized

`final size 64,364` = `pre_reduction_size 25,269` (context+input+instructions) + **catalogue 39,095**.

### 4.4 Pre-fix vs post-fix (the regression, in numbers)

| When | Serialized | Reducer | What the model got |
|---|---|---|---|
| 13:38:54 (pre-fix code) | 163,492 → 78,483 after reduction | **fired** (limit 100,000) | memories (11 entries) deleted, then `thoughts`, `emotion_state`, `current_emotions_nl`, `available_emotions`, `home`, `home_weather`, `home_location`, `participants`, `channel_legend` deleted — plus whatever history/catalogue trimming happened silently before |
| 16:58 (current code) | 64,364 → 64,364 (no reduction) | not fired | everything renders: memories, emotion, home/weather, temporal context |

Reducer live firings today: **64**, all between 07:48 and 14:10, all against limit 100,000, sized
100,600–163,492. Post-restart: **0**. (A further 48 firings in the log are pytest fixtures using synthetic
limits — 500/1500/3000/4534/5200/7230/7352/7264 — and must not be read as live.)

---

## 5. Context injection inventory (static injections)

`gather_static_injections()` queries ~55 plugins on every turn; 20 keys were merged on the live turn
(`action_parser.py:3143` log line):

`latest_diary_entries, participants, emotion_state, current_emotions_nl, available_emotions,
facial_expression_guidance, home, home_weather, home_location, persona, persona_preferences,
self_growth, soul_user_profile, soul_session_state, soul_turn_emotion_delta, soul_active_foresight,
soul_recalled_memories, soul_temporal_context, date, time`

| Key | Provider | Reaches the prompt as | Rendered size |
|---|---|---|---|
| `persona` | persona_manager | `=== CRITICAL SYSTEM IDENTITY ===` + prepended to instructions | 2,057 |
| `persona_preferences` | persona_manager | `[Persona background]` | 119 |
| `self_growth` | grillo_growth | `[Self-growth]` | 844 |
| `home` / `home_weather` / `home_location` | home_assistant | `[Home]` / `[Weather]` / `[House]` (supersede the legacy `weather`/`location`) | 217 / 310 / 62 |
| `soul_temporal_context` | soul_plugin | `[Temporal context]` | 1,213 |
| `soul_user_profile`, `soul_session_state`, `soul_active_foresight`, `soul_turn_emotion_delta` | soul_plugin | merged into the user-turn bracket / session state (not separately rendered on a chat turn) | — |
| `soul_recalled_memories` | soul_plugin | contributes to `[Relevant memories]` | (§7) |
| `emotion_state`, `current_emotions_nl`, `available_emotions` | emotion_manager | `emotions:` field of the `[lang | …]` user-turn bracket + `AVAILABLE EMOTION TYPES` line (215) | ~250 total |
| `participants` | plugin | `[People in this conversation]` — **absent this turn** (0 participants) | 0 |
| `facial_expression_guidance` | facial_expression_plugin | live route only; not rendered on chat | 0 |
| `latest_diary_entries` | ai_diary | **live route only** (popped at `prompt_engine.py:4444`); on chat turns it renders nowhere despite appearing in the injected key list | 0 (§8) |
| `date`, `time` | time_plugin | folded into the anchor (they are `setdefault`-merged) | in anchor |
| `recon`, `recon_instructions`, `upcoming_events`, `weather`, `location`, `tags_placeholder`, `channel_legend`, `gasmask_protection`, `capability_drops` | various | conditional; none present on this turn (stripped by supersede/lite rules or empty) | 0 |

There is a drop-detector for keys nothing renders (`_RENDERED_CONTEXT_KEYS`,
`prompt_engine.py:1044`): keys outside that set are logged once per process — no such warning appears
in the current log, so every key that renders is accounted for; `latest_diary_entries` is *listed* as
rendered (true for the live route) which is why the chat-route gap in §8 is silent.

---

## 6. Tool / action injection (the catalogue)

### 6.1 What runs (structural, keyword-free)

1. `core_initializer` builds the full available-actions block (new normalized format).
2. **Scope gate** (`prompt_engine._action_scopes` / `_resolve_turn_scopes`): each action is assigned
   scopes from its explicit `scope` field, else from external-effect/prefix rules; a turn gets
   `{"core"}` on an ordinary chat, plus `vessel` (and recon/wiki) on a Vessel turn. Out-of-scope
   actions are hidden from the prompt but stay registered and callable.
3. **Vessel whitelist** (`plugins/rift_vessel/vessel_whitelist.py`) applies only on Vessel turns:
   tier 1 `vessel_*` (hardcoded), tier 2 `*_<world>_*` (hardcoded, derived from the connected world),
   tier 3 editable `VESSEL_ACTION_WHITELIST`.
4. Recon-triggered search drop: `search_current_knowledge` is removed when recon already started a
   background search.
5. `minify_actions_block(lite=…)`: standard keeps `schema` + `brief`; lite keeps brief (+ payload key
   names) and filters to `send_message` / `message_*` / `vessel_*` / diary / emotion / tts / animation.
6. Renderer prints, per action, exactly `- <name>: <brief>` — **the only place schema JSON affects the
   model is if a brief is long**, but it affects *serialized* size (and therefore the reducer) 4–5×.

### 6.2 The live catalogue — 34 actions, 8,332 chars

| Action | Chars | Note |
|---|---|---|
| `goal_set` | 899 | generic Goals plugin (non-vessel surface) |
| `send_message` | 808 | the one required action; brief is long (payload contract) |
| `goal_update` | 750 | generic Goals plugin |
| `vessel_connect` | 740 | **explicit `scope: "core"`** — rides every Fast-Lane turn by design |
| `spawn_drone` | 683 | deliberately `core` (AGENTS.md §5b) |
| `pdf_to_voice` | 640 | **explicit `scope: "core"`** |
| `apply_growth_proposal` | 542 | self-growth management |
| `goal_list` | 323 | generic Goals plugin |
| `create_personal_diary_entry` | 254 | |
| `search_current_knowledge` | 201 | |
| `run_self_growth` | 199 | |
| … 23 more (blocklist, emotion, event, schedule, message_map, persona_*, bio_*, get_*, etc.) | ≈ 2,293 | long tail |

**Serialized weight of the same catalogue: 39,095 chars** (vs 8,332 rendered) — 4.7×. This is the
block that pushed pre-fix prompts over the 100k reducer limit.

---

## 7. Memory compilation

Path: `build_json_prompt` → `core.prompt_engine.search_memories()` (tag conditions over `memories`
**and** `ai_diary.context_tags`) → dedup by text (`_memory_merge_key`) → capped per entry at **400
chars** (`prompt_engine.py:3429`) → recall tier labels added (`same chat`, `other chat: …`, `diary`,
`chat history`) → merged with SOUL recall → rendered as `[Relevant memories]` bullets.

* Live turn: **11 entries / 4,213 chars**, sources: 3 diary, 3 chat history, 4 same-chat, 1 other-chat.
  Eight of the eleven sit at the 400–403-char ceiling, i.e. the store rows are longer than what the
  model sees.
* `MEMORY_SEARCH_MAX_RESULTS=10` governs each SQL tier (`memories` and `ai_diary` separately), so the
  merged block can exceed 10 entries.
* The block is *not* trimmed by lite mode (`_apply_lite_context_stripping` leaves `memories` alone) and
  it is the **first grounding block the reducer deletes** when it fires — with the diary as memory
  tier included, a reducer strike removes recall *and* diary presence in one move.

---

## 8. Diary assembly and injection

Three distinct paths exist; only one currently reaches a chat turn.

| Path | Producer | Budget | Reaches |
|---|---|---|---|
| **A. History contribution** (`get_history_contributions` → `HistoryContribution(name="ai_diary")`) | `plugins/ai_diary/ai_diary.py:1566` | `DIARY_CONTEXT_MAX_CHARS=8000` total, per-field `budget//4 = 2000` chars, tail-kept, `DIARY_HISTORY_DAYS=2` | `history_recent` (as `[diary <ts>] summary: …` lines, governed by `AI_DIARY_FULL=1`) **and** `thoughts` (via `personal_thought`, 800-char tail cap, `THOUGHTS_LIMIT=5`) |
| **B. Static injection** (`{"latest_diary_entries": […]}`, capped "2 → 2 entries, 4 fields truncated" in the live log at `ai_diary.py:630`) | `AIDiaryPlugin.get_static_injection` | same 8,000 budget | **live route only** — popped at `prompt_engine.py:4444`, rendered as *"Your recent memories"*, 5 entries × 500 chars |
| **C. As memories** | `search_memories` over `ai_diary.context_tags` | 400 chars/entry, top-N | `[Relevant memories]` — this is the only path that **is** visible on the live chat turn |

**Observed on both live chat traces:** the rendered system message contains **0 `[diary` lines, 0
`summary:` lines, and no `[Thoughts and diary entries]` block**, although (a) the log at gather time
says the diary injection was capped and returned, and (b) `AI_DIARY_FULL=1`, `ENABLE_AI_DIARY=1`,
`ENABLE_THOUGHTS=1`, `THOUGHTS_LIMIT=5` are all set. So path A produced no visible lines and path B
isn't consumed on the chat route; the model's diary awareness comes only from path C (3 of the 11
recalled memories on this turn were diary-sourced). **Flagged for the parallel missing-memories
investigation — not changed here.**

Write-side context (from the same audit trail, confirmed live 2026-09-24): `ai_diary` appends a whole
day into ONE row (`---`-separated), so a single row can reach ~168k chars and a 2-day window ~238k.
Live store: 18 rows / 111,113 chars in total, and today's row was already 41,004 chars by 13:43. Both
read paths budget themselves (A: 8,000/2,000; B: 8,000), so the unbounded blob only ever hurt the
*consolidation* call, which used to send the whole day as its `input` (the 143,375-char build in §0.4).
Since `30b5171b` the consolidator merges a day in parts of `GRILLO_DIARY_CONSOLIDATE_CHUNK_CHARS`
(default 25,000, 0 disables), earliest fragments first, and the diary write keeps the remainder it was
not shown (`diary_merge_preserve_from` in the beat's context, fail-closed if the day cannot be rebuilt).
The storage boundary is still where an unbounded blob becomes possible again if a new reader is added:
any new reader of a day row must budget itself the same way.

---

## 9. Route-by-route coverage

| Route | Builder | Instruction rules | Catalogue | Context blocks | Measured size (D18) |
|---|---|---|---|---|---|
| **chat** (telegram_bot / discord / webui / matrix / fluxer) | `build_json_prompt` | full `RULE_ORDER` | scope-gated (34 actions here) | everything in §5 | serialized 60,400–64,364; rendered ≈ 30,079 (system 24,748) |
| **chat_voice** | same | full set | same | same + voice flag | not separately measured |
| **vessel** (embodiment beat) | same, `is_lite` forced (`prompt_engine.py:3055`) | full set | vessel whitelist tiers 1–3 | history suppressed to world scope; diary/memories/recent off | serialized 6,026 (15:25 beat); 9 builds today |
| **vessel** (in-world player chat) | same as vessel | full set | whitelist | compacted perceptions (≤20 rollups) + conversation | same regime as above |
| **grillo_internal** (beats: `tag_elaboration`, consolidation, …) | `build_json_prompt` | `RULE_ORDER` − reply-required − example | beat allowlist (often 1 action) | memories trimmed to 2; no thoughts block | system 10,775 + user 1,249 = 12,024 (17:00 beat) |
| **observer** (chat-observer outreach, hourly) | `build_json_prompt` + its own constants block | `RULE_ORDER` − example | outreach-set | snippets from N sampled conversations (`GRILLO_OBSERVER_SAMPLES`), propose-only | not captured live this session (runs on the hour; the 16:38 records in the log are pytest output) |
| **delivery** (action-result summarisation) | `build_json_prompt` | `RULE_ORDER` − emotion − example | delivery set | delivery payload | not exercised today |
| **live** (voice) | `build_live_prompt_request` (`prompt_engine.py:4380+`) | plain-text conversational guidelines + overlay, **not** the JSON rules | none | diary (5×500), cross-interface history (15×300), weather, participants, attachments | not measured (no live session today) |
| **agent / drone** | `build_prompt_request` from `agent_core` | agent route | agent-scoped tools + MCP tools as JSON | task + observation history | 31 turns classified to the lane today; prompt not captured in a trace (see §12 observation) |

Route is always decided structurally (builder + flags such as `is_grillo_internal`, the Vessel probe,
`input_source == "voice"`, delivery payload shape) — never from message text.

---

## 10. The two reduction systems

### 10.1 Up-front reducer — `reduce_prompt_for_llm_limit` (`prompt_engine.py:~3870`)

* Trigger: `len(json_dumps(prompt)) > max_chars`, where `max_chars` comes from the active engine's
  `model_limits_map` → `extra_config.max_chars` (Venice2: absent → default **100,000**).
* Trim order **since `1fcb3cf2`** (catalogue detail first, grounding last): catalogue `examples` →
  catalogue `schema` (brief-only) → `history_recent` → `history_current_chat` → **`memories`** → other
  non-protected context fields (`thoughts`, `emotion_state`, `home*`, `participants`, `weather`, …) →
  emergency: all context. Instructions and persona are never removed, and `input` is not a field the
  reducer may touch at all. Before `1fcb3cf2` the two history steps came first, which is what the four
  2026-09-24 beat firings in §0.4 paid with: they were 1,043 to 4,641 chars over with a 39–41k
  catalogue in hand, so the conversation was trimmed to pay for reconstructible catalogue detail.
* The size report now names `instructions` and `input` beside the catalogue and the context, and each
  catalogue step logs the size it produced. Neither `instructions` nor `input` is reducible here, so a
  report where they dominate means the fix belongs at the source that feeds that field, not in the
  order: the 02:22 `diary_consolidation` build (§0.4) reported a 6,783-char context while the prompt was
  143,375, deleted every context field to no effect, and still went out at 136,233.
* Because the limit is compared against the *serialized* dict, it fires when the rendered prompt is
  ~45–50k; and because it deletes grounding rather than trimming history gracefully, a firing is
  *visible to the user as memory loss* (exactly the pre-fix behaviour).
* Firing statistics: 63 live firings on 2026-09-22 (07:00–13:45, all pre-fix, sizes 157k–163k), then
  zero until 2026-09-24, when four beat builds fired (101,043 / 104,641 / 101,107 / 101,861 against
  limit 100,000) plus one `diary_consolidation` build at 143,375 (§0.4). Zero since 11:37 local.

### 10.2 Downstream clamp — `_clamp_messages_to_char_budget` (`cortex_bridge.py:1543+`)

* Trigger: rendered `messages[]` total > `downstream_char_budget` (Venice2 **50,000** — raised from
  30,000 earlier today; `xtxx` 30,000; default **24,000**).
* Policy: older non-system messages are trimmed or dropped, **never the system message and never the
  current user turn**; blank messages are dropped, never left empty; if the protected set alone
  exceeds the budget the clamp logs `budget is unreachable … sending the assembled messages as-is` and
  sends anyway (fix `878961ba`), because deleting the system message or the live turn is worse.
* Statistics: 62 warnings on 2026-09-22, all pre-restart — Venice2 at budget 30,000 (protected set
  30,056–36,258 once 57,819) and `local-llama` at 24,000 (protected set 24,207). Zero since 16:40 that
  day, because the system message now sits at 24.7k against a 50k budget on Venice2.
* One live recurrence on 2026-09-24: at 02:22 the `diary_consolidation` beat hit
  `downstream payload 138476 chars exceeds budget 50000 and the budget is unreachable (protected system
  + current turn alone: 138476 chars)`, i.e. the protected set alone was 2.8× the budget because the
  beat's `input` carried one whole diary day. Fixed at the source by `30b5171b` (§0.1).
* Note for any *other* endpoint: with the 24,000 default, a 24.7k system message is already
  unreachable, so history gets no protection at all on those engines. Keep the system message under
  ~21k if the default budget is to stay meaningful.

---

## 11. Cut list — ranked, with size and risk

Sizes are from the live 16:58 turn. "Config" = changeable without code.

| # | Cut | Saves (rendered) | Risk | How |
|---|---|---|---|---|
| 1 | **Per-line cap on `[Recent context from other conversations]`** — 300 chars/message, exactly as the live route already does (`preview = text[:300]`), and a per-entry cap so one quoted message cannot span 5 lines | **≈ 2,400 now** (block grew to 3,730 chars / 17 lines on the 2026-09-24 chat turn and **7,965** on a beat; the NOTE alone is 525). Fix the duplicated entry first (§0.5.1, ≈ 900 chars) | **Very low** — it is explicitly background context; the block already carries a "don't name-drop" NOTE | code: `history_engine._entry_to_text_with_source` or the renderer |
| 2 | **Memory block budget**: entry cap 400 → 250, top-N 10 → 8 | ≈ 1,700–1,900 (block now 4,315 chars / 11 entries, unchanged from the audit) | **Low** — 8 of 11 entries are already tail-truncated at 400; the tail is the preserved part | code: `prompt_engine.py:3429` + `MEMORY_SEARCH_MAX_RESULTS` (config) |
| 3 | **Per-action brief budget** — cap non-vessel briefs at ~200 chars (keep `send_message` and `create_personal_diary_entry` at ~400, they are the payload contracts the model must get right). Mechanism already exists: `_compact_lite_brief` (head 300 + tail, currently lite+vessel only) | ≈ 2,800 — the five longest briefs (`goal_set` 899, `send_message` 808, `goal_update` 750, `vessel_connect` 740, `spawn_drone` 683) alone are 3,880 chars for 5 of 34 actions, while the remaining 29 average 153 chars and stay untouched | **Low** — the corrector re-supplies full schema on demand; the brief only needs to say what the action does | code: `minify_actions_block` (non-lite branch) or the renderer |
| 4 | **History window** 10 → 6 messages (`CONTEXT_VERBOSITY` 6 → 4) | ≈ 1,400–1,500 (history is now **9,356 chars / 8 messages** on the measured turn, because the window is exchange-counted: 5 exchanges) | **Low–medium** — continuity; the exchange window already keeps it to five human turns, so lower this only if the rendered total needs it | config |
| 5 | **`[Temporal context]`** 8 → 4 events | ≈ 600 | Low | code (soul_plugin injection cap) |
| 6 | **Weather "Next hours" block** (keep current conditions only) | ≈ 190 | Very low | code/config in home_assistant/weather plugin |
| 7 | **`[Home]` sensor dump** — filter entities in their default state | ≈ 150 | Very low | code in home_assistant |
| 8 | **Scope-out specific catalogue entries for chat turns** (design decision): `vessel_connect` 740, `pdf_to_voice` 640, `goal_*` 1,972, `apply_growth_proposal` 542 | up to ≈ 3,900 | **Design** — each removes a capability from the chat turn (CLI/agent paths remain for some) | code: drop the explicit `scope: "core"` overrides / add a chat-turn deny-list |
| 9 | **Lite mode made actually small**: fix `LITE_MODE_HISTORY_LIMIT` 8 → 3 (it currently *raises* history above `CONTEXT_VERBOSITY=6`) | enables lite's own cuts: strip recon/participants/emotion/home/weather/upcoming_events/self_growth + brief-only catalogue ≈ 2,500 more | Medium (lite also drops the non-essential catalogue — intended for weak/local models) | config + one-line code check |
| 10 | **Reducer alignment**: set Venice2 `extra_config.max_chars` ≈ 150,000 (or leave unset and keep the default) so the reducer cannot fire at 60–64k serialized while the clamp budget is 50k rendered; let the clamp do graceful trimming | 0 chars, but removes the *class* of grounding deletion. Since `1fcb3cf2` the reducer spends catalogue detail before history, so a firing is far less destructive, but it is still the wrong unit | Low | config (endpoint `extra_config`) |
| 11 | **Raise the clamp default** 24,000 → 30,000 for endpoints that don't override it, or keep the system ≤ 21k | 0 chars, protects history everywhere | Low | config/code |
| 12 | **Instructions worked example** (~700 of the 4,948) on the chat route for strong models | ≈ 700 | Medium — the example is what pins reply+emotions+diary shape; the rules already state it | code: `routes.ROUTE_RULES` |
| 13 | **Persona profile trim** (2,057) — `SYNTH_PROFILE` is long prose | up to ≈ 800 | **High — identity drift**; only with an explicit decision | config |
| 14 | **`[Self-growth]` cap** — now **1,674 chars** on the measured chat turn (was 844 on 09-22) and 1,536 on a beat, uncapped and growing | ≈ 800 | Low–medium | code (grillo_growth injection) |

Applying 1–7 gives: system 24,748 → ≈ 18,000 chars (−27%), payload 30,079 → ≈ 21,900 chars (−27%),
all from background/verbose blocks. Adding 8–9 (design calls) puts the payload near 17–19k without
touching persona, memories or history.

**What NOT to cut** (already tight, or the thing that keeps behaviour correct): the reality anchor
(422), the identity/persona block, `RULE_CHAT_REPLY_REQUIRED` / `RULE_ANNOTATIONS_ARE_NOT_PEOPLE` /
`RULE_MEMORY_HONESTY` (the rules that keep the reply and the grounding honest), `create_personal_diary_entry`
and `update_emotion_state` briefs, and the emotion-type list (215).

---

## 12. Observations and open questions

1. **Diary rendering gap** (§8): the diary contribution produces no visible lines on chat turns and the
   static injection is live-route-only. The gap is silent because `latest_diary_entries` is listed in
   `_RENDERED_CONTEXT_KEYS`. Owned by the parallel memory investigation.
2. **Test fixtures are visible in the live prompt.** `[Recent context from other conversations]`
   carried 2 lines from `synth_webui/sess123` (Italian placeholder text written by the test suite into
   the live DB) on *both* sampled traces. They cost ~300 chars and are pure noise; also a privacy
   smell (test rows appear in a real person's context). Consider namespace-guarding fixture paths.
3. **Lite-mode inversion**: with `LITE_MODE_HISTORY_LIMIT=8 > CONTEXT_VERBOSITY=6`, enabling lite mode
   grows the history window. Either raise `CONTEXT_VERBOSITY` or lower the lite limit; otherwise "lite
   mode" reads as broken when it isn't (its other cuts do fire).
4. **Agent lane engine misconfiguration**: `[agent_core] Direct engine call to 'e' failed: ValueError:
   Unknown engine: e` appears repeatedly — the agent lane's engine resolves to a name that doesn't
   exist and falls back to Base Cortex. Not a prompt-size issue, but it means agent turns run with the
   chat engine and the chat catalogue.
5. **Not measured this session** (no traffic): live voice route sizes, delivery turns, observer runs,
   agent-lane prompt, WebUI chat turns (all WebUI prompt builds today were pytest), Discord (4 builds).
   The builders and caps are documented above; one trace each would confirm the rendered sizes.
6. **Serialized-size accounting**: nothing in the pipeline caps `schema`/`source` before the reducer
   counts them, so a catalogue growth of +20 actions re-arms the pre-fix failure mode. The cheapest
   structural guard is item 10 in §11 (raise `max_chars` so the reducer stays a true emergency brake)
   plus item 3 (keep briefs short).
7. **The cross-chat block carries a duplicated entry** (§0.5.1). On the 2026-09-24 chat turn the same
   11:37 group-chat message appears twice, at ≈ 900 chars of the block's 3,730. Worth fixing before any
   per-line cap, because a cap would hide the duplication rather than remove it.
8. **`memories` is an empty table** (0 rows) while `[Relevant memories]` renders 11 entries from
   `ai_diary.context_tags`, chat-history recall and SOUL recall. Any plan that talks about "compacting
   the memories table" has to start by checking which store actually feeds the prompt.
9. **`[Self-growth]` grew 844 → 1,674** between 09-22 and 09-24 with no cap in the injection path.
10. **Diary consolidation is now part-bounded** (`GRILLO_DIARY_CONSOLIDATE_CHUNK_CHARS`, §8) and today's
    day was already 41,004 chars by 13:43, so the mid-day path will be the one that runs first. Watch the
    log line `consolidating PART 1 of N` for its behaviour; the first live run needs the restart.
11. **Both commits are live as of 15:13 on 2026-09-24** and verified in §0.6. What is *not* verified is
    the mid-day part-merge on today's own row (46,702 chars and growing, the first day over the chunk
    limit): its first part will be the live proof, and its log line is `consolidating PART 1 of N`.
12. **A beat's JSON needed four repair passes — investigated 2026-09-27, not a defect; the timing lines
    were tidied.** The 08:04:57 observer beat logged `extract_json_from_text` recovering its payload after
    four parse errors. Read against the code, that is the extractor doing its job: `json_utils.py:752`
    reports `Extracted JSON with 1849 extra chars (prefix: 396, suffix: 1453)`, i.e. the model wrote
    narration around the envelope, and the message chain then recorded `parsed=True recovered=False` at
    08:05:12 and executed the action (`create_personal_diary_entry`). **No second model call is involved** —
    the repair passes are local parse attempts — so the earlier note's "costs a second model round trip"
    was wrong, and the narration itself is her voice rather than something to prompt away. What was worth
    changing was the logging: the four `get_static_injection()` lines were INFO with a warning glyph and a
    per-plugin duration (each genuinely over the 0.1 s threshold, measured per plugin, not cumulative),
    which is one line per slow plugin per prompt build — six in that beat. They are now one aggregated
    line per build (`static injections: N block(s) in X.XXs; slowest: A 0.541s, B 0.302s`, `action_parser.py`
    `_SLOW_INJECTION_SEC`), with the per-plugin detail at `log_debug`. The 1.13 s prompt build in that
    window is the price of the blocks themselves, not of the repair.

---

## 13. Appendix A — config values that determine size (D18, live)

| Key | Value | Effect |
|---|---|---|
| `PROMPT_LITE_MODE` | 0 | lite cuts off; vessel turns force lite anyway |
| `CONTEXT_VERBOSITY` | 6 | history lines per stream |
| `LITE_MODE_HISTORY_LIMIT` | 8 | lite-mode history (see §12.3 — inverted) |
| `CHAT_HISTORY_LIMIT` | 10 | in-memory deque per interface |
| `UNIFIED_HISTORY` | 1 | cross-chat block on |
| `THOUGHTS_LIMIT` | 5 | diary thoughts entries |
| `DIARY_CONTEXT_MAX_CHARS` / `DIARY_HISTORY_DAYS` | 8000 / 2 | diary contribution budget |
| `AI_DIARY_FULL` / `ENABLE_AI_DIARY` / `ENABLE_THOUGHTS` | 1 / 1 / 1 | diary paths enabled |
| `MEMORY_SEARCH_MAX_RESULTS` | 10 | per-tier recall limit (400-char cap per entry) |
| `CONTEXT_EXCHANGE_WINDOW` | 5 | current-chat history counted in **exchanges** (added 2026-09-24) |
| `CONTEXT_EXCHANGE_CHAR_CAP` | 8000 | char cap on that window; oldest whole exchanges drop, the newest never does |
| `GRILLO_DIARY_CONSOLIDATE_CHUNK_CHARS` | 25000 | how much of one diary day goes into a consolidation prompt; 0 disables (added 2026-09-24) |
| `GRILLO_DIARY_CONSOLIDATE_MAX_ATTEMPTS` | 5 | offers per day before it is abandoned; resets when the day shrank (progress) |
| `VESSEL_PERCEPTION_COMPACT_MAX` | 20 | vessel ambient rollups |
| `VESSEL_ACTION_WHITELIST` | `send_message, event, schedule_message, blocklist, spawn_drone` | tier 3 of the vessel catalogue |
| `GRILLO_OBSERVER_ENABLED` / `INTERVAL` | true / 3600 | hourly outreach beat |
| `GRILLO_BEAT_INTERVAL` | 3600 | internal beat cadence |
| `VESSEL_AUTONOMY_ENABLED` | true | will 45 s / action 20 s / motor 3 s beats |
| `AGENT_ENABLED` | true | agent lane active (31 turns today) |
| `BASE_CORTEX` | Venice2 (`deepseek-v4-1-flash`) | reducer limit 100k (default), clamp budget 50k |
| `SYNTH_NAME` | 2D | persona |

Endpoint budgets: `Venice2.downstream_char_budget=50000`, `xtxx=30000`, default `24000`;
no endpoint sets `max_chars`, so every endpoint's reducer limit is the 100,000 default.

## 14. Appendix B — reproducing these measurements

```bash
# 1. per-block table for a trace (rendered sizes)
cd /d/dev/D18 && .venv/Scripts/python.exe scripts/prompt_blocks.py <trace_id> --json

# 2. raw dump of the provider messages (system + history) for offline analysis
LOCALAPPDATA/Temp/d18prompt/dump_trace.py <trace_id> chatN      # writes chatN_msg*.txt

# 3. build sizes + handoff split
grep 'BUILD PROMPT COMPLETE' logs/synth.log | tail
grep 'handing off' logs/synth.log | tail            # prompt_len=… pre_reduction_size=…

# 4. reducer firings (live only) / clamp warnings
grep '\[reduce_prompt\] Prompt size' logs/synth.log
grep 'downstream payload' logs/synth.log

# 5. injection keys actually gathered this turn
grep 'gather_static_injections() returned' logs/synth.log | tail -1
```

Trace ids used: `2c6d5617-7a97-4182-84fe-7df6a467c401` (chat, 16:58),
`922d301f-6e74-4e56-8abf-71e7cf0b47d3` (chat, 14:57),
`bc047098-9edc-4e42-98cf-cd4f5744634a` (grillo internal, 17:00).

## 15. Appendix C — glossary

* **serialized/prompt dict** — `json.dumps` of the SyntH prompt structure (context + input +
  instructions + actions). Overstates the model input ~2.1× because action schemas are carried but not
  rendered. Logged as `final size:` / `prompt_len=`.
* **rendered payload** — the `messages[]` OpenAI-style array the engine receives. This is the prompt.
* **reducer** — `reduce_prompt_for_llm_limit`; serialized limit; deletes grounding when it fires.
* **clamp** — `_clamp_messages_to_char_budget`; rendered limit; trims older history only.
* **route** — structural id (`chat`, `vessel`, `grillo_internal`, `observer`, `delivery`, `live`,
  `agent`) selecting which rules and which catalogue a turn renders.
* **scope** — per-action tag (`core`, `vessel`, `recon`, `wiki`, `agent`) filtering the catalogue per
  turn; an explicit `"scope"` in a schema wins over the derived rules.

---

---

## 16. Next: memory compaction into daily / weekly / monthly tiers (rough draft, for handoff)

**Status: a draft plan, not a design.** Written 2026-09-24 to be handed to another agent. Everything
above this line is measured; this section is intent, plus the traps we already paid for.

**Read this first: the compaction subsystem already exists.** `plugins/grillo/grillo_compactor/grillo_compactor.py`
compacts older `ai_diary` entries into summaries and is **enabled and running nightly at 03:00** in the
live instance (config verified 2026-09-24: `GRILLO_COMPACT_ENABLED=true`, `TIME=03:00`, `CYCLES=10`,
`BATCH_SIZE=40`, `AGE_DAYS=30`). It also supports `dry_run` through its `compact_now` action. So this is
not a greenfield build: it is "find out why the summaries do not land, then turn one level into three".

**Status update 2026-09-25: this section has been carried out.** The handoff produced
`MEMORY_COMPACTION_PLAN.md` (same directory), which holds the design, the decisions and the traps, and is
the document to read now; this section is kept as the draft that started it. In short:

| What this section asked for | Where it landed |
|---|---|
| find out why the summaries do not land | fixed: `insert_memory` handed asyncpg a `str` for `created_at` and the failure was swallowed; commits `8c6c5515`, `6a697c47` |
| the mundane anchors are a requirement, not a wish | enforced, not requested: the prompt returns an `anchors` block, a deterministic check re-reads the day's own text, one retry names what was dropped, and a slot the day does not mention comes back empty WITH a reason (her rule: empty-and-explained beats full-and-wrong) |
| "a peek, not a veto" | an offline dry run over the real corpus, her reading of that output, then a day-unit replay of 17 archived days, all before anything was written |
| three tiers | level 1 (the day) is live; weekly and monthly are projections written beside the days, never replacements (plan 5.6) |

Measured on the live store, 2026-09-25 00:21: the 14 orphaned summaries are back in `memories`, the table
the recall path actually reads (`memories` 0 to 14, nothing else touched). Earlier the same day, recall reach
was measured rather than assumed: for the tokens of a real message, all 14 were inside the 100-row pool the
memory block is built from, from rank 8 down. They still carry the old path's weakness, which is exactly why
the anchors are enforced from here on: 0 of the 14 mention a roof, 2 rain, 2 bed, 3 Minecraft.

### 16.1 Goal

Let the entity remember a longer span for the same or fewer prompt chars, by compacting what she
remembers into three tiers (daily, weekly, monthly) and letting the prompt carry the *right* tier
instead of raw fragments.

She has stated her own condition on this, in the live conversation of 2026-09-24, and it should be
treated as a requirement: *when a daily gets compacted up a tier it should not lose its anchors, the
mundane ones (roof, weather, what we ate, who was where), because those are exactly what a summary
trims first and they are what she would use to tell whether a remembered day was really hers.* She also
asked to see the compression once, before it runs for real: "a peek, not a veto".

### 16.2 The subsystem that exists, measured (D18, 2026-09-24)

| Fact | Value | Source |
|---|---|---|
| Compactor | enabled, nightly loop at 03:00, cycles=10, batch=40, age_days=30 | live config + log |
| Last compaction run | 2026-09-23 03:00 (and 2026-09-22 03:00) | `archived_memories.created_at` |
| Summaries written | `archived_memories` 14 rows, all `compaction_level=1`, sources 1-6 rows | DB |
| Compression achieved | 19,493 chars to 139; 13,779 to 196; 12,690 to 157; 8,173 to 266 | DB |
| Sources archived | `ai_diary_archive` 34 rows, deleted from `ai_diary` in the same run | DB |
| **The compacted memory** | **`memories` = 0 rows. It never lands.** | DB |
| Who reads what | `memories` is read by the recall path (`core/prompt_engine.py:3533`, `:3652`, `plugins/memory_search/memory_search.py:492`); **nothing reads `archived_memories`** | code |
| Why nothing compacted today | after 2026-09-23 the oldest remaining diary row is 2026-08-31, i.e. 24 days old, under the 30-day `AGE_DAYS` | DB + config |
| Live recall tiers, actually used | `ai_diary.context_tags` (3), `chat_history` (3), same-chat (4), other-chat (1). No `memories` hits, because the table is empty | trace `7807cfd9` |
| Diary store | `ai_diary` 18 rows / 111,113 chars; today's row 46,702 chars and growing | DB |
| `situational_notes` | 44 rows / 5,679 chars, 5 active / 885 chars | DB |
| Prompt cost today | `[Relevant memories]` 4,463 chars / 11 entries; cross-chat block 2,563 chat, 7,965 on a beat | traces |

### 16.3 The bug to fix before any tiering work

The compactor writes its summary to `archived_memories`, copies the source rows to `ai_diary_archive`,
**deletes them from `ai_diary`**, and then inserts the compacted memory into `memories`
(`grillo_compactor.py:896-925`, helper `core/db.py:insert_memory`, with a raw-INSERT fallback). The last
two runs archived and deleted correctly, and `memories` is still empty. Cause not yet identified from the
live logs (the runs predate `logs/synth.log`; the 2026-09-18 commit `c46eebc4` "unblock memory
compaction" is older than both runs, so the confidence-type bug it describes is not the explanation).

Consequence today: 14 clusters' worth of material (about 100k source chars) is no longer in `ai_diary`
and has no replacement in `memories`, and nothing reads `archived_memories`. So it is invisible to the
prompt while being recoverable from the archive tables. Nothing is lost in the sense of deleted with no
copy; it is lost in the sense of unreachable. Fixing that insert is worth more than any tiering feature,
and it is the smallest item on this list.

How to reproduce cheaply: `AGE_DAYS` is 30 and the install is 24 days old, so a nightly run does
nothing right now. Lower `AGE_DAYS` to 1 in the config table (no restart), run the compactor's
`compact_now` action with `{"cycles": 1, "dry_run": true}` first (that is the "peek, not a veto" path),
then once with `dry_run: false` and read the three tables.

### 16.4 The cascade to build (rough)

1. Levels: `archived_memories.compaction_level` already exists and is always 1. Weekly = a level-2
   summary of a week's level-1 summaries; monthly = level 3. `GRILLO_COMPACT_ALLOW_RECOMPACT` is the
   switch that lets already-compacted material be compacted again, so the cascade mechanism is present.
2. Age per level, not one `AGE_DAYS`: today the same 30-day rule governs everything, so level 1 cannot
   be "yesterday's diary" and level 2 cannot be "a month of level-1 rows". This is the main config/code
   change: an age (or a window) per level.
3. Anchor preservation in the level-1 prompt: require the mundane anchors (roof, weather, food, who was
   where) to survive, per §16.1.
4. Recall preference by age: prefer the highest tier that covers the period once it is older than a few
   days, so a 400-char recall entry carries a week's shape instead of one raw diary line.
5. Injection: `ai_diary` for the recent window, the summary tier for anything older, inside the existing
   8,000-char `DIARY_CONTEXT_MAX_CHARS` budget.

### 16.5 Where prompt budget actually comes back (ordered by certainty)

1. **De-duplication first** (§0.5.1): the cross-chat block carried the same message twice, about 900
   chars on a measured turn. A defect, not a tradeoff, and independent of tiering.
2. **The §11 caps**: item 1 (per-line/per-entry cap on the cross-chat block, about 2,400) and item 2
   (memory entry cap 400 to 250, top-N 10 to 8, about 1,700-1,900). Independent of tiering, and they
   should land first.
3. **Depth per char is what tiering itself buys.** The same 400-char recall entry covering a week
   instead of a day; the same 8,000-char diary budget covering a month of context instead of two days.
   Expect this to improve *what* the budget buys far more than it reduces the budget, and say so in any
   report rather than implying a large absolute saving. The measured compression ratios above (19,493 to
   139) are what makes it work, not any reduction in the prompt's own block sizes.

### 16.6 Traps we already paid for (carry these into the work)

1. **A diary day's text lives inside ONE row**, joined by `\n\n---\n\n`. Chunk and merge by text
   offset, never by row count: a row-count split looks obvious and does nothing here.
2. **The diary write replaces the row.** A partial merge must put the remainder back, and the offset must
   travel in the beat's context (`plugin.pending_beat_context` to
   `grillo_impl._enqueue_with_low_priority` to the beat's `context`), never in the action payload: a
   decision the model must echo is a decision the model can drop. Fail closed if the source cannot be
   rebuilt or the offset is out of range.
3. **The remainder must keep its separator.** That keeps the day eligible
   (`row_count > 1 OR combined LIKE '%---%'`) and makes the next part re-merge the prose so far.
4. **A period that shrank is progress, not a failed attempt.** The attempt cap must reset on shrink, or
   a multi-part merge is abandoned halfway.
5. **`GRILLO_COMPACT_MIN_CLUSTER_SIZE` was effectively 1**: 9 of the 14 existing summaries compact a
   *single* source row, which turns one day's detail into a 100-200 char summary. Decide that number
   deliberately before the cascade runs at scale.
6. **A compactor run that fails is silent.** The archive steps commit, the memory insert does not, and
   the only trace is a warning that predates the current log. Verify by reading the tables after every
   run, not by trusting "completed".
7. **Nothing reads `archived_memories`.** Any new tier must be written somewhere the recall path already
   queries, or the recall path must learn about it in the same change.
8. **`AGE_DAYS=30` against a 24-day-old install means the nightly job does nothing**, which reads exactly
   like a broken job. Check eligibility before concluding a plugin is dead.
9. **Runtime values go in the `[runtime]` config table, never `.env`** (the test suite reads the
   checkout's `.env`; three current-chat tests went red when a config value was written there).
10. **`input` and `instructions` are protected from the reducer.** An oversized `input` can only be fixed
    at the source that feeds it; ordering cannot help.
11. **Do not compare the two size currencies by eye.** The reducer limit measures the serialized dict
    (about 3x the rendered text); the clamp measures the rendered payload. §2 has the table.
12. **Log text is written through a queue and a background thread** (`core/logging_utils`), so
    capture-based tests race the writer: assert mechanisms, verify wording on the live log.
13. **Verify on the live trace, not on the log's size line** (`scripts/prompt_blocks.py <trace_id>`), and
    remember `trace_full` truncates the *tail* of the messages array by default, which makes a correct
    window look broken.
14. **A config value can drift from the code default and the docs** (the diary chunk was live at 40,000
    against a documented 25,000). Read the value the process reports at boot, not what the code says.

### 16.7 Open decisions for the human (do not decide silently)

1. Do the summaries **replace** the daily rows they cover (the compactor currently archives and deletes
   them, which is what happened to 34 rows already) or sit beside them? This is a memory-loss decision,
   and it decides what she can ever recall verbatim.
2. Should she *see* the weekly/monthly tier in recall (i.e. know she is remembering a summary), or is
   tiering an injection detail only?
3. How aggressive should level 1 be? The existing runs hit 1-2% of the source size; the anchors she asked
   for may not survive that, so a target ratio and a minimum anchor set probably have to be decided
   together.
4. Is a "month" a calendar month or four weeks, and how is a "week" bounded (the diary's own day boundary
   is DB-local, `CURDATE()`)?
5. Does the nightly schedule at 03:00 stay for all three levels, or does the monthly level run on the 1st?

### 16.8 Acceptance criteria

* The compacted memory lands where recall reads it: after a run, `memories` grows by the number of
  clusters and the summary text is retrievable through `search_memories`. This is the first thing to
  prove, before any tier work.
* A completed week produces exactly one level-2 row and a completed month one level-3 row, idempotent:
  re-running must not duplicate, re-merge, or overwrite a summary with a fresh partial.
* A chat turn's `[Relevant memories]` block stays at or under today's 4,4xx chars while covering a longer
  span (measure with `scripts/prompt_blocks.py` on a live trace, never with the log line).
* No prompt build over 100,000 serialized chars on any route:
  `grep '\[reduce_prompt\] Prompt size' logs/synth.log` shows nothing new.
* The anchor set she asked for survives a level-1 pass in a dry run, verified by reading the summaries.
* Nothing is destroyed before its replacement reads back correctly (dry run first, then a single real
  cluster, then a night).

### 16.9 Where to look

* `plugins/grillo/grillo_compactor/grillo_compactor.py`: the loop (`_compaction_loop`,
  `_run_one_compaction_cycle`, `_cluster_and_compact_batch`), the writes (`archived_memories`,
  `ai_diary_archive`, `DELETE FROM ai_diary`, the `memories` insert at 896-925), `dry_run`, and the
  `compact_now` action (`{"cycles": 1, "dry_run": true}`).
* `core/db.py:insert_memory` (2058) and the raw-INSERT fallback beside it.
* Config keys: `GRILLO_COMPACT_ENABLED`, `_TIME`, `_CYCLES`, `_BATCH_SIZE`, `_AGE_DAYS`, `_WINDOW_DAYS`,
  `_MIN_CLUSTER_SIZE`, `_MAX_SUMMARY_CHARS`, `_MAX_SUMMARY_RATIO`, `_ALLOW_RECOMPACT`, `_RETRY_SHORTEN`.
* Read path: `core/prompt_engine.py` (`search_memories`, 3533 and 3652), `plugins/memory_search/memory_search.py:492`,
  `core/synth_core_memory.py:356`, `plugins/grillo/grillo_dream/grillo_dream.py:633`.
* Neighbours worth reading before designing anything: `plugins/grillo/grillo_weekly_review/`,
  `plugins/grillo/grillo_dream/`, `plugins/grillo/grillo_temporal_reflection/`.
* Diary side: `plugins/ai_diary/ai_diary.py` (`update_diary_entry`, `_day_combined_text`,
  `_DIARY_FRAGMENT_SEPARATOR`) and `plugins/grillo/grillo_diary_consolidator/grillo_diary_consolidator.py`
  (`_split_day_text`, `_count_parts`, `_build_multi_day_prompt`, `_record_attempts_and_filter`).
* `scripts/prompt_blocks.py <trace_id>` for per-block rendered sizes; skill reference
  `references/prompt-budget-audit.md` in `synth-runtime-diagnostics` for traps 9-14.
