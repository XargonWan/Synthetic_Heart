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

LLM Control
-----------

* ``/llm`` – Show and select the current LLM engine.
* ``/model`` – View or set the active model.

Administration
--------------

* ``/last_chats`` – List recently active chats.
* ``/purge_map [days]`` – Remove chat mappings older than ``days`` (default 7).
* ``/logchat`` – Set the current chat as the log chat.
* ``/manage_chat_id [reset <id>|reset this]`` – Reset stored mapping for a chat.

