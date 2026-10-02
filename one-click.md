# One-click install: plan for Windows and Linux native

Status: **plan only, nothing implemented yet.**
Revision 2 (2026-09-23): one option instead of a component picker, MariaDB deprecated, engine
default in flux, "must not be ugly" promoted to a hard requirement, and a new Docker-ism sweep
section (§4) for the native path. Revision 1 was read against `develop` (`11cbf927`).

Everything marked *evidence* was read from the working tree during this investigation. Nothing was
executed or tested: no installer was built, no clean VM was used. The file/line references are the
starting point for the work, not proof that the plan compiles.

---

## 0. Short version

**One artifact per platform, one button, zero technical choices.**

| Platform | What the user gets | Internals |
|---|---|---|
| Windows | `SyntH-Setup-<version>.exe`. Double-click, watch a progress bar, browser opens on a SyntH setup page. | Inno Setup 6, no component page, no admin, no terminal window. Bundles uv + portable PostgreSQL + pgvector + prebuilt Stage + the source tree. |
| Linux | `curl -fsSL https://.../install.sh \| bash`. One command, pretty output, browser opens on the same setup page. | bash installs distro packages, then runs the shared Python bootstrap, then a systemd **user** unit. |

The shared piece is one cross-platform Python entry point (`scripts/bootstrap.py`) doing the part
that is identical on both OSes: create role + database, create the `vector` / `pg_trgm` extensions,
write a small technical `.env`, run `uv sync`, smoke-test, open the browser. The OS shims
(`install.sh`, `install_prereqs.ps1`) only do what needs root/admin: PostgreSQL, pgvector, ffmpeg, uv.

`uv` removes the Python prerequisite entirely (`.python-version` is `3.12` and uv downloads its own
CPython), so the real prerequisites collapse to PostgreSQL (+pgvector) and ffmpeg. Everything else
is packaging and taste.

The five things that actually block one-click today (detail in §3 and §4):

1. The only Windows installer that ever existed was deleted in `e40c4ab1`; it is MariaDB-based and
   required admin.
2. `scripts/windows_setup.py` still builds a MariaDB 3306 database, while the runtime is
   Postgres-only by default (`core/db.py:128-134`).
3. `.env.example` ships `DB_PORT=3306` and `DB_ROOT_PASS`; that port is used verbatim for a
   Postgres connection (`core/db.py:241`), so copying the template breaks a native install.
4. Several still-Docker-shaped defaults break or look broken on native (agent file roots default to
   `/app`, radio audio to `/app/tmp_tts`, the TLS cert dir to `/config/ssl`, both listeners bind
   `0.0.0.0` and trigger a firewall dialog). Full list in §4.
5. There is no first-run experience: a fresh DB seeds `BASE_CORTEX=selenium-llm-engine`
   (`core/db.py:1496`) which is being replaced, and no onboarding screen exists (§3.6).

---

## 1. The one option

This is the whole installer experience. If a step is not listed here, it does not happen, or it
happens silently with a sensible default.

**Windows, after double-click:**

1. Branded splash. No license page to scroll, no directory page, no component page, no
   "select additional tasks" page.
2. One progress surface with meaningful steps, not a scrolling log:
   `Installing database engine` / `Preparing SyntH` / `Installing components` / `Starting SyntH`.
3. The browser opens on `http://127.0.0.1:<port>` at the SyntH setup page.

**Linux, after the one command:** the same four steps printed as clean single-line progress, then
the browser opens on the same page.

**The SyntH setup page (the only thing the user is asked, and it is ours, so it can look good):**

