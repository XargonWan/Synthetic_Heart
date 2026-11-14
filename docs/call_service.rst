Calls (audio / video)
=====================

The SyntH can answer a call: a live audio or video conversation with a person.
The concept is modelled once, in ``core/call_manager.py``, and interfaces
register with it as *call providers*. The WebUI is the first provider; Telegram,
Discord and the others register the same way.

.. contents::
   :local:
   :depth: 2

Concepts
--------

``CallProvider``
   What an interface implements to host calls: a ``name`` (its interface name),
   ``capabilities`` (``audio``, ``video``, ``screen_share``), ``play_audio()``
   and the optional ``on_call_started()`` / ``on_call_ended()`` hooks. The call
   manager never imports an interface library: a provider is an optional
   component, and with no provider registered ``request_call()`` raises
   ``CallError`` instead of crashing.

``CallSession``
   One live call: id, interface name and path, kind (audio or video), state,
   and the latest screen-share frame.

``CallManager`` (``get_call_manager()``)
   Registry of providers and active calls: ``register_provider()``,
   ``request_call()``, ``end_call()``, ``get_active_call()``,
   ``on_message_transcribed()``, ``submit_screen_frame()`` and ``mark_turn()``.

Audio and video
---------------

An **audio call** carries audio only. A **video call** adds the SyntH's
"webcam", which is the Karada render of the model (a close-up). The render is
produced by the client, so a provider only declares the ``video`` capability; a
video call on a provider without it degrades to audio (``CallSession.degraded``
tells the caller). In the WebUI the avatar is always on screen, so a call there
is in practice an audio call; the webcam proper is for the pop-out window and
for interfaces that can show a video stream (Telegram today, see below).

The Karada render proper exists only in the browser. A call hosted by another interface
gets its video from the server: a small headless renderer draws the active VRM model
(``core/avatar_render/``), or, as the alternative, an open WebUI tab is borrowed as the
camera (``core/call_video.py``). See *The SyntH's face in other interfaces' calls* below.

Turn priority
-------------

A call turn is a person waiting in real time. ``CallManager.mark_turn()`` sets
the structural flag ``_call`` on the message and ``message_queue.enqueue()``
ranks it at ``PRIORITY_CALL`` (9):

.. code-block:: text

   emergency (12) > urgent (11) > rift / vessel reflection (10)
       > CALL (9) > chat (high 8, trainer 7, radio 6, general 5) > ...

The ranking uses the flag, never the message text. A call turn is not demoted by
the bot ward. In the WebUI queue view it shows as a ``Call`` badge.

What a call turn carries
~~~~~~~~~~~~~~~~~~~~~~~~

* **The model knows it is on a call.** The queue writes ``context["call"]`` (kind, duration,
  whether the caller sees the avatar or shares a screen) and the prompt renders it as
  ``input.payload.source.in_call`` with a short note: the reply is spoken, so short plain
  sentences, no markdown or emoji, and an utterance cut off mid-thought is waited for.
* **The reply is always spoken.** ``request_tts`` and ``is_voice_input`` are forced for the
  turn and ``message_plugin`` forces ``send_as_voice`` on every ``message_*`` action of a
  call turn, whatever the model chose and even while Vox is momentarily off (Vox then falls
  back to text). These flags survive queue coalescing: when several queued messages of a
  chat are merged into one turn, the merged message keeps ``_call``, ``call_id``,
  ``request_tts``, ``is_voice_input`` and the latest screen-share attachment.
* **The avatar stays in THINK.** A turn answered by voice has no writing phase, so neither the
  queue nor the WebUI nor Discord switch the Karada state to ``write``; ``talk`` is fired by
  ``broadcast_audio()`` when the clip starts (``core.voice_reply.is_voice_reply``).
* **A young turn is interrupted.** People speak in pieces. When a further utterance of the
  *same call* arrives while the previous call turn is younger than
  ``CALL_TURN_INTERRUPT_WINDOW_SEC`` (default 8 s, ``0`` disables it), the in-flight turn is
  cancelled and put back in the queue, where coalescing joins both utterances into the one
  turn that gets answered. An older turn is left to finish (the SyntH is already replying);
  engines that cannot be cancelled (Zen) are never interrupted.

