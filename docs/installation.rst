Installation
============

.. image:: res/Installation.png
   :alt: Installation steps
   :width: 600px
   :align: center


Three ways to install, in the order most people should consider them.

.. contents::
   :local:
   :depth: 1


One-click install (recommended)
-------------------------------

**Windows**

Download ``SyntH-Setup-<version>.exe`` from the releases page and run it. That is
the whole procedure. The installer asks you nothing: no component picker, no
directory page, no administrator prompt, and it never opens a console window.

It puts everything inside your own user profile:

- the application, in ``%LOCALAPPDATA%\Programs\SyntH``
- its own private PostgreSQL and its database, in that folder
- its own Python (through ``uv``), so no Python needs to be installed
- Start Menu shortcuts, and a desktop shortcut if you ask for one

When it finishes it starts SyntH and opens a setup page in your browser, where
you say who the persona is, where and when you are, and which AI service it
should think with. Those are the only questions, and they are asked once.

Nothing is installed system-wide, no Windows service is created, and
uninstalling removes everything except your ``data`` folder and ``.env``, so a
later reinstall resumes with the same history, persona and keys. The uninstaller
asks whether to delete those as well, and keeps them unless you say otherwise.

**Linux**

.. code-block:: bash

   curl -fsSL https://raw.githubusercontent.com/XargonWan/Synthetic_Heart/develop/install.sh | bash

The script detects your distribution, installs PostgreSQL (with pgvector) and
ffmpeg through the system package manager, fetches the source into
``~/.local/share/synthest``, sets up the database and the Python environment, and
leaves a ``synth`` command in ``~/.local/bin`` plus a desktop entry. Use
``--help`` to see the flags, and ``--dry-run`` to see what it would do without
doing it. It asks for your password only for the package manager.

**If pgvector is missing** on Windows, SyntH still installs and runs; it simply
keeps its long-term memory in memory instead of in PostgreSQL, and says so. The
vector extension is built by CI and shipped with the installer (see
``installer/vendor/README.md``); pgvector publishes no prebuilt Windows binaries.


Docker
------

The project can be deployed using Docker. Ensure you have `docker` and
`docker compose` installed on your machine. Copy ``.env.example`` to ``.env``
and uncomment the values you want to override for your environment. The
example file is intentionally short and focused on common deployment settings;
use ``docs/compose_env_vars.rst`` for the full list of advanced and
low-frequency overrides.

Build and start the services:

.. code-block:: bash

   docker compose up

PostgreSQL is started automatically and a daily backup container writes dumps to
``./backups/``.

.. note::

   Docker deployments now use PostgreSQL, the same database as the native
   installs. Older versions used MariaDB. If you are upgrading a MariaDB
   deployment, see `Coming from MariaDB`_ below.


From source
-----------

.. code-block:: bash

   git clone https://github.com/XargonWan/Synthetic_Heart.git
   cd Synthetic_Heart
   uv sync

   # Create the database, write .env, pick free ports
   uv run --no-project python scripts/bootstrap.py

   # Start it (no console window; opens the WebUI when it is up)
   uv run --no-project python scripts/start_synth.py

``scripts/bootstrap.py`` is the same bootstrap both installers use. It finds an
existing PostgreSQL server if there is one (including through ``sudo -u
postgres`` on Debian/Ubuntu), otherwise it can unpack a private one with
``--portable``, then creates the role, the database and the extensions, writes a
technical ``.env``, and runs ``uv sync``. Add ``--dry-run`` to see the plan
without changing anything, and ``--stop-cluster`` to stop a private cluster
again (what the Windows uninstaller calls).

Check that it is up with:

.. code-block:: bash

   uv run --no-project python scripts/healthcheck.py


System dependencies
-------------------

- **ffmpeg** — required for multimodal video/audio processing (frame
  extraction, audio track splitting, format conversion). The pipeline degrades
  gracefully without it, but video and voice-note features will be unavailable.
  The Windows installer can provision it (``-WithFfmpeg``), and ``install.sh``
  installs it from the distribution packages.

  .. code-block:: bash

     # Debian / Ubuntu
     sudo apt-get install ffmpeg

     # macOS (Homebrew)
     brew install ffmpeg

- **PostgreSQL** — installed and managed for you by every path above. On
  Debian/Ubuntu the vector extension comes from ``postgresql-16-pgvector``.

- **Node.js** — only needed for the Minecraft vessel bridge, which drives the
  in-world body. Not required for anything else; the Windows installer can
  provision it with ``-WithNode``.

Local speech (``torch``, ``torchaudio``, ``kittentts``, ``vosk``) is **not**
installed by default: it is several hundred megabytes and only useful for
offline TTS/STT. Add it when you want it:

.. code-block:: bash

   uv sync --extra local-voice

Cloud engines (any OpenAI-compatible endpoint) need none of it.


Coming from MariaDB
-------------------

MariaDB was the original database and is no longer used by new installs. Existing
deployments keep working while you migrate, and the migration is automatic: on
first start with a build that expects PostgreSQL, the legacy tables are copied
across, after a pre-migration backup is written. It is controlled by
``SYNTH_AUTO_MIGRATE_LEGACY_DB`` (enabled by default; set it to ``0`` to keep the
old database untouched until you migrate by hand).

To do it deliberately instead, and see what it would move:

.. code-block:: bash

   uv run --no-project python scripts/migrate_main_db_to_postgres.py --help

Nothing is deleted from MariaDB, so the old data stays available as a fallback.

Separately, older builds wrote their files to the container paths ``/app`` and
``/config``. On Windows those are drive-relative, so they became ordinary folders
such as ``D:\config``, which is where an upgraded install's encrypted API keys
and attachments still are. SyntH notices this at startup and adopts that state
into its data folder, once, without deleting the originals. To see what it would
adopt:

.. code-block:: bash

   uv run --no-project python -m core.legacy_state --dry-run


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
