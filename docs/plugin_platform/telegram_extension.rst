Telegram bot extension (M4)
===========================

``<plugins repo>/extensions/telegram_bot/`` is the first interface rewritten on the platform:
the Telegram **bot** (BotFather token) as an extension, ``official/telegram_bot``.
It replaces ``interface/telegram_bot/`` (3,470 lines, 30 Core modules) with about
600 lines that import nothing from the Core. The userbot (``interface/telegram``)
is not migrated yet.

Layout
------

* ``synth-extension.toml``: id, the ``telegram_bot`` interface and its path pattern
  ``telegram_bot/{chat}[/{thread}]``, capabilities (``log``, ``config``, ``stats``,
  ``interfaces.register``, ``interfaces.inbound``), the network destination
  ``api.telegram.org:443`` and the configuration keys ``BOTFATHER_TOKEN``
  (sensitive), ``TELEGRAM_API_URL`` and ``POLL_TIMEOUT``.
* ``ext_main.py``: ``main(ctx)``: reads its config, starts a ``Bot``, registers the
  interface, long-polls ``getUpdates``.
* ``convert.py``: pure functions (path, thread, reply, media references, text
  splitting). Unit-tested without a network.
* ``lib/``: ``python-telegram-bot==22.6`` and its dependencies, built with
  ``uv pip install --target lib`` at packaging time (git-ignored). The launcher
  puts ``install/lib`` on ``sys.path``; the Core's environment is not used.

Behaviour
---------

* **Inbound**: text, captions and files (largest photo size, document, voice,
  audio, video, video note, animation, sticker). Files are downloaded into the
  extension's inbox and handed over with ``interfaces.inbound``; the Core copies
  them and the extension deletes its own copy. A file that cannot be downloaded
  (or is over 25 MiB) does not lose the text. Messages from other bots and
  service messages are ignored. Forum topics become the thread level of the path;
  the ``message_thread_id`` Telegram puts on plain replies in ordinary groups does
  not. The forum topic opener is not reported as a reply. ``human_count`` is the
  member count minus the bot, cached for ten minutes.
* **Outbound**: replies quote the incoming message and stay in its topic. Private
  chats use Markdown, groups plain text (so ``*action*`` markers survive for other
  readers). Text over 4,096 characters is split at line or word boundaries;
  captions over 1,024 go as their own message. Files go through the matching Bot
  API method (``send_voice`` when the Core asks for a voice note). When Telegram
  rejects a send the extension sheds what is stale in order: broken Markdown, a
  deleted reply target, a deleted topic. Flood control is waited out once (up to
  30 s). A ``reply_to`` that is the chat id or not a number, and a non-numeric
  topic, are dropped instead of failing the send.
* **Names**: ``getChat`` title through ``interface.resolve_names``. Topic names
  are not available from the Bot API.
* **Failure**: a missing token leaves the extension idle (no restart loop); a
  refused token is logged once and the extension waits; network errors back off
  from 1 s to 60 s without restarting the process.

Tests
-----

``tests/test_extension_telegram_bot.py`` runs the real extension as a sandboxed
process against ``tests/support/fake_telegram.py``, a fake Bot API server (form and
multipart bodies, long polling, file download, scripted failures) that
python-telegram-bot talks to unmodified, with the real gateway, interface API and
media bridge on the Core side. ``tests/test_extension_telegram_convert.py`` covers
the pure helpers and checks that no extension file imports the Core. The ``lib/``
is built per session with ``uv pip install --offline``; the tests skip when that
is impossible. Mutation checks (removing each fallback, the inbox cleanup, the bot
filter) make the relevant test fail.

What is not migrated
--------------------

``interface/telegram_bot`` was deleted in M5 (``git log --diff-filter=D --
interface/telegram_bot`` finds it). These behaviours of the old module are **not**
in the extension; each either needs a Core API that does not exist yet or was a
Core feature reached through Telegram objects.