The "message received" sound
----------------------------

Turns are slow, so the moment the caller stops speaking the SyntH plays a short
acoustic signal, a soft two-note **"tu-din"**. ``core/call_ack_cache.py`` makes it:

* the signal is *synthesised* (two decaying bell-like notes, E5 then A5, about
  0.65 s, around -6 dBFS), not spoken, so it needs no TTS engine, no model and no
  network and works on a fresh install;
* it is written once to ``<VOX_OUTPUT_DIR>/call_ack/chime-v1.wav`` (the
  directory the WebUI already serves audio from) and reused; clips left by
  earlier versions are removed there;
* ``request_call`` prepares it in the background, and ``get()`` creates it on
  demand, so it is always ready for the first turn.

The signal is played when **text has actually been transcribed**, not when sound is
heard: ``POST /api/audio/upload`` takes a ``call_id`` form field and, once Auris has
returned non-empty text, calls ``CallManager.on_message_transcribed()``. Typing on the
keyboard, a cough or an echo transcribe to nothing, so they never trigger it (the VAD
used to, and it played "at random"). It also stays silent:

* while the SyntH is speaking (``CallProvider.is_agent_speaking``);
* when called again within 3 s of the previous signal;
* when ``CALL_ACK_ENABLED`` is off.

The signal is **not speech**. The WebUI provider plays it with
``KaradaStateServer.broadcast_sfx()``, which sends ``sfx-play``: no lip-sync, no TALK
animation, no "current audio" for late joiners, and the client does not attach it to a
chat bubble (``tts-play`` does all of that). While it plays, and for 0.7 s after, the
browser ignores the microphone.

In a call the browser also drops a VAD "speech" shorter than 0.6 s instead of sending
it, and an empty transcription (a false trigger) is ignored without an error message.

Start and end sounds
--------------------

A call opens with a short rising "pickup" sound and closes with a short falling
hang-up tone, like a desktop messenger. ``request_call`` plays ``start`` after the
provider's ``on_call_started``; ``end_call`` plays ``end`` before ``on_call_ended``. They
are synthesised like the "tu-din" (``render_signal("start")`` / ``("end")``, about
0.4 s each, saved next to it as ``pickup-v1.wav`` and ``hangup-v1.wav``) and go
through the provider's ``play_audio``, which in the WebUI is the same ``sfx-play``: not
speech, no lip-sync. They are not subject to the "tu-din" rules (a pickup plays even
while the SyntH is speaking). A provider that cannot play audio never stops a call from
starting or ending. ``CALL_SOUNDS_ENABLED`` turns them off.

Screen share
------------

A provider that declares ``screen_share`` pushes JPEG frames to
``CallManager.submit_screen_frame()``. Frames are throttled
(``CALL_SCREEN_FRAME_MIN_INTERVAL_SEC``), dropped when identical to the last
one, and downscaled to ``CALL_SCREEN_MAX_EDGE_PX``. Only the **latest** frame is
kept; ``mark_turn()`` attaches it to the next call turn as
``{"mime_type": "image/jpeg", "data": <base64>, "filename": "screen.jpg"}``, the
same attachment shape the RetroXR vessel uses for what the SyntH sees. The
generic multimodal path (``plugin_instance._extract_multimodal_attachments``)
turns it into a ``PromptRequest`` attachment; a non-vision model gets an Iris
description instead.

Interfaces with their own attachment extractor (``telegram``, ``telegram_bot``,
``discord_bot``) have to hand the frame over in their extractor; the WebUI and
any interface using the generic path need nothing more.

WebUI
-----

``core/webui_call.py`` registers ``WebUICallProvider`` (audio, video,
screen share) and mounts:

.. list-table::
   :header-rows: 1

   * - Endpoint
     - Purpose
   * - ``POST /api/call/start``
     - ``{session_id, kind?, languages?}`` starts a call, returns ``call_id``.
   * - ``POST /api/call/end``
     - ``{call_id}`` ends it. A call also ends when the session's last socket closes.
   * - ``GET /api/call/status``
     - ``?session_id=`` returns the active call, if any.
   * - ``WS /api/call/screen``
     - ``?call_id=`` binary JPEG screen-share frames.

