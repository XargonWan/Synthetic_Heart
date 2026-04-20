Installation
============

.. image:: res/Installation.png
   :alt: Installation steps
   :width: 600px
   :align: center


The project can be deployed using Docker. Ensure you have `docker` and
`docker compose` installed on your machine. Copy `.env.example` to `.env`
and adjust the values for your environment. Set ``BOTFATHER_TOKEN`` and
optionally configure ``NOTIFY_ERRORS_TO_INTERFACES`` with comma-separated
``interface:trainer_id`` pairs to select which interfaces receive error
notifications.

Build and start the services:

.. code-block:: bash

   docker compose up

A MariaDB instance is started automatically and a daily backup container
writes dumps to ``./backups/``.

System Dependencies (non-Docker)
--------------------------------

When running outside the Docker container the following system packages
must be available:

- **ffmpeg** — Required for multimodal video/audio processing
  (frame extraction, audio track splitting, format conversion).
  The multimodal pipeline degrades gracefully when ``ffmpeg`` is absent,
  but video and voice-note features will be unavailable.

  .. code-block:: bash

     # Debian / Ubuntu
     sudo apt-get install ffmpeg

     # macOS (Homebrew)
     brew install ffmpeg

     # Windows – download from https://ffmpeg.org/download.html
     # and ensure ffmpeg.exe is on PATH

- **MariaDB client libraries** — Required by ``aiomysql`` /
  ``PyMySQL`` for database connectivity.

  .. code-block:: bash

     # Debian / Ubuntu
     sudo apt-get install libmariadb3 libmariadb-dev mariadb-client

All Python dependencies (including ``discord-ext-voice-recv`` for Discord
voice reception) are managed by **uv** and declared in ``pyproject.toml``:

Modular Architecture
--------------------

Synthetic Heart follows a modular architecture where components are automatically discovered and loaded:

**Core System**
    Handles message processing, action execution, and component orchestration.

**Interfaces** (``interface/``)
    Platform integrations (Telegram, Discord, Reddit, etc.) that handle communication.

**Plugins** (``plugins/``)
    Action providers that extend functionality (terminal, weather, AI diary, etc.).

**Cortex engines**
    Runtime engine implementations (OpenAI, Google Gemini, manual input, etc.).

This design ensures that new features can be added by simply placing compatible modules in the appropriate directories without modifying the core codebase.
