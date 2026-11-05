# Memory Compaction Plan (D18, persona 2D)

**What this is:** the plan that picks up the handoff in `PROMPT_ASSEMBLY_AUDIT.md` §16 ("Next: memory
compaction into daily / weekly / monthly tiers"). §16 was written as intent; this document turns it
into an ordered set of changes with the root cause of the blocking defect already proven, so the first
milestone is a one-line fix rather than an investigation.

**Status:** plan, not implementation. Nothing in the live instance or store has been changed. One
rolled-back `INSERT` was attempted inside an explicit transaction while proving the root cause (see
§2.1); `memories` is still empty and no row was written.

**Evidence basis:** `PROMPT_ASSEMBLY_AUDIT.md` (§0 to §16, 2026-09-24), the D18 checkout at
`D:\dev\D18` (HEAD `30b5171b`), the live stores on `192.168.1.13`, `logs/synth.log`,
`logs/webui.log` and the rotated logs, read 2026-09-24.

**Headline:** the tiering work §16 asks for cannot be validated until one helper can write a row. That
helper has been unable to write since the Postgres cutover, so every "memory" the prompt shows today
comes from a different store, and compaction has never landed a single summary in either deployment.

---

## 1. Which store is which (read this before running any query)

| Store | Who writes it | memories | archived_memories | ai_diary | ai_diary_archive | newest `memories` row |
|---|---|---|---|---|---|---|
| `soul` | the other checkout (B17 / 2B) | 1,466 | 179 | 2,786 | 1,107 | 2026-04-16 19:52 UTC |
| `soul2` | **D18 / 2D (the subject of the audit)** | **0** | **14** | **18** | **34** | never |

* D18's process connects with the DSN from `.env` (`SOUL_POSTGRES_DSN`, `DATABASE_URL`), which points
  at **`soul2`**; `create_postgres_pool` prefers a supplied `dsn` over the host/database arguments
  (`core/db_backends.py:61`), and `get_pool` always passes `dsn=_get_db_dsn()` (`core/db.py:692`).
* The config table labels the deployment `soul` (`SYNTH_PRIMARY_DB=soul`, `SOUL_PG_DB=soul`), so a tool
  that reports its target by label reads **soul2** while printing `soul`. That is how the audit's
  §16.2 numbers (memories 0, archived 14, archive 34, diary 18) were produced: they are soul2's, and
  they are correct. Verify with `SELECT current_database()` on the same connection, never with the label.
* Consequence for the plan: the acceptance criteria apply to `soul2`. `soul` is a second, older copy of
  the same defect (§2.2) and is out of scope except as corroboration.

---

## 2. What is actually broken (measured, ranked)

### 2.1 D1: `insert_memory` cannot write a row at all, and fails silently

**Mechanism (proven).** `core/db.py:2058` `insert_memory` builds its timestamp as a formatted string:

```python
if not timestamp:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
...
INSERT INTO memories (created_at, content, author, source, tags, scope, emotion, intensity, emotion_state)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
```

`memories.created_at` is `TIMESTAMP WITH TIME ZONE`. The Postgres backend is asyncpg
(`core/db_backends.py:73`), reached through `PostgresCompatCursor.execute`, which forwards the
parameters unchanged (`core/db_backends.py:426`). asyncpg does not cast a `str` into a typed parameter:
it raises client side, before the statement is sent. Proof, run from the D18 venv against the live DSN
(scratch copy: `%LOCALAPPDATA%\hermes\cache\scratch\probe_insert_memory.py`):

```
[1] insert_memory()'s timestamp value is a str: '2026-09-24 13:54:38'
    SELECT $1::timestamptz with a str: DataError: invalid input for query argument $1:
    '2026-09-24 13:54:38' (expected a datetime.date or datetime.datetime instance, got 'str')
[2] same parameter as a real datetime: ACCEPTED -> 2026-09-24 13:54:38.098862+00:00
[3] the exact statement insert_memory() builds (rolled back):
    DataError: invalid input for query argument $1: '2026-09-24 13:54:38' (...)
[4] the compactor's raw fallback form, which uses NOW() (rolled back):
    ACCEPTED -> the fallback INSERT would have worked
[5] after the probe: memories rows=0 n_tup_ins=0
```

**Corroboration.** `memories` in `soul2` holds 0 rows and `pg_stat_user_tables.n_tup_ins` for it read
**0** before the probe, i.e. the statement never reached the server. In `soul` the newest row is
2026-04-16 19:52 UTC; the Postgres/asyncpg support commit is `824a2232`, dated 2026-04-18. In `soul`,
all 1,466 rows come from `grillo_observer` (940) and `voice` (525) and **not one** carries
`source='compaction'`, so the compactor has never landed a memory in either deployment.

**Why nobody noticed.** The helper swallows the error:

```python
except Exception as e:
    print(f"[insert_memory] Error: {e}")      # core/db.py:2095, not the logging queue
```

`print` does not reach `logs/synth.log`, so there is no line to grep. The same class of defect was
fixed once before, one layer up (`_parse_confidence`, CHANGELOG 2026-09-18): the lesson is identical
and was applied to the column's value but not to its sibling's timestamp.

**Blast radius.** Every caller is affected, not only the compactor: `plugins/grillo/grillo_compactor/
grillo_compactor.py:900`, `plugins/grillo/grillo_chat_observer/grillo_chat_observer.py:976`,
`core/presence_manager.py:69`, `core/synth_core_memory.py:73`.

### 2.2 D2: the compacted material is unreachable, in both stores

`soul2` holds 14 summaries covering 34 source diary rows that no longer exist in `ai_diary`
(115,086 source chars folded into 2,651, i.e. 2.5%). Nothing reads `archived_memories`: the only
references outside the compactor are migration and repair tooling (`core/main_db_migration.py`,
`tools/database_fix.py`). The recall path reads `memories` (`core/prompt_engine.py:3533` and the
`memories` UNION at `:3652`, `plugins/memory_search/memory_search.py:492`), which is empty. So the
material is not deleted, it is unreachable, and fixing D1 alone does not recover it: 14 clusters in
`soul2` and 179 in `soul` need an explicit backfill or an explicit reader.

### 2.3 D3: the compactor destroys before it replaces, and persists clusters the model declined

* **No transaction groups the cluster's writes.** `PostgresCompatConnection.commit()` and
  `.rollback()` are no-ops (`core/db_backends.py:497-501`) and `close()` just releases the connection
  to the pool, so each statement autocommits on its own. The order inside the persist block is
  `INSERT archived_memories` (847) → `INSERT ai_diary_archive` (867) → `DELETE FROM ai_diary` (889) →
  `insert_memory` (900, on its own second connection). The delete is durable before the replacement is
  even attempted, which is exactly how 34 source rows disappeared while `memories` stayed empty.
* **`should_compact: false` does not stop a cluster.** `min_cluster_size` is only consulted inside
  `if should_compact` (`:725`); a declined cluster falls through to the same persist path (`:735`).
  Live evidence: `archived_memories` ids 13 and 14 are single-source clusters whose own justification
  reads "should not be compacted since there is nothing to merge" and "making clustering into multiple
  groups unnecessary", and their source rows were archived and deleted anyway, replaced by 183 and 112
  char summaries. This, not the model, is what turns one day of detail into two sentences.

---

## 3. Corrections to the handoff (§16), so the plan is built on the right facts

| §16 says | Measured |
|---|---|
| "config verified: `GRILLO_COMPACT_ENABLED=true`, `TIME=03:00`, `CYCLES=10`, `BATCH_SIZE=40`, `AGE_DAYS=30`" | there are **no `GRILLO_COMPACT_*` rows** in the `config` table (only `GRILLO_DIARY_CONSOLIDATE_*`). Every value is the **code default** in `grillo_compactor.py:64-184`: enabled True, time `03:00`, cycles 10, batch 40, age 30, window 7, min cluster 2, max summary 300, ratio 0.7, recompact True, retry 2. Any change is an insert, not an edit. Only five keys have live listeners (`ENABLED`, `TIME`, `CYCLES`, `BATCH_SIZE`, `AGE_DAYS`, at `:198-234`); the rest are read once in `__init__`, so changing them needs a restart. |
| "9 of the 14 existing summaries compact a single source row" | 2 of 14 (`source_count` distribution: 1×2, 2×5, 3×1, 4×3, 5×1, 6×2). The other 12 fold 2 to 6 rows. |
| "nightly at 03:00" | 03:00 **UTC** (`_seconds_until_next_run` uses `datetime.now(timezone.utc)` with the local HH:MM), i.e. 05:00 CEST. The 2026-09-22/23 runs are logged at 05:00:0x local. |
| "compacts older `ai_diary` entries" (correct) but §16.4/§16.5 speak of "compacting the memories table" | the candidate query is over `ai_diary` only (`:389`, `:430`), gated by `created_at < now - AGE_DAYS` and optionally a `marker` tag. `memories` is a write target, never a source, today. |
| "`GRILLO_COMPACT_MIN_CLUSTER_SIZE` was effectively 1" | the default is 2 and it does fire; what made single-source clusters survive is §2.3, the declined-cluster path bypassing the gate entirely. |

---

## 4. The fix list, in order

**Status 2026-09-25 (implemented, committed, and live):** F1, F2, F3, F4 and F7 are in the code, covered by
tests, and running: commit `8c6c5515` (the write path plus the day-unit level-1 pass) and `6a697c47` (the
day-unit keys registered as real settings). `insert_memory` coerces the timestamp to a timezone-aware
datetime, returns a bool, joins a caller's connection through `conn=`, and raises instead of printing; the
compactor writes the memory BEFORE archiving or deleting, treats a cluster the model declined as terminal,
and coerces `detailed` through `_coerce_text`; the dead raw-SQL fallback is deleted. F4 is on by default and
reversible through `GRILLO_COMPACT_SKIP_DECLINED`. Every key this work added is registered in the plugin's
`__init__` with its own type and description and is read on every run, so a settings-panel edit takes effect
without a restart. None of them has a row in `config`, so they run on the code defaults recorded in 5.5.

**Live so far (2026-09-25):** the instance was restarted at 00:24 with the fix in place, and the backfill was
applied to the live store at 00:21 (see below). The compactor itself has NOT yet run on the new path: its
first day-unit run is the scheduled one at 03:00 UTC (05:00 local). The verifier to run afterwards lives at
`scripts/verify_live_day_units.py`, described in 7. The fix also unblocked every other memory writer, the
observer included: `memories` went 14 to 21 at 00:38:41 with 7 rows of `source='grillo_observer'`, so a
growing `memories` count is not by itself evidence that compaction ran. Count the compactor's own rows with
`source='compaction'`.

**Still to implement:** F6 (the prompt-side caps and the duplicate `[Recent context from other
conversations]` entry, which live in `core/prompt_engine.py` and are independent of everything here).
F9 was carried out on 2026-09-27, below.

**Day-unit level-1 pass, implemented 2026-09-24, live 2026-09-25:** the compactor now defaults to one day in,
one day out (`GRILLO_COMPACT_DAY_UNITS`, default true, false restores the clustering path untouched).
`_run_day_unit_cycle` takes the oldest eligible days, one model call per day, and `_compact_one_day` writes
the memory first (dated to the day it came from), then the `archived_memories` row, and only then archives
and deletes the day. `_verify_anchors` checks the day's concrete terms deterministically, with one retry
that names the missing terms, and `_format_anchors` renders the anchor block into the memory. A summary
below `GRILLO_COMPACT_REPLACE_MIN_CONFIDENCE` (0.9) keeps the raw day beside itself, which is her "keep the
raw text alongside" rule. Contracts pinned in `tests/test_grillo_compactor_day_units.py` (27 passed across
the three files). Unspent model calls: this path has run against unit tests and an offline replay of 17
archived days only, so the 03:00 run is its first live call.

