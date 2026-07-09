Matrix Interface
================

The Matrix interface lets synth participate in Matrix rooms using the
``matrix-nio`` asynchronous SDK. Once configured, any message that arrives in
your selected rooms is routed through the standard synth message chain, so the
active persona, plugins, and memory systems respond exactly like they do on the
other transports.

Overview
--------

- Optional component: the interface only activates when the required packages
  are installed and the environment variables are present.
- Fully decoupled from the core: removing ``interface/matrix_interface.py`` or
  skipping the configuration does not affect other interfaces.
- Uses the shared ``message_queue`` and the ``interface_paths`` name-resolver
  registry so prompt context, diary entries, and thread metadata flow through
  the same path as Telegram or Discord.

Prerequisites
-------------

1. Create (or choose) a Matrix account that the bot will use. Record the
   homeserver URL, the full MXID (e.g. ``@synth-bot:matrix.org``), and either
   the account password or an access token.

Environment Variables
---------------------

Set the following variables before starting synth. All values are strings unless
otherwise noted.

Required
~~~~~~~~

.. code-block:: bash

   MATRIX_HOMESERVER=https://matrix.example.org
   MATRIX_USER=@synth-bot:matrix.example.org   # full MXID

   # Pick exactly one authentication method
   MATRIX_PASSWORD=supersecret                 # account password
   # MATRIX_ACCESS_TOKEN=xxxx.yyyy.zzzz        # alternative if you prefer tokens

Optional
~~~~~~~~

.. code-block:: bash

   MATRIX_DEVICE_ID=SyntHMatrix01                # reuse an existing device ID
   MATRIX_DEVICE_NAME="synth Matrix Interface" # label that appears in clients
   MATRIX_STORE_PATH=/var/lib/synth/matrix-store # nio store for sync tokens
   MATRIX_ALLOWED_ROOMS=!roomid:example.org,#alias:example.org

``MATRIX_ALLOWED_ROOMS`` accepts a comma-separated list of room IDs or aliases.
When provided, the interface ignores any other rooms and will not respond to
invites automatically.

Trainer Access
--------------

The global ``TRAINER_IDS`` variable supports Matrix directly. Append the
interface name and user ID to the existing list, e.g.:

.. code-block:: bash

   TRAINER_IDS="telegram_bot:31321637,matrix_chat:31321637"

Only the trainer can bypass rate limits or issue protected commands from Matrix.

Starting the Interface
----------------------

1. Export the environment variables (via ``.env`` or your process manager).
2. Restart synth. During startup, the interface registers itself and schedules a
   background sync task.
3. Watch ``logs/dev/synth.log`` for confirmation:

   .. code-block:: text

      [matrix_interface] Matrix interface registered
      [matrix_interface] Sync loop scheduled

4. Invite the bot to a room or add it to the allow list; messages start flowing
   through the queue immediately.

Checking Delivery
-----------------

- Incoming messages should appear in the log with the interface ID
  ``matrix_chat`` during mention detection or rate-limit checks.
- Outgoing replies use the ``message_matrix_chat`` action. You can confirm by
  searching for the action name in the debug logs.

Troubleshooting
---------------

Missing dependency
   ``matrix-nio`` is optional; if it is not installed you will see
   ``matrix-nio dependency missing`` in the logs and the interface will stay
   disabled. Install it via ``uv sync`` (or `pip install matrix-nio`).

Login failures
   Ensure the account credentials are correct. When using an access token, leave
   ``MATRIX_PASSWORD`` unset.

Room not receiving messages
   Confirm that the room ID or alias is listed in ``MATRIX_ALLOWED_ROOMS`` (if
   you set the variable). Otherwise, invite the bot user and check your homeserver
   logs for permission errors.

Interface not listed
   Run ``python main.py --status`` (or open the startup summary) to verify that
   ``matrix_chat`` appears in the active interfaces list. If it does not, double
   check the environment variables and restart.

Uninstalling
------------

To remove Matrix support, simply delete (or rename)
``interface/matrix_interface.py`` and remove the environment variables. synth
will continue operating with the remaining interfaces.
