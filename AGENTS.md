# AGENTS.md — Synthetic Heart (SyntH)

> Repository-wide operating rules for coding agents.
> Detailed architecture and subsystem documentation lives in `docs/wiki/` and `docs/`.
> Claude Code may also load `CLAUDE.md`; repository rules in this file still apply.

---

## 1. Project and Primary Invariant

**Synthetic Heart** (**SyntH**) is a modular AI persona system. “Synth” is the digital person implemented by this repository.

The architecture is intentionally detachable:

- `core/` owns the message chain, validation, dispatch, persistence, routing, and shared services.
- `plugins/` add actions and optional behavior.
- `engines/` provide interchangeable AI/media backends.
- `interface/` connects external systems to the core chain.

**Primary invariant:** removing an optional plugin, engine, connector, or interface must not break the remaining system.

---

## 2. Sources and Reading Strategy

Before making non-trivial changes, read only the material relevant to the task in this order:

1. `AGENTS.md`
2. `AGENT_WORK.md`, when present
3. Relevant pages under `docs/wiki/` and maintained documentation under `docs/`
4. Source code and tests
5. `CHANGELOG.md` and established known-issue records when debugging regressions

The Qoder wiki export has two complementary trees:

- `docs/wiki/en/content/` contains reader-facing architecture, development, API, and operations pages. Start here for orientation.
- `docs/wiki/knowledge/en/_index.yaml` maps source paths to generated subsystem modules under `docs/wiki/knowledge/en/`. Use those modules for focused implementation detail.
- `docs/` contains the maintained Sphinx documentation and remains authoritative for published user/developer guidance.

Search by subsystem or source path and read the smallest useful set of pages. Do not recursively ingest the export. Treat generated wiki content as a navigation aid: it may lag the implementation or contain exporter-specific links and structure.

Documentation is a map, not proof. When documentation and implementation disagree:

1. inspect the current code and tests;
2. establish intended behavior from evidence;
3. report the discrepancy;
4. update stale documentation as part of the change when appropriate.

Never preserve an incorrect implementation solely because an old document describes it.

---

## 3. Non-Negotiable Architecture Rules

### One message chain

- All incoming messages enter the core-managed message chain.
- Actions attach to the existing chain; do not create parallel message flows.
- Interfaces must not bypass core validation, dispatch, history, or safety.
- Shared behavior belongs in the core only when it is broadly applicable.
- World-, engine-, plugin-, and interface-specific behavior stays in its adapter.

### Dynamic optional components

- Actions are discovered through `get_supported_actions()`.
- Validation derives from each action schema.
- Missing or disabled optional components must fail closed and degrade gracefully.
- Avoid eager imports that make optional components mandatory.
- Guard optional integrations so import, startup, and shutdown remain safe.

### No keyword-driven product logic

Do not implement routing, intent detection, salience, autonomy, or feature activation primarily through words, phrases, regex triggers, or language-specific keyword lists.

Prefer structural signals such as:

- action schemas;
- typed metadata;
- interface and session state;
- registry membership;
- enums and capability declarations;
- numeric telemetry;
- explicit configuration;
- model reasoning where semantic interpretation is required.

Small syntax parsers and user-declared commands are exceptions only when the feature is explicitly command-oriented.

### Cross-platform baseline

Linux containers are the primary runtime.

Platform-specific behavior must be:

- secondary rather than the main path;
- isolated;
- guarded with capability or platform checks;
- covered by a safe fallback.

---

## 4. Component Contracts

### Plugins

Plugins subclass `PluginBase` or `AIPluginBase` and expose actions through:

```python
def get_supported_actions(self) -> dict:
    """Return supported actions and their prompt/validation schema."""
```

Rules:

- Multi-file plugins live in `plugins/<name>/`.
- Keep implementation, `guide.md`, and `icon.<ext>` together.
- `guide.md` is the documentation source of truth for that component.
- Preserve historical import paths with the repository’s established package shim when moving a flat module into a package.
- Critical message-chain plugins must declare that runtime disabling is not allowed.
- Third-party logos require explicit permission and attribution in `LICENSE_EXTERNAL.md`; otherwise use an original glyph or the SyntH fallback.

Use `plugins/radio_host/` and the relevant wiki pages as reference implementations.

### Interfaces

Interfaces are duck-typed and register at import time.

Rules:

- Every inbound message enters the core chain.
- Shared avatar/audio state is driven through the Karada state server, never by iterating individual WebUI clients.
- Interface-native delivery and shared avatar state are separate concerns.
- Multi-file interfaces use `interface/<module>/<module>.py`, `__init__.py`, `guide.md`, and optional `icon.<ext>`.
- Preserve existing import paths with the established module-rebinding shim.
- Outbound local files must use the shared sandbox path checks in `core/outbound_file_utils.py`.

### Engines and media subsystems

- Text reasoning engines subclass `AIPluginBase`.
- Media engines follow their registry/base-class contracts.
- Current named subsystems are Cortex, Vox, Auris, and Iris.
- Do not revive obsolete paths such as `cortex/` or `llm_engines/`.
- Keep endpoint-specific workarounds on the SyntH side unless the task explicitly authorizes changes to the external engine.

### Agentic Runtime and MCP

Synth runtime MCP and developer MCP are separate systems:

- Synth runtime: `config/synth_mcp.json` and `core/mcp_bridge/`
- Developer tooling: `.mcp.json`, `.vscode/mcp.json`, and `mcp_servers/`

Never merge their configuration or lifecycle.

Registered actions share the tool/action abstraction and must pass through the same safety gate. External effects determine Agent-Lane routing. Drones are single-level sub-agents and must never spawn other Drones.

### Rift Vessel

The Rift Vessel has strict boundaries:

1. Vessel actions do not create Agent-Lane tasks or Drones.
2. A session writes one autobiographical diary entry at session end, not continuously.
3. Vessel activity has its own history voice and persistence.
4. Core Vessel verbs are world-agnostic; game-specific verbs belong in that world connector.
5. Autonomous goals are free-text and personality-driven, never a fixed quest catalogue.
6. Fast motor and survival reflexes use structural world state, not free-text goal parsing or keywords.
7. Player conversation and autonomous perceptions remain separate context buffers.

Before changing Vessel routing, autonomy, motorics, goals, session lifecycle, or message compaction, read the generated `Rift Vessel Embodiment Core` module under `docs/wiki/knowledge/en/` and any relevant source-facing documentation.

### Karada avatar state

`KaradaStateServer` is the source of truth for animation, expressions, face state, and shared speaking/audio state.

- Drive the server, not individual clients.
- Use logical animation states, never hard-coded animation paths.
- New clients implement and register a `KaradaTransport`.
- Interface-specific audio delivery must not duplicate shared avatar broadcasts.

---

## 5. Security and Data Rules

- Never commit credentials, API keys, session cookies, access tokens, private certificates, or passwords.
- Repository documentation may describe where credentials belong, but must use placeholders.
- User secrets belong in environment variables or user-owned config files outside the repository.
- Never print secrets from environment files, databases, logs, or connector configuration.
- Do not weaken action safety or sandbox path checks to make a test pass.
- Shell execution on a bare host remains disabled unless the user explicitly enables the existing guarded override.
- Do not access paths outside declared sandbox roots.
- Treat logs, model prompts, chat history, diary content, and uploaded files as potentially sensitive.

### Database naming

Never use SQL reserved words as bare column names.

In particular, do not create a column named `timestamp`. Use names such as:

- `created_at`
- `updated_at`
- `event_timestamp`
- `started_at`
- `ended_at`

Public API keys may still be named `"timestamp"` when required for compatibility; the restriction applies to SQL identifiers.

---

## 6. Development Workflow

### Initial workspace setup