.. list-table::
   :header-rows: 1
   :widths: 28 72

   * - Feature
     - State
   * - Reactions (``REACT_WHEN_MENTIONED``, wake/sleep emoji)
     - Needs a reaction call from the Core to the extension (like ``activity``).
       Not done.
   * - Avatar (``set_avatar``)
     - Needs a decision on who owns the avatar. Not done.
   * - Voice and video notes
     - Passed to the Core as audio/video attachments and flagged ``is_voice_input``,
       so the Core answers them with speech where the interface is declared
       ``voice_input_tts_only``. The old module's own live-API transcription path
       is not ported: transcription is whatever the Core does with an audio
       attachment. A voice note that is forwarded, or sent with a caption, is
       material the user hands over, not speech: the Core drops the flag.
   * - Forwarded messages (text, media or voice)
     - Passed as ``forwarded_from`` (who first said it, and when). The Core tells the
       model it is not the user's own words, and when the user forwards their own
       earlier message, that it is not being said now. Same on the bot and the userbot,
       and on any interface that sets ``forwarded_from``.
   * - Reply-to-media promotion (a reply to a photo carries that photo)
     - Not ported.
   * - Trainer direct messages while the chat sleeps
     - The old module let a trainer's private message through a sleeping chat. The
       Core's queue does not; the rule belongs in the Core if it is wanted.
   * - Peer mode, relay and message map, block list
     - Peer ids are declared (``peer_ids``) and the Core's peer policy uses them.
       The relay, message map and block-list commands were module features and are
       gone; decide per feature whether it returns as a Core feature.
   * - Delivery-failure record and the corrector notice
     - The extension returns ``ok: false``. The failure log and the "repeat with an
       explicit chat id" notice are the Core's job; not wired for remote interfaces.
   * - Weather-report sleep bypass
     - Deliberately not ported: it matched English phrases.

Done in M5 on the Core side, so the extension needs nothing of its own for them:
chat commands (``/sleep``, ``/wake``, ``/status``, ``/interface_path``, ``/logchat``)
read the chat from the message; wake and sleep phrases apply before queueing;
``sender_username``/``chat_username`` teach the Core ``@handle`` aliases;
``activity`` shows "typing..." and "recording..."; the traits above replace the
Core's hardcoded interface lists.


The user account (``official/telegram``, M4b)
=============================================

``<plugins repo>/extensions/telegram/`` replaces ``interface/telegram/`` (4,600 lines, deleted):
the Telethon **user account**, with voice and video calls. About 1,900 lines, none of
them importing the Core. ``telethon`` and ``py-tgcalls`` live in its own ``lib/``.

Layout: ``convert.py`` (pure helpers), ``tl.py`` (the only module that touches Telethon:
reaching a user by id or ``@username``, sending, reactions, avatar, reading back),
``login.py`` (phone, code, two-step password), ``calls.py`` (py-tgcalls), ``ext_main.py``
(wiring).

* **Login** is a panel the Core draws (no JavaScript from the extension). The session
  string goes into the extension's private storage, never into a log or the browser; code
  and password are never logged or echoed. The interface registers **disabled** ("login
  required") until connected, so the panel is always reachable; logging out forgets the
  session.
* **Inbound** has the same shape as the bot's, plus: the message's own time; messages
  older than ``TELEGRAM_BACKLOG_MAX_AGE_SECONDS`` are stored as history only (a command in
  an old message is not run); at start the chats active in the last
  ``TELEGRAM_RECOVERY_WINDOW_SECONDS`` are read back and what the Core does not have yet
  is handled once; a message delivered twice is handled once; forum topics are the thread.
* **Outbound** is the bot's ladder (stale reply, stale topic, broken markup, flood
  control) over Telethon. A user account can only write to people it has *seen*: the
  account searches its dialogs and shared groups once for an unknown id and remembers a
  miss for ten minutes; ``telegram/@username`` is resolved to the numeric id.
* **Also**: typing and recording indicators, reactions, chat titles, the synth avatar
  (only when ``TELEGRAM_USE_SYNTH_AVATAR`` is on, never deleting a picture it did not
  set), and the ``answer_call_telegram`` action.
* **Calls**: the trainer and ``TELEGRAM_CALL_ALLOWED_IDS`` are answered at once; anyone
  else rings while the trainer is asked, and the model answers or declines with
  ``answer_call_telegram`` within 45 seconds. The Core's tone is the stream that answers
  the call; without one a short silence is. The caller's voice goes to the Core in
  100 ms chunks; replies are played from a private copy kept until they finish; the
  SyntH's face is pushed with ``send_frame`` and a library that keeps refusing frames
  drops the call to audio.

What was **not** verified: everything that needs a real Telegram account. The fake
Telethon and the fake py-tgcalls follow the libraries' documented names; their real
behaviour (the incoming-audio format, the end-of-call status names, ``record`` on
private calls, the I420 frames) is as unverified as it was in the module this replaces,
and the two settings that exist to adjust it (``TELEGRAM_CALL_RX_RATE`` /
``TELEGRAM_CALL_RX_CHANNELS``) are kept.

Not carried over from the in-process interface: the trainer's media-for-a-pending-
response flow, replies relayed through the trainer, peer turn coordination in shared
groups (the Core's peer policy still works from the declared peer ids), the live-API
transcription of voice notes (a voice note is passed as an audio attachment and flagged
as spoken) and the English phrase list that let some weather questions through a
sleeping chat. The old settings (``TELEGRAM_API_ID``, the session...) are carried over at
the first start after the update: see the post-update step in :doc:`migration_plan`.
