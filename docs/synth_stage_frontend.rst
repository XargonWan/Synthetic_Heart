SyntH Stage frontend
====================

**SyntH Stage** is an alternative WebUI: a standalone Vue 3 single-page
application focused on the avatar stage, chat overlay, and live-voice client
for SyntH — well suited to OBS browser sources and desktop-companion overlays.
It lives in the ``frontend/`` directory and is the one Node.js corner of an
otherwise uv-only Python repository. It is where most new frontend work is
targeted.

The classic WebUI (``core/webui_templates/`` + ``res/synth_webui/js/``) is the
official interface. It keeps working alongside Stage — both are
:doc:`Karada <animation_system>` clients that receive avatar state from the
single source of truth (:pyfile:`core/animation_handler.py`).

.. note::

   Stage must be built before it can be served: if ``frontend/dist/`` has not
   been built, the ``/stage`` route is not mounted and only the official
   (classic) WebUI remains available. The backend still starts either way.

   - **In Docker** (the default deployment): the image builds Stage
     automatically (see `Production deployment`_), so ``/stage`` is available
     out of the box — no manual step required.
   - **Outside Docker** (running SyntH directly on the host): you must build
     Stage yourself before it appears at ``/stage``. Once (and again whenever
     the frontend changes) run:

     .. code-block:: bash

        cd frontend
        pnpm install        # first time only
        pnpm build          # emits frontend/dist/

     Restart the backend afterwards; on startup look for ``Mounted /stage to
     .../frontend/dist`` in the logs to confirm it was picked up. See
     `Toolchain`_ for the Node ≥ 22 / pnpm requirements.

Architecture
------------

Stage is a pure client. It talks to the existing backend surfaces and never
bypasses the message chain or the Karada state server:

- ``/ws`` — chat messages and Karada avatar events (animation, face,
  expressions, ``tts-play`` audio commands).
- ``/api/karada/*`` — avatar state and asset access (late-join replay of the
  current animation and audio).
- ``/api/audio/stream`` and ``/api/audio/upload`` — microphone input for
  VAD/STT (see :doc:`auris_vox`).

Because Stage receives ``tts-play`` and animation commands from the Karada
state server like any other client, opening Stage while a turn originates from
another interface (for example an audio message received via Telegram) still
drives the avatar correctly.

Toolchain
---------

.. warning::

   ``frontend/`` uses Node ≥ 22 and **pnpm**. None of the Python tooling rules
   apply inside it; conversely, never run ``uv`` or ``pip`` in here.

.. code-block:: bash

   cd frontend
   pnpm install
   pnpm dev        # http://localhost:5173
   pnpm build      # emits frontend/dist/
   pnpm typecheck

The dev server proxies ``/api``, ``/ws``, ``/skins``, ``/uploads`` and
``/avatars`` to the backend. It targets ``https://localhost:8080`` by default
(self-signed TLS is accepted by the proxy, so the browser never sees it).
Override the target with:

.. code-block:: bash

   SYNTH_BACKEND_ORIGIN=https://host:port pnpm dev

Production deployment
---------------------

``pnpm build`` produces ``frontend/dist/``. The backend
(:pyfile:`core/webui.py`) mounts it at ``/stage`` on the same origin — no CORS
involved — whenever the directory exists. Visit ``https://<host>:<port>/stage``
to load it.

Docker build
~~~~~~~~~~~~~

The Dockerfile builds Stage automatically as part of the image build, so no
manual ``pnpm build`` step is needed for containerized deployments. A dedicated
``node:22-slim`` build stage (``stage_builder``) runs ``pnpm install
--frozen-lockfile`` and ``pnpm build``, then the resulting ``dist/`` is copied
into the runtime image at ``/app/frontend/dist``. The Node/pnpm toolchain stays
in the build stage and never bloats the final Python image. After
``docker compose up -d --build``, look for ``Mounted /stage to
/app/frontend/dist`` in the startup logs.

URL flags
~~~~~~~~~

- ``?transparent=1`` — transparent background with minimal chrome, intended
  for OBS browser sources and desktop-companion overlays.

Switching between interfaces
----------------------------

The two WebUIs cross-link so users can move between them without editing the
URL:

- **Classic WebUI → Stage**: the *Settings* tab shows a "SyntH Stage" card with
  an *Open SyntH Stage* button linking to ``/stage/``. The card is only
  rendered when Stage is actually mounted — the backend exposes a
  ``STAGE_AVAILABLE`` flag on ``window.__SYNTH_CONFIG`` (set from
  ``self._stage_mounted`` in :pyfile:`core/webui.py`), and the settings JS hides
  the card when the flag is false, so the link never 404s.
- **Stage → Classic WebUI**: the settings drawer has an *Interface* section with
  an *Open classic WebUI* button linking to ``/``. No flag is needed — the
  classic WebUI is always served at the root path.

API token gate
--------------

The Karada REST router (``/api/karada/*``), the audio endpoints
(``/api/audio/upload``, ``/api/audio/stream``) and the ``/ws`` WebSocket can be
protected with an optional bearer token. This is off by default (the classic
WebUI has no auth layer today).

Set the environment variable ``SYNTH_WEBUI_API_TOKEN`` to require a token on
every request. When set, clients must present it as either:

- ``Authorization: Bearer <token>`` header, or
- ``?token=<token>`` query parameter (used by the WebSocket handshake).

Stage stores the token client-side and attaches it to REST calls and the
WebSocket connection automatically. See
:pyfile:`core/karada_api.py` (``_configured_api_token`` /
``_require_api_token``) for the implementation.

.. note::

   ``SYNTH_WEBUI_API_TOKEN`` is read from the environment, not the config
   registry. Set it in your ``.env`` / compose environment. Comments must go on
   their own line — inline ``#`` comments in ``.env`` values can be injected
   verbatim by some IDE-launched shells.

Live voice and barge-in
------------------------

Stage includes a microphone capture pipeline and a playback manager that
supports sentence-streamed TTS and **barge-in** (interrupting an in-flight
spoken turn when a new one arrives). Audio playback state is owned by the audio
store, which coordinates with the lipsync driver so the avatar's mouth follows
whatever is currently speaking.

.. warning::

   Microphone access requires a **secure context**. It works over
   ``https://`` and on ``localhost`` / ``127.0.0.1`` (browsers treat loopback
   as secure), but plain-HTTP LAN access (``http://<lan-ip>:<port>/stage``) has
   no ``navigator.mediaDevices`` and the mic button will fail. Serve Stage over
   TLS for LAN use.

Attribution
-----------

Portions of Stage are ported from
`Project AIRI <https://github.com/moeru-ai/airi>`_ (MIT). See
``frontend/NOTICE.md`` for the license text and the per-file port table.
