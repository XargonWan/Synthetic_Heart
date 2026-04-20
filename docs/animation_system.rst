=====================
Animation System
=====================

Overview
========

The SyntH Animation System is powered by **KaradaStateServer** — the single source of
truth for three independent streams: the active VRM model, the animation state, and
face blend-shape values.  Clients receive push-updates via WebSocket; only real state
changes are broadcast.

For a complete list of HTTP and WebSocket endpoints exposed by the WebUI (including
those used by the animation subsystem) see :doc:`api_endpoints`.

.. note::

   “Karada” (からだ) is the Japanese word for **body**.  The KaradaStateServer is the
   centralized *body state* manager for VRM animations and facial values.  In earlier
   versions this module was casually referred to as the “animation handler”; the new
   name emphasizes its broader role as a general state server and aligns with the
   project’s move away from the legacy `AnimationHandler` concept.

The system coordinates between backend logic and frontend rendering to create a
coherent and responsive avatar experience across any number of simultaneously-connected
WebUI windows.

Architecture
============

The animation system consists of three main components:

Backend: KaradaStateServer
---------------------------

Located in ``core/animation_handler.py``, this component is the canonical VRM state service:

- Maps logical animation states to FBX animation files (clients never
  choose a file directly; they request a state and Karada picks an appropriate
  animation based on the active skin and Rei fallback)
- Tracks and broadcasts the current animation state to **all** connected clients
- Manages VRM model state and pushes it on connect/change
- Manages animation contexts and automatic fallback to Idle

Client‑side Animation Renderer
-----------------------------

Located in ``res/synth_webui/js/vrm-viewer.mjs``, this frontend component:

- Receives animation commands from the backend
- Loads and manages FBX animation files
- Controls the THREE.js AnimationMixer
- Handles smooth transitions between animations

(The previous “Frontend Animation Handler” terminology has been retired to avoid
confusion with the server‑side KaradaStateServer.)

WebUI Integration
-----------------

The ``SynthWebUIInterface`` (``core/webui.py``) coordinates between the backend and frontend:

- Initializes ``KaradaStateServer`` on startup
- Triggers animations at appropriate lifecycle points
- Sends animation commands via WebSocket

Animation States
================

Clients and internal components never supply a file path – they request one of the
logical states below.  Karada (the ``AnimationHandler``) chooses an appropriate
FBX from the active skin (falling back to Rei) and sends that filename to the
front-end.

The system defines four logical animation states:

Idle
----
**Trigger:** No active animations or tasks  
**Files:** ``Idle.fbx``, ``Idle2.fbx``, ``Happy Idle.fbx`` (random selection)  
**Loop:** Yes  
**Description:** Default state when the avatar is not actively processing or responding

Think
-----
**Trigger:** When a message is received from a user  
**Files:** ``Thinking.fbx``  
**Loop:** Yes  
**Description:** Indicates the AI is processing the incoming message

Write
-----
**Trigger:** When the LLM starts generating a response  
**Files:** ``Texting While Standing.fbx``, ``Texting.fbx`` (random selection)  
**Loop:** Yes  
**Description:** Indicates the AI is formulating and writing a response

Talk
----
**Trigger:** Can be triggered by components/plugins for speech output  
**Files:** ``talking.fbx``  
**Loop:** Yes  
**Description:** Indicates the avatar is speaking or vocalizing

Animation Flow
==============

Standard Message Handling
--------------------------

1. **User sends message** → Backend triggers ``THINK`` animation
2. **LLM starts processing** → Backend triggers ``WRITE`` animation
3. **Response complete** → Backend triggers ``IDLE`` animation (via context cleanup)

The animation flow is automatic and managed by the WebUI's message handling logic.

Usage
=====

Backend Usage
-------------

Components access the global ``KaradaStateServer`` instance:

.. code-block:: python

    from core.animation_handler import get_karada_state_server, AnimationState
    
    # Get the server instance
    handler = get_karada_state_server()
    
    # Trigger an animation
    await handler.transition_to(
        AnimationState.THINK,
        session_id="session_123",
        context_id="my_context"
    )
    
    # Stop animation and return to idle
    await handler.stop_animation("my_context", "session_123")

Context Management
------------------

The animation handler uses context IDs to track multiple concurrent animations.
When all contexts are stopped, the handler automatically returns to Idle state.