In the chat window the 📞 Call button (top right, after *Open Archives*) starts
and ends a call: it keeps the mic open and tags every spoken turn with
``call_id``, so the turn is ranked as a call and always answered by voice (``mark_turn`` forces ``request_tts``, so the reply is spoken on every interface and never falls back to chat text only). During
a call the mic button disappears from the composer (the mic is always open); the
➤ send button still appears when there is text to send. The 🖥️ button, shown
during a call, shares the screen.

The existing pop-out (the webcam icon, Document Picture-in-Picture) is the SyntH's webcam:
with a call active it shows a close-up of the model in a landscape window
(640x360); without a call it shows the full body in a portrait window (360x640).
When the call starts or ends while the pop-out is open, the camera reframes at
once and the window is resized with ``resizeTo()``; Chrome may refuse that
without a user gesture, in which case the framing is already right and the
resize is retried on the next click. The framing mode is
``window.__vrmFramingMode`` (``closeup`` / ``full`` / ``null`` outside the
pop-out), set by ``pip-window.mjs`` from the ``synth:call-state`` event that
``chat-window.mjs`` sends.

A call with the pop-out open is a **video call** (``window.__synthCallVideo``, event
``synth:call-video``): the SyntH keeps looking into the camera
(``window.__synthSetCameraGazeLock``, a sustained camera gaze that taps and camera drags
do not release) until the pop-out closes or the call ends.

If no signal is heard, ``GET /api/call/status`` reports ``last_ack``: ``not_ready`` (the file could
not be written; see the ``[call_ack]`` log lines), ``disabled``
(``CALL_ACK_ENABLED`` off), ``debounced``, ``playback_failed`` or ``played``. The client code is ``res/synth_webui/js/call-client.mjs``. A ``call_id``
is only honoured for the session that started the call.

How a call hears and answers
----------------------------

For every interface except the WebUI (whose browser does its own voice detection) a call
is heard the same way, whatever the platform (``core/call_audio.py``):

.. code-block:: text

   audio frames -> voice detection -> one utterance -> Auris (speech to text)
       -> the "tu-din" -> a normal turn at PRIORITY_CALL (request_tts forced)
   the reply is spoken by Vox and played into the call (CallManager.route_speech)

A provider feeds mono 16-bit 16 kHz PCM to ``CallAudioPipeline.feed()`` (from any
thread), implements ``CallProvider.deliver_turn()`` (build the interface's own message
for the text and enqueue it through ``CallManager.mark_turn``) and sets
``plays_speech_in_call`` so that Vox hands it the spoken reply (``play_speech``) instead of
sending a voice message. Voice detection uses Silero when it is available and otherwise a
simple energy detector (logged once per call). Speech shorter than 0.6 s is ignored, and
the SyntH is not listened to while it speaks (nor for 0.5 s after).

**The Live engine is optional and off by default.** ``CALL_LIVE_ENGINE_ENABLED`` (default
off) is a call-wide switch: when it is on, a call whose interface has a Live
implementation hands the conversation to a bidirectional Live engine (e.g. Gemini Live,
which does its own speech recognition and voice) instead of the pipeline above. The route
may be removed; nothing else depends on it. Today only Discord has a Live implementation.

Support per interface
---------------------

What an interface can do is set by the platform, not by this project. Checked on
2026-11-13:

.. list-table::
   :header-rows: 1

   * - Interface
     - Audio call
     - Video call
     - Notes
   * - ``synth_webui``
     - yes
     - yes (the pop-out webcam; screen share too)
     - Reference provider (``core/webui_call.py``); the browser detects speech.
   * - ``discord_bot``
     - yes
     - no
     - A voice session is a call (``DiscordCallProvider``): the generic pipeline by default,
       the Live engine only with ``CALL_LIVE_ENGINE_ENABLED``. Discord's API gives bots no
       camera or Go Live video; the libraries that stream video use a user token, which
       Discord's terms forbid. A video request degrades to audio.
   * - ``telegram`` (user account)
     - yes, **untested**
     - yes, **untested**: the face is drawn by the server (no browser); audio-only where the
       server cannot draw it
     - ``interface/telegram/telegram_call.py`` on py-tgcalls (installed on first use). Written
       from the library's published examples and API without an account to run it on: see
       *Debugging Telegram calls* below. For the video see *The SyntH's face in other
       interfaces' calls*.
   * - ``telegram_bot``
     - no
     - no
     - The Bot API has no call methods: a bot can neither place nor receive calls. It only
       exchanges voice messages and video notes.
   * - ``matrix_chat``, ``fluxer_bot``, ``openai_api_server``, ``vessel``
     - no
     - no
     - No call transport in the library or protocol the interface uses.

