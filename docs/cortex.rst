Cortex
======

The **Cortex** is the canonical runtime-engine abstraction in Synthetic Heart. A
cortex represents a reasoning or execution backend and has a `kind` (for
example: ``llm``, ``live``, ``agent``). Cortex engines are discovered and
registered at startup and can be switched at runtime.

Quick summary (for users)
-------------------------

- What is a Cortex? — A pluggable runtime engine (text model, live/streaming
  model, or agentic runtime) that produces responses and/or executes actions.
- How to switch? — Use the Web UI **Components** page or the ``/cortex`` command.
- Common kinds: ``llm`` (chat-style models), ``live`` (low-latency audio/video
  sessions), ``agent`` (action-capable runtimes), and legacy Selenium-based
  drivers.

User-facing notes
-----------------

- Cortex engines are first-class: when you read "engine" in the docs, think
  "Cortex".

- Use the Components page in the Web UI to enable/disable, login, or configure
  a Cortex engine.

Switching and configuration
---------------------------

- Switch at runtime with the command: ``/cortex <engine_name>`` (``/llm`` is a
  deprecated alias).
- Each Cortex exposes its configuration fields (API keys, model selectors,
  limits) in the Components view so administrators can update settings
  without editing code.

Available Cortexes (user guide)
--------------------------------

This section explains the Cortex types you can *use* and how to configure them
from the Web UI (Components) — developer details belong in the Developer
Guide (linked below).

Selenium (browser-driven) — what & how
-------------------------------------

- What it is: a browser-automation connector that drives web UIs (ChatGPT,
  Gemini, Grok) when an official API is not used.
- When to use: quick way to use a personal/web account or to access UI-only
  features.
- Configure (user steps):

  1. Open `Web UI → Components` and find the Selenium engine (examples:
     ``selenium_chatgpt``, ``selenium_gemini``, ``selenium_grok``).
  2. Click **Enable**, then click **Login** and follow the displayed URL
     (the page shows a Selkies login URL such as ``https://{host}:{port}``).
  3. Complete the interactive browser login; the component will show
     **Logged** when done.
- Do I need to login? Yes — Selenium cortexes require an interactive login
  (no API key).
- Common settings: model selectors like ``CHATGPT_MODEL`` or ``GEMINI_MODEL``
  can be edited in the component settings.

Gemini API (official API)
-------------------------

- What it is: the API-backed Cortex that talks to Google's GenAI/Gemini.
- Configure (user steps):

  1. Obtain an API key from Google Cloud Console (enable the GenAI API or
     "Generative AI" product, then create an API key or service account).
  2. In `Web UI → Components` set ``GEMINI_API_KEY`` (and optional
     ``GEMINI_MODEL``).
  3. The engine will report **Loaded** once the key is valid.
- Do I need API keys? Yes — Gemini API requires a Google API key (billing may
  be required on your Google project).
- Where to get the key: Google Cloud Console → APIs & Services → Credentials
  (create API key or service account key; enable GenAI API; ensure billing).

.. note::

   If you want Gemini API to participate in the Auris or Live subsystems,
   add it through the External Endpoints UI and enable the corresponding
   subsystem mapping. It will not be exposed automatically as an Auris
   provider unless configured explicitly.

Live (Gemini Live — real-time voice)
-------------------------------------

- What it is: low-latency audio sessions (used for Discord voice and other
  real-time integrations). See the full guide: :doc:`/gemini/synth-live-voice-integration`.
- How to enable & use (user steps):

  1. Configure ``GEMINI_API_KEY`` (Gemini Live requires it).
  2. If you want Discord voice, also set ``DISCORD_BOT_TOKEN`` and invite the
     bot to your server/voice channel.
  3. Enable the Gemini API cortex and the Discord interface in
     `Web UI → Components`.
  4. Invite the bot to voice — the persona may start a Live session
     automatically, or you can ask it to join voice during chat.
- Do I need API keys? Yes — ``GEMINI_API_KEY`` (and a Discord token for
  Discord voice). See the Live integration page for audio/config details.

.. note::

   Gemini Live support must be registered through the External Endpoints
   workflow and mapped to the ``live`` subsystem before it is available
   in the Components page.
- Quick troubleshooting: make sure ``google-genai`` is installed, ffmpeg is
  available, and the Components page shows the Live/session manager as
  available.

Agent (action-capable runtimes)
-----------------------------------

- What it is: Cortexes that can execute actions or tools on your behalf.
  Normal users only need to *use* actions — administration and permissions are
  handled through the Web UI and server configuration.

Quick FAQ
---------

- Where do I configure a Cortex? — `Web UI → Components` (recommended for
  non-developers). You can also set env vars in your deployment (``.env-dev``)
  for advanced setups.
- Do I need API keys? — Depends on the Cortex: **Gemini API / Live** need
  ``GEMINI_API_KEY``; **Selenium** needs an interactive login (no API key).
- How do I verify it works? — Components shows status (Loaded / Logged);
  ask the persona a question or run the Quickstart examples.

See also
--------

- :doc:`/quickstart` — quick start and Components overview
- :doc:`/gemini/synth-live-voice-integration` — Live (real‑time) integration
- :doc:`/webui_controls` — how settings appear in the Web UI
- Developer guide (components & Cortex internals): :doc:`/component_development_guide`



Related pages
-------------

- :doc:`/quickstart` — quick start and Components overview
- :doc:`/gemini/synth-live-voice-integration` — example Live integration
- :doc:`/component_development_guide` — developer guide for components

