Windows (Native) — Running Synthetic Heart on Microsoft Windows
===============================================================

This page documents how to run Synthetic Heart natively on Windows (non-Docker).

Overview
--------
- The project is Docker-first (turnkey), but it supports native runs on Windows using environment variables to configure services (DB, ports, etc.).
- Some components (Webtop/X/PulseAudio) are Docker-only conveniences and can be ignored for native installs.

Prerequisites
-------------
- Python 3.10+ installed and on PATH
- `uv <https://docs.astral.sh/uv/>`_ installed (``pip install uv`` or see Astral docs)
- A running MySQL/MariaDB instance reachable from this machine (or set ``DB_HOST`` to a host that provides one)
- **ffmpeg** on PATH — required for multimodal video/audio processing and Discord voice features. Download from https://ffmpeg.org/download.html and add to PATH.
- Optional: Chrome and ``undetected-chromedriver`` if you plan to use Selenium-based LLM engines

Quick start
-----------
1. Copy the env example and edit it:

.. code-block:: powershell

   copy .env.example .env

2. Edit `.env` and set your DB and host service configuration (example values):

.. code-block:: ini

   DB_HOST=127.0.0.1
   DB_PORT=3306
   DB_USER=synth
   DB_PASS=synth
   DB_NAME=synth
   WEBVIEW_HOST=localhost
   WEBVIEW_PORT=3000

3. Install dependencies using **uv**:

.. code-block:: powershell

   uv sync

4. (Optional) Initialize the DB schema if you prefer (otherwise the app will attempt to initialize on startup):

.. code-block:: powershell

   mysql -u root -p < init-db.sql

5. Run a smoke import test:

.. code-block:: powershell

   python -m compileall .
   python -m pytest tests/test_imports.py -q

6. Start the app:

.. code-block:: powershell

   python main.py

Notes & Caveats
---------------
- The `webtop/` folder contains container-oriented scripts and an embedded desktop environment that rely on Linux services such as PulseAudio and X server; those are Docker-only conveniences.
- Some tests/integration may assume a MySQL server or other services; use env variables to point tests to local services or mock them in CI.
- If you intend to use the Selenium engines, ensure Chrome is installed and `undetected-chromedriver` works in your environment.

Maintenance guidance for maintainers
-----------------------------------
- Keep runtime behaviour controlled by environment variables (the repository already does this).
- When adding scripts or build steps that are container-specific, document them clearly and add platform-scan checks to CI.

Testing branches & automation
-----------------------------
A convenient helper script to prepare a testing branch and optionally create a Draft PR is available at `tools/push_windows_branch.ps1`.

Example usage (PowerShell):

.. code-block:: powershell

   # Create a branch, commit the current changes, push, and attempt to open a Draft PR via the GitHub CLI
   pwsh .\tools\push_windows_branch.ps1 -CreatePR

   # Provide an explicit branch name and title
   pwsh .\tools\push_windows_branch.ps1 -BranchName "windows-automation/add-windows-ci" -CreatePR -Title "WIP: Windows support"

Notes:
- The script will ensure a branch named `windows-automation/*` is created and pushed to `origin`. The repository is configured to auto-open a Draft PR for branches under `windows-automation/**`.
- If you use automation agents to commit/push, point them at the same script or replicate its commands. Keep tokens / credentials in GitHub Secrets and avoid hardcoding personal tokens.

If you prefer, run the script locally and it will stage, commit, and push the prepared files (it also removes `docs/windows.md` in favor of `docs/windows.rst`). The `-CreatePR` flag attempts to create a Draft PR using `gh` if available.
