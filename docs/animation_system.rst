=====================
Animation System
=====================

Overview
========

The SyntH Animation System provides a centralized mechanism for managing VRM avatar animations
throughout the application lifecycle. The system coordinates between backend logic and frontend
rendering to create a cohesive and responsive avatar experience.

Architecture
============

The animation system consists of three main components:

Backend Animation Handler
--------------------------

Located in ``core/animation_handler.py``, this component:

- Maps logical animation states to FBX animation files
- Tracks the current animation state
- Sends animation commands to the WebUI via WebSocket
- Manages animation contexts and automatic fallback to Idle

Frontend Animation Handler
---------------------------

Located in ``res/synth_webui/js/vrm-viewer.mjs``, this component:

- Receives animation commands from the backend
- Loads and manages FBX animation files
- Controls the THREE.js AnimationMixer
- Handles smooth transitions between animations

WebUI Integration
-----------------

The ``SynthWebUIInterface`` (``core/webui.py``) coordinates between the backend and frontend:

- Initializes the animation handler on startup
- Triggers animations at appropriate lifecycle points
- Sends animation commands via WebSocket

Animation States
================

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

Components can trigger animations using the global animation handler:

.. code-block:: python

    from core.animation_handler import get_animation_handler, AnimationState
    
    # Get the handler
    handler = get_animation_handler()
    
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

Emitted by ``AnimationHandler._send_animation_command()`` whenever the active
animation changes.  This is the primary playback command.

.. code-block:: json

    {
        "type": "vrm_animation",
        "file": "/skins/Rei/animations/think/Thinking.fbx",
        "state": "think",
        "loop": true,
        "descriptor": {
            "intro":  {"start_frame": 0,  "end_frame": 15},
            "loop":   {"start_frame": 16, "end_frame": 60},
            "outro":  {"start_frame": 61, "end_frame": 90},
            "fps": 30
        }
    }

.. note::

    The key is ``"file"`` (not ``"animation"``).  The legacy ``"type": "animation"``
    spelling is still accepted by ``chat-window.mjs`` for backwards compatibility,
    but the backend always emits ``"vrm_animation"``.

``vrm_model`` — Set active VRM model
--------------------------------------

Emitted by ``AnimationHandler.set_vrm_model()`` when the persona's VRM changes.

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

Emitted to update the avatar's facial expression sliders.

.. code-block:: json

    {
        "type": "vrm_face",
        "values": {"happy": 0.8, "neutral": 0.1}
    }

``vrm_preload`` — Preload an animation file
--------------------------------------------

Asks the frontend to preload an FBX file in the background so it is ready when
``vrm_animation`` requests it.

.. code-block:: json

    {
        "type": "vrm_preload",
        "file": "/skins/Rei/animations/idle/Idle.fbx"
    }

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

1. **Single Source of Truth**: ``AnimationHandler`` maintains the current animation state
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
    Backend (AnimationHandler) 
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

New Methods Added to AnimationHandler
---------------------------------------

``get_full_state() → dict``
    Returns the complete VRM state in a single dict with three keys:

    .. code-block:: python

        {
            "vrm_model":   {"name": "...", "url": "...", "hash": "..."},
            "animation":   {"file": "...", "url": "...", "state": "idle",
                            "loop": True, "descriptor": {...}},
            "face_values": {"happy": 0.0, ...}
        }

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
3. The ``AnimationHandler`` adds the upload root as a **temporary search path** so
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
full responsibility for animation state), it may call the animation handler directly.

Example for an interface that wants to show the avatar is "thinking":

.. code-block:: python

    from core.animation_handler import get_animation_handler, AnimationState
    
    class MyInterface:
        async def handle_message(self, message):
            handler = get_animation_handler()
            
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

Known Issues Fixed
==================

Stale ``window.animationHandler`` (idle-only animation)
---------------------------------------------------------

**Symptom:** Only the Idle animation played; Think/Write state changes were logged by
``chat-window.mjs`` (``vrm_animation received: think``) but ``[AnimationHandler] startAction``
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

Animation handler logs will appear with the prefix ``[AnimationHandler]``.

Limitations
===========

- Animations are only visible in the WebUI interface
- Multiple concurrent animations on the same session may conflict (use context IDs properly)
- Animation files must be Mixamo-compatible FBX format
- File names in the mapping must match exactly (case-sensitive)

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