Example:

.. code-block:: python

    # Start animation with context
    await handler.play_animation(
        AnimationState.WRITE,
        session_id="session_123",
        loop=True,
        context_id="response_generation"
    )
    
    # Later, stop this context
    await handler.stop_animation("response_generation", "session_123")
    # If no other contexts are active, returns to Idle

Frontend WebSocket Protocol
============================

The backend communicates animation state to connected WebUI clients through five
distinct WebSocket message types.  All messages are JSON objects.

``vrm_animation`` — Play an animation
--------------------------------------

Emitted by ``KaradaStateServer._send_animation_command()`` whenever the active
animation changes.  This is the primary playback command.

.. code-block:: json

    {
        "type": "vrm_animation",
        "file": "/skins/Rei/animations/think/Thinking.fbx",
        "state": "think",
        "loop": true,
        "reset_eyes": true,
        "descriptor": {
            "intro":  {"start_frame": 0,  "end_frame": 15},
            "loop":   {"start_frame": 16, "end_frame": 60},
            "outro":  {"start_frame": 61, "end_frame": 90},
            "fps": 30
        },

.. note::
   The descriptor object above comes directly from the companion
   ``<animation>.fbx.json`` file located next to the FBX.  that file is
   the *single source of truth* for loop/intro/outro timings, fps, and
   related metadata; duplicating the same values elsewhere is a bug.
   When no descriptor file exists, the handler synthesises sensible
   defaults (idle animations loop, other states play once, and the
   implicit loop section spans frames ``0``–``max``).

        "animation_state": {
            "action": "think",
            "phase": "loop",
            "animation": "/skins/Rei/animations/think/Thinking.fbx",
            "descriptor": { "..." : "..." },
            "clip": {"name": "Thinking", "duration": 1.47, "fps": 30.0},
            "timing": {"started_at": "2026-03-04T12:00:00Z", "time_in_clip": 0.0, "current_frame": 0},
            "expressions": [],
            "blink": {"auto": true},
            "eye_movement": {"auto": true},
            "emotions": {"dominant": "happy", "values": {"happy": 7.5}},
            "lipsync": false,
            "priority": 10,
            "source": "core"
        }
    }

.. note::

    The key is ``"file"`` (not ``"animation"``).  The legacy ``"type": "animation"``
    spelling is still accepted by ``chat-window.mjs`` for backwards compatibility,
    but the backend always emits ``"vrm_animation"``.

    ``reset_eyes`` is emitted only for **targeted** session plays (not broadcast), so
    each client can perform a smooth eyes-reset when the animation changes.  It is
    **not** included in global broadcasts (``session_id=None``).

    ``animation_state`` is populated only when a descriptor and/or emotions are
    available.  Clients should treat it as optional.

``vrm_model`` — Set active VRM model
--------------------------------------

Emitted by ``KaradaStateServer.set_vrm_model()`` when the persona's VRM changes.

.. code-block:: json

    {
        "type": "vrm_model",
        "name": "SyntH.vrm",
        "url": "/avatars/SyntH.vrm",
        "hash": "sha256:abc123"
    }

The optional ``hash`` field allows clients to skip reloading an already-cached model.

``vrm_face`` — Blend-shape / emotion values
---------------------------------------------

Emitted to update the avatar's facial expression sliders.  Because the
client's smoothing pipeline decays values rapidly, this message is generally
only used for slow-changing EmotionManager state.  (Facial expression tags
use ``vrm_expression_set`` instead.)

.. code-block:: json

    {
        "type": "vrm_face",
        "values": {"happy": 0.8, "neutral": 0.1}
    }

``vrm_expression_set`` / ``vrm_expression_clear`` — instantaneous facial overrides
-------------------------------------------------------------------------------

New in 2026 release.  These packets allow the backend to inject a short-lived
facial expression with high priority, bypassing the normal emotion decay.
They are consumed by the WebUI's expression pipeline which applies a smooth
lerp and automatically removes the source when a matching ``vrm_expression_clear``
packet is received (typically after a cooldown).  This is the mechanism used
by ``[em_*]`` tags inserted into LLM-generated text.

.. code-block:: json

    {"type": "vrm_expression_set", "name": "grin", "intensity": 0.9}

