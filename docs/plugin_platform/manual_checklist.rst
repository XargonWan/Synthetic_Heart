Manual Test Checklist
=====================

Things the automated tests cannot establish. Every item below was exercised only against
fakes (fake servers, fake libraries, real extension processes), never against the real service
or in a browser. It covers the whole platform plan, from the first milestone to the last
piece built, and it is **kept up to date as work lands**: when something is built that only a
person can check, it is added here in the same change.

Status as of 2026-11-28. Items marked **new** came with the last work (Discord, the duplex
audio API, Gemini Live); the rest is older. Use a **test** Discord bot and server and a test
Gemini key, never the real ones.

Reminders before you start
--------------------------

#. On GitHub, in the official plugins repository, set the **secret** ``SYNTH_PUBLISHER_KEY``
   (base64 of ``~/.config/synth/publisher.key``) and the **variable**
   ``SYNTH_CORE_REF`` (``refactor/plugin-system``). Without them the release pipeline cannot
   sign or build, and a fresh install has nothing to download.
#. Nothing is pushed yet, in either repository. The image clones the plugins repository's
   ``develop``, which has none of the extensions' newest commits until you push it.
#. Rebuild the Docker image (``docker compose build``) to see any WebUI change: the frames,
   the Skins tab, the store's dialogs and progress bar were never looked at in a browser.
#. ``official/discord`` and ``official/gemini_live`` are **not defaults**: install them from
   Plugins > Catalog (Discord is fetched by the migration if the old interface was in use).

Platform
--------

#. A crashing extension restarts with backoff and does not touch the Core; a hostile one
   cannot read ``.env``, open the database or import ``core`` (it is tested with real hostile
   processes, but **the dedicated-user sandbox, ``tests/test_extensions_privileged.py``, needs
   root and has never run**: run it once).
#. An extension that asks for a capability it did not declare is refused; one that was not
   granted is refused; revoking a grant stops it at the next call. Installing grants
   nothing: the permission question is an in-app dialog, not the browser's own box.
#. ``SqlStorage`` (the extensions' storage) against **MariaDB** and **PostgreSQL**: an
   extension writes, reads and loses nothing across a restart. Only a fake was used.
#. The latency of the process boundary on the real machine (the measured 0.2 to 0.4 ms per
   turn was on a laptop with in-memory stand-ins): a normal conversation does not feel slower.
#. ``EXTENSIONS_ENABLED`` is on by default: the Core starts, and works, with **no extension
   installed** (a development folder, an empty data volume).

First start and migration
-------------------------

#. Fresh install (empty database, empty data volume, network on): the first start downloads
   the defaults from the official repository (``latest-develop``) and switches them on, granted
   what they declare: ``synth_stage``, ``synth_dashboard``, ``openai_api``, ``fluxer``,
   ``telegram_bot``, ``zen``, ``protocol_harmony``, ``protocol_fish``, ``vox_kitten``,
   ``auris_vosk``. With no network the step is retried at the next start and the Core still
   runs.
#. The Engines tab lists the voice engine ``kitten``, the hearing engine ``vosk`` and the
   cortex endpoint ``zen-llm-engine`` without anyone adding them (a fresh install keeps its
   Zen endpoint although the local engines created theirs first). The first spoken reply
   downloads the KittenTTS model (about 25 to 80 MB) and is heard; a voice note you send is
   transcribed.
#. Update an installation that used the old plugins, then start it. Old settings carried
   over: ``BOTFATHER_TOKEN``, ``TELEGRAM_*`` and the login session, ``FLUXER_*``, the old
   ``OLLAMA_*`` become the extensions' settings; features that were never on stay off, and an
   old component that was switched off is not fetched. Run it **twice**: the second start
   changes nothing.
#. Stored paths and keys renamed at startup (only unit-tested with fake cursors, never on a
   real database): ``ollama_serve/`` to ``openai_api/``; ``matrix_chat/...``,
   ``MATRIX_CHAT_*`` and the trainer id to ``matrix``; live-voice history ``discord_live_*``
   to ``live_*`` (**new**). Old conversations still show up.
