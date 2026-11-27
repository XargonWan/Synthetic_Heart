Telegram User-Account Extension
===============================

``official/telegram`` (``<plugins repo>/extensions/telegram/``) lets SyntH use a **real Telegram
account**, a normal user with a phone number, instead of a BotFather bot. It talks MTProto
through `Telethon <https://docs.telethon.dev>`_ and can take **voice and video calls**. Use
``official/telegram_bot`` when a bot is enough; use this one when SyntH must appear as an
ordinary contact. It is an extension (a separate sandboxed process); how it works inside is
in :doc:`plugin_platform/telegram_extension`.

.. warning::

   Automating a personal account may breach Telegram's Terms of Service and can get the
   account limited. Use a dedicated number.

Setup
-----

1. Create API credentials at https://my.telegram.org (*API development tools*).
2. Fill **API ID** and **API hash** in the extension's settings.
3. Open the **Telegram (User)** interface in the WebUI. Its **Account login** card is
   drawn by the Core: enter the phone number (``+<country><number>``), press *Send code*,
   enter the code Telegram sends and, if two-step verification is on, the password.
4. Optionally add ``telegram:<your_user_id>`` to ``TRAINER_IDS``.

The login result (a Telethon ``StringSession``) is kept in the extension's private storage
and gives full access to the account: treat the database like a secret. It is never
logged and never sent to the browser; codes and passwords are never logged. *Log out*
forgets the session. Until logged in the interface shows "Telegram login required".

Behaviour
---------

- **Interface path:** ``telegram/<chat_id>[/<topic_id>]`` or ``telegram/@username``
  (Bot-API style ids: negative for groups). Forum topics are the thread.
- **Routing:** a user account sees *every* group message, so the usual addressing rules
  (alias, ``@username``, reply, wake/sleep) decide when SyntH answers. Private chats are
  always directed.
- **Backlog is history, not conversation:** a message older than
  ``TELEGRAM_BACKLOG_MAX_AGE_SECONDS`` (default 120, ``0`` = everything is live) when it
  reaches the account is stored under its **original timestamp** and nothing else: no
  reply, no command, no triggers.
- **Messages missed while offline are recovered:** after connecting, every chat active
  within ``TELEGRAM_RECOVERY_WINDOW_SECONDS`` (default 3600, ``0`` = off) is re-read and
  each incoming text message the Core does not have yet is handled once, oldest first.
  Telegram's read/unread flags are never consulted. Media-only messages are not recovered.
- **Writing first:** a user account can only message a bare user id it has seen. It looks
  once in its dialogs and shared groups, remembers a miss for ten minutes and otherwise
  fails with an error saying what to do (they message the account first, or send to their
  ``@username``).
- **Calls:** the trainer and ``TELEGRAM_CALL_ALLOWED_IDS`` are answered at once; others
  ring while the trainer is asked and SyntH answers with ``answer_call_telegram``. Written
  against py-tgcalls' documentation and **not verified on a live call**; see
  :doc:`call_service`.
- **Not carried over from the old in-process interface:** the trainer media/relay flows,
  peer turn coordination, live-API transcription of voice notes (a voice note is an audio
  attachment flagged as spoken), and a weather phrase list that let some questions
  through a sleeping chat.

Avatar
------

With ``TELEGRAM_USE_SYNTH_AVATAR`` on, the account's profile picture is the core synth
avatar (see :doc:`synth_avatar`), applied at start-up and whenever it changes. It is off
by default because a real account's picture is visible to all its contacts. Removing the
synth avatar deletes the account's current picture only if this extension set it.

Configuration keys
------------------

======================================  ========================================================
Key                                     Purpose
======================================  ========================================================
``TELEGRAM_API_ID``                     API id from my.telegram.org (sensitive)
``TELEGRAM_API_HASH``                   API hash from my.telegram.org (sensitive)
``TELEGRAM_PHONE``                      Phone number suggested in the login card
``TELEGRAM_USE_SYNTH_AVATAR``           Opt in to the core synth avatar as profile picture
``TELEGRAM_BACKLOG_MAX_AGE_SECONDS``    Older messages are history only (default 120; 0 = off)
``TELEGRAM_RECOVERY_WINDOW_SECONDS``    Re-read chats at startup for missed messages (default 3600; 0 = off)
``TELEGRAM_CALLS_ENABLED``              Let the account take voice calls (default on)
``TELEGRAM_CALL_ALLOWED_IDS``           Ids besides the trainer whose calls are answered
``TELEGRAM_CALL_RX_RATE`` / ``_CHANNELS``  Format of the caller's audio frames (default 48000 Hz, 1)
``TELEGRAM_CALL_VIDEO_ENABLED``         Show the SyntH's face in answered calls (default on)
``TRAINER_IDS``                         Add ``telegram:<user_id>`` for trainer features
======================================  ========================================================
