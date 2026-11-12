Bot Ward
========

A bot answers every message it receives, and in a private chat every incoming
message is addressed to the synth. Two accounts can therefore keep each other
talking forever: the synth writes, the bot replies, the reply starts a turn, the
turn writes again. The **bot ward** (``core/bot_ward.py``) stops that in core,
for any interface, without the interface knowing about it.

It complements :doc:`peer_synths`: ``peer_policy`` handles the SyntH instances
you configured (and groups); the ward handles *any* bot in a *private* chat.

What it does
------------

For each incoming message from a bot in a private chat, ``core.message_queue.enqueue``
asks the ward for a verdict:

1. **Accept, one band below ordinary chat.** A bot's message is queued at
   ``PRIORITY_GENERAL - BOT_WARD_PRIORITY_DROP`` (``PRIORITY_AMBIENT``). A person
   is always answered first and other chats never wait behind a bot, yet the
   message is still above the background band, so it is answered and never
   cancelled when someone else writes.
2. **Pause.** Once ``BOT_WARD_MAX_MESSAGES`` of the bot's messages were accepted
   inside ``BOT_WARD_WINDOW_SECONDS``, the next one starts a pause of
   ``BOT_WARD_COOLDOWN_SECONDS``. During the pause its messages are **kept in the
   chat history** (the interface stores them before queueing) but **start no
   turn**. When the pause ends the count starts again from zero.

State is per conversation, so one chatty bot never silences another chat. It is
kept in memory: a restart forgets it, and a loop that survives one is stopped
again after another ``BOT_WARD_MAX_MESSAGES`` messages. Only *incoming* messages
are judged; the synth can still write to a bot of its own accord.

Who is a bot, what is a private chat
------------------------------------

* **Bot:** the sender's ``is_bot`` flag (Telegram user and bot interfaces, and
  Discord's ``author.bot``), or — on Telegram, where the platform enforces it — a
  username ending in ``bot`` (``Foo_bot``, ``FooBot``).
* **Private chat:** an ``interface_path`` with a single non-negative id
  (``telegram/123``, ``telegram_bot/123``, ``discord_bot/<user_id>``). Groups
  (negative id, or several levels) are out of scope.

What the model sees
-------------------

* On a turn started by a bot, ``input.payload.source`` carries ``"is_bot": true``
  and a short note ("automated account: it replies to every message, so keep it
  short and stop once the task is done"). Other turns get nothing, so there is no
  cost elsewhere.
* When a pause starts, a ``bot_ward`` system event is recorded and appears in
  ``[Recent system events]``: *conversation with bot telegram/123 paused for 30 min
  after 10 messages in 10 min*.

Configuration
-------------

=================================  =========  ============================================
Key                                Default    Meaning
=================================  =========  ============================================
``BOT_WARD_ENABLED``               ``true``   Turn the ward on or off
``BOT_WARD_MAX_MESSAGES``          ``10``     Bot messages allowed inside the window
``BOT_WARD_WINDOW_SECONDS``        ``600``    Length of the counting window
``BOT_WARD_COOLDOWN_SECONDS``      ``1800``   Length of the pause
``BOT_WARD_PRIORITY_DROP``         ``1``      Bands below ordinary chat for a bot's message
=================================  =========  ============================================

A bot you use on purpose (a game, a command-driven service) may need more than 10
messages in 10 minutes: raise ``BOT_WARD_MAX_MESSAGES`` or switch the ward off.
