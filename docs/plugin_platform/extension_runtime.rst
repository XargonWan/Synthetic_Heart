Extension Runtime (milestone M2)
================================

This page describes what milestone M2 **built**: the gateway, the SDK, the
launcher and supervisor, and the first API surface. Everything here exists in the
code. What is *not* verified is listed in :ref:`pp-m2-not-verified`.

.. contents::
   :local:
   :depth: 1

Switching it on
---------------

The runtime is **on by default** (decision of 2026-11-25; the Core no longer carries the
interfaces that are extensions now). It starts unless the ``EXTENSIONS_ENABLED``
configuration key is false, which stops every extension. With nothing installed it
starts nothing.

``main.py`` calls ``start_extension_runtime`` once start-up has finished, and
``stop_interfaces`` calls ``stop_extension_runtime``, so every shutdown and
restart path stops the extension processes. Both calls swallow every exception: a
failure in the runtime is logged and the Core carries on.

.. note::

   The hook is in ``main.py`` and **not** in ``CoreInitializer.initialize_all``.
   GitNexus rates ``initialize_all`` HIGH risk, and it is also called by
   ``core.config.switch_active_cortex_engine`` and ``core.action_parser.initialize_core``,
   i.e. more than once per run. A start-up hook there would run again every time the
   Cortex engine is switched. ``start_application`` and ``stop_interfaces`` are LOW
   risk (one direct caller each).

Directory layout
----------------

Under ``<data root>/extensions`` (``core.app_paths.data_root``):

.. code-block:: text

   installed/<publisher>/<name>/synth-extension.toml   code of an extension (read-only to it)
   data/<publisher>/<name>/                            its only writable place, mode 0700
   grants.json                                         capabilities the user granted, mode 0600
   uids.json                                           numeric user per extension, mode 0600

On every start the runtime **restores these permissions**
(``secure_runtime_tree``). This matters in the container: its start-up script runs
``chmod a+rwx -R /config`` and the runtime root normally lives under ``/config``.
Without the repair an extension could rewrite ``grants.json`` and grant itself
anything. The repair happens before any extension is launched; consider also
excluding the extensions directory from that blanket ``chmod``.

The wire protocol
-----------------

JSON-RPC 2.0, one JSON object per line, over the extension's ``stdin``/``stdout``
pipes (``synth_sdk/rpc.py``, shared by both ends). No port and no socket file are
opened, so there is no token to steal: the pipe **is** the identity of the caller.

* A message is at most **1 MiB**. A larger one closes the connection.
* Malformed input is answered with an error; **5 violations in a row** close the
  connection. Batches are not supported.
* At most **32** calls from one extension are handled at the same time.
* A handler exception is turned into a generic ``internal error``. No traceback,
  path or internal detail crosses the pipe.

Handshake, then calls. The extension sends ``session.hello`` with the protocol
version and its own id. The gateway checks the id against the process it
launched; a mismatch closes the connection. Calls before the handshake are
refused.

Core to extension: ``lifecycle.ping``, ``lifecycle.stop`` (with a grace period)
and ``events.deliver``. Extension to Core: the API below.

Error codes: ``-32601`` unknown method, ``-32602`` invalid parameters, ``-32001``
permission denied, ``-32002`` rate limited, ``-32003`` quota exceeded, ``-32004``
timeout, ``-32005`` connection closed, ``-32603`` internal error.

What the gateway checks on every call
-------------------------------------

In this order, in the Core, before any handler runs:

#. the handshake happened;
#. the method exists (an extension cannot name anything that is not registered);
#. the rate limit (token bucket, default 100 calls/second, burst 200);
#. the method's capability is **declared in the manifest and granted** (the grant
   store is asked each time, so a revoke is immediate);
#. the parameters match the declared shape: unknown keys are refused, types,
   lengths and patterns are enforced;
#. the handler runs with a 10 second timeout.

The gateway supplies the caller's id itself. There is no parameter by which an
extension could name another extension's data, statistics series or topic.
Refusals are recorded in ``Gateway.audit`` (bounded) and logged.

The first API surface
---------------------

.. list-table::
   :header-rows: 1
   :widths: 22 14 64

   * - Method
     - Capability
     - Behaviour
   * - ``log.write``
     - ``log``
     - A log line tagged ``[ext:<id>]`` in the Core log (levels debug to error,
       4000 characters at most).
   * - ``config.get``, ``config.all``
     - ``config``
     - Only the keys declared in the extension's own manifest. Stored in
       ``config_registry`` as ``EXT__<escaped id>__<KEY>`` (reversible escaping:
       two ids never share a key) and **never overridable by an environment
       variable**.
   * - ``storage.get/set/delete/keys``
     - ``storage``
     - Key/value JSON in the Core database, namespaced by the caller's id. Limits:
       key 256 characters, value 256 KiB, 10,000 keys, 5 MiB in total per extension.
   * - ``stats.incr/observe/gauge``
     - ``stats``
     - The existing statistics registry, series ``ext.<publisher>.<name>.<name>``.
       The prefix is added by the Core; at most 4 labels.
   * - ``events.publish``
     - ``events``
     - Publishes on ``ext.<publisher>.<name>.<topic>`` only.
   * - ``events.subscribe/unsubscribe``
     - ``events``
     - Own topics, ``core.*`` topics, and topics of extensions it declares in
       ``requires.extensions``. Anything else is refused. A slow subscriber loses
       events instead of stalling the publisher.