Discord in detail: a call opens when the bot starts a voice session (the
``join_voice_discord`` action, the ``/join`` command, or the automatic join when the
trainer is in the channel) and ends when it stops, the bot is disconnected, or the channel
empties. One always-on audio source plays everything into the channel (the pickup and
hang-up tones, the SyntH's voice), so nothing cuts anything else off. Each person's audio
(48 kHz stereo) is converted to 16 kHz mono and fed to the pipeline; their utterances are
turns addressed to Synth without a mention.

Debugging Telegram calls
------------------------

Telegram's Bot API has no calls, so this exists only for the Telethon **user** account.
The trainer (and ids in ``TELEGRAM_CALL_ALLOWED_IDS``) is answered at once. For anyone
else the call is left ringing and the trainer gets a message in their Telegram chat saying
who is calling; once the trainer has replied, the SyntH picks up or declines with the
``answer_call_telegram`` action (``user_id``, ``answer``). It only works while the call
rings (about 45 s). ``TELEGRAM_CALLS_ENABLED`` (default on) turns it off. Every library call
is guarded and logs with the ``[telegram_call]`` prefix. The parts most likely to need
adjusting:

* **Incoming audio format.** The first frame of each call is logged with its size and the
  format it was assumed to have; set ``TELEGRAM_CALL_RX_RATE`` (default 48000) and
  ``TELEGRAM_CALL_RX_CHANNELS`` (default 1) to match what py-tgcalls delivers. A call that
  "hears nothing" or hears chipmunks is this.
* **End of call.** The statuses that mean "call over" are matched by name
  (``_ENDED_STATUS_NAMES``); names the library does not have are skipped. A call that never
  ends on hang-up is this.
* **Answering and playing.** The call is answered by ``play(chat_id, MediaStream(...))`` with
  the pickup tone (as in py-tgcalls' ``p2p_example``); the SyntH's voice is the same call.
  The py-tgcalls documentation does not describe private calls (a 2.0.0 release with
  "private_calls" methods was yanked as broken), so whether this works on the installed
  version is the first thing to find out.
* **Video.** The call starts with audio *and* video when a source for the face exists
  (the server renderer, or a WebUI tab; see the next section): the first stream is
  ``MediaStream(ExternalMedia.VIDEO, audio_path=<wav>, video_parameters=...)`` and each frame
  goes out with ``send_frame(chat_id, Device.CAMERA, <I420 bytes>, Frame.Info(...))``, the
  external-frame API of py-tgcalls 3.x (read from its installed code; not tried on a live
  call). Look for these lines:

  * ``call with <id> is audio-only: ...`` — video was not started (switch off, library
    without external frames, no source for the face); the call is a plain audio call.
  * ``call with <id> has video (WxH @ N fps)`` and ``first video frame sent`` — the pump is
    running. The first line without the second: no frame arrives (see the next bullets).
  * ``[call_video] the server cannot draw the avatar: ...`` — no GL context (see
    *Server renderer* below).
  * ``no video frame has arrived after 8 s`` / ``no new video frame for N s`` — the source
    stopped sending (server renderer: see the ``[avatar_render]`` lines; browser source: a
    hidden tab is throttled by the browser).
  * ``send_frame failed (n/5)`` — py-tgcalls refused the frames. After five in a row the
    call becomes audio-only (``call with <id> is audio-only now``). If every frame fails,
    suspect the pixel format first: planar limited-range I420 of exactly the configured size.
  * What the caller sees after the source is lost (frozen last frame or nothing) depends on
    Telegram's client; the SyntH stops sending and treats the call as audio from then on.

The SyntH's face in other interfaces' calls
-------------------------------------------

A call hosted outside the WebUI (Telegram) shows the SyntH's face, which is the Karada
render of the model. ``CALL_VIDEO_SOURCE`` says where the frames come from:

* ``server`` (what ``auto`` tries first): the server draws the avatar itself, with no
  browser.
* ``browser``: an open WebUI tab is the camera.
* ``auto`` (default): the server renderer, and a tab if the server cannot draw.

**Server renderer** (``core/avatar_render/``). A small headless renderer built on
``moderngl`` and an EGL context:

* It reads the active ``.vrm`` (0.x or 1.0; ``CALL_VIDEO_MODEL`` to force a file, else the
  active avatar of the Karada state server, else ``skins/Rei/model.vrm``) and draws a close-up
  of the head and shoulders: textured and lit, the arms lowered from the T-pose.
* The animation is its own and small, **not** the Karada animation engine: a little head sway
  and breathing, blinking, the emotion intensities of the Karada state server as expressions
  (happy, sad, angry, relaxed, surprised), and the mouth following the loudness of the
  SyntH's voice while it speaks. No FBX animations, no MToon shading or outlines: it looks
  like the browser render, it is not identical.
* **Nothing to install in the Docker image.** The ``Dockerfile`` installs Mesa's EGL and
  software rasteriser (llvmpipe: ``libegl1``, ``libegl-mesa0``, ``libgl1``,
  ``libgl1-mesa-dri``), links the unversioned ``libEGL.so`` / ``libGL.so`` that moderngl
  opens, and, after ``uv sync``, prints ``avatar renderer GL: llvmpipe ...`` (or a warning,
  without failing the build). ``moderngl`` and ``numpy`` come from ``pyproject.toml``. The
  image grows by the Mesa/LLVM packages (roughly a hundred MB; check with ``docker images``).
  Outside Docker the same Debian packages are needed. The first call tries a GL context once
  (``[call_video] the server can draw the avatar (GL: ...)``); if that fails the log says why
  and the call is audio-only. A host with a GPU uses it. ``LP_NUM_THREADS`` limits
  llvmpipe's threads.
* **Cost** (measured on a 16-core desktop with llvmpipe, 854x480 at 12 fps): about 0.6 of a
  core while a call has video, nothing otherwise; the model loads in about half a second at
  the start of the call. Lower ``CALL_VIDEO_FPS`` / ``CALL_VIDEO_WIDTH`` / ``CALL_VIDEO_HEIGHT``
  on a slow CPU.

**Browser source.** A WebUI tab keeps an idle WebSocket open on ``/api/call/camera`` while the
avatar is on screen (``res/synth_webui/js/call-camera.mjs``, same token as the other call
sockets). When a call asks for video the server sends ``{"type": "start", call_id, fps, width,
height, quality}``; the tab then paints the avatar canvas (page colour behind it, scaled to fit)
into a small canvas on every rendered frame, at most ``fps`` times a second, and sends each
JPEG as a binary message (``{"type": "stop"}`` ends it). The canvas is read from the render
loop itself (``window.__synthFrameTap``) because the WebGL buffer is not preserved between
frames. A frame is accepted only from the tab that was told to start, only for that call, and
only if it is a JPEG under 2 MB. This needs a WebUI tab with the avatar open for the whole call
(a hidden tab is throttled by the browser; the pop-out webcam window keeps rendering). The
browser can be on any device that reaches the WebUI: it does not run on the server.

Either way only the latest frame is kept; the provider reads it at the call's frame rate and
pushes it into its call. If the source is lost (the tab closes, the renderer fails) the call
carries on as audio: ``CallSession.kind`` becomes ``audio``, so the model is no longer told
that the caller sees it.

.. list-table::
   :header-rows: 1

   * - Key
     - Default
     - Meaning
   * - ``TELEGRAM_CALL_VIDEO_ENABLED``
     - ``true``
     - Show the SyntH's face in answered Telegram calls.
   * - ``CALL_VIDEO_SOURCE``
     - ``auto``
     - ``server``, ``browser`` or ``auto`` (see above).
   * - ``CALL_VIDEO_MODEL``
     - (empty)
     - ``.vrm`` the server draws; empty means the active avatar, else ``skins/Rei``.
   * - ``CALL_VIDEO_FPS``
     - ``12``
     - Frames per second (1-30).
   * - ``CALL_VIDEO_WIDTH``, ``CALL_VIDEO_HEIGHT``
     - ``854``, ``480``
     - Size of the call video (made even, 64-1920 by 64-1080).
   * - ``CALL_VIDEO_JPEG_QUALITY``
     - ``0.7``
     - JPEG quality of each frame from a browser tab (0.1-1.0).

Registering an interface
------------------------

.. code-block:: python

   from pathlib import Path
   from core.call_manager import (
       CallCapabilities, CallProvider, CallSession, get_call_manager,
   )

   class MyCallProvider(CallProvider):
       name = "my_interface"
       capabilities = CallCapabilities(audio=True, video=True)

       async def play_audio(self, session: CallSession, path: Path) -> None:
           ...  # play the clip to the caller

   get_call_manager().register_provider(MyCallProvider())

Register in the interface's ``start()`` and call ``unregister_provider()`` when
it stops. An interface that exposes ``start_call`` advertises the ``call``
capability (``core/interface_capabilities.py``). When a turn from that interface
arrives, call ``get_call_manager().mark_turn(message, call_id_or_session)``
before ``message_queue.enqueue()``.

A provider whose video depends on something at call time (Telegram: a source for the face)
can make ``capabilities`` a property; the manager reads it when a call is requested.

Configuration
-------------

.. list-table::
   :header-rows: 1

   * - Key
     - Default
     - Meaning
   * - ``CALL_ENABLED``
     - ``true``
     - Allow calls at all.
   * - ``CALL_ACK_ENABLED``
     - ``true``
     - Play the "tu-din" once the caller's speech was transcribed.
   * - ``CALL_SOUNDS_ENABLED``
     - ``true``
     - Play a pickup sound at call start and a hang-up tone at the end.
   * - ``CALL_LIVE_ENGINE_ENABLED``
     - ``false``
     - Hand calls to a Live engine where the interface supports it.
   * - ``CALL_SCREEN_FRAME_MIN_INTERVAL_SEC``
     - ``2.0``
     - Minimum seconds between accepted screen frames.
   * - ``CALL_SCREEN_MAX_EDGE_PX``
     - ``1280``
     - Longest edge of a kept screen frame.
   * - ``CALL_TURN_INTERRUPT_WINDOW_SEC``
     - ``8.0``
     - A call turn younger than this is cancelled and merged with the caller's next
       utterance (``0`` = never).
   * - ``TELEGRAM_CALLS_ENABLED``
     - ``true``
     - Let the Telegram user account answer calls.
   * - ``TELEGRAM_CALL_ALLOWED_IDS``
     - (empty)
     - Extra user ids, besides the trainer, whose calls are answered.
   * - ``TELEGRAM_CALL_VIDEO_ENABLED``
     - ``true``
     - Show the SyntH's face in answered Telegram calls.
   * - ``CALL_VIDEO_SOURCE``, ``CALL_VIDEO_MODEL``
     - ``auto``, (empty)
     - Where the face comes from (``server``/``browser``/``auto``) and which ``.vrm`` the server draws.
   * - ``CALL_VIDEO_FPS``, ``CALL_VIDEO_WIDTH``, ``CALL_VIDEO_HEIGHT``, ``CALL_VIDEO_JPEG_QUALITY``
     - ``12``, ``854``, ``480``, ``0.7``
     - Frame rate, size and JPEG quality of the call video.
   * - ``TELEGRAM_CALL_RX_RATE``, ``TELEGRAM_CALL_RX_CHANNELS``
     - ``48000``, ``1``
     - Format of the raw audio py-tgcalls delivers from the caller.