* Who is your SyntH (name, profile text, optional aliases).
* Who are you (your name).
* Where are you (location + timezone, pre-filled from the browser's locale).
* How should your SyntH think (pick a provider preset, paste one API key, "Test" button, done).

Then it writes straight to the config registry and drops the user into the normal WebUI.

**What the installer never asks and never does:**

* No component selection. PostgreSQL, pgvector, uv, ffmpeg, and the Stage frontend are always
  installed. Node.js and the local voice stack are **not** in the installer (see D6).
* No database questions. No port questions. No directory questions. No "custom install" path.
* No MariaDB. Deprecated: it stays in the tree only so existing installs keep working, and gets one
  documentation note for people who have not migrated yet.
* No interface setup. WebUI only in the install flow; Telegram/Discord/Matrix stay a later,
  optional, in-WebUI affair.
* No terminal window, no console output, no "press any key to continue", no `cmd.exe` flash on
  launch (§2).

---

## 2. "It must not be ugly" as a requirement

The user sees two surfaces before they see the product: the installer, and the app's own setup page.
Both are ours to control. Concretely:

**Installer (Windows).** Inno Setup supports all of this natively, and the deleted script used none
of it:

* `WizardStyle=modern`, custom `WizardImageFile` (164x314) and `WizardSmallImageFile` (55x55) built
  from the existing brand assets (`docs/res/synth_banner.png`, `docs/res/synth_logo*`).
* `DisableDirPage=yes`, `DisableProgramGroupPage=yes`, `DisableReadyPage=yes`, no `[Types]`, no
  `[Components]`. The old script's `Types: standard/full/custom` plus a four-item component checkbox
  list is exactly the "annoying and really ugly" thing being dropped.
* Product icon, version, publisher, and a stable `AppId` so upgrades replace rather than stack.
* No spawned PowerShell console. The old script's `[Run]` entry launched
  `powershell.exe -File scripts/install_prereqs.ps1`, which both flashes a console and ends with
  `Write-Host "Press any key to close this window..."` (`scripts/install_prereqs.ps1:251-252`).
  Replace with an Inno progress page driven by a `beforeinstall`/`afterinstall` helper, or
  `Flags: runhidden` plus a status label. Errors surface as a styled dialog with the log path, never
  as a console that scrolls away.
* Finish page: a single "Open SyntH" checkbox, checked, opening the setup page.

**Runtime chrome (Windows).** "One-click install" is wasted if launching the app afterwards looks
like a developer tool:

* `scripts/start_synth.bat` opens a console window (`uv run python main.py`, then `pause`). Replace
  with a shortcut that runs the venv's `pythonw.exe` with no console, plus a separate "Stop SyntH"
  shortcut. Auto-start via a Task Scheduler logon task, so the normal experience is "it is just
  there".
* No firewall prompt: the WebUI defaults to binding `0.0.0.0` (`core/webui.py:249`) and so does the
  OpenAI-compatible server (`interface/openai_api_server/openai_api_server.py:1115`). On Windows
  that raises the Defender "Allow access?" dialog, which reads as a security scare. Native installs
  bind `127.0.0.1` (D7).
* No certificate interstitial: TLS is on by default (`core/webui.py:254-257`, `SECURE_CONNECTION=1`)
  and the cert dir defaults to `/config/ssl` (`core/webui.py:13483`), which on Windows is
  `C:\config\ssl` and normally cannot be created. Native installs run plain HTTP on loopback (D7).

**Linux.** The one command unavoidably prints something, so make it short and deliberate: a banner,
four progress lines, a final URL. No `apt` noise unless something fails. Colours off when not a TTY.
`--quiet` and `--no-browser` flags for people who script it.

**Branding consistency.** Installer, setup page, and WebUI should share the accent colour
(`WEBUI_ACCENT_COLOR` is already a config key) and the logo. Same wordmark, same spacing.

---

## 3. Current state audit

### 3.1 What already works

* `uv sync` + `uv run main.py` is a good developer path (`README.md:260-289`), and uv removes the
  need to install Python at all.
* The runtime self-heals its schema on Postgres: `core/db.py:1451-1480` applies
  `scripts/sql/app_main_postgres.sql` statement-by-statement at boot, logging and skipping
  failures. The installer only needs to create the database, the role, and the extensions.
* SOUL creates its own extensions at runtime (`core/soul/repository.py:543-544`), with the
  superuser caveat in D4.
* The Docker path is solid and documented (`docs/quickstart.rst`, `docker-compose.yml`), including
  `pgvector/pgvector:pg16`.

### 3.2 What exists but is stale or wrong for one-click

| Item | Problem | Evidence |
|---|---|---|
| `scripts/windows_setup.py` (1255 lines) | Stage 2 is MariaDB: `pymysql` on 127.0.0.1:3306, creates the `synth` DB/user, then pipes the MariaDB `init-db.sql`. Its SOUL stage does handle Postgres. | `scripts/windows_setup.py:455-566`, `:967-1119` |
| `scripts/install_prereqs.ps1` | MariaDB is step 1 and required; PostgreSQL + pgvector sit behind `-InstallPostgres 1`. pgvector is installed by copying DLLs into the PG tree from a GitHub release zip. Ends by waiting for a keypress. | `:72-88`, `:160-246`, `:228-238`, `:251-252` |
| `.env.example` | Ships MariaDB values as live settings: `DB_PORT=3306`, `DB_ROOT_PASS=root`, `EXT_DB_PORT=3306`. | `.env.example:19-25`, `core/db.py:241` |
| `docs/installation.rst` | "A MariaDB instance is started automatically", MariaDB client libs, no mention of pgvector. | `docs/installation.rst:10-24`, `:48-54` |
| `docs/windows.rst` | Prereq list is "MySQL/MariaDB", `DB_PORT=3306`, `mysql -u root -p < init-db.sql`, `python main.py`. | `docs/windows.rst:15`, `:31-37`, `:49` |
| First-run URL | The wizard writes `SYNTH_WEBUI_HTTP_PORT=8001` + `SYNTH_WEBUI_TLS=0`; the container template ships HTTPS 8000 / HTTP 8080; the app defaults to TLS on, port 8080. Three answers to "where is my WebUI". | `scripts/windows_setup.py:760-794`, `:1190`, `.env.example:37-38`, `core/webui.py:254-298` |
| `README.md` native section | Warns "DATABASE SETUP REQUIRED ... not automated" and tells the user to install PostgreSQL by hand. | `README.md:260-289` |

### 3.3 What is missing entirely

* **No Linux native installer.** `git grep -l "install.sh"` returns nothing; the only launchers are
  `synth.sh` (container entrypoint) and `synth` (a netcat CLI toy).
* **No installer in CI.** `.github/workflows/build-release.yml` builds multi-arch Docker images
  only.
* **No first-run experience.** No onboarding route, page, or gate exists: grepping `core/webui.py`
  and `core/webui_templates/sections/` for onboarding/first-run/welcome/setup returns nothing, and
  there is no `SETUP_COMPLETE`-style flag anywhere.
* **No update path, no uninstall** beyond the deleted Inno `[UninstallRun]`.
* **Leftover ignores** for the deleted installer: `.gitignore:86-88`.

### 3.4 The Windows installer that already existed

Deleted as "obsolete local/editor config and installer files" in `e40c4ab1` (2026-05-25). Recover
with:

```
git show e40c4ab1^:installer/synth-installer.iss
git show e40c4ab1^:installer/build_installer.ps1
```

Worth keeping from it: `{localappdata}\SyntH` as the install dir, the file exclusion list, the
uninstall that removes the service, and `build_installer.ps1`'s pinned vendor downloads with SHA-256
verification. Worth discarding: the component and type pages, the MariaDB requirement,
`PrivilegesRequired=admin`, the console-spawning `[Run]` entries, and reading a `../version.txt`
that no longer exists.

### 3.5 Install-time cost

`torch`, `torchaudio` and `kittentts` are **base** dependencies (`pyproject.toml:8-72`), from the
pytorch-cpu index and a GitHub release wheel (`pyproject.toml:146-153`). A one-click installer that
runs plain `uv sync` downloads hundreds of MB before the user sees anything, and `AGENTS.md` already
documents the `kittentts` wheel URL as a flaky-network failure mode. This is the second biggest
drop-off risk after the database.

### 3.6 The first-run dead end

A fresh database seeds `BASE_CORTEX=selenium-llm-engine` (`core/db.py:1496`). The in-process
Selenium engine is gone (`core/webui.py:12967-12981` returns 422 and points at the external
service), and that service is a separate Docker image (`docker-compose.yml:106-120`, port 14848).
The engine landscape is also moving: per xargon, Selenium is being replaced, so **the installer must
not hardcode any engine default at all.** The first-run page asks for an endpoint + key, probes it,
and enables it. Everything the page needs already exists as API: `POST /api/config`
(`core/webui.py:858`), `GET /api/external-endpoints/presets` (`:1009`),
`POST /api/external-endpoints` (`:1007`), `POST /api/external-endpoints/{id}/probe` (`:1017`),
`POST /api/external-endpoints/{id}/enable` (`:1026`).

---

## 4. Docker-ism sweep (the docker to native path audit)

The repo was Docker-only for most of its life, so "works in the container" and "works on the host"
are not the same claim. Most of this is already fixed; this section is the remaining list, split by
whether it breaks, looks broken, or is only cosmetic. **These are P0 items**: they are small, and
every one is a native-only bug that no Docker test can catch.

**(a) Still Docker-shaped, needs fixing before shipping a native installer**

| Where | Default | What happens on native |
|---|---|---|
| `plugins/agent_plugin/agent_plugin.py:339-340`, `plugins/document_skill/document_skill.py:107-108` | `AGENT_FS_ROOT=/app`, `SYNTH_LOG_DIR=/app/logs` | The agent's file tools and the document skill sandbox themselves to `C:\app`, which does not exist. Tools fail or see nothing. |
| `core/plugin_instance.py:2117` | `AGENT_FS_ROOT=/app` | Same class of bug for attachment persistence. |
| `plugins/radio_host/radio_host_plugin.py:26` | module-level `AUDIO_STORAGE_DIR = Path("/app/tmp_tts/radio_host")` | Resolves to `C:\app\tmp_tts\radio_host`; the mkdir normally fails, so radio banter replay storage is broken. |
| `core/webui.py:3486` | `/app/logs/synth.log` | Log streaming falls back to a path that does not exist. |
| `core/webui.py:13483` | `SYNTH_WEBUI_CERT_DIR=/config/ssl` | `C:\config\ssl` cannot be created without admin, so TLS generation fails while TLS is on by default. D7 avoids it, but the default should still be platform-aware. |
| `core/webui.py:10930`, `:11020`, `:11070` | `SYNTH_EXPOSED_STORAGE_ROOT=/config/storage` | Same shape, affects exposed-file serving. |
| `core/db_cutover.py:187` | `/config/db-cutover-state.json` | Legacy-migration path only, but it should not write to `C:\config` either. |

The good pattern already exists in-tree and should be copied: `core/outbound_file_utils.py:199`
defaults to the application root rather than literal `/app`, and
`core/external_endpoints/crypto.py:36-44` falls back from `/config/.synth_secret` to
`~/.synthetic_heart/.synth_secret` when `/config` is not writable.

**(b) Correctly platform-branched already (no action)**

* `core/webui.py:384-394`: attachments dir branches on `os.name == "nt"` to a temp dir.
* `plugins/rift_vessel/minecraft/minecraft.py:640`: the `127.0.0.1` to `host.docker.internal` remap
  is gated by `_is_in_container()` (`:190`) with a `SYNTH_IN_CONTAINER` override. Note for the
  installer: **set `SYNTH_IN_CONTAINER=0` explicitly**, so a mis-detection can never remap a
  same-machine LAN world to a name that does not resolve on native Windows.
* `core/config.py:38` and `core/logging_utils.py:25` load `/app/.env` as an extra fallback after the
  repo-local `.env`; harmless.

**(c) Cosmetic Docker-first copy that shows on native**

* `res/synth_webui/js/engines.js:850` tells the user to "use host.docker.internal instead of
  localhost if needed", which is nonsense on a native install.
* Interface action schema examples use `/app/data/photo.png` style paths
  (`interface/telegram_bot/telegram_bot.py:2358`,
  `interface/discord_interface/discord_interface.py:853`,
  `interface/matrix_interface/matrix_interface.py:351`,
  `interface/fluxer_interface/fluxer_interface.py:600`). These are examples the model reads, so a
  native install should show native-looking paths.

**How to keep this honest:** add a CI job that greps the app tree for `/app`, `/config`, and
`host.docker.internal` and fails on new hits outside an allowlist. That is the only mechanism that
stops the list from growing again, and it is cheap.

---

## 5. Design decisions

**D1. Form factor.** Windows: Inno Setup `.exe`, restored and rewritten. Linux: a `curl | bash`
installer, same script kept in-tree so it can be read and pinned by hash. Alternative considered:
winget/Chocolatey manifests (good later as a wrapper) and `.deb`/`.rpm` (later; does not cover
Fedora+Arch+Ubuntu in one artifact and still needs the Postgres step).

**D2. No components, fixed bundle, no admin.** One install profile. PostgreSQL from the EDB
**binary-only zip** (postgresql.org documents this option explicitly) under
`{localappdata}\SyntH\pgsql`, `initdb` + `pg_ctl` as the user, pgvector DLLs copied into that tree
(the trick `install_prereqs.ps1:228-238` already uses). Auto-start as a Task Scheduler logon task,
not a Windows service, so no UAC and no NSSM binary. Linux prefers distro packages via
apt/dnf/pacman, with a `--portable` no-sudo fallback that unpacks the same style of tarball under
`~/.local/share/synth/pgsql`. Node.js and the local voice stack are **not** installed (see D6).

**D3. MariaDB is deprecated.** New installs are Postgres-only. The installer never offers MariaDB;
`scripts/windows_setup.py`'s MariaDB stage becomes a legacy migration switch (for people who have
not moved yet) and the docs get one "coming from MariaDB" page. `.env.example` keeps a clearly
labelled legacy block, nothing more.

