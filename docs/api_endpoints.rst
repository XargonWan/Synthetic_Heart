API Endpoints Reference
=======================

This page collects all HTTP and WebSocket endpoints exposed by Synthetic Heart. It
is intended as a developer reference: if you're building an external application,
automation script, or integration, you can consult this list to discover what APIs
the server offers and how to interact with them.

The system is built on FastAPI for the WebUI and compatibility layers. Most
endpoints are authenticated implicitly by virtue of being on the local network
(or by using the same trainer ID/session system that the WebUI uses). Consult the
individual handlers for exact parameter details; the descriptions here are
leant to give you an overview of available functionality.

.. note::

   This document is automatically updated on each release. Whenever new endpoints
   are added (e.g. by new features such as the KaradaStateServer or agent tasks),
   please ensure they are listed here.

General Convention
------------------

* ``GET`` endpoints retrieve information
* ``POST`` endpoints submit data or trigger actions
* ``DELETE`` removes resources
* ``PUT``/``PATCH`` are rare and used for specific updates
* WebSocket endpoints begin with ``ws://`` or ``wss://`` and usually require a
  handshake message (e.g. the WebUI uses the ``/ws`` socket for chat,
  animation updates, etc.).

System & Configuration
----------------------

- **POST** `/api/system/restart` – restart the synth process (with confirmation).
- **POST** `/api/database/backup` – create an immediate runtime database backup and return the generated filename/path.
- **GET** `/api/config` – dump current configuration values.
- **POST** `/api/config` – update a configuration entry.
- **POST** `/api/config/{key}/upload` – upload a file for an exposed config variable.
- **GET** `/api/config/{key}/file` – download the file associated with an exposed
  config variable.
- **GET** `/api/selkies` – return Selkies adapter configuration.
- **GET** `/api/selkies/health` – health check for Selkies service.

Components
----------

- **GET** `/api/components` – report enabled/disabled status of various component
  categories (interfaces, plugins, cortex engines, etc.).
- **POST** `/api/components/dev/toggle` – enable/disable development components at
  runtime (flag resets on restart).  Payload: ``{"enabled": true/false}``.
- **POST** `/api/components/cortex` – switch the active Cortex engine. Payload: ``{"name": "<engine>"}``.
- **POST** `/api/components/cortex/login` – begin/login flow for Selenium–based
  Cortex engines. Payload: ``{"name": "<engine>"}``.