There is **no** method for the database, Cortex, Karada or the file system. The
message chain is reachable only through ``interfaces.inbound`` (below).

Extension storage: ``SqlStorage``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``core/extensions/db_storage.py`` stores the data in one table
``extension_storage`` keyed by ``(extension_id, SHA-256 of the key)``. Every value
is a bound parameter, no extension text is ever in the SQL string, and listing by
prefix is done in Python, so nothing depends on ``LIKE`` or collation. It uses
only ``SELECT``/``INSERT``/``UPDATE``/``DELETE`` so it needs no entry in the
per-table conflict-key translation of ``core/db_backends.py``.

Interfaces as extensions (M3)
-----------------------------

An interface extension talks to a chat platform. Its three calls into the Core
and the one call back:

.. list-table::
   :header-rows: 1
   :widths: 24 20 56

   * - Method
     - Capability
     - Behaviour
   * - ``interfaces.register``
     - ``interfaces.register``
     - Announces an interface id **declared in the manifest** (``[[provides.interfaces]]``).
       Refused if the id is not declared, or already used by any other interface
       (in-process or remote). The Core creates a ``RemoteInterface`` and registers it
       like any interface. ``send_message`` is always offered; ``send_file``,
       ``send_audio`` and ``send_voice`` can be added to receive attachments in replies, and
       ``activity`` to be told when SyntH is busy (``interface.activity``: ``typing``,
       ``record_audio``, ``upload_audio``, ``upload_file``; failures are ignored).
   * - ``interfaces.unregister``
     - ``interfaces.register``
     - Removes the extension's own interface. It cannot remove another's.
   * - ``interfaces.inbound``
     - ``interfaces.inbound``
     - A message arrived on the platform. Allowed only on an interface this session
       registered, and only on a path matching the patterns declared in the manifest
       (no pattern declared: the default ``chat[/thread]`` shape). May carry
       ``attachments`` (see below), ``sender_username`` and ``chat_username`` (the
       Core learns ``@handle`` aliases from them). A ``/command`` is answered from the
       message itself, so ``/sleep``, ``/wake``, ``/status``, ``/interface_path`` and
       ``/logchat`` work on any interface; a configured wake or sleep phrase changes
       the chat's state before the message is queued. The **Core** then
       records the history, tracks the path, answers ``/commands`` and calls
       ``message_queue.enqueue``. Returns ``{"queued": true}`` or ``{"handled": "command"}``.
   * - ``interface.send_message`` (Core to extension)
     - n/a
     - The Core asks the extension to deliver a reply. Only ``text``,
       ``interface_path``, ``reply_to`` and ``send_as_voice`` are forwarded, plus the
       id and path of the message being answered, plus ``attachments`` when files are sent. A gone or failing extension is a
       failed delivery (``False``), never an exception.

``RemoteInterface`` implements the same duck-typed contract as the in-process
interfaces, so everything downstream is unchanged: the action prompt, the
capability gate, ``_dispatch_send_message`` (a test runs the Core's real dispatcher
against a real extension process) and name resolution (optional
``interface.resolve_names``, 3 second timeout, fails soft). When the session ends,
however it ends, the interface is removed from the Core's registries; a restarted
extension registers again.

Registration goes through the same Core functions an in-process interface uses
(``register_interface``, ``CoreInitializer.register_interface``, the action-prompt
rebuild) and persists nothing: an extension starting or stopping is not the user
toggling an interface. Trainer ids from ``TRAINER_IDS`` are kept across a restart.

.. warning::

   ``interfaces.inbound`` lets the extension say **who** sent a message. Trainer
   ids are matched on those ids, so an interface extension can speak as the trainer
   **on its own interface** and run trainer commands there. It cannot do that on any
   other interface. Grant ``interfaces.inbound`` only to extensions you trust with
   that interface.

Attachments
~~~~~~~~~~~

An extension runs in another process, usually as another user, so a file cannot just be
handed over: the other side may not even be allowed to open it. Files cross only as
**copies the Core makes after checking**, and the Core never trusts a path it was given
(``core/extensions/media.py``).

**Inbound** (a user sent a photo, voice note or document). The extension saves the file
under its own data directory (``ctx.inbox_path(name)`` returns a fresh path there) and
lists it in ``interfaces.inbound``. The Core:

#. resolves the path and requires it to be **inside that extension's data directory**
   (``..`` and symlinks that lead out are refused; the directory itself is refused);
#. opens it without following a symlink, requires a **regular file** (a FIFO is refused
   without waiting on it), and, with a dedicated user, requires the file to be **owned by
   that user**;
#. re-checks the file that was *actually opened* (Linux: ``/proc/self/fd``), so a path
   swapped for a symlink between the check and the open still cannot lead out;
#. copies at most the size limit (checked while copying, not only beforehand) into a
   directory the Core owns, with a sanitised file name, and gives **the copy** to the
   message chain in ``message.attachments`` (``mime_type``, ``filename``, ``path``): the
   shape the Core's existing extractor already reads, so images, audio and documents
   reach the model like those from any other interface.

If any attachment is refused the **whole message** is refused and nothing is kept. The
extension may delete its own file as soon as the call returns. A summary
(``filename``, ``mime_type``, ``size``) is stored in the history metadata.

