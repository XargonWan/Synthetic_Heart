# Running Synthetic Heart natively on Windows (non-Docker)

This document shows a minimal, supported way to run SyntH natively on Windows for development and testing without Docker.

## Overview
- The project is Docker-first (turnkey), but it supports native runs on Windows using environment variables to configure services (DB, ports, etc.).
- Docker-only bits (Webtop s6 scripts, PulseAudio/X server helpers) are container conveniences and not required for a functional native install.

---

## Pre-requisites
- Python 3.11+ installed and on PATH
- A running MySQL/MariaDB instance reachable from this machine (or set `DB_HOST` to a host that provides it)
- Optional: Chrome and `undetected-chromedriver` if you plan to use Selenium-based LLM engines

## Quick start
1. Copy the env example:

   ```powershell
   copy .env.example .env
   ```

2. Edit `.env` and set your DB and host service configuration (example values):

   ```text
   DB_HOST=127.0.0.1
   DB_PORT=3306
   DB_USER=synth
   DB_PASS=synth
   DB_NAME=synth
   WEBVIEW_HOST=localhost
   WEBVIEW_PORT=3000
   ```

3. Create and activate a virtual environment:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

4. (Optional) Initialize the DB schema if you prefer (otherwise the app will attempt to initialize on startup):

   ```powershell
   mysql -u root -p < init-db.sql
   ```

5. Run a smoke import test:

   ```powershell
   python -m compileall .
   python -m pytest tests/test_imports.py -q
   ```

6. Start the app:

   ```powershell
   python main.py
   ```


## Notes & Caveats
- The `webtop/` folder contains container-oriented scripts and an embedded desktop environment that rely on Linux services (PulseAudio, X server) and s6 init; these are Docker-only and not required when running natively.
- Some tests/integration may assume a MySQL server or other services; use env variables to point tests to local services or mock them in CI.
- If you intend to use the Selenium engines, ensure Chrome is installed and `undetected-chromedriver` works in your environment. See the README and `requirements.txt`.

## Maintenance tips for maintainers
- Keep all runtime behaviours controlled by environment variables (the app already does this).
- When adding scripts or build steps that are container-specific, document them clearly in `docs/docker.md` or mark them as Docker-only.

---

If you'd like, I can open a draft PR with these docs and small automation helpers (PowerShell start script + Windows CI) so the maintainer can review and merge.