```bash
uv sync
GITNEXUS_HOME=.gitnexus-home npx gitnexus analyze --skip-agents-md
```

PowerShell equivalent:

```powershell
$env:GITNEXUS_HOME = ".gitnexus-home"
npx gitnexus analyze --skip-agents-md
```

Use the repository’s configured MCP servers only after their dependencies and credentials are available.

Never use `pip install` or create an ad-hoc virtual environment. Dependency changes go through `uv` so the lockfile remains authoritative.

### Before editing

1. Read the relevant wiki/docs and nearby tests.
2. Inspect repository status and existing uncommitted work.
3. Trace the current execution path.
4. Use GitNexus upstream impact analysis for symbols you plan to modify materially.
5. Identify the smallest safe change and its validation plan.

Do not overwrite unrelated work or “clean up” files outside the task.

### While editing

- Keep changes scoped.
- Follow existing naming, architecture, and error-handling patterns.
- Add complete Python parameter and return annotations.
- Prefer explicit failure over ambiguous partial success.
- Add or update focused tests with the behavior change.
- Avoid broad rewrites unless the task requires one.
- Do not silently alter public schemas, action names, config keys, database layouts, or import paths.

### Two-attempt escalation rule

After two materially different attempts at the same failing fix, stop repeating speculative edits.

Report:

```text
⚠️ Stuck on <error or unresolved condition>.
Evidence collected:
- ...
Attempts made:
- ...
Likely next investigation:
- ...
```

This rule does not prohibit deeper investigation; it prevents looping on the same unsupported fix.

### Git rules

- Do not push.
- Do not stage or commit unless the user explicitly asks.
- Never discard, reset, rewrite, or amend the user’s work without explicit authorization.
- Before a requested commit, inspect the diff and run the required validation.
- Use GitNexus change detection after non-trivial code changes and before committing.

---

## 7. Investigation and Debugging

When asked to fix a runtime problem:

1. Reproduce or locate the concrete failure.
2. Read relevant logs before proposing a cause.
3. Trace the execution path from the observed symptom.
4. Compare current behavior with focused tests and documentation.
5. Form a falsifiable hypothesis.
6. Make the smallest change that addresses the evidenced cause.
7. Re-run the reproduction and regression tests.

Do not answer a bug-fix request with guesses when logs or runtime evidence are available.

Useful commands:

```bash
docker exec synth-dev tail -f /app/logs/synth.log
docker exec synth-dev tail -f /app/logs/synth.log | grep -E "run_action|execute_action"
docker exec synth-dev tail -f /app/logs/synth.log | grep -E "\[grillo\]|grillo"
```

Record genuinely recurring, non-obvious defects in the repository’s established issue/changelog location. Do not duplicate resolved issue narratives inside `AGENTS.md`.

---

## 8. GitNexus Rules

Use GitNexus as code-intelligence support, not as a substitute for reading code.

### Required before changing a symbol

Run upstream impact analysis for each materially modified function, class, or method.

- Review direct callers and affected execution flows.
- Warn the user before proceeding when the result is HIGH or CRITICAL risk.
- Update all direct dependents required by the change.

### Required for refactors

- Query context before extracting or moving code.
- Use GitNexus rename tooling for symbol renames; do not rely on global text replacement.
- Run change detection after the refactor.

### Required after non-trivial code changes

Run change detection and confirm the affected symbols and flows match the intended scope. Repeat it before a requested commit if the working tree changed after the previous run.

If the index is stale:

```bash
GITNEXUS_HOME=.gitnexus-home npx gitnexus analyze --skip-agents-md
```

Preserve embeddings when the existing index uses them:

```bash
GITNEXUS_HOME=.gitnexus-home npx gitnexus analyze --skip-agents-md --embeddings
```

On PowerShell, set `$env:GITNEXUS_HOME = ".gitnexus-home"` first. Consult `.gitnexus/meta.json` before choosing whether to preserve embeddings. Keep the registry workspace-local and retain `--skip-agents-md` so analysis does not rewrite this file.