**Outbound** (the model attaches a file to a reply). The model chooses the path, so it is
first checked by the Core's own outbound rule (``resolve_safe_outbound_path``: allowed
roots, never credential or key files, must exist and be a regular file). The Core then
copies each file into ``<data dir>/outbox/<random>/`` (mode 0600, owned by the
extension's user), sends the copies in ``payload["attachments"]``
(``path``, ``name``, ``mime_type``, ``kind``, ``size``) and **deletes them as soon as the
extension's delivery call returns**, also if it fails. The original is never touched.
``send_as_voice`` is forwarded so the extension can send audio as a voice note.

Limits (``MediaLimits``): 25 MiB per file, 8 files and 50 MiB per message, 2 GiB of kept
inbound copies per extension. Inbound copies older than 14 days, and staged files a crash
left behind, are removed when the runtime starts. Copies run in a worker thread, so a
large file does not stall the event loop.

An extension that does not declare ``send_file``/``send_audio``/``send_voice`` gets no
attachments: the Core drops media from replies to it and tells the model, as for any
interface lacking the capability.

Calls and live voice are in "Interface extras" below.

Interface extras
~~~~~~~~~~~~~~~~

Beyond send/receive, an interface extension can use these (each is an optional
capability it announces in ``interfaces.register(capabilities=[...])``):

.. list-table::
   :header-rows: 1
   :widths: 28 72

   * - API
     - Behaviour
   * - ``interfaces.set_state``
     - Say whether the interface can deliver right now (``enabled``, ``reason``). A user
       account that is not logged in registers **disabled**, so its setup panel can be
       shown; a disabled interface fails sends and the WebUI shows why.
   * - ``interfaces.inbound``: ``timestamp``, ``backlog``
     - ``timestamp`` (ISO-8601, never in the future) is the message's own time.
       ``backlog=true`` stores it in the history under that time and does **nothing
       else**: no command, no turn, attachments ignored. For history Telegram-like
       platforms deliver late.
   * - ``interfaces.inbound``: ``wait_reply``, ``history``, ``reply_timeout``
     - For request/response interfaces (an HTTP API). ``wait_reply=true`` runs the turn
       and answers the call with its text: ``{"queued", "reply", "pending"}`` (a slash
       command answers ``{"handled": "command", "reply", "pending"}``). ``pending=true``
       means the agent lane took the turn: its text arrives later as a normal outbound
       delivery, which the extension matches to the waiting request. ``timed_out=true``
       when ``reply_timeout`` (at most 900 s) passed. ``history`` is the conversation the
       client resent (``[{role: system|user|assistant, text}]``, at most 60); it, not the
       chat's stored history, builds the prompt. The call's own timeout is raised for
       this method only (``ApiMethod.timeout``); the SDK's ``ctx.call(..., _timeout=)``
       and ``ctx.interfaces.inbound`` wait accordingly.
   * - ``core.info`` capability
     - ``core.info`` answers ``{"synth_name", "capabilities", "in_container"}``: the
       synth's name, the union of what its engines provide (booleans only), and whether
       the Core runs in a container (so a server can pick a bind address). SDK:
       ``await ctx.core.get()``. Read-only.
   * - ``interfaces.seen``
     - Whether a message from a sender at a time is already in the history, to catch up
       after downtime without handling the same message twice.
   * - ``interfaces.trainer``
     - The trainer id the operator configured for the interface (``TRAINER_IDS``).
   * - ``avatar`` capability
     - The Core pushes the synth avatar as ``interface.set_avatar`` when it changes (and
       when the interface becomes ready, if it was out of date). The extension decides
       whether to apply it (the operator's opt-in is the extension's own setting). The
       general avatar API, for any extension, is below.
   * - ``reaction`` capability
     - ``interface.reaction`` (chat, message, emoji) when the Core reacts to a mention.
   * - ``actions`` (manifest ``actions = [...]``)
     - Extra actions the model may use on the interface. The extension describes each
       (``description``, ``fields``, ``notes``) at register time; the Core validates
       payloads against it and runs ``interface.action`` in the extension.
   * - ``call`` capability
     - ``interface.start_call`` / ``interface.end_call`` for dialling out.

**The synth avatar** (capability ``avatar``; ``core/extensions/avatar.py``). The avatar is
part of the Core, not of a plugin (Settings > Synth Avatar): one image the operator
uploads and frames, versioned by the Core. Any extension, an interface or not, can read
it with ``avatar.get`` and **stay in sync**: the Core publishes ``core.avatar.changed``
(``{"version"}``, ``null`` when removed) on a real change (an upload, a removal, a
resend, not a start-up catch-up), and ``ctx.avatar.sync(handler)`` in the SDK calls the
handler now and on every change (subscribing needs the ``events`` capability). An
interface additionally gets it pushed, as above.

**Panels** (capability ``ui.panel``). Some extensions need a guided step that is not a
setting, such as logging a user account in with a code. An extension never ships
JavaScript into the WebUI (it would run with the operator's token). It pushes a panel as
data with ``panels.set`` (text, error, form, button: a closed vocabulary, bounded in
size, unknown keys dropped, code and password fields never echoed back) and the Core
draws it with its own script (``extension_panels.js``, DOM calls and ``textContent``
only). The WebUI routes ``/api/extensions/panels/...`` need the WebUI token; a button or
form becomes ``panel.action`` on the extension with the values typed. Values are never
logged. Panels are shown in the interface's detail card.

**Calls** (capability ``calls``; ``core/extensions/calls.py``). The Core owns the call:
the session, speech detection and transcription, the spoken reply, the SyntH's face. The
extension carries the sound and the picture:

.. list-table::
   :header-rows: 1
   :widths: 28 72

   * - API
     - Behaviour
   * - ``calls.provider``
     - Announce that a registered interface (with the ``call`` capability) hosts calls,
       and whether it can carry video.
   * - ``calls.open``
     - A call began. The Core opens the session; the tone that answers arrives as a
       ``call.play`` request **before this returns**. Returns ``call_id``, ``kind`` and,
       for video, ``{fps, width, height}``.
   * - ``calls.audio``
     - What the caller says: raw 16-bit PCM at any rate, mono or stereo, in chunks of
       about 100 ms; the Core converts to 16 kHz mono and feeds the speech pipeline.
   * - ``calls.video_frame``
     - The newest frame of the SyntH's face as planar I420 (at most 700,000 bytes; a
       bigger face means an audio call).
   * - ``calls.end``
     - The call is over on the platform.
   * - ``call.play`` (Core to extension)
     - Play this file into the call. The path is a private copy **deleted when the
       handler returns**, so the extension copies it if playback needs it. Returns the
       seconds it will take, which is how the Core knows the SyntH is speaking.
   * - ``call.ended``, ``call.video_lost``
     - The Core ended the call; the face is no longer available (the call goes on as
       audio).

An extension can only drive calls it opened; when it dies its calls end and the camera
stops. Whether py-tgcalls works against a live Telegram call was **not** verified (see
:doc:`telegram_extension`).

Cost of the process boundary
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Measured by ``tests/test_extensions_latency.py`` (3 runs on a development laptop,
small messages, sequential calls, the Core's pipeline replaced by an in-memory
stand-in so only the boundary is timed):

.. list-table::
   :header-rows: 1
   :widths: 40 20 20 20

   * - Call
     - p50
     - p95
     - Throughput
   * - Bare JSON-RPC round trip, in-memory pipe
     - 0.05-0.10 ms
     - 0.07-0.12 ms
     - 10,000-20,000/s
   * - ``interfaces.inbound`` from a real extension process
     - 0.11-0.24 ms
     - 0.16-0.35 ms
     - not measured
   * - ``RemoteInterface.send_message`` to a real process
     - 0.08-0.18 ms
     - 0.10-0.22 ms
     - 5,000-11,000/s
   * - One conversational turn (inbound + outbound, p50)
     - 0.2-0.4 ms
     - n/a
     - n/a

For scale, one LLM call takes hundreds of milliseconds to seconds, so the boundary
adds well under one percent. This does **not** include the work the Core already did
in-process (history write, ``message_queue`` processing), which is unchanged. Large
messages (up to the 20,000 character limit) and many extensions at once were not
measured. The gateway's default limit of 100 calls per second per extension is far
above a chat interface's real rate but would throttle a bulk import.

External endpoints
------------------

An extension that fronts an engine (an LLM, speech or vision service) registers it as an
*external endpoint* with ``endpoints.ensure`` (capability ``endpoints.manage``,
``core/extensions/endpoints.py``); ``endpoints.get`` reads one. The Core runs nothing for
it. What it may touch is narrow, because an endpoint can hold an API key and its URL
decides where the key goes:

* only endpoints whose names the manifest declares (``provides.endpoints = ["..."]``);
* never one that **holds an API key** (refused for ``ensure``; ``get`` reports
  ``has_key``); the extension cannot set or read a key;
* an existing endpoint only gains the ``extra_config`` keys it lacks, so what the
  operator set stays; its base URL changes only with ``update_base_url=true``;
* ``only_if_no_cortex=true`` does not add an endpoint when the operator already has an
  enabled endpoint that serves the cortex (voice and hearing engines, such as the default
  KittenTTS and Vosk, do not count: a fresh install still gets its Zen endpoint); ``extra_config`` keys starting with ``_`` and credentials in the URL
  are refused.

SDK: ``ctx.endpoints.get(name)`` and ``ctx.endpoints.ensure(name, base_url, ...)``. The
Zen LLM Engine extension (``<plugins repo>/extensions/zen``) is the first user: the Core no longer
seeds, names or tunes anything for Zen.

Turns of its own, the synth's voice, and a Recon question
----------------------------------------------------------

An extension that is not a chat but still *speaks*, such as a radio DJ (the AzuraCast
Radio DJ, ``<plugins repo>/extensions/radio_host``, is the first), needs four more things from the
Core. Each is a small, scoped API; none gives access to the model, the voice engine or the
avatar directly.

.. list-table::
   :header-rows: 1
   :widths: 24 22 54

   * - Method
     - Capability
     - Behaviour
   * - ``turns.request``
     - ``turns.request``
     - Runs one **background turn** on one of the extension's own interfaces
       (``core/extensions/turns.py``). ``text`` is what the model is asked; the turn is
       not written to the chat history, is scoped to ``allowed_actions`` (at least one,
       all actions of that interface, so the model cannot reach anything else) and is
       queued at a named ``priority``: ``background``, ``low``, ``ambient``, ``general``
       or ``elevated`` (one step above ordinary chat, below the trainer and below every
       call). ``context`` (at most 4 KB of JSON) comes back, untouched, in the extension's
       ``interface.action`` call when the model answers with one of its actions, so a
       request and its answer meet again without a table of pending turns. At most 20
       requests a minute, because each costs a model call.
   * - ``voice.synthesize``
     - ``voice.synthesize``
     - Speaks ``text`` with the synth's voice (or a named engine) and returns the audio as
       **a file in the extension's own data directory** (a copy made by the Core, readable
       by that extension only, removed within an hour). No engine: ``{"available": false}``.
   * - ``voice.play``
     - ``voice.play``
     - Makes the avatar speak a clip of the extension's, with lip-sync, for everyone
       watching the stage. The path must be inside the extension's data directory; the
       Core copies it with the same checks as an attachment and opens no other path.
   * - ``recon.contribute`` (Core to extension)
     - ``recon.contribute``
     - The extension declares ``[[provides.recon]]`` (``key``, ``instruction``,
       ``priority``): a question added to Recon, the step before every turn in which the
       model answers a few structured questions about the message. With the model's answer
       the Core calls the extension and puts what it returns into the prompt as snippets
       (at most three, 2,000 characters each, five seconds to answer). A key the Core's
       own recon plugins use cannot be taken, and nothing else can be contributed.

