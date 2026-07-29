# Rift Vessel Compactor

Turns each **Rift Vessel** embodiment session into a compact, factual
**operational recap** and stores it in the dedicated `vessel_diary` table.

This is a **different scope** from the *G.R.I.L.L.O. Compactor*: Grillo
synthesises long-term memories; this plugin only ever summarises a single vessel
session's activity log. They are intentionally two separate plugins.

## When it runs

Compaction happens **only when a session reaches the ENDED state** — a true
disconnect, an explicit logout, or a cooldown/grace-window close. It never fires
while the session is CONNECTED or merely RECONNECTING.

The work is done **off the message chain**: when a session ends, the id is
pushed onto this plugin's internal low-priority worker queue and the recap is
produced in the background. No in-world turn, no Agent Lane, no Drone.

## What it produces

* **Source:** the session's rows in `vessel_activity_log` (event type, summary,
  metadata such as positions, quantities and action outcomes).
* **Output:** a factual, third-person recap (no first-person / personality),
  chunked and folded so a long session never overruns a single LLM call, saved
  to `vessel_diary` with `reason = "activity_recap"`.
* **Fail-safe:** any LLM error degrades to a deterministic plain-text join; an
  empty activity log produces no entry.

It **never** writes to the shared `ai_diary` — that would pollute every
non-vessel prompt.

## Run it manually

Use the **Run compaction** button on this plugin's card (WebUI → Plugins). With
no payload it recaps the most recently ended session; pass
`{"session_id": "..."}` to target a specific one.

## Configuration

| Key | Default | Purpose |
|-----|---------|---------|
| `VESSEL_COMPACTOR_ENABLED` | `True` | Enable/disable end-of-session recap. |

Requires the core **Rift Vessel** plugin to be enabled — otherwise no session
can ever start, so there is nothing to compact.