**D4. The installer owns database bootstrap, the app owns its schema.** Create role + database,
then `CREATE EXTENSION IF NOT EXISTS vector` / `pg_trgm` **as the PostgreSQL superuser**, because a
runtime `CREATE EXTENSION` by a non-superuser role fails even though
`core/soul/repository.py:543` attempts it. Detection first: if PostgreSQL is already listening on
5432 and reachable, reuse it and only create what is missing. Never reinstall over an existing
server, never touch other databases or roles.

**D5. One bootstrap, thin OS shims.** `scripts/bootstrap.py` (uv-run, stdlib-only) owns OS/port
detection, role+DB+extension creation, `.env` generation with a generated password, `uv sync` with
progress, the smoke test, and the final "opening your browser". `install.sh` and
`install_prereqs.ps1` only install OS packages and call it. `windows_setup.py` re-points at the same
code, so there is exactly one implementation of "configure a database for SyntH", which is what
drifted between the wizard and `.env.example` in the first place.

**D6. Extras decided by detection, never by a checkbox.** Node.js: not installed; when the user
enables the Minecraft Vessel the existing provisioner already fails cleanly with a clear reason
(`interface/minecraft_provisioner.py`), and that is the moment to offer "install Node for me" from
the WebUI. Local voice (`torch`/`torchaudio`/`kittentts`/`vosk`): see §3.5 and open question Q4.
Stage frontend: always ship a prebuilt `frontend/dist` (CI builds it exactly like `Dockerfile:8-17`
does), because the Stage WebUI only mounts when that directory exists (`core/webui.py:589-607`).