Two small manifest and SDK pieces go with them. ``message_action`` in
``[[provides.interfaces]]`` names the interface's own action that is how it *speaks* (a
radio's "say on air"), so the Core treats it as a message without knowing its name.
``ctx.interfaces.on_action`` hands the handler the turn's ``context`` as its fourth argument.

SDK: ``ctx.turns.request(...)``, ``ctx.voice.synthesize(text)`` / ``ctx.voice.play(path)``
and ``ctx.recon.on_contribute(handler)``. Tests: ``tests/test_extensions_turns_voice_recon.py``
(the API against fakes), ``tests/test_extensions_core_turns_voice.py`` (the Core services),
``tests/test_extension_radio_host_process.py`` (a real extension process, a fake station
and the whole path of one remark).

Rift Vessel connectors
----------------------

The Vessel (session, salience filter, beats, diary, goals) stays in the Core. What a *world*
adds is a **connector**: the body that connects to the game or room, reports what happens and
does what it is asked. An extension provides one with ``[[provides.vessels]]`` (``name``,
``label``, ``remembers_chat``, ``capabilities`` flags) and the ``vessel.connector`` capability
(``core/extensions/vessels.py``); the Vessel then loads it from its registry like any other
connector, and nothing else in the Vessel changes.

The contract has two halves, because the Core's connector contract mixes coroutines with
plain methods called from synchronous code:

* **Awaited questions** (``connect``, ``disconnect``, ``act``, ``act_deliberate``,
  ``motor_step``, ``get_world_state``, the goal, base and quest lookups,
  ``lookup_knowledge``, ``get_embodiment_assets``, ``probe_external_liveness``) become one
  ``vessel.call`` request each. A failed ``act`` comes back as ``ok=False``, never as an
  exception; the others fall back to the contract's default.
* **Immediate answers** (``is_connected``, ``idle_busy``, ``describe_surroundings``,
  ``get_world_actions``, ``get_progression_*``, ``get_in_world_name``,
  ``get_world_identity``, ``describe_capabilities``, ``get_knowledge_sources``...) cannot
  wait for a message. The extension pushes a **state snapshot** (``vessel.state``, at most
  64 kB, known keys only) whenever one changes and the Core answers from the last one; a key
  never sent gets the contract's default. A connector whose extension has stopped is not
  connected.

From the extension to the Core: ``vessel.perceive`` (an event of the world; taken only while
the Vessel has the connector connected, only for a connector the extension declared) and
``vessel.state``. SDK: ``synth_sdk.vessel.VesselConnector`` carries the same method names as
the Core's contract, collects the snapshot and pushes it after every call and every second
while connected (``changed()`` pushes at once); ``ctx.vessels.serve(name, connector)`` serves
it. Tests: ``tests/test_extensions_vessels.py`` (the API against fakes and one real process,
``tests/support/world_extension``).

Karada for extensions
---------------------

An extension that draws or carries the avatar somewhere else (a game room, a headset, a
renderer) becomes a **transport** of the Karada state server (``core/extensions/karada.py``).
``karada.subscribe`` registers it; from then on each broadcast reaches it as a ``karada.frame``
notification ``{kind, payload}`` (``animation``, ``audio``, ``face``, ``model``, ``expression``,
``presence``, ``direct``), the payload exactly Karada's own and audio as the URL Karada already
serves, never as bytes. It also returns the state to catch up with (``{animation, presence}``,
also ``karada.state``). ``karada.presence`` announces she is arriving in (``summon``) or leaving
a world; an extension may only clear a presence it set, and one that ends releases its own. Each
extension has a bounded queue (64 frames: the oldest are dropped, the server never waits).
SDK: ``ctx.karada.on_frame(handler)`` (one frame at a time, in order), ``subscribe()``,
``state()``, ``set_presence()``. Tests: ``tests/test_extensions_karada.py``.

Wire protocols for external endpoints
-------------------------------------

An *external endpoint* (an LLM, speech or vision service the operator configured) names a
``protocol``, and the Core keeps one adapter per protocol for the operations on it (list
models, chat, speech, transcription, vision, probing). An extension can provide a protocol
(``core/extensions/protocols.py``): it declares ``[[provides.protocols]]`` (``name``, an
optional ``base`` protocol, and the ``ops`` it implements) and holds ``endpoints.protocol``.
While it runs, the adapter of that protocol is a proxy that sends each operation to the
extension (``protocol.request``).

* **Layering.** Operations that are not listed fall back to the ``base`` protocol's adapter
  in the Core. Harmony (``official/protocol_harmony``) is OpenAI-compatible for chat and
  vision, and implements only ``models``, ``tts`` and ``stt``; Fish Audio
  (``official/protocol_fish``) is a TTS-only provider and implements everything it has,
  plus the voice-picker extras (``list_speakers``, ``fetch_model_detail``,
  ``parse_model_id_from_url``).