#. Gemini Live (**new**): an old installation that had ``LIVE_CORTEX`` set to a Live engine
   gets ``official/gemini_live`` fetched, with ``GEMINI_API_KEY`` and the ``LIVE_*`` options
   (affective dialog, proactive audio, thinking level and budget); one with only a Gemini key
   for the LLM, or ``LIVE_CORTEX`` left on ``Default``, gets nothing installed.
#. Radio: the old ``radio_activity_log`` rows are **not** carried over (the last 40 remarks
   start empty); check nothing else of the old plugin is expected.
#. SearXNG is its own container (``synth-searxng``, started by ``docker compose up``); a
   stored ``SEARXNG_URL`` of ``http://127.0.0.1:8888`` is moved to it. Minecraft's first start
   downloads Node.js into ``/config/node`` (pinned version, a wrong hash is refused).
#. The image no longer has S6: ``tini`` is PID 1 and ``container/entrypoint.sh`` starts the
   Core. Stop, restart and ``docker logs`` behave.
#. Zen: no endpoint is seeded by the Core; the extension registers its own, and an
   installation with no endpoint at all shows the explicit "no cortex" message.

Plugins, store and the WebUI
----------------------------

#. Plugins > Installed, Catalog and Repositories. Catalog lists Available before Installed;
   install, update and remove one extension; Roll back names the version and appears only
   after an update; Turn off is only the toggle of Installed; the red **Uninstall** is in the
   detail pane of every extension-backed plugin. Repositories lists, adds and removes
   repositories (a development folder is one too); the official one shows as *built in* and
   cannot be removed; an untrusted one asks for confirmation in an in-app modal.
#. Plugins > Installed (**new**): the list on the left is the same width and look as the
   Catalog's (about 220 to 340 px, no draggable divider, no frame around the details), also
   if you had dragged it narrow before (the saved width is forgotten); the page scrolls as a
   whole; on a phone-width window it shows the list, then the details with the back button.
#. **Refresh** (**new**): Catalog and Repositories, on Plugins and on Skins, have a 🔄
   button that reads the repositories again (push a new release, press it, the new version
   shows). They also refresh by themselves after an install, update, remove, roll back, or a
   repository added or removed, with no manual reload.
#. Skins > Catalog (**new**): each skin shows its picture as the list icon and large in its
   details (a click zooms it), as the Skins tab does; **Miku** (whose preview is 4.3 MB in the
   package) shows as well as **Riko**; an installed skin keeps its picture.
#. Install and Update show a progress bar with the percentage. A plugin with
   ``screenshots/`` shows them in the catalog before install (a click shows one large).
#. An update is offered once and goes away after it is done (a bug offered it for ever).
   **Update all** counts the plugins with an update, and a failing one does not stop the
   others; each plugin has its own update button beside its switch.
#. Settings > Extensions > **GitHub access token**: with a private repository's release, an
   install works with the token and fails with a message naming where to enter it without.
#. The authors show in the plugin detail. The extension menu is called **More**, before
   **About**, and closes the collapsed menu when an entry is chosen.
#. Extensions' windows in the three slots: Activity and Logs sub-tabs, and the More menu.
   A frame (Stage, Dashboard) sits under the top bar, **hides the chat window and the
   minimized-windows stack**, and its close button is at the bottom-left. The Dashboard's
   ``prompt``/``confirm`` work inside it.
#. Activity > Queue (**new**): the turn being processed appears only once, as the card under
   *Running now* (not again in the list below); expand that card and **scroll with the mouse
   wheel** to the bottom, inside a long block and over the whole panel, while the queue
   refreshes every 3 seconds: the position is not lost, and the search box and priority filter
   stay at the top while you scroll. With one message waiting behind it, the list shows
   that one only.
#. Activity turn cards: one expandable card per turn, live in Queue and then in History; the
   card says **Done** (highlight colour) or **Error** (red) and, for a turn with no reply,
   says why (engine and reason, or that the model was not asked).
#. Add External Endpoint: the **OpenAI / Claude API** button with its provider dropdown fills
   protocol, address, name and key hint; Gemini and Harmony keep their own buttons.
#. The plugin guide text is readable (it was made smaller).

