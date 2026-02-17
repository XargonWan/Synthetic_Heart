Changelog
=========

2026-02-17 - Improvements
-------------------------

- Add: conservative, centralized runtime DB recovery that auto-creates missing tables/columns and retries failing statements on schema errors. Controlled via `DB_AUTO_HEAL` (default `1`). (see `core/db.py`, `tests/test_db_auto_heal.py`)
- Add: unit tests for DB auto-heal behavior and documentation entry in `docs/compose_env_vars.rst` and `.env.example`.
- Fix/Hardening: make X11 wait non-blocking at container startup and optionally create a placeholder socket to allow headless startup. Controlled by `SYNTH_X11_WAIT_SECONDS` and `SYNTH_CREATE_X11_PLACEHOLDER`. (see `webtop/s6-services/synth/run`)

2025-12-15 - Bugfixes
---------------------

- Fix: WebUI chat resizing regression. Made the client-side `CHAT_RESIZABLE` variable mutable and exposed the `createChatResizeHandles()` function so runtime toggling and edge/corner resizing works as expected. (res/synth_webui/js/vrm-viewer.mjs)
- Fix: Discord interface routing bug where `interface_path` parsing could pass a list as channel id, causing send failures. Correctly unpack the parsed levels to extract the channel/thread id. (interface/discord_interface.py)
