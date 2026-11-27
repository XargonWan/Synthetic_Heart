Fluxer Interface
================

The Fluxer extension (``official/fluxer``, source in ``<plugins repo>/extensions/fluxer/``) lets
synth participate in `Fluxer <https://fluxer.app>`_ chat servers. Fluxer is a
self-hostable, Discord-compatible chat platform, so the extension speaks the same REST +
gateway (WebSocket) protocol and hands every incoming message to the Core, which routes
it through the standard message chain: the active persona, plugins and memory systems
respond exactly like they do on Telegram, Discord or Matrix.

It is an **extension**: a separate process that reaches the Core only through the
extension gateway (see :doc:`plugin_platform/extension_runtime` and
:doc:`plugin_platform/extension_developer_guide`). It imports nothing from the Core.

Overview
--------

- **Native Python client**: a small aiohttp REST client plus a WebSocket gateway client,
  with aiohttp bundled in the package. No .NET runtime or ``discord.py`` dependency.
- **Optional**: install and switch the extension on; until ``FLUXER_TOKEN`` is set it
  stays idle. Removing it affects no other interface.
- **Self-host friendly**: every endpoint (REST base, gateway URL, API version) is a
  setting, so it works against the public Fluxer service or a private deployment.
- **Interface path**: ``fluxer_bot/<guild>/<channel>`` in a server and
  ``fluxer_bot/<channel>`` in a direct message. The *channel* is the chat; the Core
  reads that from the placeholders the manifest declares, never from path positions.

Prerequisites
-------------

1. Create a bot account on your Fluxer server and copy its bot token.
2. (Self-host only) note the REST API base URL and gateway WebSocket URL of your
   deployment.

Settings
--------

The extension's settings are declared in its manifest and edited in the WebUI
(Plugins > Fluxer):

.. code-block:: text

   FLUXER_TOKEN=...                                      # bot token (required, secret)
   FLUXER_API_BASE_URL=https://api.fluxer.app/v{v}       # {v} is replaced with FLUXER_API_VERSION
   FLUXER_GATEWAY_URL=wss://gateway.fluxer.app/?v=1&encoding=json
   FLUXER_API_VERSION=1
   FLUXER_USE_SYNTH_AVATAR=true                          # set the synth avatar as the bot's picture

The extension declares the network destinations it uses (``*.fluxer.app:443``); a
self-hosted instance is the operator's to add. Without a valid token it logs a warning
and stays idle; a token the gateway refuses (close code ``4004``) marks the interface as
unable to deliver, with the reason shown in the WebUI.

Trainer access
--------------

Set the trainer's Fluxer user id with the standard per-interface trainer configuration
(``TRAINER_IDS``). Numeric and string ids are accepted, so Discord-style snowflakes work.

Message flow
------------

1. The gateway client connects, receives ``HELLO`` and starts a heartbeat, then sends
   ``IDENTIFY``; ``READY`` tells it the bot's own user id so it never answers itself or
   another bot.
2. Each ``MESSAGE_CREATE`` becomes ``interfaces.inbound`` on the extension API, with the
   channel as ``chat_id``, and is queued by the Core on the shared ``message_queue``.
3. Slash commands are handled by the Core's command registry; everything else goes to the
   LLM.
4. Replies come back as ``interface.send_message`` calls; the extension posts them to
   ``/channels/{id}/messages``, quoting the original message when it can, and uploads files
   with a Discord-style multipart request.
5. The gateway reconnects with exponential backoff and resumes the session when possible.

Disabling
---------

Switch the extension off (or clear ``FLUXER_TOKEN``). No other component is affected.
