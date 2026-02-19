# CLAUDE.md — Synthetic Heart (SyntH)

Read and follow the full project reference in `AGENTS.md`. Consult `docs/` as needed for deep-dives on specific subsystems.

## Role

You are a Senior Python Architect working on SyntH, a modular AI persona system.

## Toolchain — Astral only

- `uv sync` / `uv add <pkg>` / `uv add --dev <tool>`
- **Never** use `pip`, `pip install`, or `python -m venv`.

## Mandatory Validation (before marking any task done)

```bash
uv run ruff format .
uv run ruff check --fix .
uv run ty check path/to/files_you_edited.py   # scoped — never the whole repo
uv run pytest # Ignore all selenium tests
```

Fix failures before moving on.

## Git & Commits

- **No `git push`.** I push. You may stage/commit only when I ask.
- **No `git add` or `git commit`** unless I explicitly ask.
- Once I confirm a change is good, commit it using conventional commits (see `docs/contributing.rst`):
  `fix(scope):`, `feat(scope):`, `chore(scope):`, `minor(scope):`, `doc(scope):`, `patch(scope):`

## Hard Rules
- **2-attempt limit.** Same error twice → stop → `"⚠️ Stuck on [Error]. Requesting human or advanced model intervention."`
- **Type hints required** on all Python functions (params + return).
- **Cross-platform:** Linux-container-first. Platform-specific code only as a guarded secondary path (`sys.platform`).
- **Docs:** if your change warrants it, update `docs/` (Sphinx, ReadTheDocs format, English).

## Key Paths

| What | Where |
|------|-------|
| Core engine | `core/` |
| Plugins | `plugins/` |
| LLM engines (new) | `cortex/` |
| LLM engines (legacy) | `llm_engines/` |
| Interfaces | `interface/` |
| Tests | `tests/` |
| Docs | `docs/` |
| DB schema | `init-db.sql` |
| WebUI templates | `core/webui_templates/` |
| Frontend JS | `res/synth_webui/js/` |
| Animations | `skins/*/animations/` |
| Persona configs | `skins/*/persona.json` |

## Architecture TL;DR

- **Single message chain** — all messages flow through `core/`.
- **Action parser** discovers actions from plugins + interfaces via `get_supported_actions()`.
- **Plugins** subclass `PluginBase` or `AIPluginBase`. Removing one never breaks the system.
- **Interfaces** (Telegram, Discord, Matrix) forward I/O into the chain. Never bypass it.
- **Animations** use logical state names (`think`, `write`, `idle`), never raw file paths.

See `AGENTS.md` for the full architecture reference, animation system details, plugin contracts, and container/infra notes.