To clear the override and return to the emotional baseline:

.. code-block:: json

    {"type": "vrm_expression_clear"}


``preload_animation`` — Preload an animation file
---------------------------------------------------

Asks the frontend to preload an FBX file in the background so it is ready when
``vrm_animation`` requests it.  Up to 3 IDLE variants are pre-warmed before any
non-IDLE animation plays (see ``ensure_idle_preloaded()``).

.. code-block:: json

    {
        "type": "preload_animation",
        "animation": "/skins/Rei/animations/idle/Idle.fbx",
        "descriptor": {
            "loop": {"start_frame": 0, "end_frame": 120},
            "fps": 30
        }
    }

.. note::

    The message type is ``"preload_animation"`` and the URL key is ``"animation"``.

``animation_state`` — Informational state summary
---------------------------------------------------

A lightweight broadcast that communicates *what* is playing without necessarily
triggering a re-play.  Used by clients that arrived after the original
``vrm_animation`` command was sent and need to know the current state.

.. code-block:: json

    {
        "type": "animation_state",
        "state": "think",
        "animation_file": "Thinking.fbx"
    }

New-client handshake (hello / has_assets)
------------------------------------------

When a new client establishes a WebSocket connection it may send a ``hello``
message listing assets it already has cached:

.. code-block:: json

    { "type": "hello", "has_assets": ["/avatars/SyntH.vrm"] }

The backend calls ``get_missing_assets(has_assets)`` and pushes only the missing
assets to the client, avoiding redundant transfers.  Immediately after, the full
current state (VRM model + active animation + face values) is pushed via
``get_full_state()``.

.. note::

    All ``vrm_animation`` commands are broadcast to **all connected sessions**
    (``session_id=None``), ensuring that every open WebUI window shows the
    same animation simultaneously.

Centralized Animation State
=============================

The animation system maintains a **centralized state on the backend** that is synchronized
across all connected clients. This ensures that when multiple users/devices view the same
avatar simultaneously (through different WebUI windows), they all see the **exact same animation**.

**How It Works**

1. **Single Source of Truth**: ``KaradaStateServer`` maintains the current animation state
   - Current state (IDLE, THINK, WRITE, TALK)
   - Current animation file being played
   - Animation descriptor (frame info for intro/loop/outro)

2. **State Change Notifications**: When an animation changes:
   - Backend notifies all registered callbacks via ``_notify_animation_state_changed()``
   - WebUI broadcasts the new animation state to all connected WebSocket clients
   - Each client receives the identical animation command

3. **New Client Synchronization**: When a client connects:
   - WebSocket endpoint retrieves current animation state via ``get_current_animation_state()``
   - Sends the current animation to the new client before any other messages
   - New client immediately displays the correct animation

**Use Case Example**

::

    Timeline:
    --------
    
    User 1 (Telegram)  → sends message
                       ↓
    Backend (KaradaStateServer)
                   ↓ triggers THINK animation
                   ↓ updates _current_animation_file, _current_animation_descriptor
                   ↓ calls _notify_animation_state_changed()
                   ↓ WebUI broadcasts to all clients
    
    Client A (WebUI, Device 1) ← receives THINK animation
    Client B (WebUI, Device 2) ← receives THINK animation  (same video view!)
    Client C (WebUI, Phone)    ← receives THINK animation
    
    All three devices see the same avatar doing the same THINKING motion simultaneously.

**Configuration**

No special configuration required. The synchronization is automatic:

1. Backend calls ``register_animation_state_changed_callback()`` during initialization
2. WebUI broadcasts to all connected clients when animation changes
3. New clients receive current state on connection

KaradaStateServer API
----------------------

The following public methods are available for plugins and interfaces.

``get_full_state() → dict``
    Returns the complete VRM state in a single dict with four keys:

    .. code-block:: python

        {
            "vrm_model":   {"name": "...", "url": "...", "hash": "..."},
            "animation":   {"file": "...", "url": "...", "state": "idle",
                            "loop": True, "descriptor": {...}},
            "face_values": {"happy": 0.0, ...},
            "audio":       {"url": "...", "audio_duration_s": 3.2,
                            "offset_s": 0.7, "lipsync_data": null}
        }

    The ``audio`` key is ``None`` when no TTS audio is currently playing.
    Called on every new WebSocket connection to push the current state to
    the newly-connected client.