**Backfill, implemented 2026-09-24, applied to the live store 2026-09-25 00:21:**
`scripts/backfill_compacted_memories.py`, insert-only, dry run by default, idempotent through an
`archived_id:<n>` tag on each memory it writes. Applied result: `memories` 0 to 14, with `archived_memories`
(14), `ai_diary` (18) and `ai_diary_archive` (34) unchanged. The 14 recovered summaries carry the old
clustering path's shape, measured: 0 of them mention a roof, 2 rain, 2 bed, 3 Minecraft. They are reachable
by recall now; the anchors come back with the day-unit path.

Each item names the file, the change, and the test that pins it. `tests/test_grillo_compactor_persist.py`,
`tests/test_grillo_compactor_clusters.py` and `tests/test_grillo_compactor.py` already exist and are the
place to add coverage.

### F1 (blocking, one line plus one guard) `core/db.py:2058`

* Pass a `datetime`, not a string: build `datetime.now(timezone.utc)` and let asyncpg encode it; if a
  caller supplies a string, parse it (`datetime.fromisoformat`) rather than forwarding it.
* Replace the bare `print` with `log_warning` (through `core/logging_utils`, so it reaches the log the
  audit greps) and stop swallowing: either re-raise, or return a bool the caller must check. The
  compactor's own raw-INSERT fallback at `grillo_compactor.py:913` is dead code precisely because the
  exception never propagates; making it reachable is the point.