**D7. Native defaults are localhost and no TLS.** Write `SYNTH_WEBUI_HOST=127.0.0.1`,
`OLLAMA_HOST=127.0.0.1`, `SYNTH_WEBUI_TLS=0`, `SYNTH_IN_CONTAINER=0`, and pick a free port at
install time. Rationale: no firewall dialog, no self-signed certificate warning, no listener exposed
on a laptop. Docker keeps its current defaults; this is native-only.

**D8. `.env` holds technical values only.** The installer writes database credentials, ports, host
bindings, `SYNTH_HOST_OS`, and the generated secret. Persona, location, timezone, and engine keys
belong in the config registry and go in through the setup page (`TRAINER_NAME`, `SYNTH_NAME`,
`SYNTH_PROFILE`, `SYNTH_ALIASES`, `TZ`, `PROMPT_LOCATION`; `core/config_manager.py:36`,
`core/time_zone_utils.py:18-37`). Note that `TZ` is registered with `allow_env_override=False`, so
the environment value only seeds the default and the DB value wins afterwards: correct for us, but
it means the installer must not pretend `.env` is where the timezone lives.

**D9. The setup page is the pretty surface.** A first-run gate in the WebUI: if the setup flag is
absent, `/` shows the setup page; when it is finished, a `SETUP_COMPLETE`-style registry flag is
written and it never appears again. Reuse `POST /api/config` and the external-endpoints API (§3.6).
Nothing new in the core beyond the flag and the gate.

