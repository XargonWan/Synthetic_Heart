<div align="center">
      <img src="docs/res/synth_banner.png" alt="Synthetic Heart Logo" style="max-width: 700px; object-fit: contain;" />
</div>

![Docker Pulls](https://img.shields.io/docker/pulls/xargonwan/synthetic_heart)
| Branch    | Build Status                                                                                                                                         | Docs Status                                                                                                                                      |
|-----------|------------------------------------------------------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------|
| `main`    | [![CI Status](https://img.shields.io/github/actions/workflow/status/XargonWan/Synthetic_Heart/build-release.yml)](https://github.com/XargonWan/Synthetic_Heart/actions)      | [![Docs Status](https://readthedocs.org/projects/synthetic-heart/badge/?version=latest)](https://synthetic-heart.readthedocs.io/en/latest/?badge=latest) |
| `develop` | [![Develop CI Status](https://img.shields.io/github/actions/workflow/status/XargonWan/Synthetic_Heart/build-release.yml?branch=develop)](https://github.com/XargonWan/Synthetic_Heart/actions) | [![Docs Status](https://readthedocs.org/projects/synthetic-heart/badge/?version=latest)](https://synthetic-heart.readthedocs.io/en/latest/?badge=latest) |


## Meet SyntH — your digital friend

Synthetic Heart (SyntH) is a FOSS application and framework that helps you create and meet a persistent AI persona — a "Synth" — that can follow you across platforms: Discord, Telegram, WebUI and more. Put simply: it's a digital friend that keeps its own memory, personality and state.

### The SyntH is alive — not just a chatbot
A SyntH isn't just a prompt-driven chatbot. Their identity, memories and personality live in the database instead of within a single LLM session. That means a Synth can think, reflect, and make choices even while you're not interacting with it. It preserves continuity and evolves over time; it can be right or wrong, and can develop opinions — much like a real social being.

### Completely free & swappable
Because SyntH decouples persona data from the underlying LLM, you can connect any LLM engine you prefer (ChatGPT, Gemini, local models such as Ollama, or others). No expensive hardware required — use the LLM engine you already have access to.

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

- Switchable LLM engines (Selenium-driven ChatGPT, Gemini or Grok sessions). **Note: Currently, only Selenium ChatGPT (Legacy) is fully functional. Other engines are experimental and may not work reliably.**
- Multiple chat interfaces including the builtin webui, Telegram, Discord and Matrix
- **VRM Avatar System**: 3D animated avatars with idle, talking, and thinking states.
- **SyntH Web UI**: A production-ready web interface featuring VRM avatar support and real-time animations.  
   The avatar's animations reflect the persona's global state—for example, if the character is replying on Telegram, connecting via the web UI will show the avatar busy typing on its smartphone. This ensures the visual representation always matches the character's current activity, regardless of the interface in use.
- Action plugins such as a persistent terminal and scheduled events
- Action plugins such as a persistent terminal and scheduled events
- G.R.I.L.L.O. ("grillo"): an autonomous internal "beat" system that periodically triggers reflective prompts (memory consolidation, tag elaboration, self-reflection, curiosity, relationship checks) and can create diary entries, schedule actions, or enqueue other tasks. G.R.I.L.L.O. stands for "Generator for Reflective Inner Loop & Logical Observation" — and the word "grillo" in Italian literally means 'cricket' (see the Pinocchio reference: "grillo parlante", the talking cricket). See `plugins/grillo_plugin.py` for details; it's configurable and may be enabled or disabled.
- Context memory injection with `/context`
- Ollama-compatible HTTP bridge so existing Ollama clients can talk to Synthetic Heart
- Docker deployment with automatic database backups

> [!NOTE]
> **G.R.I.L.L.O. System**: SyntH personas already maintain persistent awareness and memory. The G.R.I.L.L.O. system (Generator for Reflective Inner Loop & Logical Observation) enables them to autonomously think and initiate actions based on their interests and internal motivations—much like a real person deciding to act on their own. The name "grillo" nods to the Italian "grillo parlante" (the talking cricket) from Pinocchio — the companion conscience.
> This is already available and may be enabled or disabled depending on your security preferences.

<div align="center">
   <img src="docs/res/screenshots/components.png" alt="SyntH Home Screenshot" style="max-width: 700px; border-radius: 8px; margin: 16px 0;" />
</div>

For more information, see the [FAQ](https://synthetic-heart.readthedocs.io/en/latest/faq.html).

### Ollama Compatibility

The project ships with an **Ollama-compatible interface** (`interface/ollama_compat_server.py`). It mirrors the standard Ollama HTTP endpoints (`/api/generate`, `/api/chat`, `/api/tags`) so any client that normally talks to a local Ollama daemon can connect to Synthetic Heart instead. Point your tools at `http://<synth-host>:11434` (configurable via `OLLAMA_HOST` / `OLLAMA_PORT`) and they will stream responses generated by your active persona. Native Ollama engine support will arrive later, but the compatibility layer lets you reuse the existing ecosystem today.

## Quickstart

<div align="center">
   <img src="docs/res/quickstart.png" alt="SyntH Home Screenshot" style="max-width: 700px; border-radius: 8px; margin: 16px 0;" />
</div>

1. Copy `.env.example` to `.env` and fill the required values.
2. Start the stack:
   ```bash
   docker compose up
   ```
3. If using the Selenium engine with ChatGPT or Gemini, open `http://<host>:5006` and log into the web interface. You might want to send a message to the bot to trigger a browser session if you're unsure.
From there you can login.

See the [documentation](https://synthetic-heart.readthedocs.io) for installation details, advanced features and contribution guidelines.

## Docker image repository
You can browse and manage Docker images for this project on [Docker Hub](https://hub.docker.com/repository/docker/xargonwan/synthetic_heart).

## Contributing

Pull requests are welcome! Everyone is encouraged to submit contributions—especially new components, plugins, and LLM engines—to expand SyntH's capabilities. Please read the guidelines in the documentation before submitting.

## What's next (Planned features & fixes)
Here are the main improvements and integrations we plan to work on — contributions are welcome:

- [ ] Event system fixes
- [ ] Enhancements to the WebUI (usability & feature parity)
- [ ] Global animation engine fixes — make animations always reflect the actual state of the SyntH and their current actions
- [ ] Helper LLM engine — offload some background/service actions to a dedicated helper model running alongside the user-facing LLM
- [ ] Memory retagging engine — improve tagging and indexing of memory entries for better recall and context
- [ ] Memory compressor engine — compact/condense long-term memory while retaining critical information
- [ ] Grok web LLM engine support
- [ ] Deepseek web LLM engine support
- [ ] Voice message plugin — enable synth to record and send voice messages
- [ ] Desktop presence — allow SyntH to show up on a desktop environment (outside web interfaces)
- [ ] First gaming plugin: Minecraft integration
- [ ] Matrix interface

If you're interested in helping implement these features or testing them, open an issue or a PR and tag it with the relevant area (e.g. `interface`, `llm`, `plugin`, etc.).