---

## 9. Validation

Run the narrowest useful checks during development, then complete the applicable final sequence.

### Python changes

```bash
uv run ruff format <edited paths>
uv run ruff check --fix <edited paths>
uv run ty check <edited Python files>
uv run pytest <focused tests>
```

Before marking a broad code task complete, run the wider relevant suite:

```bash
uv run pytest
```

Do not run whole-repository type checking unless requested; use scoped `ty` checks because the repository may contain unrelated legacy findings.

### Validation expectations

- A passing formatter is not a test.
- A passing unit test is not proof that container startup works.
- Changes involving startup, imports, plugins, interfaces, migrations, or Docker require an appropriate smoke test.
- Changes involving schemas or migrations require validation against the supported database paths.
- Changes involving an external service must test failure and unavailable-service behavior.
- Report any checks you could not run and why.

### OpenAI-compatible API smoke test

When appropriate:

```bash
curl -X POST http://localhost:11435/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "default",
    "messages": [
      {"role": "system", "content": "Respond with ONLY valid JSON: {\"actions\": []}"},
      {"role": "user", "content": "Your test message"}
    ],
    "stream": false
  }'
```

---

## 10. Documentation

Update documentation when a change affects:

- public behavior;
- installation or deployment;
- configuration;
- action schemas;
- plugin/interface layout;
- architecture or extension points;
- troubleshooting;
- persistent data;
- security boundaries.

Documentation locations:

- `docs/wiki/en/content/` — exported reader-facing wiki
- `docs/wiki/knowledge/en/` — exported generated subsystem knowledge
- `docs/` — maintained user/developer documentation
- component `guide.md` — source of truth for plugin/interface guides
- root `README.md` — project entry point
- `CHANGELOG.md` — user-visible or recurring change history

Do not turn `AGENTS.md` back into an encyclopedic architecture dump. Keep durable operating constraints here and put subsystem detail in the wiki or maintained docs.

When code changes invalidate a wiki page, update that page in the same task or clearly report the stale page.

---

## 11. Infrastructure Notes

Container rebuild:

```bash
docker compose up -d --build
```

Development deployment, when that file/environment exists:

```bash
docker compose -f docker-compose-dev.yml --env-file .env-dev up -d --build
```

Do not delete logs before collecting evidence for a bug. Clear generated logs only when explicitly required and after preserving relevant diagnostics.

Selkies convention:

- HTTP: container port `3000`
- HTTPS: container port `3001`
- certificates: `/config/ssl/`

---

## 12. External Planning Systems

The AFFiNE board is a shared planning system.

- Read it when the task depends on roadmap, status, or recorded design decisions.
- Do not write, edit, or reorganize board content unless the user explicitly asks.
- Keep AFFiNE credentials outside the repository.
- Documentation must show placeholders, never real passwords.

Example user-owned config:

```text
AFFINE_BASE_URL=<board URL>
AFFINE_EMAIL=<agent account>
AFFINE_PASSWORD=<secret stored outside the repository>
```

---

## 13. Completion Report

Before finishing a code task, verify:

- [ ] Relevant repository guidance and wiki pages were read.
- [ ] Existing user work was preserved.
- [ ] Impact analysis was run for modified symbols.
- [ ] The implementation follows the single-chain and optional-component rules.
- [ ] No keyword-based semantic behavior was introduced.
- [ ] No credential or sensitive data was added.
- [ ] Focused tests cover the change.
- [ ] Formatting, linting, and scoped type checks pass.
- [ ] Wider tests or smoke checks were run where appropriate.
- [ ] For non-trivial code changes, GitNexus change detection matches the intended scope.
- [ ] Documentation was updated or explicitly identified as unchanged.
- [ ] No staging, commit, push, reset, or destructive action occurred without authorization.

Final responses should state:

1. what changed;
2. why;
3. files affected;
4. validation performed;
5. remaining risks, limitations, or unrun checks.