- **POST** `/api/components/run` – execute a component by name (used by the "Run
  now" button in the WebUI).

Diary & Chat History
--------------------

*Diary* refers to the internal note-taking feature; *chat history* is the
persistent conversation cache used by the WebUI and interfaces.

- **GET** `/api/diary` – list diary entries.
- **POST** `/api/diary/archive` – archive selected diary entries.
- **POST** `/api/diary/unarchive` – unarchive entries.
- **DELETE** `/api/diary/archive` – delete archived entries.

- **POST** `/api/chat/archive` – archive the current WebUI conversation. Returns
  an ``archive_id``.
- **GET** `/api/chat/archives` – list available chat archives.
- **GET** `/api/chat/archives/{archive_id}` – download a specific archive.
- **POST** `/api/chat/restore` – restore a previously saved archive into the
  current session (payload ``{"archive_id": "..."}``).
- **DELETE** `/api/chat/archives/{archive_id}` – remove an archive file.
- **POST** `/api/chat/archives/{archive_id}/rename` – rename an archive.
- **POST** `/api/chat/session_meta` – set metadata for the current session.
- **GET** `/api/chat/session_meta` – read metadata for the current session.

Historic data endpoints (used by the WebUI for timeline/history views):

- **GET** `/api/history/diary` – diary history.
- **GET** `/api/history/grillo` – Grillo activity log entries.
- **GET** `/api/history/chat` – raw chat history records.

Agent‑related APIs
------------------

(The agent subsystem allows scheduled/background AI tasks.)

- **GET** `/api/agent/tasks` – list active/past agent tasks.
- **GET** `/api/agent/tasks/{task_id}` – fetch details for a specific task.
- **POST** `/api/agent/tasks` – create a new agent task.
- **POST** `/api/agent/tasks/{task_id}/pause` – pause a running task.
- **POST** `/api/agent/tasks/{task_id}/resume` – resume a paused task.
- **POST** `/api/agent/tasks/{task_id}/cancel` – cancel a task.
- **GET** `/api/agent/proposals` – list agent action proposals waiting
  approval.
- **POST** `/api/agent/proposals/{proposal_id}/approve` – approve a pending
  proposal.

Animations, VRM & Emotion State
-------------------------------

These APIs are primarily consumed by the WebUI but are available to external
clients (e.g. the Mate Engine integration, animation tools, or custom
frontends).

- **GET** `/api/animations/{skin}/{animation_type}` – list available animations of
  a given type for a skin (``think``, ``write``, ``idle``, etc.).
- **GET** `/api/skins/{skin}/animations/{animation_type}/{animation_file}.json` –
  fetch a JSON descriptor for a specific animation file.
- **GET** `/api/skins/current_skin` – return the folder name of the currently-
  active skin (or ``null`` when a custom/uploaded VRM is in use).  Clients can
  call this before hitting the `/api/animations/{skin}/…` endpoints.
- **GET** `/api/animation_state` – query the current centralized animation state.
  Returns the Karada v2 tuple ``{state, descriptor, started_at}``.
- **GET** `/api/karada/state` – fetch the full Karada bootstrap state
  ``{vrm_model, animation, face_values, audio}``, where ``animation`` is also a
  tuple-based object containing ``state``, ``descriptor``, and ``started_at``.
- **GET** `/api/karada/animations/manifest` – list reusable descriptor ids with
  their resolved animation URLs and descriptor metadata.
- **GET** `/api/karada/animations/resolve?descriptor_id=...` – resolve one
  descriptor id into ``animation_url`` plus ``descriptor_data`` for client-side playback.
- **POST** `/api/animation_state` – request a centralized animation state change.
  Body: ``{state, session_id?, loop?, context_id?, source?}``.
- **GET** `/api/emotion_state` – retrieve current facial/emotion blendshape
  intensities.
- **GET** `/api/locations` – suggest locations for the persona (used by UI).

Temporary uploads and promotions (used by the WebUI and Mate Engine plugin):

- **POST** `/api/animations/upload` – multipart upload for a temporary animation
  (FBX/VRMA).  The server assigns an ``upload_id`` and adds the path to the
  Karada state server search paths.
- **GET** `/api/animations/uploads` – list pending temporary uploads.
- **DELETE** `/api/animations/uploads/{upload_id}` – remove a temporary upload.
- **POST** `/api/animations/promote` – move an upload into the active skin (requires
  promotion to be enabled).

WebSocket Endpoints
-------------------

- **WS** `/ws` – primary WebUI socket for chat messages, animation updates,
  face values, and other realtime events.  Clients must send a "hello" message
  containing a session token to authenticate and then listen for broadcast
  messages.
- **WS** `/logs` – debug log stream for the WebUI (enabled when ``WEB_DEBUG=1``).

Integration & Extended API
--------------------------

The Web UI exposes a handful of generalized endpoints that are useful for
external services such as Mate Engine or other automations.

- **GET** `/api/about` – basic status payload (uptime, active session count,
  system info).
- **GET** `/api/prompt_override` – returns prompt injection/override hints used by
  the UI.
- **POST** `/api/integrations/messages` – generic message ingress endpoint.  Body:
  ``{source, type, payload, metadata}``.  ``type=chat`` entries are forwarded to
  the main message chain; other types are stored for later retrieval.
- **GET** `/api/integrations/outbox?source=<source>` – retrieve queued outbound
  messages for a given integration, clearing the queue on read (use
  ``source=mate`` for Mate Engine).
- **GET** `/api/weather/current` – get a snapshot of the current weather from the
  weather plugin.  Returns ``{status, weather, last_fetch}``, where ``status`` is
  ``ok`` or ``unavailable``.

Config & Miscellaneous
----------------------

- **GET** `/api/logchat/info` – information about current logchat settings.
- **GET** `/api/history/chat` – alias for the chat history endpoint described
  above.
- **GET** `/api/skins` – list available persona skins (used by the WebUI to
  populate dropdowns and support skin uploads).
- **POST** `/api/vrm` – upload a new VRM file for the active persona (WebUI only).

Ollama‑Compatibility Server
---------------------------

When the ``openai_api_server`` interface is enabled, SyntH speaks the same
HTTP protocol as an Ollama daemon.  The following endpoints are implemented
(usually on port ``11435`` by default):

- **GET** `/api/tags` – list available models (returns `models` array).
- **POST** `/api/chat` – standard chat endpoint accepting OpenAI‑style
  ``messages`` payloads and streaming NDJSON responses.
- **POST** `/api/generate` – simplified prompt‑only endpoint.  Equivalent to a
  single-user message to `/api/chat`.
- **GET** `/v1/models` – OpenAI‑style model list (returns ``object=list``).
- **GET** `/api/models/{name}` – retrieve a single model descriptor by name or ID.

Clients familiar with the Ollama/StableLM ecosystem can point to the compat
server and immediately begin issuing requests without any code changes.

Further Reading
---------------

* The `WebUI source <https://github.com/XargonWan/Synthetic_Heart/tree/develop/res/synth_webui>`_
  contains JavaScript clients that exercise many of the above endpoints.
* Tests in ``tests/test_*_api.py`` demonstrate how to call each endpoint using
  the built‑in FastAPI test client.
* For programmatic control, use the Python helper functions in ``core/webui.py``
  (they mirror the HTTP routes).

If you add a new endpoint during development, please update this page with a
brief description and the HTTP method/URL pattern so that other developers can
find it quickly.