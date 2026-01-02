Quickstart
==========

.. image:: res/quickstart.png
   :alt: Quickstart screenshot

Logging in to Gemini or ChatGPT via Selkies (Chromium Synth browser)
------------------------------------------------------------------

Sometimes you need the Synth instance to interact with external LLM web UIs such as
ChatGPT or Google Gemini. If you are running the project with Selkies (the
web-desktop), you can open the embedded XFCE desktop and launch the included
Chromium Synth browser to log in and use those web services.

Steps:

1. Open the Selkies desktop in your browser. By default Selkies runs on
   https://<host>:3001 (HTTPS) or http://<host>:3000 (HTTP) — the exact ports
   are configurable via the environment variables `SELKIES_HTTPS_PORT` and
   `SELKIES_HTTP_PORT` (check `docker-compose.yml` or your environment).

2. From the XFCE Applications menu choose "Internet" → "Chromium SynthH" to
   open the project browser (see the menu screenshot below).

.. image:: res/selkies_chromium_menu.png
   :alt: XFCE Applications menu showing Chromium SynthH
   :align: center

3. In the Chromium window navigate to ChatGPT (https://chat.openai.com) or
   Gemini (https://gemini.google.com) and sign in with the account you want the
   Synth to use. Note: many automated flows will send or generate many messages;
   we recommend using a dedicated account for the Synth to avoid affecting any
   personal or production accounts.

4. If the provider requests multi-factor authentication or CAPTCHA, complete
   them in the Selkies desktop as you would in a normal browser session.

Security & operational notes:

- Use a dedicated or disposable account for synthetic agents — they may spam
  chat endpoints heavily.
- Check provider terms of service before running automated agents against a
  public LLM web UI.

   :width: 600px
   :align: center


This guide outlines the typical steps to run **Synthetic Heart** using Docker. synth is a modular AI system that automatically discovers and loads components (interfaces, plugins, and LLM engines) at startup.

#. Copy ``.env.example`` to ``.env`` and adjust values as needed. Important
   variables include database credentials for persistent features and
   ``TRAINER_IDS`` for security. The optional ``NOTIFY_ERRORS_TO_INTERFACES``
   mapping (e.g. ``telegram_bot:123456``) defines where error notifications
   are sent.
#. Build and start the services:

   .. code-block:: bash

      docker compose up -d

#. Open the WebUI in your browser via HTTPS (default host port is ``8000``).
   Once in the WebUI navigate to **Components** and select the desired LLM
   Engine. If using a Selenium-based engine (e.g., ChatGPT or Gemini), press
   the **Login** button for that engine and complete the provider login flow
   in the browser that opens (Selkies/Chromium may be used to perform the
   login if available).

.. note::

   **Note about logs:** The stack uses a Docker-managed volume for application
   logs by default (``synth_logs`` -> ``/app/logs``). This avoids common
   host-permission problems so a user can run ``docker compose up -d``
   out-of-the-box. If you prefer to keep logs on the host, replace the
   volume mapping in ``docker-compose.yml`` with a bind-mount (uncomment
   ``./logs:/app/logs``). On systems with SELinux enabled, append ``:Z`` to
   the mount (for example: ``./logs:/app/logs:Z``).

.. note::

   **Skins folder (optional):** The image ships with built-in skins, so the
   ``skins`` folder is optional for most users. If you do not intend to
   provide custom skins, comment out the skins bind-mount in the compose file
   to avoid overriding the included skins with an empty host folder.

.. note::

   **HTTPS & certificates:** The WebUI is served over HTTPS at
   ``https://localhost:8000`` by default. If no certificate is provided a
   self-signed certificate will be generated automatically. If the database
   is initializing on a first run, give the services a few seconds; the
   service will retry until the DB is ready.

Database backups are written hourly to ``./backups/``. To tear down the
containers, press :kbd:`Ctrl+C` or run ``docker compose down``.

Component Auto-Discovery
------------------------

synth uses a zero-configuration approach where components are automatically discovered:

- **Interfaces**: Chat platforms like Telegram, Discord, Reddit
- **Plugins**: Action providers like terminal access, weather, file operations
- **LLM Engines**: AI backends like ChatGPT (via Selenium), Gemini, Grok (experimental), or manual input. **Note: Only Selenium ChatGPT (Legacy) is currently fully functional.**

Simply place compatible Python files in the appropriate directories and restart - no manual registration required. This modular architecture ensures that functionality can be added or removed without modifying the core system.