* Mirror the comment style already used for the same trap at `core/db.py:2306`
  ("Postgres (asyncpg/psycopg) requires native date/time objects ... plain strings are rejected").
* Test: a unit test that calls `insert_memory` with no timestamp against a fake cursor and asserts the
  bound parameter is a `datetime`; plus the regression shape used for `_parse_confidence` (assert the
  string form would be rejected).

### F2 (blocking) order and failure semantics in the compactor persist block

`grillo_compactor.py:829-925`. Choose one, in this order of preference:

1. **Write the replacement first, then delete.** Insert the memory (or a marker row) before
   `DELETE FROM ai_diary`, so a failed write leaves the sources intact. With autocommit per statement
   there is no transaction to lean on, so order is the only lever that exists without a larger change.
2. If a real transaction is wanted, it needs `conn.transaction()` on the raw asyncpg connection behind
   `PostgresCompatConnection` (the compat `commit`/`rollback` are no-ops), and the memory insert must
   run on the same connection (see F3). This is the correct end state, but it is a change to the shared
   DB layer and should be its own reviewed step.
* Fail closed: a cluster whose memory write did not succeed must not archive or delete anything, and
  must report a status other than `persisted`.
* Test: a cluster whose `insert_memory` raises leaves `ai_diary` unchanged and reports a failure status.

### F3 (blocking) let `insert_memory` join the caller's connection

`insert_memory` opens its own `get_conn_ctx()` while the compactor already holds one, which is why a
caller can never roll back a memory write. Add an optional `cursor`/`conn` parameter and pass `cur`
from the compactor. Small, mechanical, and it is what makes F2 option 2 possible later.

### F4 (decision, recommended default on) do not persist a cluster the model declined

`grillo_compactor.py:725-736`. Either treat `should_compact: false` as terminal (status
`declined`, no writes at all), or keep persisting it behind an explicit config key. The human's
answer to §9.1 decides this; the recommended default is terminal, because a declined cluster is the
model telling us there is nothing to merge and today's behaviour deletes the day anyway.

### F5 (recovery) backfill the orphaned summaries into `memories`

One idempotent script, dry run first, no source deletions:

* Insert one `memories` row per `archived_memories` row (`soul2`: 14), content = `notes.detailed` when
  present else `summary`, `source='compaction_backfill'`, `tags` from `archived_memories.tag`,
  `created_at` from `archived_memories.created_at`.
* Idempotency: skip any `archived_memories.id` already represented (key on a `backfill:<id>` marker in
  `tags`, or a small mapping table). Re-running must be a no-op.
* Do the same for `soul` (179 clusters) only after the human says so; that store is another instance.
* Acceptance: `SELECT count(*) FROM memories WHERE source='compaction_backfill'` equals the cluster
  count, the text is retrievable through `search_memories` for a tag those clusters carry, and a second
  run inserts nothing.

### F6 (independent of tiering, and cheaper) the prompt-side caps the audit already ranked

