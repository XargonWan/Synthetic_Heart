Telegram User-Account Interface
===============================

The ``telegram`` interface (``interface/telegram/``) lets SyntH use a **real
Telegram account** — a normal user with a SIM / phone number — instead of a
BotFather bot. It talks MTProto through `Telethon <https://docs.telethon.dev>`_.
Use :doc:`interfaces` ``telegram_bot`` when a bot is enough; use ``telegram``
when SyntH must appear as an ordinary contact.

.. warning::

   Automating a personal account may breach Telegram's Terms of Service and can
   get the account limited. Use a dedicated number.

Setup
-----

1. Create API credentials at https://my.telegram.org (*API development tools*).
2. In the WebUI open the **Telegram (User)** interface and fill **Telegram API
   ID** and **Telegram API Hash** (``TELEGRAM_API_ID`` / ``TELEGRAM_API_HASH``),
   save and reload the interface.
3. In the **Account login** card enter the phone number (``+<country><number>``),
   press *Send code*, enter the code Telegram sends and, if two-step
   verification is enabled, the account password.
4. Optionally add ``telegram:<your_user_id>`` to ``TRAINER_IDS``.

The login result is stored as a Telethon ``StringSession`` in the hidden,
read-only ``TELEGRAM_SESSION`` config key. It is written only by the login flow
(never through ``POST /api/config``, which logs payloads) and gives full access to
the account: treat the database like a secret. *Log out* in the card terminates
the session on Telegram's side and clears the key. While no session exists the
interface shows a grey "Telegram login required" state.

If ``SYNTH_WEBUI_API_TOKEN`` is set, the login routes
(``/api/telegram/login/*``) require the same bearer token as the other protected
WebUI endpoints. Codes and passwords are never logged.

Behaviour
---------

- **Interface path:** ``telegram/<chat_id>[/<topic_id>]`` (Bot-API style chat ids:
  negative for groups). Forum topics map to the thread id.
- **Routing:** a user account sees *every* group message, so the usual
  addressing rules (alias, ``@username``, reply, wake/sleep via
  ``core.chat_attention``) decide when SyntH answers. Private chats are always
  directed.
- **Features:** text, replies, media (photos, documents, audio, video, stickers),
  voice notes (Auris transcription in, ``send_as_voice`` out), reactions,
  commands via the central command registry, trainer relay, peer-turn
  coordination and delivery-failure recording — the same flow as ``telegram_bot``.
- **Implementation note:** Telethon objects are wrapped in python-telegram-bot
  shaped classes (``interface/telegram/_compat.py``) and Telethon errors are
  translated to PTB exceptions, so the shared helpers
  (``message_queue``, ``mention_utils``, ``message_send_utils``) are reused
  unchanged.

Avatar
------

With ``TELEGRAM_USE_SYNTH_AVATAR`` enabled the account's profile picture is
the core synth avatar (see :doc:`synth_avatar`), applied at start-up and every
time the avatar changes. It is off by default because a real account's picture
is visible to all its contacts. Removing the synth avatar deletes the account's
current picture only if this interface set it.

Configuration keys
------------------

==============================  ====================================================
Key                             Purpose
==============================  ====================================================
``TELEGRAM_API_ID``             API id from my.telegram.org (sensitive)
``TELEGRAM_API_HASH``           API hash from my.telegram.org (sensitive)
``TELEGRAM_PHONE``              Account phone number, set by the login card
``TELEGRAM_SESSION``            Stored session (hidden, written by the login flow)
``TELEGRAM_USE_SYNTH_AVATAR``   Opt in to the core synth avatar as profile picture
``TRAINER_IDS``                 Add ``telegram:<user_id>`` for trainer features
==============================  ====================================================
