---
kind: configuration_system
name: Centralized Configuration Registry with Environment, Database, and WebUI Layering
category: configuration_system
scope:
    - '**'
source_files:
    - core/config_manager.py
    - core/config.py
    - main.py
    - providers/anthropic.json
    - config/synth_mcp.json
    - config/mcporter.json
    - core/model_config.json
---

Synthetic Heart implements a unified configuration system built around a singleton `ConfigRegistry` (`core/config_manager.py`) that layers three sources of truth in strict precedence: environment variables (highest), persisted database values, and hard-coded defaults. The registry is the single entry point for all runtime configuration across core modules, interfaces, plugins, and engines.

**What system/approach is used**
- A custom `ConfigRegistry` class provides typed configuration variables with automatic type coercion (`bool`, `int`, `float`, `str`, `json`, or custom callables).
- Environment variables are loaded via `python-dotenv` from `/app/.env` at module import time, with a fallback loader in `main.py` that reads a repo-local `.env` using `os.environ.setdefault` to avoid overwriting existing values.
- Persistent storage uses a `config` table in the application database (PostgreSQL or MariaDB/MySQL), with upsert logic that adapts per-dialect (`ON CONFLICT` for Postgres, `REPLACE INTO` / `ON DUPLICATE KEY UPDATE` for MySQL variants).
- The WebUI exposes all registered variables through an API endpoint that serializes definitions (label, description, group, component, value_type, constraints, hidden, readonly flags) so the UI can render a cohesive settings dashboard.
- A `ConfigVar` wrapper returns live-updating references to config values, automatically reflecting DB changes without manual listener registration.

**Key files and packages**
- `core/config_manager.py` — Core registry implementation, persistence, type conversion, listener/notification system, bootstrap flush, batch DB loading.
- `core/config.py` — Declares all core configuration variables (cortex scope selection, live voice settings, trainer IDs, timeouts, feature toggles) using `config_registry.get_var()` with rich metadata.
- `main.py` — Loads repo-local `.env` defaults before any imports, initializes database, then calls `config_registry.persist_bootstrap_configs()` and later `load_all_from_db()` after full registration.
- `providers/*.json` — Provider definitions for LLM engines (Anthropic, OpenAI, Gemini, etc.) that feed into the cortex engine registry.
- `config/synth_mcp.json` — Runtime MCP server registry consumed by the internal MCP bridge.
- `config/mcporter.json` — External MCP client configuration for development tooling.
- `core/model_config.json` — Simple model override file.

**Architecture and conventions**
- Every configuration variable is declared via `config_registry.get_var(key, default, label=..., description=..., value_type=..., group=..., component=..., advanced=..., sensitive=..., tags=..., constraints=..., getter=..., setter=..., needs_component_reload=..., readonly=..., hidden=..., allow_env_override=...)`. This metadata drives both the WebUI rendering and programmatic behavior.
- Bootstrap-configured variables (database connection strings, migration flags) are tagged with `bootstrap` and handled specially: they are loaded from environment first, persisted to DB once available, and skipped during the main `load_all_from_db()` pass.
- Per-scope cortex routing uses a dual-format storage: either a bare engine name string (legacy) or a JSON object `{"engine": "...", "model": "..."}` allowing per-scope model overrides. Resolution logic in `get_active_cortex_engine()` enforces a self-healing rule: if a configured engine is unavailable (unregistered, missing credentials, or probed as non-cortex-capable), it transparently degrades to the base cortex with logging and one-time LogChat notification.
- Per-path cortex overrides exist as an in-memory volatile map for live sessions, taking priority over scope-based routing.
- Vox TTS language overrides are stored as a JSON map keyed by normalized language codes, with async caching and TTL invalidation on writes.
- Component reload hooks are registered via `register_reload_handler(component_name, callback)` and triggered automatically when a variable marked `needs_component_reload=True` changes.
- Listener callbacks are invoked synchronously on every `set_value()` update, with error isolation so one failing listener doesn't block others.

**Conventions and constraints**
- Environment variables always take precedence over DB values; when an env var is present, the variable is marked `env_override=True`, becomes read-only in the UI, and its value is buffered for eventual DB persistence.
- Variables with `allow_env_override=False` ignore environment entirely and load only from DB/default.
- Sensitive variables (API keys, tokens) are flagged with `sensitive=True`; their values are redacted in logs and exports.
- Choice constraints are enforced at write time via `constraints={"choices": [...]}`, raising `ValueError` for invalid values.
- Default values are logged once per variable during first load; repeated use does not re-spam logs.
- Database persistence is best-effort: failures log warnings but never raise to callers, keeping the WebUI/API responsive even when the DB is temporarily unavailable.
- Persona-related config updates (`SYNTH_NAME`, `SYNTH_PROFILE`, `SYNTH_ALIASES`) are queued and retried by a background worker when DB writes fail during early startup.
- The registry deliberately skips DB loads during sync import phases when an event loop is running, deferring to `load_all_from_db()` called after full initialization to avoid deadlocks and ensure persona configs are properly reloaded.