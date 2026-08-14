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
| 1 | Agent mode on by default | ◑ | Loop hardened — `f57e7486` "stabilize agentic loop routing, parsing, and delivery", `0b752643` drone budget, `70a62a70` drone delegation, `a599b0bb` native-tool cap. **But** no commit flips the `AGENTIC_ROUTING_ENABLED` / `AGENT_ENABLED` / `ENABLE_RECON` defaults — the config still ships off. |
| 2 | Delivery circuit breaker + dead-channel registry | ✅ | `core/delivery_guard.py` — a fail-open `DeliveryGuard` singleton: per-target consecutive dead-target ("unknown channel/user") failure counter, trips after `DELIVERY_BREAKER_MAX_FAILURES` (default 3), persists to `delivery_dead_targets`, and skips delivery at the `universal_send` choke point (`core/transport_layer.py::_send_text`). Discord now raises `DeadTargetError` instead of `RuntimeError("Unknown channel or user")`. WebUI Logs > Dead Targets sub-tab lists/purges (`GET`/`DELETE /api/dead-targets`). Config: `DELIVERY_BREAKER_ENABLED`, `DELIVERY_BREAKER_MAX_FAILURES`. |
| 3 | Correction-loop dedup + error-class learning | ✅ | `8c3d9544` "harden corrector against duplicate replies", `103de362` "corrector teaches canonical action types", `fb84d332` "bound the corrector and recover alternative tool-call dialects", `e6e4824d` correction/diary hardening. **Still in flight**: uncommitted `core/json_utils.py` adds stray-`")` repair. |
| 4 | Staged fallback chain | ◑ | `3c0fdf25` retry empty LLM responses, `e181b7f3` "degrade unavailable cortex overrides to Base Cortex", `1514f015` documents cortex fallback. A full "primary → local → cached" timeout hierarchy is not present. |
| 5 | Interface capability validation | ✖ | `8a594404` only removes the duplicate Discord module. No pre-dispatch `send_message` capability check / queue-fallback found. |
| 6 | Persistent per-world + per-person memory | ◑ | The *wrong-parent* bug is fixed — `1111e6ec` "stop goal churn and mama misattribution", `fe696a36` "isolate vessel history from global context", plus goal-scope fixes (`dd2a1caa`, `799fa5b2`). **Auto-learning** `MINECRAFT_KNOWN_PLAYERS` and persistent build/geography memory are not yet implemented. |
| 7 | Health dashboard + "why did I say that" trail | ◑ | `c3451c55` "Dashboard control deck", `d8517b32` theme/refresh, `f8900725` uptime fix. Dashboard exists; the per-turn reason trail (which memory/goal/beat drove a reply) does not. |
| 8 | Voice first-class + PDF→voice | ◑ | `e820cb09` "text-aware TTS suppression, restore `tts_speak` handler", `08b85a35` TTS on observer replies, `f4f962df`/`8e514e0f` Vox webui config. Native voice is restored and smarter; the PDF→per-chapter-voice flow is still a dream. |
| 9 | Weekly self-review beat | ✖ | No weekly-review beat. Grillo has observer/reflection/dream/goal beats but no "weekly life review" that writes next-week goals. |
| 10 | Test-harness isolation | ✖ | `tests/test_transport_recovery.py` uses the `fake` interface but there is no flag/DB to exclude test entries from runtime failure summaries. |

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
(items 3, plus blank-turn/staleness/DSP) — that part of the agent output is
done. What remains genuinely open, in order of pain:

1. **Agent defaults on** (item 1) — the loop works now, so flipping the config
   is low-risk and unblocks everything else.
2. **Interface capability validation** (item 5) — the silent
   "no send_message" failure path is still there.
3. **Weekly self-review beat** (item 9) and **test-harness isolation** (item 10)
   — both unstarted.