* **What it is given.** The endpoint's name, base URL, default model and extra settings and
  its **decrypted API key**, because it needs the key to speak the protocol. It is given the
  keys of the endpoints that use *its* protocol and of no others, which is what the
  capability's grant means. It cannot take over a protocol the Core has built in (OpenAI,
  Gemini, Anthropic and ``custom``: the prompt and tool rendering is built around those
  dialects).
* **Files, not blobs.** Audio and images do not travel in a message: the Core stages a file
  in the extension's data directory for what it sends (transcription, vision), and the
  extension returns a file path for what it makes (speech), which the Core reads
  (``MediaBridge.take_inbound``: inside the data directory, a regular file, size limited) and
  deletes. A request that would not fit in a message is refused before it is sent.
* **Lifecycle.** An endpoint saved with a protocol keeps it while the extension is off (any
  lowercase identifier is a valid protocol); its engines appear when the extension starts and
  go away when it stops (``ExternalEndpointRegistry.sync_protocol``).

**A local engine is a protocol too.** An engine that runs inside the extension (the
KittenTTS voice, ``official/vox_kitten``) provides a protocol and creates one endpoint of it
with ``endpoints.ensure(name, "", protocol="kitten", subsystem_map={"vox": True})``: an empty
address is allowed for a protocol the extension provides. The Core's voice, hearing and vision
bridges then treat it as any endpoint, with its voice picker, active-engine setting and
per-voice key (``<NAME>_VOICE``). The Core's voice pipeline runs adapters in a worker thread
with a loop of its own; the proxy makes its call on the Core's loop, so that works.

**The local voice and hearing engines are extensions.** ``official/vox_kitten`` (KittenTTS) and
``official/auris_vosk`` (Vosk) are installed by default and work from the first start: each
carries its libraries (onnxruntime and espeak-ng; Vosk), downloads its own model on first use
into its data directory, and creates its endpoint (``kitten``, ``vosk``) under the names the
built-in engines had, so ``ACTIVE_VOX_ENGINE``, ``KITTEN_VOICE`` and the old settings keep
working (``core/extensions/legacy.py`` copies ``KITTEN_*`` and ``VOSK_*``). KittenTTS runs the
upstream model code without PyTorch or spaCy, which the upstream package pulls in but the
inference never uses. They replaced the Core's built-in engines, the model manager (its "Manage Models" panel and
``/api/models`` routes went with it: nothing registered a model any more) and the first-use package installer.

**Spoken replies to a chat.** An interface declares ``voice_format`` (``ogg_opus``, ``mp3`` or
``m4a``) for the audio its chats take as a voice message; a reply that is spoken while no call
is open is converted with ffmpeg (``core/voice_delivery.py``) and sent with ``send_as_voice``, so
Telegram receives a voice note instead of a ``.wav`` file. Without ffmpeg, or on a clip it cannot
read, the original is sent.

SDK: ``ctx.protocols.on_request(handler)``, ``handler(protocol, op, endpoint, api_key, args)``.
Tests: ``tests/test_extension_protocols.py`` (the Core side, both extensions' logic against a
fake service, and a real Harmony extension process against a fake Harmony service).

Web content
-----------

An extension can add pages, HTTP routes and WebSockets to the Core's web server
(``core/extensions/web.py``, ``webui_web.py``). It never opens a port: the Core serves
and forwards, so the WebUI's own token check cannot be walked around. Declared in the
manifest under ``[provides.web]`` and gated by capabilities that are declared **and**
granted:

