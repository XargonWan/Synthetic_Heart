Fluxer Interface
================

The Fluxer interface lets synth participate in `Fluxer <https://fluxer.app>`_
chat servers. Fluxer is a self-hostable, Discord-compatible chat platform, so
the interface speaks the same REST + gateway (WebSocket) protocol and routes
every incoming message through the standard synth message chain — the active
persona, plugins, and memory systems respond exactly like they do on Telegram,
Discord, or Matrix.

Overview
--------

- **Native Python client**: a small embedded aiohttp REST client plus a
  WebSocket gateway client. No .NET runtime or ``discord.py`` dependency — this
  keeps the interface working inside the slim ``python:3.12`` container.
- **Optional component**: the interface only becomes active once
  ``FLUXER_TOKEN`` is configured; without a token it stays visible but inert.
- **Fully decoupled**: removing ``interface/fluxer_interface.py`` or leaving the
  token empty does not affect any other interface.
- **Self-host friendly**: every endpoint (REST base, gateway URL, API version)
  is configurable, so the same interface works against the public Fluxer
  service or a private deployment.
- Uses the shared ``message_queue`` and the ``interface_paths`` name-resolver
  registry so prompt context, diary entries, and thread metadata flow through
  the same path as the other transports.

Prerequisites
-------------

1. Create a bot account on your Fluxer server and copy its bot token.
2. (Self-host only) note the REST API base URL and gateway WebSocket URL of your
   deployment.

Configuration
-------------

All values are managed through the config registry and are also exposed in the
WebUI *Interfaces* section. They can be seeded via environment variables.

Required
~~~~~~~~

.. code-block:: bash

   FLUXER_TOKEN=Bot xxxxxxxxxxxxxxxxxxxx   # bot token (the "Bot " prefix is added automatically if omitted)

Optional
~~~~~~~~

.. code-block:: bash

   FLUXER_API_BASE_URL=https://api.fluxer.app/v{v}          # {v} is replaced with FLUXER_API_VERSION
   FLUXER_GATEWAY_URL=wss://gateway.fluxer.app/?v=1&encoding=json
   FLUXER_API_VERSION=1                                     # integer

The ``{v}`` placeholder in ``FLUXER_API_BASE_URL`` is substituted with
``FLUXER_API_VERSION`` at load time. All three optional keys ship with defaults
pointing at the public Fluxer service; override them only for a self-hosted
instance. Changing any of the four keys triggers a hot reload and reconnects the
gateway — no container restart is needed.

Authentication notes
~~~~~~~~~~~~~~~~~~~~~~

- **REST** calls send the token with a ``Bot `` prefix (added automatically if
  you did not include it). User tokens beginning with ``flx_`` are sent as-is.
- **Gateway** IDENTIFY uses the *raw* token with any ``Bot `` prefix stripped,
  matching the Discord-compatible protocol.

Trainer Access
--------------

Set the trainer's Fluxer user id via the standard per-interface trainer
configuration. The interface accepts both numeric and string ids, so
Discord-style snowflake ids work unchanged.

Actions
-------

The interface registers two actions (both automatically re-exposed as MCP tools
``synth_message_fluxer_bot`` and ``synth_send_file_fluxer_bot``):

.. list-table::
   :header-rows: 1
   :widths: 30 30 40

   * - Action
     - Required fields
     - Optional fields
   * - ``message_fluxer_bot``
     - ``text`` and (``channel_id`` **or** ``interface_path``)
     - ``reply_to_message_id``
   * - ``send_file_fluxer_bot``
     - ``path`` and (``channel_id`` **or** ``interface_path``)
     - ``caption``

``send_file_fluxer_bot`` is ``security_level: "medium"`` with
``external_effects: ["filesystem"]``, so the Agentic Runtime router sends it to
the Agent Lane automatically. The source path is confined to the shared
outbound-file sandbox (see :doc:`agentic_tools`).

Message Flow
------------

1. The gateway client connects, receives ``HELLO`` (op 10) and starts a
   heartbeat loop, then sends ``IDENTIFY`` (op 2).
2. On ``READY`` it caches the bot's own user id (also confirmed via
   ``GET /users/@me``) so synth never replies to its own messages.
3. Each ``MESSAGE_CREATE`` dispatch is turned into an ``interface_path`` of the
   form ``fluxer_bot/<guild_id>/<channel_id>`` (DMs omit the guild level) and
   enqueued on the shared ``message_queue``.
4. Slash-style ``/`` commands are handled by the command registry; everything
   else is routed to the LLM.
5. Outbound replies are sent via ``POST /channels/{id}/messages``; files use a
   Discord-style multipart upload.

If the gateway connection drops it reconnects with exponential backoff and
resumes the session (op 6) when possible. Close code ``4004`` (authentication
failed) is treated as fatal and stops the reconnect loop.

Disabling
---------

To disable the interface, clear ``FLUXER_TOKEN`` (WebUI or config registry) or
remove ``interface/fluxer_interface.py``. No other component is affected.
