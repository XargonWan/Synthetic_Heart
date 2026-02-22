Commands
========

synth exposes a unified backend for slash commands that can be used from any
interface (e.g. Telegram, Discord).

General
-------

* ``/help`` – Display the list of available commands.

Context Mode
------------

* ``/context`` – Toggle context memory for forwarded messages.

Messaging
---------

* ``/cancel`` – Cancel a pending operation.

User Management
---------------

* ``/block <user_id>`` – Block a user.
* ``/unblock <user_id>`` – Unblock a user.
* ``/block_list`` – List blocked users.

Cortex Control
-----------

* ``/cortex`` – Show and select the current Cortex engine (deprecated alias: ``/llm``).  The status message now lists which engine is active for the
  live/base context as well as any ``grillo`` or ``trainer`` overrides.
* ``/cortex_live <engine>`` – Explicit alias for changing the *live* (base)
  cortex.  Same behavior as ``/cortex`` but the name makes the intent clearer.
* ``/cortex_grillo <engine>`` – Set or show the cortex engine used for grillo
  background beats.  Without an argument the command lists the current
  override and available engines.
* ``/cortex_trainer <engine>`` – Same as ``/cortex_grillo`` but targets the
  trainer scope.
* ``/model`` – View or set the active model.

Administration
--------------

* ``/last_chats`` – List recently active chats.
* ``/purge_map [days]`` – Remove chat mappings older than ``days`` (default 7).
* ``/logchat`` – Set the current chat as the log chat.
* ``/manage_chat_id [reset <id>|reset this]`` – Reset stored mapping for a chat.