**D10. Versioning and update.** Reintroduce a version file written by CI (`GitVersion.yml` already
produces the semver; the deleted `.iss` expected `../version.txt`). Update = re-run the installer or
the script over the same directory, keeping `.env`, the database, and `logs/`. Never silently
overwrite `.env`. A "check for updates" is P2.

**D11. Signing.** An unsigned `.exe` gets a SmartScreen wall, which is a one-click failure in
practice. Ship unsigned for v1 with a clean bypass note, and start a free-for-OSS signing
application in parallel, because approval takes weeks and the experience differs a lot once signed.

**D12. Uninstall and data.** Uninstall removes code, venv, and the bundled PostgreSQL binaries, and
asks before deleting data. Default: keep data and print where it is. Never delete a database the
installer did not create.

---

## 6. Deliverables

### Windows

| Path | Action | Notes |
|---|---|---|
| `installer/synth-installer.iss` | restore from `e40c4ab1^`, rewrite | no `[Types]`, no `[Components]`, `DisableDirPage`/`DisableReadyPage`/`DisableProgramGroupPage=yes`, `WizardStyle=modern`, branded wizard images, `PrivilegesRequired=lowest`, `runhidden` helpers, no console windows, finish page opens the setup page |
| `installer/build_installer.ps1` | restore, update | pinned vendor set: uv installer, EDB Postgres binaries zip, pgvector zip, ffmpeg build, prebuilt Stage bundle, version file; SHA-256 verification for every download; `installer/Output/` cleaned |
| `installer/vendor/` | stays gitignored | `.gitignore:86-88` keeps working; add `vendor/README.md` with the pins |
| `installer/README.md` | new | build locally, bump a pin, test in a clean VM |
| `scripts/install_prereqs.ps1` | rewrite | uv, PostgreSQL (portable), pgvector, ffmpeg, no MariaDB, no keypress pause, progress output the installer can display; keep the TEMP+app-dir logging and the winget-location workaround |
| `scripts/windows_setup.py` | replace stage 2 | Postgres stage delegating to `scripts/bootstrap.py`; MariaDB moves to a legacy migration switch; keep `--stage`, `--reconfigure`, `--non-interactive` |
| `install.ps1` (repo root) | new | `irm ... \| iex` path, thin wrapper that fetches the release tarball and runs the bootstrap |
| launch shortcuts | new | `pythonw.exe` shortcut (no console) plus a Stop shortcut, replacing `scripts/start_synth.bat` in the Start Menu and desktop |

