# SOUL

Runtime orchestration of the **SOUL** memory/emotion architecture. SOUL
compiles conversation transcripts into structured `MemCells` (and a DSP
representation), rolls them up over time, and injects the resulting SOUL
context into the prompt — a higher-level layer over raw diary/memory storage.

It runs a scheduler that compiles buffered transcript after an idle period and
performs periodic roll-ups. Persistence can be in-memory or PostgreSQL.

## Actions

| Action | Purpose |
|--------|---------|
| `static_inject` | Inject SOUL context into the prompt. |

## Configuration

| Key | Purpose |
|-----|---------|
| `SOUL_PLUGIN_ENABLED` | Master enable toggle. |
| `SOUL_COMPILE_IDLE_SECONDS` | Idle seconds before compiling buffered transcript. |
| `SOUL_SCHEDULER_INTERVAL_SECONDS` | Scheduler tick interval. |
| `SOUL_REPOSITORY_BACKEND` | Persistence backend (`memory` or `postgres`). |
| `SOUL_POSTGRES_DSN` | PostgreSQL DSN when the backend is `postgres`. |
