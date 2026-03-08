<div align="center">
      <img src="docs/res/synth_banner.png" alt="Synthetic Heart Logo" style="max-width: 700px; object-fit: contain;" />
</div>

![Docker Pulls](https://img.shields.io/docker/pulls/xargonwan/synthetic_heart)
| Branch    | Build Status                                                                                                                                         | Docs Status                                                                                                                                      |
|-----------|------------------------------------------------------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------|
| `main`    | [![CI Status](https://img.shields.io/github/actions/workflow/status/XargonWan/Synthetic_Heart/build-release.yml)](https://github.com/XargonWan/Synthetic_Heart/actions)      | [![Docs Status](https://readthedocs.org/projects/synthetic-heart/badge/?version=latest)](https://synthetic-heart.readthedocs.io/en/latest/?badge=latest) |
| `develop` | [![Develop CI Status](https://img.shields.io/github/actions/workflow/status/XargonWan/Synthetic_Heart/build-release.yml?branch=develop)](https://github.com/XargonWan/Synthetic_Heart/actions) | [![Docs Status](https://readthedocs.org/projects/synthetic-heart/badge/?version=latest)](https://synthetic-heart.readthedocs.io/en/latest/?badge=latest) |

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

- Switchable Cortex engines (Selenium-driven ChatGPT, Gemini or Grok sessions). **Note: Currently, only Selenium ChatGPT (Legacy) is fully functional. Other engines are experimental and may not work reliably.**
- Multiple chat interfaces including the builtin webui, Telegram, Discord and Matrix
- **VRM Avatar System**: 3D animated avatars with idle, talking, and thinking states.
- **SyntH Web UI**: A production-ready web interface featuring VRM avatar support and real-time animations.  
   The avatar's animations reflect the persona's global state—for example, if the character is replying on Telegram, connecting via the web UI will show the avatar busy typing on its smartphone. This ensures the visual representation always matches the character's current activity, regardless of the interface in use.
- Action plugins such as a persistent terminal and scheduled events
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
2.  **[OPTIONAL]** Copy `.env.example` to `.env` to customize the deployment.
3.  Start the stack:
    ```bash
    docker compose up -d --build
    ```

    > **Note about logs:** The default configuration uses a Docker-managed volume for application logs (`synth_logs` -> `/app/logs`). This avoids host-permission issues.
    >
    > **For Developers:** If you want to view logs directly in your project folder, uncomment the bind-mount line in `docker-compose.yml` (`./logs:/app/logs`).

4.  Connect to the WebUI via HTTPS (default port is **8000**): `https://localhost:8000`.

### Option B: Windows Native (with `uv`)

For the fastest development experience on Windows, we recommend using **uv**. It handles Python installation, virtual environments, and dependencies automatically.

1.  **Install uv** (if not installed):
    ```powershell
    pip install uv
    ```
2.  **Clone the repository** and enter the folder:
    ```powershell
    git clone [https://github.com/Scarlet-Raine/Synthetic_Heart.git](https://github.com/Scarlet-Raine/Synthetic_Heart.git)
    cd Synthetic_Heart
    ```
3.  **Sync Dependencies:**
    ```powershell
    # This creates the environment and installs all packages instantly
    uv sync
    ```
4.  **Run the App:**
    ```powershell
    uv run main.py
    ```

---

### First Run Setup
1.  **Access the WebUI:** Navigate to `https://localhost:8000` (Accept the self-signed certificate warning if prompted).
2.  **Select Engine:** Go to **Components** and select your desired Cortex kind + engine.
3.  **Login (Selenium Engines):** If using a Selenium engine (like ChatGPT or Gemini), click the **Login** button to authenticate via the virtual browser.

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

## What's next (Planned features & fixes)
Here are the main improvements and integrations we plan to work on — contributions are welcome:

- [ ] Event system fixes
- [ ] Global animation engine fixes — make animations always reflect the actual state of the SyntH and their current actions
- [ ] Deepseek web Cortex engine support
- [ ] SetpFun web Cortex engine support
- [ ] Desktop presence — allow SyntH to show up on a desktop environment (outside web interfaces)
- [ ] First gaming plugin: Minecraft integration
- [ ] Matrix interface

If you're interested in helping implement these features or testing them, open an issue or a PR and tag it with the relevant area (e.g. `interface`, `cortex`, `plugin`, etc.).
