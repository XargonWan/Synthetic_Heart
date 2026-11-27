Matrix Interface
================

The Matrix interface lets the synth take part in Matrix rooms (matrix.org or your own
homeserver). It is an extension, ``official/matrix`` (sources in ``<plugins repo>/extensions/matrix/``),
that runs in its own process with the ``matrix-nio`` library bundled in it. Every message that
arrives in an allowed room goes through the standard message chain, so the persona, plugins and
memory answer as they do on any other interface.

It is not installed by default: get it from the official repository (Plugins > Catalog). An
installation that had the old built-in interface configured gets it fetched, with its
settings, the first time it starts after the update.

Settings
--------

Set in the extension's settings (Plugins). The names are the old ones, so existing values carry
over.

.. list-table::
   :header-rows: 1
   :widths: 34 14 52

   * - Setting
     - Default
     - Description
   * - ``MATRIX_HOMESERVER``
     - ``https://matrix.org``
     - Base URL of the homeserver.
   * - ``MATRIX_USER``
     - empty
     - The bot's MXID (e.g. ``@yoursynth:matrix.org``). Required.
   * - ``MATRIX_PASSWORD`` / ``MATRIX_ACCESS_TOKEN``
     - empty
     - One of the two is needed (hidden). A token is preferred for a long-running bot.
   * - ``MATRIX_DEVICE_ID`` / ``MATRIX_DEVICE_NAME``
     - empty / ``SyntH``
     - The device to reuse and the name it shows in clients.
   * - ``MATRIX_ALLOWED_ROOMS``
     - empty
     - Comma separated room ids or aliases the bot answers in; empty means every room.
   * - ``MATRIX_AUTO_JOIN``
     - ``true``
     - Join the rooms it is invited to, subject to the invite policy.
   * - ``MATRIX_INVITE_POLICY``
     - ``trainer_only``
     - ``trainer_only`` joins invitations from the trainer and the trusted users; ``allow_all``
       joins every invitation.
   * - ``MATRIX_PRIVATE_MESSAGES``
     - ``trainer_only``
     - ``trainer_only`` answers a private room (two members or fewer) only for the trainer and
       the trusted users; ``allow_all`` answers everyone.
   * - ``MATRIX_TRUSTED_USERS``
     - empty
     - Comma separated MXIDs always allowed under ``trainer_only``.
   * - ``MATRIX_USE_SYNTH_AVATAR``
     - ``true``
     - Set the synth avatar as the account's profile picture.

The trainer is the one configured for the interface (``TRAINER_IDS``, entry ``matrix``).

Paths
-----

The interface id is ``matrix`` and a path is ``matrix/<room>`` (``matrix/<room>/<thread event>``
inside a thread). The id used to be ``matrix_chat`` while the stored history used the prefix
``matrix``; both are now ``matrix``, and a startup migration renames a trainer entry and any
path stored under the old name.

Behaviour
---------

* A rich reply carries the quoted text only in its plain-text fallback; the extension reads
  that structurally so the model sees what the user replied to.
* Replies and threads are sent with the right ``m.relates_to``; a message to a room alias
  resolves the alias first.
* Files are uploaded to the media repository and sent with the right ``msgtype`` (image, audio,
  video or file); the caption follows as a text message.
* The typing indicator shows while the synth works.
* The sync state is kept in the extension's own data directory.

Testing
-------

.. code-block:: bash

   uv run pytest tests/test_extension_matrix.py tests/test_migrations_interface_rename.py -v