Skins
-----

#. Skins has the same Installed / Catalog / Repositories tabs as Plugins; the Catalog shows
   skins only. Install **Riko** and **Miku** from the catalog: they appear in the skin list,
   turn off and uninstall remove exactly their folder, a skin you made yourself is never
   touched, and **Rei** and **Zero** cannot be deleted. The start-up sync restores an
   installed skin on a fresh image.
#. ``official/skin_miku``: its ``readme.txt`` forbids redistribution of the model. **Decide**
   whether it stays in the public repository.

Interfaces
----------

#. **Telegram bot and userbot**: text, media, threads, aliases, a call. After the caller
   hangs up the phone does **not** ring back. A spoken reply that reaches Telegram outside a
   call arrives as a **voice note** (ogg/opus), not as ``vox_*.wav``, on both (needs
   ``ffmpeg``). Calls through py-tgcalls were never run against a live Telegram call.
#. **Fluxer** and the **OpenAI API server** (``/v1/models``, ``/v1/chat/completions``,
   ``/v1/completions``, SSE, the optional bearer key). No Ollama route answers any more.
#. **Matrix** against a real homeserver: login by password and by token, room and thread
   replies, files, the trainer, the avatar.
#. Nothing named after one interface leaks any more: after the Core's name clean-up check
   a message round trip, ``/commands`` and wake/sleep phrases on **each** interface.

Discord (**new**: the extension replaces the built-in interface)
-----------------------------------------------------------------

Use a test bot and a test server. ``official/discord`` was only tested against a fake
library and fake voice client.

#. Install ``official/discord`` (or let the migration fetch it if the old interface was in
   use). ``DISCORD_BOT_TOKEN``, ``DISCORD_BOT_USE_SYNTH_AVATAR`` and the old trainer-only
   voice flag (now ``DISCORD_VOICE_TRAINER_ONLY``) carried over; the bot comes online.
   A token that was never set does not install anything.
#. Text: a server channel, a thread (it repeats its id in the path), a direct message, a
   reply, a file in and out, a reaction, the typing indicator, the names and the avatar.
   Old stored ``discord_bot/...`` conversations still appear.
#. Voice: ``join_voice_discord`` and ``leave_voice_discord``, ``/leave``, the SyntH hears you
   (DAVE decryption, the resilience patches for discord-ext-voice-recv) and **answers aloud**
   through the classic call path (hearing, a normal turn, speech). Joining is limited by
   ``DISCORD_VOICE_TRAINER_ONLY``. Leave and rejoin, and a call that runs more than ten
   minutes.
#. A video request degrades to audio (Discord offers bots no video).

Calls and Live voice (**new**)
------------------------------

#. A classic call on the WebUI: the pick-up sound, the "tu-din" after you stop speaking, a
   normal reply spoken by Vox, the hang-up tone; in a video call the pop-out is the webcam and
   the face looks into the camera. She does not hear herself while she speaks.
#. **Gemini Live** (``official/gemini_live``, never run against the real API). Install it,
   paste a test Gemini API key, choose **gemini_live** as the Live engine (Engines > Live)
   and turn on ``CALL_LIVE_ENGINE_ENABLED``. Then, in a Discord voice channel:

   * the SyntH greets once and hears you, answers in the model's own voice, and a turn you
     interrupt stops her at once (the queued audio is dropped);
   * she calls a tool (send a message, search memory) and the call goes on while it runs; a
     failing tool is told to the model and does not hang up;
   * the conversation reaches the chat history (``live_...``), memory, and a **diary entry
     when the call ends**; a text sent to the same chat during the call is forwarded to her;
   * a call longer than ten minutes **reconnects by itself** (look for the
     ``reconnecting`` and ``connected`` lines in the log) without the call dropping;
   * with no key, a wrong key or no network, the call stays a **classic call** instead of
     failing;
   * speech detection ``manual`` (default) and ``server`` both work; affective dialog and
     proactive audio switch to the older 2.5 model;
   * known gaps: the avatar's mouth does not move in a Live call (Karada takes a file, not a
     stream), and context lines are held only while the model is generating, not while the
     channel is still playing.
