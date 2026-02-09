Web UI Chat Archiving
=====================

This document explains the Web UI chat archiving and session persistence behavior.

Archived operations available:

- Create (archive): `/api/chat/archive` - archives current session messages and session meta
- List: `/api/chat/archives` - lists available archives
- Load: `/api/chat/archives/{id}` - loads a specific archive
- Restore: `/api/chat/restore` - restores a previously created archive into the current session
- Delete: `/api/chat/archives/{id}` - deletes an archive
- Rename: `/api/chat/archives/{id}/rename` - rename an archive
- Session Meta: `/api/chat/session_meta` (GET/POST) - get/set session metadata such as camera rects and processing state

Notes and behavior
------------------

- The Web UI persists chat layout and position in `localStorage`, and stores session metadata (rect, camera, persist) to the server for cross-device sync.
- Archiving clears server-side persistence (chat history cache and in-memory context) and the Web UI will clear local UI state - *but* it also clears localStorage history to avoid duplicates when switching views.
- The `processing` session meta flag is used to persist the "typing" indicator across screens: this allows the UI to show that a response is still being generated even if the user changes sections, or opens the UI on another device.

Developer notes
---------------

- If you are seeing duplicate messages after archiving and switching screens, ensure the Web UI has been updated to call `persistHistory()` and clear `HISTORY_KEY` from `localStorage` when archiving.
- The session meta key `processing: bool` is set by the server when message processing starts and cleared when processing completes. The UI will re-add the typing indicator when `processing === true` on session restore.

Running integration tests
-------------------------

To run the integration test that validates the archive+restore flow and verifies the absence of duplicates across WS reconnections:

1) Ensure the dev environment is up (e.g., Docker Compose for dev):

```bash
docker compose -f docker-compose-dev.yml --env-file .env-dev up -d --build
```

2) Run the integration test (inside the container or with the dev server reachable):

```bash
# inside the container or on host if service accessible
RUN_INTEGRATION=1 venv/bin/pytest tests/integration/test_webui_archives_e2e.py -q
```


If you encounter issues with DB connection or timeouts, ensure the MariaDB service is available to the server and the environment variables in `.env-dev` are correct.