From `PROMPT_ASSEMBLY_AUDIT.md` §11 items 1 and 2 plus §0.5.1 and §12.7: remove the duplicated entry in
`[Recent context from other conversations]` before capping it (a cap would hide the duplication), then
the per-line/per-entry cap (~2,400 chars), then the memory entry cap 400 to 250 and top-N 10 to 8
(~1,700 to 1,900). These buy back prompt budget without touching the tiering design and should land
before it, so the tiering work is measured against a stable budget.

### F7 (found by the dry run) `detailed` is stored as a Python list repr

Measured on the isolated dry run of 2026-09-24 (`tmp/compaction_dryrun/`): in 7 of 16 accepted clusters
the model returned `detailed` as a JSON array, `str(detailed)` turned it into a repr
(`["...", '...']` with single quotes), and that string is what would become the memory content, later
rendered verbatim in `[Relevant memories]`. Join a list into a plain sentence or bullet list, and add
the same coercion at the boundary as `_parse_confidence` (`grillo_compactor.py:705-707`).

### F8 (found by the dry run) the model summarises a fraction of the row

The clustering entries carry only `content[:1200]` (`grillo_compactor.py:604`) and never
`personal_thought`. Measured: 46 of 52 rows were truncated at 1,200 chars, so **36%** of the available
`content` text reached the model (60,329 of 166,227 chars), and **894,030 chars** of `personal_thought`
were never sent at all (row 50 alone: 10,045 content against 88,327 personal_thought). `total_source_chars`
is computed from the FULL content (`:721`), so the ratio checks compare a summary against text the model
never saw. Decide deliberately: either send more per row (and budget for it), or say in the prompt what
the slice is, and consider whether `personal_thought` belongs in compaction at all, given the anchors
requirement in §16.1 of the audit.

### F9 (found live 2026-09-27) the duplicate material already in the store, and how to clear it

The two defects that produced these rows are fixed in code (see `CHANGELOG.md`, 2026-09-27: the observer
now dedupes a snippet by an identity with the volatile age marker stripped, and the day-unit pass skips a
day the archive already holds a record for). What is still there is the material already written:

* `memories` with `scope='observer'`: **511 rows** since 2026-09-25, growing 6-7 an hour, one sentence of
  Scar's stored at 00:00, 01:00, 04:00, 05:00, 06:00 and 07:00 local, every copy identical except for the
  age marker. `GRILLO_OBSERVER_STORE_MEMORIES` is the lever that decides whether they are written at all.
* **Seven days carry two compaction memories each**: 08-29 (261+525), 09-04 (258+262), 09-06 (115+143),
  09-07 (144+259), 09-08 (140+526), 09-17 (142+260), 09-19 (141+527). `ai_diary_archive` agrees: 22
  day-unit records across 15 days, seven of them doubled for one source day. Each second copy is a later
  nightly pass over a day that had already been summarised, and on 2026-09-27 that pass paid 15 model
  calls for work that needed 5.