#. Discord plays the model's voice as it arrives (``official/discord`` 0.2.0, ``stream=True``,
   ``call.stream``, ``call.flush``): the voice is **not choppy, not too fast or slow** (24 kHz
   mono converted to 48 kHz stereo) and a barge-in cuts it at once. Other interfaces'
   calls are unchanged: Telegram and the WebUI do not announce streaming and stay classic.

Extensions that were Core code
------------------------------

#. **Radio host** against a real AzuraCast station with a model and a voice engine: it
   speaks between tracks, the Radio window in Activity shows the last remarks with replay,
   the radio status reaches the prompt when you ask about the radio, ``ffmpeg`` is present.
#. **Dashboard** window in the More menu (settings as knobs, toggles and readouts, it edits
   settings as you).
#. **Synth Stage** loads from its extension at ``/stage/``, with avatar, chat and voice.
#. **Harmony** and **Fish Audio** endpoints: add one from the wizard, list models and
   voices, add a Fish voice by its URL, synthesise a sentence, check the Vox and Auris
   engines that use them (they run from worker threads).
#. **KittenTTS and Vosk** (default): the voice picker lists Bella to Leo, a reply is heard,
   a voice note is transcribed, ``ACTIVE_VOX_ENGINE`` and ``KITTEN_VOICE`` kept their old
   values. The image grew by about 235 MB.
#. **Facial expressions** are part of Karada now: the ``[em_NAME:INTENSITY]`` tags move the
   face, nothing happens when nobody watches, and disabling a plugin no longer turns the
   face off. The server-drawn face in a call follows the Karada face and no longer shakes
   its head by itself.

Rift Vessel
-----------

#. The Vessel connector API (``provides.vessels``, ``vessel.connector``) exists, but **no
   connector is an extension yet**: RetroXR and Minecraft are still Core code. Check that
   they and their goals, quests and stats still work after the Core clean-ups (a visit shows
   under its world in Activities, once; Minecraft is offered by ``vessel_connect``; she can
   join a named Minecraft server). Two RetroXR tests are timing-dependent on this machine.
#. When RetroXR becomes an extension (not done): the mod's URL changes to
   ``/x/<id>/ws/...``, in the mod and in the RetroXR checkout; her goals are not carried
   over; the stats series are prefixed. This needs the real mod, with you present.

Publication
-----------

#. Run the release pipeline on GitHub in the official plugins repository: ``latest-develop``
   gets one zip per extension and a signed ``synth-repo.json``; install from it on a clean
   machine. Check that a package contains no ``tests/`` folder (tests live inside each
   extension and are not shipped) and that ``gemini_live`` and ``discord`` are published but
   not installed by default.
#. The pipeline's test step runs ``ext_tests/`` **and** ``extensions/`` (each extension's own
   ``tests/``): a failing test in either stops the release.

Decisions still open (not tests)
--------------------------------

#. Legacy ``engines/external_engines/*`` (Anthropic, Gemini, OpenAI-style, OpenRouter, xAI)
   overlap the endpoint presets and are used by nothing new: retire them, engine by engine.
#. The in-tree Gemini Live manager (``core/live_session_manager.py``,
   ``plugins/live_engines/gemini.py``, ``engines/live/``) is uncalled; delete it, in a commit
   of its own, **after** the Gemini Live run above passes.
#. The OpenAI-compatible, Gemini, Anthropic and ``custom`` endpoint protocols stay in the
   Core on purpose; the prompt and tool renderers and the cortex bridge are built around
   them.
#. Known flaky tests on this machine, unrelated to the platform work: the Telegram userbot's
   process tests (``extensions/telegram/tests/test_extension_telegram_user.py``, a different
   test fails in about half of the runs, also in the layout before the tests moved) and two
   RetroXR tests (a walk simulation that finishes before the drop). A real race is possible
   in the userbot's start-up: worth a look when you test Telegram by hand.
#. Not built: moving RetroXR and Minecraft to extensions (needs the mod, a storage API use
   for goals and bases, and the Node bridge as a subprocess), and the renderer-agnostic
   Karada with the VRM renderer as an extension.