``async set_vrm_model(url, name, hash_=None) → None``
    Stores the active VRM model info internally and broadcasts a
    ``vrm_model`` message to all connected WebSocket clients.  Should be
    called by the persona manager or WebUI when the active VRM file changes.

``get_missing_assets(has_assets: list[str]) → list[str]``
    Given a list of asset URLs that the client already has cached, returns
    the subset of server-known assets (currently the active VRM) the client
    is missing.  Used during the hello/has_assets handshake.

``register_state_animations(state, animations: dict[str, list[str]], sequential=False) → None``
    Override plugin animations for a logical state.  ``animations`` is a dict
    with optional keys ``loop``, ``post``, ``other``, each mapping to a list of
    FBX file names.

    .. code-block:: python

        handler.register_state_animations(
            "think",
            {"loop": ["DeepThought.fbx"], "post": ["PostThink.fbx"]},
            sequential=True,
        )

``add_temporary_search_path(path: Path) → None``
    Prepend a high-priority search path (used by animation uploads).  Temporary
    paths are tracked separately and can be removed via
    ``remove_temporary_search_path()``.

``remove_temporary_search_path(path: Path) → None``
    Remove a previously-added temporary search path.

``get_animation_variants(state: str) → dict``
    Returns discovered animation variants for a state, classified into three
    buckets:

    .. code-block:: python

        {
            "loop":  ["Thinking.fbx"],          # descriptor has loop section (or play_once=False)
            "post":  ["ThinkPost.fbx"],          # descriptor has play_once=True
            "other": ["Unclassified.fbx"],       # no descriptor at all
        }

``async ensure_idle_preloaded(session_id=None) → None``
    Pre-warms up to 3 IDLE animation variants.  Called automatically before
    any non-IDLE animation plays so that returning to IDLE is instant.

Adding New Animations
=====================

Backend
-------
1. Place your FBX file(s) under the active skin's ``animations/<state>/`` folder, e.g.

    skins/Rei/animations/think/Thinking.fbx

2. Optionally add a JSON descriptor alongside an FBX file named ``<animation>.fbx.json``
    to describe ``intro``, ``loop`` and ``outro`` frame ranges or a ``play_once`` flag.

3. The backend will dynamically discover available animations. Plugins may also:

    - Register override lists via ``register_state_animations(state, animations, sequential=False)``
    - Register aliases via ``register_state_aliases({})``
    - Add search paths via ``set_animation_search_paths([...])``

Frontend
--------

1. Ensure the animation files are accessible via the ``/animations/`` endpoint
2. Update the ``animationMappings`` in the WebUI template if adding a new state:

.. code-block:: javascript

    const animationMappings = {
        think: ['Thinking.fbx'],
        write: ['Texting While Standing.fbx', 'Texting.fbx'],
        talk: ['talking.fbx'],
        idle: ['Idle.fbx', 'Idle2.fbx', 'Happy Idle.fbx'],
        custom: ['CustomAnimation.fbx']  // New animation
    };

Temporary Animation Uploads
===========================

The WebUI exposes endpoints to upload **temporary** animations that do not modify
the active persona skin until explicitly promoted. Uploaded files are stored under
``skins/temp/<upload_id>/animations/<state>/`` with a companion metadata file at
``skins/temp/<upload_id>/meta.json``.

**Upload flow**

1. Client uploads an FBX/VRMA to ``POST /api/animations/upload``.
2. The server writes the file to ``skins/temp/<upload_id>/animations/<state>/``.
3. The ``KaradaStateServer`` adds the upload root as a **temporary search path** so
   the animation can be discovered without touching the active skin.

**Promotion flow**

When you are ready to make the animation permanent, call
``POST /api/animations/promote`` to copy the upload into
``skins/<persona>/animations/<state>/``.

**Endpoints**

- ``POST /api/animations/upload`` (multipart)
- ``GET /api/animations/uploads``
- ``DELETE /api/animations/uploads/{upload_id}``
- ``POST /api/animations/promote``

**Notes**

- Temporary uploads are prioritized using search paths; they can be removed at any time.
- Descriptors can be provided alongside the upload as JSON (`<file>.fbx.json`).
- Cleanup runs automatically based on ``SYNTH_MATEENGINE_UPLOAD_TTL_DAYS`` (default: 7 days).
- Promotion is guarded by ``SYNTH_MATEENGINE_PROMOTE_ENABLED=1``.