### Linux

| Path | Action | Notes |
|---|---|---|
| `scripts/install.sh` | new | apt/dnf/pacman/zypper detection, `postgresql` + pgvector package (PGDG repo when the distro package is too old), ffmpeg, curl, uv, then `scripts/bootstrap.py`; `--dry-run`, `--non-interactive`, `--portable` (no sudo), `--quiet`, `--no-browser` |
| `scripts/synth.service` | new | systemd **user** unit template (`%h` paths, `Restart=on-failure`), `systemctl --user enable --now` |
| `install.sh` (repo root) | new | one-liner entry point, same script, hash-verified |
| `scripts/uninstall.sh` | new | stops the unit, removes code/venv, asks about data |

### Shared

| Path | Action | Notes |
|---|---|---|
| `scripts/bootstrap.py` | new | the single post-OS-deps bootstrap (D5); runnable standalone as `uv run python scripts/bootstrap.py` |
| `scripts/healthcheck.py` | new | DB reachable, schema present, HTTP port answering; used by installers and CI |
| `.env.example` | rewrite the DB block | Postgres-first (`DB_PORT=5432`, no `DB_ROOT_PASS`), legacy MariaDB clearly labelled, plus a comment that template values are live values |
| `.env.template` | new | what the bootstrap copies and fills; one canonical first-run URL and port across every path (§3.2) |
| `version.txt` | new, written by CI | installer + runtime version in one place |
| WebUI setup page | new | first-run gate + setup screen (D9); the only new UI surface, and everything it writes already has an API |
| Docker-ism fixes | §4(a) + §4(c) | agent/doc-skill/plugin roots default to the app root, radio audio dir, log-stream fallback, exposed storage root, cert dir, engines.js copy, interface schema examples |
| CI guard | new | grep job that fails on new `/app`, `/config`, `host.docker.internal` hits outside an allowlist |

### Docs and site

* `README.md`: Quickstart becomes "Option A: one-click (Windows `.exe` / Linux command)", "Option B:
  Docker (recommended for servers)", "Option C: manual/developer". Drop the "DATABASE SETUP
  REQUIRED" warning.
* `docs/installation.rst`: rewrite for Postgres (still MariaDB-era) and add the native section.
* `docs/windows.rst`: rewrite; drop MariaDB/3306/`init-db.sql` and the `push_windows_branch.ps1`
  tangent.
* `docs/oneclick.rst`: new. What the installer does, what it creates, where data lives, how to
  update, how to uninstall, how to migrate from MariaDB.
* `website/index.html`: download buttons next to "Get started" (`website/index.html:22-36`).

### CI

Add to `.github/workflows/build-release.yml`:

* `build-stage-frontend`: build `frontend/dist` once, reuse it for both artifacts.
* `build-installer-windows` (`windows-latest`): install Inno Setup, fetch pinned vendor files,
  compile, run the installer silently (`/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`), run
  `healthcheck.py`, upload and attach to the release on `main`.
* `install-linux-smoke`: matrix `ubuntu-24.04`, `debian-12`, `fedora-41`, `archlinux`: run
  `scripts/install.sh --non-interactive`, then `healthcheck.py`.
* `docker-ism-guard`: the grep job from §4.

---

## 7. Phases and acceptance criteria

**P0: Windows one-click (kills the ticket volume).**
Scope: `.env.example` Postgres fix, `bootstrap.py`, Postgres stage in `windows_setup.py`,
`install_prereqs.ps1` rewrite, Docker-ism fixes from §4(a) and §4(c), the setup page, the rewritten
Inno installer, the CI job that builds it.
Acceptance:
* Fresh Windows 11 VM, no Python, no uv, no PostgreSQL, no git: double-click the `.exe`, watch the
  progress, land on the setup page, fill it in, chat works. No terminal, no UAC prompt, no firewall
  dialog, no certificate warning.
* Same VM with PostgreSQL 16 already installed and a `synth` database present: reused, not
  clobbered.
* Uninstall keeps data and says where it is.

**P1: Linux.**
Scope: `scripts/install.sh`, systemd user unit, `install.sh` one-liner, `uninstall.sh`, CI smoke
matrix, `--portable` no-sudo path.
Acceptance:
* Fresh `ubuntu:24.04`: one command, setup page reached, chat works with one API key.
* Same on Debian 12 and Fedora.
* `--portable` with no sudo completes and runs.