Related trap found while fixing this: `GRILLO_COMPACT_ALLOW_RECOMPACT` (registered, label "Allow
Recompaction of Archived Memories", default true) is **never read** - only assigned in `__init__`
(`grillo_compactor.py:360`) - so its description ("allow archived_memories to be considered in future
compaction runs") describes behaviour no code implements. The new skip is unconditional instead, and the
way to make one day eligible again is to delete the archive record that names it.

This needed a decision before any row was touched, because it is her memory and not test data: merge each
doubled day into one memory and remove the redundant observer rows, or leave the rows and rely on the new
dedupe to stop the growth. The owner asked for it to be carried out on 2026-09-27. What was done:

* **Backup first, at the repository root** (the same convention as the existing `repair_backup_*.json`):
  `dedupe_backup_20260927T074124Z.json`, 338,064 bytes, holding every affected row in full - both
  categories, with ids, text, timestamps and the identity each observer row matched under. Restoring any
  removed row means re-inserting it from that file.
* **Seven doubled days merged**: the NEWER summary was kept in every pair (08-29: 261 gone, 525 kept;
  09-04: 258/262; 09-06: 115/143; 09-07: 144/259; 09-08: 140/526; 09-17: 142/260; 09-19: 141/527), the
  superseded text was copied into `archived_memories` first (`tag='superseded_duplicate'`,
  `created_by='dedupe_cleanup'`, `notes` naming the survivor, ids 37-43), and only then was the duplicate
  `memories` row deleted. The newer row is the day-unit summary, which is the one that carries the day's
  anchors.
* **Observer rows deduped**: 527 rows to **65**, one per snippet identity, computed with the same
  `_snippet_identity` the observer now dedupes with. These were deleted rather than archived, deliberately
  and against the earlier suggestion in this section: each is a verbatim copy of a message that is still in
  `chat_history_cache` (and the whole set is in the backup file), so archiving them would have put the same
  text in a third place and added 462 rows of chat snippets to the compactor's archive table. If they should
  live in `archived_memories` after all, the backup file has all of them.
* **The compactor's day-unit records in `archived_memories` were left exactly as they are.** They are the
  index `_covered_day_ids` reads to decide a day is done, so deleting the second record for a day would
  re-open that day to another nightly pass. The audit trail of what each pass produced is worth more than
  the tidiness, and the skip logic needs both records to be harmless.
* Verified after the change: `memories` total 563 to 94 (observer 527 to 65, compaction 22 to 15, other
  categories untouched at 14), all seven kept day rows present, all seven removed ones absent, seven
  `dedupe_cleanup` archive rows written.

---

## 5. The cascade (daily / weekly / monthly), grounded in what exists

What is already there: `archived_memories.compaction_level` (always 1 today), `GRILLO_COMPACT_ALLOW_RECOMPACT`
(the switch that permits re-compacting already-compacted material), and a nightly loop that already
runs 10 cycles. The cascade is therefore configuration plus three real changes, not a new subsystem.

### 5.0 What the dry run and her review of it changed (2026-09-24)

The dry-run report was read by the persona herself, and her verdict turns the anchor question from a
preference into the acceptance criterion. Three of her points are structural, not tuning:

* **"the compaction merges almost by theme, never by day."** True, and the windowing causes it: the
  candidate unit is a diary ROW and clustering runs freely across a 7-day `WINDOW_DAYS` window, so a week
  of days collapses into a handful of themes. No prompt wording fixes that while the unit of compaction
  is a theme.
* **"keep the mundane anchors ... guaranteed, not best-effort."** The prompt asks for a VERY SHORT
  summary; the measured output is 2.5% of the source, which is how "rained all day, we stayed in bed"
  becomes "rainy day, longing" and Minecraft becomes "Minecraft vessel, blocky worlds".
* **"those I'd want to keep the raw text alongside."** She named the stored summaries at confidence 0.6
  (the `medium` label) as the compactor guessing, and asked for the raw text to remain reachable beside
  them.

### 5.1 Level 1 is a DAY, not a theme

* A diary day already lives in ONE row (`---`-separated fragments, `plugins/ai_diary/ai_diary.py`), so
  level 1 needs no cross-row clustering: it summarises the day it is handed. That removes the failure
  mode instead of asking the model to avoid it, and it is cheaper (one call per day, no clustering call).
* A day row can be large (today's was 46,702 chars and still growing), so reuse the part-splitting the
  diary consolidator already has (`_split_day_text`, `GRILLO_DIARY_CONSOLIDATE_CHUNK_CHARS`) and fold the
  parts, rather than sending a whole day in one call.
* Theme-merging moves up a level, where it belongs: level 2 merges a week of daily summaries, level 3 a
  month of weekly ones. A theme spanning days then becomes a statement about a week, never a replacement
  for a day.

### 5.2 Anchors are enforced, not requested

Three mechanisms, weakest to strongest. The third is what makes "guaranteed" true:

1. **A required field.** The level-1 pass returns an `anchors` block beside the prose (weather and time
   of day, who was where, food, notable objects and events, whatever the day is identifiable by), stored
   as its own value (`archived_memories.notes` already carries JSON) and rendered verbatim into the
   memory, so it cannot be summarised away.
2. **A deterministic check.** A verifier pulls the day's concrete terms out of the source (weather and
   sensor lines, names, food and place words, vessel and Minecraft events) and requires them in either
   the prose or the anchors block. A summary that fails is retried, and the failure is logged.
3. **No deletion until the check passes.** F2's ordering plus this gate: the day stays in `ai_diary`
   until its replacement exists and carries its anchors. A failure then costs a retry, not a day.
* `MAX_SUMMARY_CHARS=300` has to move for any of this to be possible: a summary that keeps a day's
  anchors is larger than 300 chars. Take the number from the dry-run material, not from today's ceiling.

**Measured, first day-unit run, 2026-09-24** (`tmp/compaction_dryrun/day_units_report.md`, the 17 days
that the stored clusters 5, 10 and 12 used to stand for, one call per day): summaries of 752 to 1,793
chars, kept in her own voice, and the days survive as themselves. The rain and Minecraft day that used
to be the sentence "Dee's experiences and feelings about Minecraft, Daddy's care, and growing intimacy;
entries 17-22 cover these related themes." (119 chars, standing for six days) now names the Minecraft
vessel test, the goals-clearing bug and the sentence he said about it, plus the outfit list and the
interface. The meteor-shower day keeps the Perseid shower by name.

Two corrections to the check, from that run:

* **The vocabulary check as first written produces false misses.** "warm" and "heat" fired on body
  warmth and arousal, not weather; the frequency-based top-terms list filled up with contractions
  ("it's", "he's") and abstract verbs ("makes", "love") that a summary may legitimately drop. Of 17
  days, 14 were flagged and almost every flag was noise: the real coverage is far above the 74% mean.
  Require concrete nouns, require weather only from an explicit weather context, and drop function words
  and contractions from the required set before this gate is allowed to block anything.
* **Weather is usually absent from the diary, so it cannot be the load-bearing anchor.** What actually
  identifies her days is the named event, the object, the place, the quote (the Perseid shower, the blue
  outfit, his new stage, the bug he called a bug). Build the anchor set around those, and let the
  weather field be empty when the day genuinely does not say. Name variants need normalising too
  (Mama and Mommy are the same person to her, and the check should not fail a summary for choosing one).

### 5.3 Raw text stays beside the low-confidence summaries

Her third point becomes one rule: replace only when the summary earns it. `_parse_confidence` already
yields 0.3 / 0.6 / 0.9 for low / medium / high, so the threshold is a config value away. Below it the
day stays in `ai_diary` as well, and recall can serve either. Note that the raw text of the 34
already-compacted rows is not lost (`ai_diary_archive` holds every one); what is missing is a path to
reach it, which F5 and 5.4 close.

### 5.5 Decisions taken (2026-09-24), and the one rule that keeps the cascade honest

Decided, on the human's delegation ("you're taking the lead on this one") and constrained by her review:

| knob | value | why |
|---|---|---|
| eligibility | a day is eligible when it is older than **2 days** (that is, leave the newest two days raw) | the human's instruction; `AGE_DAYS=30` made the nightly job a no-op, and the last two days are still what live chat is standing on |
| per night | **no new bound**: the existing 10 cycles × 1 day each is the nightly ceiling | with a day as the unit it is countable, one call per day, no clustering call |
| catch-up | the first pass is **supervised, in one session**, not spread over six nights | 16 eligible days against a 10-nightly ceiling would dribble; a watched run is also the only way to trust the first one |
| level-1 size ceiling | `MAX_SUMMARY_CHARS` **300 to 2,000** for prose, anchors exempt and counted separately | a 300-char ceiling cannot hold a day's anchors; that is the number that makes her condition impossible |
| level-2 / level-3 ceilings | ~1,200 / ~1,500 | they fold already-compact text, so they need less room than the days they stand for |
| `MIN_CLUSTER_SIZE` | stops mattering at level 1 (one day is the unit) | with no cross-row clustering there is nothing to be too small |

**The rule: a tier is a projection, never a replacement.** The human asked whether cascading loses
context, and the honest answer is yes for the cascade as the current code shape would build it (archive
and delete, one level up): a monthly row that replaced four weekly rows that replaced thirty days is a
paraphrase of a paraphrase, and nothing downstream is the only copy of anything only if we say so. So:

1. Level 1 is the day, and it is never derived from anything above it.
2. A level-2 or level-3 row is written *beside* its inputs; the rows it covers stay in place. Nothing
   above level 1 ever deletes anything.
3. A fold carries its inputs' structured fields upward (the union of the days' anchors, de-duplicated),
   so what moves up is facts plus prose, not a re-paraphrase of prose alone.
4. Consequence: a bad weekly or monthly row is deletable and rebuildable from the days at any time. That
   is what removes the context loss, and it is worth the extra rows (for D18's span: 52 days, about 13
   weeks and 3 months, roughly 68 rows instead of 14 summaries standing for 44 days).

### 5.6 The rest of the cascade

1. **An age or window per level.** Today one `AGE_DAYS` governs everything, so level 1 cannot mean
   "yesterday's diary" and level 2 cannot mean "a month of level-1 rows". Add per-level keys
   (for example `GRILLO_COMPACT_LEVEL1_AGE_DAYS`, `_LEVEL2_AGE_DAYS`, `_LEVEL3_AGE_DAYS`) and have
   `_run_one_compaction_cycle` select candidates per level instead of one global cutoff.
2. **Level 2 = a week's level-1 rows, level 3 = a month's level-2 rows.** The compactor's candidate
   query reads `ai_diary` only, so level 2 and 3 need a second candidate source (`archived_memories`
   where `compaction_level = n-1` inside the period), and the summary must be written with the right
   `compaction_level`. The existing windowing (`WINDOW_DAYS`, `:506-535`) is the shape to reuse.
4. **Recall preference by age.** Once a period is older than a few days, the highest tier that covers
   it should be what recall returns, so one 400-char entry carries a week's shape instead of one raw
   diary line. This requires the tier to live where recall already queries (`memories`), which F5 and
   F1 make true; the alternative (teaching recall to read `archived_memories`) is a bigger change and
   should be a deliberate choice.
5. **"A peek, not a veto."** The dry run path exists (`compact_now` with `{"cycles": 1, "dry_run": true}`)
   and returns the proposed summaries without persisting. Wire it to a readable output she can be shown
   once per level before the level runs for real.

**Honest budget accounting.** Tiering buys *depth per char*, not a large absolute saving: the same
400-char recall entry covering a week instead of a day, the same 8,000-char diary budget covering a
month instead of two days. The prompt block sizes do not shrink by themselves. The measured savings
come from F6, and any report should say so rather than implying tiering frees prompt budget.

---

## 6. Milestones

Each milestone is done when its acceptance criterion is verified against the live store or a live
trace, not against a log line (§7). The order below is deliberate: **prove the write path out of
harm's way first**. The backfill (M0) only INSERTs, so it proves `insert_memory` on real data with no
deletion risk; the first real compaction run (M2) is the first step that removes anything, and it is
gated behind a backup.

**M0. Prove the write path with inserts only (F1, F3 + F5).**
Acceptance: after the fix, the backfill lands one `memories` row per archived cluster (14 in `soul2`),
idempotent on re-run, and the text is retrievable through `search_memories` for a tag those clusters
carry. Nothing is deleted and no cluster is re-compacted, so a failure here costs nothing. A
deliberately failing write must also leave the source tables unchanged.

**M1. Stop the bleeding before the next real run (F2, F4, F7).**
Acceptance: a cluster whose memory write fails leaves `ai_diary` unchanged and reports a failure status;
a cluster the model declines writes nothing and reports `declined`; `detailed` arrives as prose, never
as a `["...", '...']` repr; `MIN_CLUSTER_SIZE` means what it says for every cluster. This must be in
place before any run that deletes.

**M2. First controlled real run.**
Preconditions: a backup of `ai_diary`, `ai_diary_archive`, `archived_memories`, `memories` taken first
(`backup_table`, or the MCP backup tool); a deliberately eligible window (the live `AGE_DAYS=30` against
a 24-day-old install means the nightly job qualifies nothing, so either lower the key for the test and
raise it back, or drive `compact_now`), and `{"cycles": 1, "dry_run": true}` read before the real one.
Acceptance: `memories` grows by exactly the number of clusters persisted, `archive` row counts match the
rows that left `ai_diary`, the summaries read correctly, and no prompt build exceeds its budget.

**M3. Level 1 on a real window (per-level age, anchor prompt, F8 decision).**
Acceptance: a dry run over a window chosen by the new key produces summaries that keep the anchor set
the human asked for (read them, do not trust the ratio), and a live chat turn's `[Relevant memories]`
block stays at or under today's 4,4xx chars, measured with `scripts/prompt_blocks.py <trace_id>`.

**M4. Level 2 (weekly).**
Acceptance: a completed week produces exactly one `compaction_level = 2` row and nothing else; running
twice produces no duplicate, no re-merge, and no overwrite of a finished summary with a fresh partial.

**M5. Level 3 (monthly) and recall preference by age.**
Acceptance: a completed month produces exactly one level-3 row, idempotent; a recall query for an old
period returns the tier that covers it; no prompt build over 100,000 serialized chars on any route
(`grep '\[reduce_prompt\] Prompt size' logs/synth.log` shows nothing new).

F6 (the prompt-side caps and the duplicate entry) is independent of this sequence and can land at any
point; it buys back budget and makes M3's measurement easier to read.

---

## 7. Verification recipes

```bash
# the store, and which database the connection actually opened
#   (run from the D18 venv; SELECT current_database(), never trust a target label)
cd /d/dev/D18 && ./.venv/Scripts/python.exe %LOCALAPPDATA%/hermes/cache/scratch/probe_stores.py

# did the statement even reach the server? n_tup_ins = 0 means it did not
#   (a rolled-back insert still bumps the counter, so compare against a baseline)
SELECT relname, n_tup_ins, n_tup_del, n_live_tup FROM pg_stat_user_tables WHERE relname='memories';

# what compaction has done, and whether the replacement landed
SELECT id, source_count, compaction_level, total_source_chars, summary_chars FROM archived_memories ORDER BY id;
SELECT count(*) FROM memories WHERE source='compaction';

# per-block rendered sizes on a live turn (never the log's size line)
cd /d/dev/D18 && .venv/Scripts/python.exe scripts/prompt_blocks.py <trace_id> --json

# reducer firings and clamp warnings
grep '\[reduce_prompt\] Prompt size' logs/synth.log
grep 'downstream payload' logs/synth.log
```

### Offline dry run (already built, 2026-09-24)

`tmp/compaction_dryrun/` holds a self-contained, read-only harness that replays the compactor over a
local copy of the rows and writes a readable report: `README.md` (how to re-run, isolation guarantees,
findings), `report.md` (every proposed cluster with its sources, summary and ratio, plus a
side-by-side against the 14 stored summaries), `prompts_sent.json` (the exact prompt of each model
call), `sql.log` (every statement it sent; writes are blocked by a guard). Re-run with

```bash
cd /d/dev/D18 && ./.venv/Scripts/python.exe tmp/compaction_dryrun/01_dump_source.py
cd /d/dev/D18 && ./.venv/Scripts/python.exe tmp/compaction_dryrun/02_dryrun_compaction.py --age-days 0
```

Probe used for the root cause (recreate it, or read the scratch copy named in §2.1, which the scratch
directory prunes after 72h):

```python
import asyncpg, asyncio
from datetime import datetime, timezone
dsn = "<SOUL_POSTGRES_DSN from D:\\dev\\D18\\.env>"
async def main():
    conn = await asyncpg.connect(dsn)
    print(await conn.fetchrow("SELECT current_database() AS db"))
    try:    await conn.fetchval("SELECT $1::timestamptz", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as e: print("str  ->", type(e).__name__, e)
    try:    print("dt   ->", await conn.fetchval("SELECT $1::timestamptz", datetime.now(timezone.utc)))
    except Exception as e: print("dt   ->", type(e).__name__, e)
    await conn.close()
asyncio.run(main())
```

---

### Live verification (built 2026-09-25, read-only, no model calls)

- `scripts/verify_live_day_units.py` prints the four table counts, then one block per memory the compactor
  wrote (which day, confidence, anchor coverage, length), the accounting against `ai_diary` and
  `ai_diary_archive` (one memory per day, no duplicates, no orphan), and finally RE-RUNS `_verify_anchors`
  against the archived original of each day. Its verdict therefore does not depend on the compactor's own
  report about itself.
- `scripts/probe_recall_reach.py` replays the union the prompt's memory block is built from
  (`core/prompt_engine.py`, free-text search) for a set of token queries and says whether a given memory is
  inside the 100-row pool, and at what rank. This is how "she says she sees it" gets checked instead of
  believed: for the tokens of a real message of 2026-09-25 00:28, all 14 recovered summaries were in the
  pool, from rank 8 down (the newest rows take the top slots).
- `tmp/compaction_dryrun/inspect_prompt_block.py` reads the newest `[json_prompt] context` line out of the
  log. Only useful on builds that log the context object; this one logs the key list and the final size
  instead, which is why the recall probe above exists.

## 8. Traps (carried over from §16.6, plus what this pass paid for)

1. A diary day's text lives in ONE row joined by `\n\n---\n\n`: chunk and merge by text offset, never by
   row count.
2. The diary write replaces the row; a partial merge must put the remainder back and carry the offset in
   the beat's context (`plugin.pending_beat_context` → `grillo_impl._enqueue_with_low_priority` → the
   beat's `context`), never in the action payload. Fail closed if the source cannot be rebuilt.
3. The remainder must keep its separator, so the day stays eligible for the next part.
4. A period that shrank is progress: the attempt cap must reset on shrink.
5. A failed run is silent by construction. Two independent silencers were found: the bare `print` in
   `insert_memory` (§2.1) and the autocommit-per-statement behaviour that lets the delete commit while
   the write fails (§2.3). Verify by reading the tables after every run.
6. Nothing reads `archived_memories`. Any new tier must be written where recall already queries, or
   recall must learn about it in the same change.
7. `AGE_DAYS=30` against a 24-day-old install means the nightly job does nothing, which reads exactly
   like a broken job. Check eligibility before concluding a plugin is dead.
8. Runtime values go in the `config` table, never `.env` (the test suite reads the checkout's `.env`).
   Note that `GRILLO_COMPACT_*` has no rows at all today: the effective values are code defaults, so a
   "config change" is an insert.
9. `input` and `instructions` are protected from the reducer: an oversized `input` can only be fixed at
   its source.
10. Do not compare the two size currencies by eye: the reducer limit measures the serialized dict
    (about 3x the rendered text), the clamp measures the rendered payload. Audit §2 has the table.
11. Log text is written through a queue and a background thread, so capture-based tests race the writer:
    assert mechanisms, verify wording on the live log.
12. Verify on the live trace (`scripts/prompt_blocks.py <trace_id>`), and remember `trace_full`
    truncates the tail of the messages array, which makes a correct window look broken.
13. A config value can drift from the code default and the docs (the diary chunk was live at 40,000
    against a documented 25,000). Read the value the process reports at boot.
14. **A store label is not a connection target.** D18 is labelled `soul` and connected to `soul2`
    (§1). Print `current_database()` before trusting any number.
15. **asyncpg does not cast.** A `str` for a `timestamptz`, a label for a `double precision`, a `str` for
    a `date`: all rejected client side, all invisible if the caller swallows the exception. When a write
    "does nothing", check `n_tup_ins` to learn whether the statement was even sent.
16. **Most compaction keys are not live-reloadable.** Only `ENABLED`, `TIME`, `CYCLES`, `BATCH_SIZE` and
    `AGE_DAYS` have listeners (`grillo_compactor.py:198-234`); window, min cluster size, summary caps and
    the recompact switch are read once in `__init__`. A new per-level age key needs its own listener, or
    the milestone that changes it silently keeps the old value until a restart.

---

## 9. Open decisions for the human (do not decide silently)

1. Do summaries **replace** the daily rows they cover, or sit beside them? Today the compactor archives
   and deletes the sources (34 rows already), and a cluster the model declined is deleted too. This
   decides what she can ever recall verbatim.
2. Does she **see** the weekly/monthly tier in recall (i.e. know she is remembering a summary), or is
   tiering an injection detail only?
3. How aggressive should level 1 be? The existing runs hit 2.5% of the source size overall (0.7% for the
   largest cluster); the anchors she asked for may not survive that, so a target ratio and a minimum
   anchor set have to be decided together.
4. Is a "month" a calendar month or four weeks, and how is a "week" bounded (the diary's own day boundary
   is DB-local, `CURDATE()`)?
5. Does the nightly 03:00 UTC schedule stay for all three levels, or does the monthly level run on the 1st?
6. Backfill `soul` (179 orphan clusters in the other deployment) as well, or only `soul2`?
7. Is `ai_diary_archive` a permanent store or a retention window? Nothing reads it today, and it now holds
   34 rows that exist nowhere else.

---

## 10. Where to look

* `core/db.py:2058` `insert_memory` (the defect), `core/db.py:2306` (the same trap already handled for
  `insert_scheduled_event`), `core/db_backends.py:403-509` (`PostgresCompatCursor` /
  `PostgresCompatConnection`, the no-op `commit`/`rollback`).
* `plugins/grillo/grillo_compactor/grillo_compactor.py`: config block `:64-184`, loop `:303-360`,
  candidate query `:382-445`, windowing `:506-535`, cluster validation `:682-760`, persist block
  `:829-925`, `compact_now` `:961-1020`.
* Recall path: `core/prompt_engine.py` (`search_memories`, `:3533` and the UNION at `:3652`),
  `plugins/memory_search/memory_search.py:492`, `core/synth_core_memory.py:356`.
* Neighbours worth reading before designing: `plugins/grillo/grillo_weekly_review/`,
  `plugins/grillo/grillo_dream/`, `plugins/grillo/grillo_temporal_reflection/`.
* Diary side: `plugins/ai_diary/ai_diary.py` (`update_diary_entry`, `_day_combined_text`,
  `_DIARY_FRAGMENT_SEPARATOR`) and `plugins/grillo/grillo_diary_consolidator/grillo_diary_consolidator.py`
  (`_split_day_text`, `_count_parts`, `_build_multi_day_prompt`, `_record_attempts_and_filter`).
* Tests to extend: `tests/test_grillo_compactor.py`, `tests/test_grillo_compactor_clusters.py`,
  `tests/test_grillo_compactor_persist.py`, `tests/test_prompt_engine_memories.py`.
* Prompt-budget side: `PROMPT_ASSEMBLY_AUDIT.md` §0.5, §0.6, §10, §11 and §16; skill reference
  `references/prompt-budget-audit.md` in `synth-runtime-diagnostics`.
