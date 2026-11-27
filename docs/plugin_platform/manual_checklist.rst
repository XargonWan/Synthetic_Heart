Manual Test Checklist
=====================

Things the automated tests cannot establish: every item below was exercised only against
fakes (fake servers, real extension processes), never against the real service. Tick them
on a real installation.

First start and migration
-------------------------

#. Fresh install (empty database): the Engines tab lists the voice engine ``kitten``, the
   hearing engine ``vosk`` and the cortex endpoint ``zen-llm-engine`` without anyone adding them;
   the first spoken reply downloads the KittenTTS model (about 25 to 80 MB) and is heard.

#. Update an installation that used the old plugins; start it. The defaults
   (``synth_stage``, ``synth_dashboard``, ``openai_api``, ``fluxer``, ``telegram_bot``,
   ``zen``, ``protocol_harmony``, ``protocol_fish``) appear in Plugins > Installed, switched
   on and granted what they declare.
#. Old settings carried over: ``BOTFATHER_TOKEN``, ``TELEGRAM_*`` and the login session,
   ``FLUXER_*``, ``OLLAMA_*`` become the extensions' settings; features that were never on
   stay off.
#. Matrix: stored ``matrix_chat/...`` paths, ``MATRIX_CHAT_*`` keys and the trainer id are
   renamed to ``matrix``; old room history still shows up.
#. Rebuild the Docker image: the bundled extensions are installed offline at first start.
#. Zen: no endpoint is seeded by the Core; the Zen extension registers its own, and an
   installation with no endpoint at all shows the explicit "no cortex" message.

Interfaces
----------

#. Telegram bot and userbot: send and receive text, media, threads, a call; aliases still
   resolve.
#. Fluxer and OpenAI API server (``/v1/models``, ``/v1/chat/completions``, SSE).
#. Matrix against a real homeserver: login by password and by token, room and thread
   replies, the trainer, the avatar.
#. Discord is still the in-tree interface: confirm nothing regressed after the Core name
   clean-up (text, voice, Live).

Extensions that were Core code
------------------------------

#. Radio host against a real AzuraCast station with a model and a voice engine: it speaks
   between tracks, the Radio window in Activity shows the last remarks with replay, the
   radio status reaches the prompt when you ask about the radio, ``ffmpeg`` is present.
#. Dashboard window in the "More" menu.
#. Harmony and Fish Audio endpoints: add one from the wizard, list models and voices, add a
   Fish voice by its URL, synthesise a sentence, check the Vox and Auris engines that use
   them (they run from worker threads).
#. KittenTTS and Vosk extensions (installed by default): the first sentence downloads the
   model, a voice reply is heard, the voice picker lists Bella..Leo, a voice note you send is
   transcribed. A spoken reply that reaches Telegram (no call open) arrives as a **voice note**,
   not as a ``vox_*.wav`` file (the userbot too; with ffmpeg present).
#. Stage still loads from its extension.

Publication
-----------

#. Run the release pipeline on GitHub in the official plugins repository (secret
   ``SYNTH_PUBLISHER_KEY``, variable ``SYNTH_CORE_REF``): ``latest-develop`` gets one zip per
   extension and a signed ``synth-repo.json``; install from it on a clean machine.
#. Plugins > Store lists the catalog; install, update and remove one extension.

Platform
--------

#. An extension that asks for a capability it did not declare is refused; one that was not
   granted is refused; revoking a grant stops it at the next call.
#. A crashing extension restarts with backoff and does not touch the Core.