.. list-table::
   :header-rows: 1
   :widths: 24 76

   * - ``[provides.web]``
     - Behaviour
   * - ``static = "web"`` (``web.static``)
     - The package directory is served read-only at ``/x/<id>/`` (assets only, never
       secrets; the WebUI's token gate, when on, covers it like every route). Files only, no listing; ``..`` and a symlink that leaves the
       directory are refused; ``nosniff`` is set. ``alias = "stage"`` also serves it at
       ``/stage/`` (one lowercase segment, not one the Core uses, first running
       extension wins).
   * - ``routes = true`` (``web.routes``)
     - ``/x/<id>/api/<path>`` (GET, POST, PUT, PATCH, DELETE) needs the WebUI token and
       is forwarded as the call ``web.request`` with the method, path, query, a short
       list of safe request headers (never ``Authorization`` or ``Cookie``) and the body
       (base64, at most 600 kB each way). The answer's status must be 200-599; only
       ``content-type``, ``cache-control``, ``content-language``, ``etag`` and
       ``location`` are passed on (no cookies). SDK: ``ctx.web.on_request(handler)``
       with ``WebRequest`` / ``WebResponse``. A slow extension is a 504, a bad answer a
       502.
   * - ``websockets = true`` (``web.ws``)
     - ``/x/<id>/ws/<path>`` behind the WebUI token. The Core calls ``web.ws.open``
       (the extension may refuse), ``web.ws.message`` for each text or binary frame (at
       most 600 kB) and ``web.ws.closed``; the extension answers with ``web.ws.send``
       and ``web.ws.close`` and can only touch its own connections. At most 16 per
       extension; they all close if it stops. SDK: ``ctx.web.on_websocket(...)``,
       ``ws_send``, ``ws_close``.

**Same origin or sandbox.** Static files are served on the Core's own origin, so a page
the extension ships could read the WebUI session (token in storage) and act as the
person. Therefore every static file carries ``Content-Security-Policy: sandbox
allow-scripts allow-forms allow-popups allow-downloads``: the page gets an opaque origin,
with no cookies, no storage and no way to use the session. An extension that must act as
the person (Stage, the Dashboard) declares ``same_origin = true`` and needs the capability
``web.origin``, which is the dangerous one: it is shown for what it is and granted
explicitly. A sandboxed page can still run scripts, but it cannot use the WebUI session (the
frames the Core draws also allow ``allow-modals``, so ``alert``/``confirm``/``prompt`` work):
it cannot call ``/x/<id>/api/`` or any ``/api``. **Known limit**: when the WebUI token gate
is on, the session cookie is ``SameSite=Strict``, so the sandboxed page's own sub-requests
(scripts, styles, images) do not carry it and are refused: a sandboxed window then works
only as one self-contained document (inline CSS and JS); its WebSocket is refused too
(the origin is ``null``). A window made of several files, or one that talks to its
extension, needs ``web.origin``. This is a trade-off, not a bug to
patch around: the alternative is serving extension pages from another origin, which the
single-port WebUI cannot do.

WebUI slots
-----------

The WebUI takes windows from extensions at three fixed points and nowhere else
(``core/extensions/ui.py``, ``res/synth_webui/js/extension_slots.js``):

.. list-table::
   :header-rows: 1
   :widths: 14 86

   * - Slot
     - Where it appears
   * - ``activity``
     - A sub-tab of the Activity section (the ``history`` template).
   * - ``logs``
     - A sub-tab of the Logs section.
   * - ``more``
     - A line in the **More** menu of the top bar (before About); choosing it opens the window, or a frame (see below).

An extension declares a window with ``[[provides.ui]]`` (``slot``, ``id``, ``title``,
``icon``, ``page``, ``presentation``) and needs ``provides.web.static``: the window is that page of its own
files, shown in a frame the Core draws (sandboxed unless ``web.origin``). Titles and ids
are escaped, never markup, and no extension script runs in the WebUI page itself. Only a
running extension with ``web.static`` granted appears; ``GET /api/extensions/ui`` (WebUI
token) lists what is available. The Plugins section is the extension manager's own and
takes no contribution. Not verified in a browser here.

``presentation`` is ``window`` (the default: a resizable WinBox window, for a panel or a
tool) or ``frame`` (only in ``more``): a whole view that fills the main area under the top
bar, like the avatar's 3D page, with no window chrome. It cannot be moved or resized, and
choosing a tab of the bar, the close button or Escape puts it away. The Stage is a frame.

The process
-----------

Command line (``Launcher``)::

    python -S -I -u synth_sdk/_launch.py <sdk dir> <install dir>

* ``-S -I``: no ``site-packages``, no ``PYTHON*`` environment, no user site. The SDK
  is loaded **from its own directory by explicit path**; the repository root is
  never on ``sys.path``, so ``import core`` fails. This is a convenience for authors,
  not a security boundary.
* **Environment: nothing is inherited.** Only ``PATH``, ``LANG``, ``LC_ALL``,
  ``HOME`` and ``TMPDIR`` (inside its data directory), ``PYTHON*`` flags and the
  ``SYNTH_EXTENSION_*`` variables. None of the Core's secrets reaches the process.
* ``stdin`` is empty and ``stdout`` is redirected to ``stderr``: the protocol runs
  on private duplicated file descriptors, so a stray ``print`` cannot corrupt it.
  ``stderr`` is read line by line into the Core log.
* Working directory is the extension's data directory, ``umask`` 077, its own
  session and process group (a stop signals the whole group).
* Resource limits, applied inside the process and not raisable by it: open files
  256, file size 256 MiB, core dumps 0, and processes 128.

**A dedicated user.** When the Core runs as root (it does in the container) each
extension runs as its **own numeric user**, allocated once and kept in
``uids.json`` (range from 200000), with no supplementary groups. It then cannot
read the Core's files or another extension's data, cannot write its own code, and
cannot signal the Core. The process-count limit is only applied in this mode,
because the limit counts *every* process of a user and would break forking for an
extension that shares the Core's user.

**Same-user development mode.** Without root (a developer on the host) the Core
cannot change the user of a child. The extension still gets the clean
environment, its own directories and limits, but runs as the same user as the
Core: it **can read** ``.env`` and signal the Core. A warning is logged once. Set
the policy to *require* a dedicated user to refuse to start extensions instead.
The runtime also refuses when the interpreter, the SDK or the extension code
would not be readable by the new user, and says why.

Supervision
-----------

.. code-block:: text

   STOPPED -> STARTING -> RUNNING -> (exit) -> BACKOFF -> STARTING ...
                                  \-> FINISHED   clean exit on its own, not restarted
                                  \-> FAILED     too many crashes: gives up

* A crash (non-zero exit, failed start, or no handshake within 10 s) is a failure.
  The restart delay doubles from 1 s up to 60 s. Five failures within 5 minutes
  make it ``FAILED``: it stays down until started again.
* Stopping asks the extension to stop (``lifecycle.stop``), waits the grace period,
  sends ``SIGTERM`` to its group, waits, then ``SIGKILL``. An extension that blocks
  its own event loop or ignores ``SIGTERM`` is still killed, together with the
  processes it spawned in its group.
* If the gateway **closes a session** (protocol violation, false identity), a
  process that keeps running is killed after a short grace period: it is cut off
  from the Core anyway.
* Start-up follows the resolver's order, dependencies first. An extension that
  cannot start is reported and excluded; the others and the Core are unaffected.

Writing an extension (SDK)
--------------------------

.. code-block:: python

   # my_extension/ext_main.py   (manifest: [entry] module = "ext_main")
   from synth_sdk.runtime import Context, PermissionDenied

   async def main(ctx: Context) -> None:
       await ctx.storage.set("started", True)
       ctx.log.info("up")
       ctx.stats.incr("starts")

       async def on_config(topic, payload):
           ctx.log.info(f"config changed: {payload}")

       await ctx.events.subscribe("core.config", on_config)
       try:
           token = await ctx.config.get("API_TOKEN")
       except PermissionDenied:
           ctx.log.error("the user has not granted 'config'")
       await ctx.stopping.wait()      # set when the Core asks the extension to stop

The function must be ``async def``. Returning from it ends the extension cleanly
(``FINISHED``); raising makes it crash (and be restarted by the supervisor).

An interface extension (manifest: ``[[provides.interfaces]]`` with
``id = "chat"`` and ``paths = ["chat/{room}[/{thread}]"]``, capabilities
``interfaces.register`` and ``interfaces.inbound``):

.. code-block:: python

   from synth_sdk.paths import build_interface_path
   from synth_sdk.runtime import Context

   async def main(ctx: Context) -> None:
       async def send(interface_id: str, payload: dict, origin: dict) -> bool:
           await my_platform.post(payload["interface_path"], payload["text"])
           return True                       # or {"ok": False} on failure

       ctx.interfaces.on_send_message(send)  # the Core's replies arrive here
       await ctx.interfaces.register("chat", display_name="My Chat", bot_username="synth")

       async for event in my_platform.events():          # your platform client
           await ctx.interfaces.inbound(
               "chat",
               build_interface_path("chat", event.room, event.thread),
               text=event.text,
               sender_id=event.user_id,
               sender_name=event.user_name,
               message_id=event.id,
               chat_type="group" if event.is_group else "private",
           )

How it is tested
----------------

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - File
     - Covers
   * - ``tests/test_synth_sdk_rpc.py``
     - Wire format, limits, violations, timeouts, cancellation
   * - ``tests/test_extensions_gateway.py``
     - Handshake, capabilities and revoke, validation, rate limit, storage,
       config, stats, events: every method against hostile input
   * - ``tests/test_extensions_runtime.py``
     - **Real processes**: lifecycle, crash and backoff, killing an unresponsive
       extension with its children, clean environment, isolated Python
   * - ``tests/test_extensions_hostile.py``
     - **Real hostile processes**: garbage, oversized messages, flooding, false
       identity, probing for internal methods, revoked grants mid-flight, secrets
   * - ``tests/test_extensions_sandbox.py``, ``test_extensions_host.py``
     - Policy, uid allocation, permission repair, runtime facade, host hooks
   * - ``tests/test_extensions_db_storage.py``, ``test_extensions_core_ports.py``
     - Storage conformance (memory and SQL), the Core services behind the ports
   * - ``tests/test_extensions_interfaces*.py``, ``test_extensions_core_interfaces.py``
     - Interface registration and ownership, inbound validation and isolation, outbound
       forwarding, real processes, the Core's real dispatcher and registries, the Core
       functions behind the ports
   * - ``tests/test_extensions_media.py``, ``test_extensions_interface_media.py``
     - Files across the boundary: path checks, limits, copies, cleanup, the Core's own
       attachment extractor, and hostile attempts to make the Core read other files
   * - ``tests/test_synth_sdk_paths.py``
     - Interface Path helpers (compared with the Core's) and declared patterns
   * - ``tests/test_extensions_latency.py``
     - Measures the boundary (above) and guards against large regressions
   * - ``tests/test_extensions_privileged.py``
     - The dedicated-user sandbox. **Skipped unless run as root.**

Run the privileged tests in the container::

    docker compose exec synth /app/venv/bin/python -m pytest \
        tests/test_extensions_privileged.py -q

or on a Linux host with ``sudo -E env "PATH=$PATH" uv run pytest
tests/test_extensions_privileged.py -q`` (the interpreter and the repository must
be readable by other users; if not, the test says why it cannot run).

.. _pp-m2-not-verified:

What is not verified, and known limits
--------------------------------------

**Not verified in the session that built M2**

* **The dedicated-user sandbox was not run.** Building M2 had no root, no ``sudo``,
  no subordinate ids for a user namespace, and no container image. The user change,
  the "cannot read the Core's file / cannot signal the Core" checks and
  ``extra_groups=[]`` are covered by ``tests/test_extensions_privileged.py``, which
  was written but has **never been executed**. Run it before relying on the
  isolation.
* **``SqlStorage`` was not run against MariaDB or PostgreSQL.** It is tested against
  SQLite through a fake of the Core's connection (same conformance tests as the
  in-memory port). The DDL and statements use the portable subset the Core already
  translates, but that is unconfirmed.

**Limits of the design, by intent**

* Same-user development mode offers no protection against hostile code (above).
* **Network egress is not restricted.** An extension can open any connection the
  host allows. The manifest's ``permissions.network`` is declared but not enforced.
* A process that deliberately leaves its process group (``setsid``) is not killed
  with the group. Resource limits and, later, cgroups are the answer.
* There is no per-extension memory or CPU limit by default (``address_space`` and
  ``cpu_seconds`` exist but are unset).
* Capabilities are coarse (``storage``, not ``storage.read``/``storage.write``).
* The user interface to grant capabilities does not exist yet (milestone M6);
  today ``grants.json`` is edited by hand or by the host.
