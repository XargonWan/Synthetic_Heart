<div align="center">
      <img src="docs/res/synth_banner.png" alt="Synthetic Heart Logo" style="max-width: 700px; object-fit: contain;" />
</div>

![Docker Pulls](https://img.shields.io/docker/pulls/xargonwan/synthetic_heart)
| Branch    | Build Status                                                                                                                                         | Docs Status                                                                                                                                      |
|-----------|------------------------------------------------------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------|
| `main`    | [![CI Status](https://img.shields.io/github/actions/workflow/status/XargonWan/Synthetic_Heart/build-release.yml)](https://github.com/XargonWan/Synthetic_Heart/actions)      | [![Docs Status](https://readthedocs.org/projects/synthetic-heart/badge/?version=latest)](https://synthetic-heart.readthedocs.io/en/latest/?badge=latest) |
| `develop` | [![Develop CI Status](https://img.shields.io/github/actions/workflow/status/XargonWan/Synthetic_Heart/build-release.yml?branch=develop)](https://github.com/XargonWan/Synthetic_Heart/actions) | [![Docs Status](https://readthedocs.org/projects/synthetic-heart/badge/?version=latest)](https://synthetic-heart.readthedocs.io/en/latest/?badge=latest) |

> **Audio engines notice:** the default Vox engine is now backed by the
> real `kittentts` package (or the vendored stub).  The old `pyttsx3`
> / system‑voice implementation has been removed, so the container no longer
> needs `espeak-ng` or similar.  Install KittenTTS via ``uv add kittentts`` to
> enable neural voices.  Development‑only engines such as Chatterbox have been
> moved to ``plugins/_dev`` and are not loaded by default.
[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/xargon)

## Meet SyntH — your digital friend

Synthetic Heart (SyntH) is a FOSS application and framework that helps you create and meet a persistent AI persona — a "Synth" — that can follow you across platforms: Discord, Telegram, WebUI and more. Put simply: it's a digital friend that keeps its own memory, personality and state.

### The SyntH is alive — not just a chatbot
A SyntH isn't just a prompt-driven chatbot. Their identity, memories and personality live in the database instead of within a single LLM session. That means a Synth can think, reflect, and make choices even while you're not interacting with it. It preserves continuity and evolves over time; it can be right or wrong, and can develop opinions — much like a real social being.

### Completely free & swappable
Because SyntH decouples persona data from the underlying LLM, you can connect any Cortex engine you prefer (LLM providers like ChatGPT or Gemini, Selenium-based web engines, local models such as Ollama, or others). No expensive hardware required — use the engine you already have access to.

### Fully pluggable — build anything
- Dev friendly: craft new interfaces, plugins, or hook into games and apps.
- User friendly: meet your synth where you already chat (Discord, Telegram, etc.)

### Learns from you and its environment
SyntHs learn from their trainer (you) and from the social contexts they are placed in (Discord servers, Telegram chats, or the WebUI). They can expand knowledge about you and the world, given the strong reasoning that active LLMs provide.

### Wild future potential
Today SyntH has a WebUI and VRM avatars, but the possibilities are much broader: game companions, VR friends, or NPCs that react naturally to the player — all integrated thanks to the pluggable architecture.

### Status
Beta, but stable enough for daily use. Development branch gives access to the latest features.

---

<div align="center">
   <img src="docs/res/screenshots/home.png" alt="SyntH Home Screenshot" style="max-width: 700px; border-radius: 8px; margin: 16px 0;" />
</div>
<p align="center" style="font-size: 0.9em; color: #888;">
   <em>* Some default SyntH avatars are included, but users can provide their own VRM avatar file.</em>
</p>

### Features

- Switchable Cortex engines (API-driven Gemini, OpenAI, Claude, Grok, or local Ollama instances). Hot-swappable at runtime.
- Typed prompt pipeline with native renderers for OpenAI-compatible, Anthropic, Gemini, external-endpoint, and Live engine paths.
- Multiple chat interfaces including the builtin webui, Telegram, Discord and Matrix
- **VRM Avatar System**: 3D animated avatars with idle, talking, and thinking states.
- **SyntH Web UI**: A production-ready web interface featuring VRM avatar support and real-time animations.  
   The avatar's animations reflect the persona's global state—for example, if the character is replying on Telegram, connecting via the web UI will show the avatar busy typing on its smartphone. This ensures the visual representation always matches the character's current activity, regardless of the interface in use.
- Action plugins such as a persistent terminal and scheduled events
- G.R.I.L.L.O. ("grillo"): an autonomous internal "beat" system that periodically triggers reflective prompts (memory consolidation, tag elaboration, self-reflection, curiosity, relationship checks) and can create diary entries, schedule actions, or enqueue other tasks. G.R.I.L.L.O. stands for "Generator for Reflective Inner Loop & Logical Observation" — and the word "grillo" in Italian literally means 'cricket' (see the Pinocchio reference: "grillo parlante", the talking cricket). See `plugins/grillo_plugin.py` for details; it's configurable and may be enabled or disabled.
- Ollama-compatible HTTP bridge so existing Ollama clients can talk to Synthetic Heart
- Docker deployment with automatic database backups
- Mobile support

<div align="center">
   <img src="docs/res/screenshots/mobile_home.jpg" alt="SyntH Mobile Home Screenshot" style="max-width: 120px; border-radius: 8px; margin: 4px; display: inline-block;" />
   <img src="docs/res/screenshots/mobile_menu.jpg" alt="SyntH Mobile Menu Screenshot" style="max-width: 120px; border-radius: 8px; margin: 4px; display: inline-block;" />
   <img src="docs/res/screenshots/mobile_archive.jpg" alt="SyntH Mobile Chat Archive Screenshot" style="max-width: 120px; border-radius: 8px; margin: 4px; display: inline-block;" />
   <img src="docs/res/screenshots/mobile_config.jpg" alt="SyntH Mobile Config Screenshot" style="max-width: 120px; border-radius: 8px; margin: 4px; display: inline-block;" />
</div>
<p align="center" style="font-size: 0.9em; color: #888;">
   <em>* SyntH is fully usable on mobile devices via the WebUI.</em>
</p>

> [!NOTE]
> **G.R.I.L.L.O. System**: SyntH personas already maintain persistent awareness and memory. The G.R.I.L.L.O. system (Generator for Reflective Inner Loop & Logical Observation) enables them to autonomously think and initiate actions based on their interests and internal motivations—much like a real person deciding to act on their own. The name "grillo" nods to the Italian "grillo parlante" (the talking cricket) from Pinocchio — the companion conscience.
> This is already available and may be enabled or disabled depending on your security preferences.

<div align="center">
   <img src="docs/res/screenshots/components.png" alt="SyntH Home Screenshot" style="max-width: 700px; border-radius: 8px; margin: 16px 0;" />
</div>

For more information, see the [FAQ](https://synthetic-heart.readthedocs.io/en/latest/faq.html).

Join the community on Matrix: [#synthetic-heart:matrix.org](https://matrix.to/#/#synthetic-heart:matrix.org)

### Ollama Compatibility

The project ships with an **Ollama-compatible interface** (`interface/ollama_compat_server.py`). It mirrors the standard Ollama HTTP endpoints (`/api/generate`, `/api/chat`, `/api/tags`) so any client that normally talks to a local Ollama daemon can connect to Synthetic Heart instead. Point your tools at `http://<synth-host>:11434` (configurable via `OLLAMA_HOST` / `OLLAMA_PORT`) and they will stream responses generated by your active persona. Native Ollama engine support will arrive later, but the compatibility layer lets you reuse the existing ecosystem today.

## Quickstart

<div align="center">
   <img src="docs/res/quickstart.png" alt="SyntH Home Screenshot" style="max-width: 700px; border-radius: 8px; margin: 16px 0;" />
</div>

### Option A: Docker (Recommended)

1.  Clone this repository or simply download the `docker-compose.yml` and the `skins` folder (see the note below).
2.  **[OPTIONAL]** Copy `.env.example` to `.env` to customize the deployment. The example file is trimmed to common deployment overrides; use `docs/compose_env_vars.rst` if you need the full advanced env reference.
3.  Start the stack:
    ```bash
    docker compose up -d --build
    ```

    > **Note about logs:** The default configuration uses a Docker-managed volume for application logs (`synth_logs` -> `/app/logs`). This avoids host-permission issues.
    >
    > **For Developers:** If you want to view logs directly in your project folder, uncomment the bind-mount line in `docker-compose.yml` (`./logs:/app/logs`).

4.  Connect to the WebUI via HTTPS (default port is **8000**): `https://localhost:8000`.

#### Optional: Enable SOUL PostgreSQL + pgvector backend

By default, SOUL uses in-memory persistence. To enable persistent SOUL storage:

1. Set these environment variables in your `.env`:
   - `SOUL_REPOSITORY_BACKEND=postgres`
   - `SOUL_POSTGRES_DSN=postgresql://soul:soul@synth-soul-db:5432/soul_memory`
   - Optional overrides: `SOUL_PG_DB`, `SOUL_PG_USER`, `SOUL_PG_PASSWORD`, `EXT_SOUL_DB_PORT`
2. Start the SOUL DB service and apply schema:
   - Linux/macOS: `bash scripts/bootstrap_soul_postgres.sh`
   - Windows PowerShell: `./scripts/bootstrap_soul_postgres.ps1`
3. Restart the stack:
   - `docker compose up -d --build`

#### Optional: Migrate Existing MariaDB memories into SOUL

If your Synth already has months of history, you can import legacy data into SOUL `mem_cells`.

1. Ensure SOUL Postgres is running and schema is applied:
   - Linux/macOS: `bash scripts/bootstrap_soul_postgres.sh`
   - Windows PowerShell: `./scripts/bootstrap_soul_postgres.ps1`
2. Run a dry-run first:
   - `uv run python scripts/migrate_legacy_to_soul.py --dry-run --days 180`
3. Run the real migration:
   - `uv run python scripts/migrate_legacy_to_soul.py --days 180`
4. Verify results in Postgres:
   - `SELECT COUNT(*) FROM mem_cells;`

Migration notes:
- Sources migrated: `chat_history_cache`, `memories`, `ai_diary`
- IDs are deterministic (`legacy:<table>:<id>`), so reruns are safe (upsert behavior)
- The script uses `SOUL_POSTGRES_DSN` for the destination and `DB_*` values for legacy MariaDB source

### Option B: Windows Native (with `uv`)

> [!WARNING]
> **DATABASE SETUP REQUIRED**
> Database setup is **not automated** on Windows native environments. You must install MariaDB/MySQL separately, configure your local database (using the schema found in `init-db.sql`), and manually set the connection parameters in your `.env` file before running the application!

For the fastest development experience on Windows, we recommend using **uv**. It handles Python installation, virtual environments, and dependencies automatically.

1.  **Install uv** (if not installed):
    ```powershell
    pip install uv
    ```
2.  **Clone the repository** and enter the folder:
    ```powershell
    git clone https://github.com/XargonWan/Synthetic_Heart.git
    cd Synthetic_Heart
    ```
3.  **Configure `.env` and Database:**
    - Install MariaDB or MySQL.
    - Create a database and run the `init-db.sql` script to set up the necessary tables.
    - Copy `.env.example` to `.env` and update the `DB_*` connection strings to match your local setup.
4.  **Sync Dependencies:**
    ```powershell
    # This creates the environment and installs all packages instantly
    uv sync
    ```
5.  **Run the App:**
    ```powershell
    uv run main.py
    ```

---

### First Run Setup
1.  **Access the WebUI:** Navigate to `https://localhost:8000` (Accept the self-signed certificate warning if prompted).
2.  **Select Engine:** Go to **Components** and select your desired Cortex kind + engine.

> **Note on Skins:** The `skins` folder is optional if you do not intend to edit them. If you skip downloading it, ensure the volume mapping for `./skins` is commented out in your compose file, otherwise, an empty folder will override the built-in skins.

### Customize your Synth

Then you might want to edit the following settings on the WebUI -> Settings:
- Default Location: your location, so the synth knows where they are, useful for the weather for example
- Timezone: (if you didn´t do via compose) with your timezone, useful to make the synth aware of what time is actually in your place
- Trainer Name: your name, else the synth don't know who you are
- Synth Name: The name of the Synth. To not be mistaken with the name of the skin, that is just a name given to the skin but itś not set as the synth name. A Symnth can be called Kotone and have the skin of Rei for example.
- Synth Profile: A description of how your synth is, written in second person, check the default one.

Moreover you can add more skins or just upload your vrm model.  Uploaded VRMs replace any previous upload and automatically become the active avatar; only one user file is kept in cache at a time.

See the [documentation](https://synthetic-heart.readthedocs.io) for installation details, advanced features and contribution guidelines.

## Docker image repository
You can browse and manage Docker images for this project on [Docker Hub](https://hub.docker.com/repository/docker/xargonwan/synthetic_heart).

## Contributing

Pull requests are welcome! Everyone is encouraged to submit contributions—especially new components, plugins, and Cortex engines—to expand SyntH's capabilities. Please read the guidelines in the documentation before submitting.

### AI-assisted development

The repo ships with a full AI agent setup out of the box. If you use Claude Code, Cursor, Copilot, or similar tools, these are already wired up for you:

**One-time setup after cloning:**
```bash
uv sync                   # installs all deps including the MCP server
npx gitnexus analyze      # builds the code intelligence index (~1-2 min)
```

**What you get automatically:**

- **`synth-logs` MCP server** (`mcp_servers/synth_logs.py`) — gives AI agents structured access to all log files across rotations. Instead of reading raw log files, agents can call `get_recent_errors()`, `search_logs()`, and `tail_log()` directly. Logs rotate fast in DEBUG mode (2000 lines), so this saves a lot of manual hunting.

- **GitNexus code intelligence** — pre-configured in `.mcp.json` and `.vscode/mcp.json`. Gives agents a queryable map of the codebase: callers, callees, execution flows, and safe rename/refactor operations. Run `npx gitnexus analyze` to build or refresh the index after large changes.

Both servers are pre-configured for **Claude Code** (`.mcp.json`) and **VS Code Copilot** (`.vscode/mcp.json`). No manual setup beyond the two commands above.

**`AGENTS.md`** is the canonical reference for any AI agent working on this codebase — architecture overview, plugin contracts, DB schema, config keys, known issues, and debugging SOP. Read it before starting a non-trivial task.

---

## Performance Test Results

### Stress Test Configuration

The following tests evaluate the system's stability under load with long prompts and increasing concurrency.

- **Test Date**: 2026-04-26
- **Target Engines**: selenium-llm-engine (Gemini Web), OpenRouter (ChatGPT), Anthropic, Gemini API
- **Prompt Type**: Long prompts (~1200+ chars, requiring 3-4 chunks)
- **Concurrency**: Ramp-up from 30s delay down to simultaneous (last 3 prompts sent at the same time)

### Test Results Summary

| Engine | Total Prompts | Success | Failed | Success Rate | Avg Response Time |
|--------|---------------|---------|--------|--------------|-------------------|
| selenium-llm-engine | 10 | 2 | 8 (429 Rate Limit) | 20% | ~117s |
| openrouter | 10 | 2 | 8 (429 Rate Limit) | 20% | ~110s |
| anthropic | 5 | 0 | 5 (500 Error) | 0% | N/A |
| gemini_api | 5 | - | - | Not tested | - |

### Per-Request Timing (selenium-llm-engine)

| Phase | Avg Duration |
|-------|--------------|
| page_ready | 0.11s |
| find_element | 0.14s |
| fill_input | 3.5s |
| click_send | 3.3s |
| post_send_check | 2.9s |
| wait_for_response | 8.5s |
| **TOTAL generate** | ~18-20s |

### Stability Assessment

| Scenario | Status | Notes |
|----------|--------|-------|
| Single prompt | ✅ Stable | ~18-20s response time |
| Sequential (delay >60s) | ✅ Stable | Works with sufficient delay |
| Concurrent (3+ simultaneous) | ⚠️ Degraded | Rate limit kicks in immediately |
| Large prompt (chunking) | ✅ Stable | Works correctly after fixes |

### Key Findings

1. **Rate Limiting**: The primary bottleneck is the upstream LLM provider rate limits (~20 requests/minute for Gemini). Requests exceeding this threshold receive HTTP 429 responses.

2. **Chunking**: For prompts requiring chunking (3-4 parts), the system correctly fills and sends each chunk sequentially, waits for generation to complete before proceeding.

3. **System Overhead**: The total response time includes Synth's transport layer, action parsing, and bridge overhead in addition to the actual LLM generation time.

### Running the Stress Test

To reproduce these results, run the manual stress test:

```bash
# From the project root
python tests/stress_test_engines.py
```

The test script:
- Sends 10 long prompts to selenium-llm-engine with decreasing delays (30s → 0s)
- Sends 10 long prompts to OpenRouter with the same delay pattern
- Sends 5 short prompts to other engines (anthropic, gemini_api, openapi)
- Reports timing statistics and success/failure rates

> **Note**: This test is not included in the CI/CD workflow. It requires the Ollama compat server running (`localhost:11435`) and is meant for manual performance evaluation only.

### Recommendations

1. **Implement request throttling** at the orchestrator level to prevent 429 errors
2. **Add exponential backoff** retry logic for rate-limited requests
3. **Use minimum 60s delay** between prompts to Gemini Web to avoid rate limits
4. **Consider alternative engines** with higher rate limits for high-throughput scenarios

---

## What's next (Planned features & fixes)
Here are the main improvements and integrations we plan to work on — contributions are welcome:

- [ ] Event system fixes
- [ ] Global animation engine fixes — make animations always reflect the actual state of the SyntH and their current actions
- [ ] Deepseek Cortex engine support
- [ ] StepFun Cortex engine support
- [ ] Desktop presence — allow SyntH to show up on a desktop environment (outside web interfaces)
- [ ] First gaming plugin: Minecraft integration
- [ ] Matrix interface

If you're interested in helping implement these features or testing them, open an issue or a PR and tag it with the relevant area (e.g. `interface`, `cortex`, `plugin`, etc.).
