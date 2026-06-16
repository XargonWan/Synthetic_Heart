Quickstart
==========

.. image:: res/quickstart.png
   :alt: Quickstart screenshot

This guide outlines the typical steps to run **Synthetic Heart** using Docker. synth is a modular AI system that automatically discovers and loads components (interfaces, plugins, and Cortex engines) at startup.


1. **Clone the repository**

   .. code-block:: bash

      git clone https://github.com/XargonWan/Synthetic_Heart
      cd Synthetic_Heart


2. **[OPTIONAL] Environment file setup**
   Copy ``.env.example`` to ``.env`` and uncomment only the values you want to
   override. The example file is intentionally trimmed to the settings most
   people need on day one. If you need rarer runtime knobs, use
   ``docs/compose_env_vars.rst`` as the full reference.
   This step is optional as now everything can be done via WebUI later.


3. **Start Synthetic Heart**

   .. code-block:: bash

      docker compose up -d

   **NOTE:** if you wish to make sure you're running the latest version, or you wish to update, you can use:

   .. code-block:: bash

      docker compose up -d --pull always

4. **Open the WebUI**
   Open your browser and navigate to ``https://localhost:8000``.
   This is the default address; if you set it up on a remote host or changed the port mapping in ``docker-compose.yml``, adjust accordingly.

5. **Configure a Cortex (engine)**
   In the WebUI navigate to **Components** and select the desired Cortex
   engine (for example: ``selenium_gemini`` or ``gemini_api``).

   -  If using a Selenium-driven Cortex (browser adapter), press the
      **Login** button for that engine and complete the provider login flow in
      the browser window that opens.
   -  If using an API-based Cortex, enter the required API key or credentials
      in the component configuration fields.

Security & operational notes
------------------------------------------------------------------

- Use a dedicated or disposable account for synthetic agents — they may spam
  chat endpoints heavily.
- Check provider terms of service before running automated agents against a
  public LLM web UI.

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

Database backups are written hourly to ``./backups/``.

Stop Synthetic Heart
----------------------

To stop Synthetic Heart containers run ``docker compose down`` oin the directory where the `docker-compose.yml` file is located.

Component Auto-Discovery
------------------------

SyntH uses a zero-configuration approach where components are automatically discovered:

- **Interfaces**: Chat platforms like Telegram, Discord, Reddit
- **Plugins**: Action providers like terminal access, weather, file operations
- **Cortex Engines**: categories of runtime engines (kinds: ``llm``, ``live``, ``agent``) such as ChatGPT (via Selenium), Gemini Live or Agent-based engines. Each engine can provide a short human-readable label at registration time (``ENGINE_LABEL`` or registry ``label``) to explain its intended use. **Note: Only Selenium ChatGPT (Legacy) is currently fully functional.**

Simply place compatible Python files in the appropriate directories and restart - no manual registration required. This modular architecture ensures that functionality can be added or removed without modifying the core system.