**P2: polish.**
Code signing (D11), update check (D10), light dependency profile (§3.5), Node-on-demand for the
Vessel (D6), `.deb`/winget manifests, downloads page, `SYNTH_IN_CONTAINER` auto-detection hardening.

---

## 8. Test matrix

| Scenario | Windows | Linux |
|---|---|---|
| Clean machine, no toolchain | required | required (container per distro) |
| Existing PostgreSQL 16, `synth` DB present | required | required |
| Existing PostgreSQL 16, 5432 held by a foreign DB | required | required |
| All default ports taken (5432/8080/8000/11435) | required | required |
| No admin / no sudo | required (default path) | required (`--portable`) |
| Path with spaces and non-ASCII characters | required | n/a |
| Re-run over an existing install (update) | required | required |
| Uninstall then reinstall (data retained) | required | required |
| Offline after the artifact is downloaded | required | required |
| Slow or flaky network during `uv sync` | required (kittentts wheel) | required |
| Antivirus / SmartScreen present | required | n/a |
| No firewall prompt, no cert warning on first run | required | n/a |

---

## 9. Risks

* **SmartScreen and antivirus** on an unsigned installer. Mitigation: D11.
* **pgvector DLL copying is version-fragile** (`install_prereqs.ps1:228-238`). Mitigation: pin the
  PostgreSQL version in the vendor set and fail loudly with manual instructions rather than
  half-copying.
* **Clobbering an existing PostgreSQL.** Mitigation: D4 detection-first.
* **Port collisions** are normal on a gamer's machine. Mitigation: probe and pick free ports, write
  them to `.env`, and use the same value in the shortcut URL.
* **Install duration and disk** (§3.5): torch dominates, and a filling `D:` drive is a known local
  failure mode. Mitigation: light default profile, visible progress.
* **A console window reappearing somewhere** (a helper, a scheduled task, a restart path) would undo
  the whole "not ugly" requirement. Mitigation: test on a clean VM with a screenshot pass at each
  step, not just a functional pass.
* **DB setup logic drifting into three places again.** Mitigation: D5, plus a CI check that the
  wizard's DB stage is the bootstrap call.
* **Docs drifting again.** Mitigation: generate `docs/oneclick.rst` from the bootstrap's `--help`
  where practical, and fail the CI smoke job if the documented URL/ports stop matching what the
  bootstrap writes.

---

## 10. Open questions

Closed since revision 1: MariaDB (deprecated, D3), engine default (not hardcoded, §3.6), the
component picker (gone, §1).

Still open:

1. **Distribution:** GitHub Releases assets for both artifacts, with the website linking to them?
2. **Signing:** start the free-for-OSS application now, or ship unsigned and revisit?
3. **Dependency profile:** may I move `torch`/`torchaudio`/`kittentts`/`vosk` into an optional extra
   so the default install is light? This changes what a fresh Docker user gets too.
4. **Setup page placement:** build it into the Vue Stage (`frontend/`), or as a classic
   `core/webui_templates/` section? Stage is prettier and is already the direction; the classic
   template is faster to ship and works without Node.
5. **Linux distros:** Ubuntu 24.04 + Debian 12 + Fedora as required, Arch best-effort. `.deb` now or
   later?
6. **Update mechanism:** re-run the installer/script, an in-app update check, or both?
7. **Port choice:** keep 8001 as the native default (what the old wizard used) or move to 8080 to
   match the container? Either way it is written to `.env` and to the shortcut, so this is only
   about what people see in the URL.
8. **Node.js:** confirm "not installed by default, offered from the WebUI when the Vessel is
   enabled" is acceptable for the Minecraft users you already have.

---

## 11. Notes on the fly

* The old installer's component page is the thing to never rebuild. Every "let the user choose"
  screen costs a support ticket and looks like 2005. One path, decided by detection.
* The setup page is the highest-leverage piece of design work in this plan: it is the first branded
  screen a user sees, it is ours, and it turns "installed some software" into "met my SyntH". Worth
  real effort on copy and layout, not just fields.
* The Docker-ism sweep is cheap and improves the repo for every existing native user immediately,
  even before any installer exists. It could ship as its own small PR ahead of P0.
* `SYNTH_IN_CONTAINER=0` in the native `.env` is a one-line insurance policy against the Minecraft
  loopback remap misfiring.
* The "press any key to close this window" pattern in `install_prereqs.ps1` and `setup_launcher.ps1`
  is the exact texture of the ugliness being complained about: it is a developer's way of not losing
  an error message, and to a user it reads as a crash.
* `windows_setup.py` is 1255 lines of wizard and most of it is the `.env` question flow (stage 3
  runs from `:574` to `:934`). Once the persona and engine questions move to the WebUI setup page,
  most of that file stops being needed, which is a good outcome: less code, one DB path.
