# Grillo — Weekly Review Beat

Part of the [G.R.I.L.L.O.](../guide.md) background subsystem.

## Purpose

A weekly life review: once a week Synth pauses for a private retrospective of
the past week and authors her **next-week goals** as real, persistent goals in
the generic [Goals](../../goals/guide.md) store. The goal store is deliberately
catalogue-free — Synth writes whatever she actually wants, in her own words.

## Beat

- **Trigger:** weekly (Sunday 02:00 by default — not a weighted beat).
- **Output:** `goal_set` (new next-week goals) and `goal_update`
  (`status: "done"`) actions written by the model, all scoped
  `scope="none"` for personal, non-game goals.

## How it works

The plugin builds a reflective prompt that injects (a) the last N days of diary
entries and (b) the current personal goals, then enqueues it as a low-priority
G.R.I.L.L.O. message. The **model** authors the goals — the plugin never writes
goals in Python. The enqueued context restricts the turn's allowed actions to
`goal_set` / `goal_update` (both `security_level: "low"`, no `external_effects`,
so the turn stays on the Fast Lane) and marks it as a grillo beat so history and
context injection are skipped. This is a private review: the model is told not
to speak to any user.

The beat type `weekly_review` is deliberately **not** added to
`GrilloPlugin.BEAT_TYPES` — that dict is the weighted-random interval scheduler
and cannot express a weekly day+time cadence. This plugin runs its own weekly
scheduler.

## Configuration

| Key | Purpose |
|-----|---------|
| `GRILLO_WEEKLY_REVIEW_ENABLED` | Enable the weekly life review (default `True`). |
| `GRILLO_WEEKLY_REVIEW_DAY` | Day of the week the review runs (Mon..Sun, default `Sunday`). |
| `GRILLO_WEEKLY_REVIEW_TIME` | Local time (HH:MM) the review runs (default `02:00`). |
| `GRILLO_WEEKLY_REVIEW_DIARY_DAYS` | How many days of diary entries to reflect on (default `7`). |
| `GRILLO_WEEKLY_REVIEW_MEMORY_LIMIT` | Max long-term memories recalled (default `20`, reserved). |

Plus the shared Grillo settings (`GRILLO_CORTEX`, …). See the
[G.R.I.L.L.O. guide](../guide.md).