Integration with Interfaces
============================

While the WebUI interface automatically manages animations for message handling,
other interfaces (Telegram, Discord, Matrix) can integrate with the animation system.

Preferred integration pattern
-----------------------------

Interfaces should generally **not** broadcast animation states directly on message receipt.
The core message queue is the fallback owner of the lifecycle:

- Message accepted/enqueued → THINK
- Generation start → WRITE (or TALK)
- Generation end → IDLE

If an interface needs a different mapping (e.g. TTS prefers TALK instead of WRITE), it
should pass override hints through the message context (or implement the optional
interface hooks used by the queue) rather than bypassing the core chain.

Direct control (advanced)
------------------------

If an interface explicitly opts out of the core queue animation broadcast (and takes
full responsibility for animation state), it may call the server directly.

Example for an interface that wants to show the avatar is "thinking":

.. code-block:: python

    from core.animation_handler import get_karada_state_server, AnimationState
    
    class MyInterface:
        async def handle_message(self, message):
            handler = get_karada_state_server()
            
            # Get the session_id from WebUI if available
            # Note: This only works if the user has a WebUI session
            # For pure Telegram/Discord, animations are WebUI-only
            webui_session = self.get_webui_session_for_user(message.from_user.id)
            
            if webui_session:
                await handler.transition_to(
                    AnimationState.THINK,
                    session_id=webui_session,
                    context_id=f"interface_{message.message_id}"
                )

Flexible Animation Sections (Intro/Loop/Outro)
================================================

The animation system supports flexible combinations of intro, loop, and outro sections,
allowing for more sophisticated animation sequences:

**Full Animation Flow**

An animation can define up to three sections:

- **Intro**: Initial/setup frames (e.g., transition into thinking pose)
- **Loop**: Repeating frames that play continuously (e.g., thinking motion)
- **Outro**: Wind-down/transition frames (e.g., returning to rest pose)

.. code-block:: json

    {
      "intro": {"start_frame": 0, "end_frame": 20},
      "loop": {"start_frame": 21, "end_frame": 120},
      "outro": {"start_frame": 121, "end_frame": 160}
    }

**Smart Playback**

When ``play_animation()`` is called:

- If ``loop`` section exists → always loop until ``stop_animation()`` is called
- If only ``intro`` → play once and stop automatically
- WebUI uses the descriptor to determine which frames to play


**Graceful Stopping**

When ``stop_animation()`` is called:

- If ``outro`` exists → play outro sequence before returning to Idle
- If no outro → immediately return to Idle
- Duration calculated from frame count (approximately 30fps)

**Supported Combinations**

All combinations work correctly:

- intro + loop + outro: Full animation flow
- loop + outro: Repeating with graceful ending
- intro + loop: Intro then repeating motion
- loop only: Simple repeating animation
- intro + outro: One-shot animation
- Any solo section: Works as expected

See :doc:`animation_flow_flexible` for detailed documentation.

``effective_loop`` Determination
==================================

``play_animation()`` computes the effective loop behaviour from the descriptor and the
requested ``loop`` parameter according to the following hierarchy:

.. list-table:: effective_loop decision table
   :header-rows: 1

   * - Condition
     - ``effective_loop``
   * - State is IDLE
     - ``True`` (always)
   * - Descriptor has ``intro`` **or** ``outro`` **and** ``loop`` section
     - ``True``
   * - Descriptor has ``intro`` / ``outro`` but **no** ``loop`` section
     - ``False`` (play once)
   * - Descriptor has ``loop`` only + ``play_once: true``
     - ``False`` (loop plays once)
   * - Descriptor has ``loop`` only, no flags
     - ``True``
   * - Descriptor present, ``play_once: true``, no sections
     - ``False``
   * - No descriptor
     - Respects the ``loop`` parameter passed to ``play_animation()``

When ``effective_loop`` is ``False`` and the state is not IDLE, a background task
(``_non_loop_fallback``) schedules a return to IDLE after the clip completes,
acting as a safety net in case the client does not send a completion event.

The duration passed to ``_non_loop_fallback`` is calculated as follows:

* If a descriptor is available, all defined sections (``intro``, ``loop``,
  ``outro``) are measured in frames, converted to seconds using the descriptor's
  ``fps`` value (default 30), and summed together.  A safety buffer of 1.5&nbsp;seconds
  is then added to accommodate network latency and the front end's own "finished"
  event handling.
* If no descriptor or usable frame information exists, a conservative default of
  3&nbsp;seconds is used before adding the 1.5&nbsp;second buffer.

This scheme prevents the backend fallback from firing partway through an
animation's outro – a problem that used to manifest as the VRM dropping into
T‑pose mid‑transition when playing non‑looping clips such as ``write``.

IDLE Rotation Loop
==================

When IDLE has multiple FBX variants, the ``_rotation_loop()`` background task
switches to the next variant every 30–60 seconds (random interval).  The default
mode is **sequential** (all IDLE animations are listed alphabetically in order);
other states use random selection by default.

.. code-block:: python

    # Register a state as sequential (cycles in order, no repeat)
    handler.register_state_animations(
        "idle",
        {"loop": ["Idle.fbx", "Idle2.fbx", "Happy Idle.fbx"]},
        sequential=True,
    )

The rotation task is cancelled automatically when a higher-priority context
starts and restarted when returning to IDLE.

Smart Eye Behaviour
===================

The frontend automatically suspends blink and saccade loops when the avatar's
``eyes_closed`` blend-shape exceeds ``0.5`` (e.g. during a blinking animation).
They resume once the value drops below the threshold.

Additionally, the ``eyes_closed`` value is **clamped to 0.85** to prevent
visual artefacts (eyelash/cheek clipping).

The backend emits ``"reset_eyes": true`` in every **targeted** (non-broadcast)
``vrm_animation`` command so that the client can smoothly reset eye state when
a new animation starts.

Known Issues Fixed
==================

Stale ``window.animationHandler`` (idle-only animation)
---------------------------------------------------------

**Symptom:** Only the Idle animation played; Think/Write state changes were logged by
``chat-window.mjs`` (``vrm_animation received: think``) but ``[KaradaStateServer] startAction``
never appeared in the console.

**Root cause:** ``window.animationHandler`` was set at module-load time (when
``vrm-viewer.mjs`` was parsed), at which point the closure variable ``animationHandler``
was still ``null``.  The real ``AnimationHandler`` instance was created inside
``loadDefaultAnimations()`` (called after a VRM file is loaded) and assigned only to
the *module-scoped closure variable*, never back to ``window.animationHandler``.
Every subsequent call to ``VRMAnimations.play()`` hit the guard
``if (!window.animationHandler) return`` and silently exited.

**Fix (vrm-viewer.mjs, 2026-03-03):**

1. ``loadDefaultAnimations()`` now updates ``window.animationHandler`` immediately
   after creating the real instance:

   .. code-block:: javascript

       animationHandler = new AnimationHandler(currentMixer, vrm);
       window.animationHandler = animationHandler; // ← added

2. ``VRMAnimations.play / preload / setFaceValues`` use the closure variable first,
   falling back to the global only as a safety net:

   .. code-block:: javascript

       const handler = animationHandler || window.animationHandler;
       if (!handler) return;
       handler.startAction(state, animation, playOnce, playSection, descriptor);

Debugging
=========

Enable debug logging to see animation state changes:

.. code-block:: bash

    export LOGGING_LEVEL=debug

Animation state server logs appear with the prefix ``[KaradaStateServer]``.

HTTP fallback
--------------

If a WebSocket client cannot receive the state via push, it can poll:

.. code-block:: bash

    GET /api/animation_state

The frontend (``vrm-viewer.mjs``) uses this as a safety net on reconnect.

Limitations
===========

- Animations are only visible in WebUI and Karada API clients
- Multiple concurrent animations on the same session may conflict (use context IDs properly)
- Animation files must be Mixamo-compatible FBX format
- File names in the mapping must match exactly (case-sensitive)

Transport Layer
===============

KaradaStateServer is decoupled from any specific I/O mechanism through an abstract
**transport** layer (``core/karada_transport.py``).  Each transport implements
``KaradaTransport`` and is registered at runtime via ``add_transport()``.

Built-in transports:

- **WebSocketTransport** (``core/karada_ws_transport.py``) — wraps the WebUI's
  ``connections`` dict and delegates to ``WebSocket.send_json()``.
- **KaradaApiTransport** (``core/karada_api.py``) — serves clients that connect
  through the public REST + WebSocket API (``/api/karada/ws``).

Custom transports (e.g. a native desktop client or XR headset) only need to
subclass ``KaradaTransport`` and call ``handler.add_transport(transport)``.

.. code-block:: python

    from core.karada_transport import KaradaTransport

    class MyTransport(KaradaTransport):
        async def broadcast_animation(self, payload: dict) -> None: ...
        async def broadcast_audio(self, payload: dict) -> None: ...
        async def broadcast_face(self, payload: dict) -> None: ...
        async def broadcast_model(self, payload: dict) -> None: ...
        async def broadcast_expression(self, payload: dict) -> None: ...
        async def send_to_session(self, session_id: str, payload: dict) -> None: ...
        async def preload_asset(self, session_id: str | None, payload: dict) -> None: ...
        def get_connected_sessions(self) -> list[str]: ...

    handler.add_transport(MyTransport())

Priority & Preemption
=====================

Every animation state has a numeric priority (higher = more important).  When
``play_animation()`` is called, the server compares the new request's priority
against the currently active priority.  Lower-priority requests are silently
rejected.

Default priorities (defined in ``ANIMATION_STATE_PRIORITIES``):

.. list-table::
   :header-rows: 1

   * - State
     - Priority
   * - IDLE
     - 0
   * - WRITE
     - 3
   * - TALK
     - 5
   * - THINK
     - 10

Plugins can register custom priorities:

.. code-block:: python

    handler.register_state_priority("touch", 7)

Audio State Tracking
====================

KaradaStateServer tracks the currently playing TTS audio so that late-joining
clients can resume playback from the correct offset.

.. code-block:: python

    # Called by the WebUI when TTS audio starts
    handler.set_current_audio("/static/audio/tts/reply_42.wav", duration_s=3.2)

    # Late-joining client receives this when it connects
    audio = handler.get_current_audio()
    # → {"url": "...", "audio_duration_s": 3.2, "offset_s": 0.7, "lipsync_data": ...}

Audio state is automatically cleared when the estimated playback duration
(plus a small buffer) has elapsed.  ``get_full_state()`` includes an ``"audio"``
key so late-joiners get everything in one shot.

Watchdog
========

A background watchdog task (10 s interval) detects stuck animation states — for
example if THINK remains active after all ``_active_tasks`` have been cleared due
to a race condition or bug.  When a stuck state is detected, the watchdog forces
a return to IDLE.

The watchdog starts automatically when the first transport is registered.  No
manual configuration is needed.

Karada REST & WebSocket API
============================

A public API router is mounted at ``/api/karada/`` and exposes the full body
state to external clients (native apps, XR headsets, monitoring dashboards).

**State endpoints** (GET):

- ``/api/karada/state`` — full state (model + animation + face + audio)
- ``/api/karada/state/animation`` — current animation only
- ``/api/karada/state/model`` — current VRM model
- ``/api/karada/state/face`` — current face blend-shapes
- ``/api/karada/state/audio`` — current audio playback (if any)

**Action endpoints** (POST):

- ``/api/karada/action`` — request a state change (``{"state": "think"}``)

**Discovery endpoints** (GET):

- ``/api/karada/animations/{state}`` — list available animations for a state
- ``/api/karada/animations/{state}/{file}/descriptor`` — get descriptor for a file
- ``/api/karada/skins`` — list available skins

**Asset distribution** (GET / POST):

- ``/api/karada/assets/manifest`` — SHA-256 asset manifest for cache validation
- ``/api/karada/assets/missing`` — given a list of owned hashes, returns missing assets

**WebSocket** (WS):

- ``/api/karada/ws`` — real-time push stream (same protocol as the WebUI WS)

Future Enhancements
===================

Potential improvements to the animation system:

- Dynamic animation blending based on response content
- Configurable animation mappings via config system
- Integration with TTS for lip-sync animations
- Binary (non-URL) file transfer for embedded VRM assets

See Also
========

- :doc:`vrm_animations` - VRM animation file documentation
- :doc:`component_pattern` - Component development patterns
- :doc:`interfaces` - Interface development guide