* If the setup page lands, the installer's only remaining job is the technical half, and the
  installer can then be boring, which is what makes it reliable.
* Long term, the same `bootstrap.py` is what would make a macOS path and a `winget`/`.deb` package
  cheap later, because none of them would need a new install story.

---

## 12. Deliberately not in this plan

* macOS.
* Replacing the Docker path or changing the compose stack. Docker stays the recommended route for
  servers and stays untouched.
* Removing MariaDB from the tree. It stays for existing installs; only new installs ignore it.
* Any change to cortex, engine, prompt, or plugin behaviour. This is packaging, bootstrap, and the
  native-path fixes in §4.

---

## 13. Implementation status

Written after the build. Everything here is uncommitted working-tree changes; nothing was staged or
pushed. Validation at the time of writing: `ruff check` clean, `ty check` clean (0 diagnostics) on
every new module, 112 tests passing across the seven new test files, `scripts/check_dockerisms.py`
green, and the Windows installer compiling to a 36.8 MB `SyntH-Setup-<version>.exe`.

### Built

| Piece | State |
|---|---|
| `core/app_paths.py` | done — container-path resolution, `in_container()`, `default_bind_host()`, `usable_container_dir()` probes read-only and never `mkdir`s |
| Docker-ism sweep | done — 11 modules plus 4 interface doc examples; guard enforces it in CI |
| `core/legacy_state.py` | done — adopts Docker-era `/app` + `/config` leftovers into the data root, `--dry-run`, startup-call from `main.py` before anything reads the secret or data root |
| `scripts/bootstrap.py` | done — stdlib-only, finds or creates PostgreSQL, writes `.env`, never prompts (`-w`, closed stdin, `sudo -n`), `--portable`, `--stop-cluster`, honest degradation when `vector` is missing |
| `scripts/healthcheck.py` | done — one-line "is it up?"; reports a scheme mismatch when a running process disagrees with `.env` |
| `scripts/start_synth.py` | done — no console window, waits for readiness, opens the browser, `--status/--stop/--setup/--no-browser` |
| `install.sh` | done — distro detection, `uv`, bootstrap hand-off, `~/.local/bin/synth`, default source `XargonWan/develop` with `--repo`/`--branch` |
| `scripts/install_prereqs.ps1` | rewritten — user-scoped, no admin, no MariaDB, no dead pgvector download URL |
| `installer/synth-installer.iss` | done — one option, no component picker, no admin, no console windows, user-scoped |
| `core/webui_templates/setup.html` + `/setup` | done — classic template, first-run redirect from `index()`, thin UI over the existing config and endpoint APIs |
| `.github/workflows/build-pgvector-windows.yml` | done — builds pgvector against the exact EDB archive the installer ships |
| `build-release.yml` → `windows-installer` | done — builds the DLLs, compiles, uploads the exe |
| `pyproject.toml` extras | done — `local-voice`, `vosk`, `whisper`, `tts-local`; torch no longer a base dependency |
| `.env.example`, README, `docs/installation.rst`, `docs/windows.rst` | rewritten for Postgres-only and the native path |

### Found while building

* The installer was shipping the repository's real `.env` — a hand-written exclude list, and a
  rewrite of it dropped the `.env*` guard. `tests/test_installer_payload.py` now pins the patterns,
  including that `.env.example` must still ship. Same audit found a local report file and ~35 MB of
  the live instance's TTS cache in the payload.
* `Path("/config")` on Windows is drive-relative, so a writability probe silently *created*
  `D:\config`. Container-path probes are read-only and require `is_absolute()`.
* `initdb` refuses to run when a password file sits inside the cluster directory.
* `psql` blocked an unattended install on an interactive password prompt; `pg_ctl -w` also hangs on
  leftovers from a killed run.
* The `--source` inspection was suppressed by the migration marker; the marker stops a real run
  repeating adoption, a dry run must always be answerable.

### Known gaps

* `install.sh` has only been dry-run tested under a fake `uname` shim; it has not run on a real
  Linux box. `shellcheck` was not available, so only `bash -n` passed.
* The installer has been compiled but never run end to end. A silent install pulls ~320 MB of
  PostgreSQL plus ~2 GB of wheels, so that remains a deliberate manual step.
* The example personas (Zero, Miku, Riko) are excluded by default (`/DWithExampleSkins=1` includes
  them). That is the 148 MB → 37 MB difference; the default persona still ships.
* pgvector is absent from a build where CI has not vendored it: SyntH installs and runs, SOUL keeps
  its memory in memory, and the bootstrap says so.
* macOS is untouched, as scoped in §12.
