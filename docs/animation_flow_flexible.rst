Animation Flow System - Flexible Intro/Loop/Outro
====================================================

Overview
--------
The animation system now supports flexible combinations of intro, loop, and outro sections.
Each animation can define any combination of these sections, and the system will intelligently
handle playback based on what's available.

The legacy ``play_once`` flag is still supported but works differently depending on the animation structure.


Animation Structure
-------------------
Each animation can have up to three sections defined in its `.fbx.json` descriptor:

.. code-block:: json

    {
      "intro": {
        "start_frame": 0,
        "end_frame": 20
      },
      "loop": {
        "start_frame": 21,
        "end_frame": 120
      },
      "outro": {
        "start_frame": 121,
        "end_frame": 160
      }
    }

Sections are optional and can be defined in any combination.


Play Once Flag Behavior
-----------------------

The ``play_once`` flag interacts differently with animation structures:

**Case 1: play_once + intro/outro (CONFLICT)**

.. code-block:: json

    {
      "play_once": true,
      "intro": {"start_frame": 0, "end_frame": 20},
      "outro": {"start_frame": 121, "end_frame": 160}
    }

Behavior:
- ``play_once`` flag is **IGNORED** (structured sections take precedence)
- A **warning** is logged explaining the conflict
- Animation executes its intro → outro flow normally
- Rationale: intro/outro define a complete structured flow; play_once is redundant

**Case 2: play_once + loop only (COMPATIBLE)**

.. code-block:: json

    {
      "play_once": true,
      "loop": {"start_frame": 0, "end_frame": 100}
    }

Behavior:
- Loop section plays **once only** (not repeated)
- No looping occurs
- Useful for isolating a portion of animation to play once
- Rationale: ``loop`` defines frame range to use; ``play_once`` restricts to single playback


Supported Combinations
----------------------

1. **Full Animation (intro + loop + outro)**
   
   Playback flow::
   
       START → [INTRO] → [LOOP (repeat)] → STOP command → [OUTRO] → IDLE
   
   Example: Thinking animation starts with intro frames, loops the thinking motion,
   and ends with outro frames before returning to idle.


2. **Loop with Outro (loop + outro)**
   
   Playback flow::
   
       START → [LOOP (repeat)] → STOP command → [OUTRO] → IDLE
   
   Example: A repeating animation that has a graceful ending sequence.


3. **Intro + Loop (no outro)**
   
   Playback flow::
   
       START → [INTRO] → [LOOP (repeat)] → STOP command → IDLE
   
   Example: Animation starts with intro but stops immediately without outro.


4. **Loop Only (no intro, no outro)**
   
   Playback flow::
   
       START → [LOOP (repeat)] → STOP command → IDLE
   
   Special case with ``play_once``:
   
   .. code-block:: json
   
       {
         "play_once": true,
         "loop": {"start_frame": 30, "end_frame": 90}
       }
   
   Behavior: Plays loop section once only (doesn't repeat). Useful for extracting
   a portion of animation and playing it as a one-shot.


5. **Intro + Outro (no loop)**
   
   Playback flow::
   
       START → [INTRO] → [OUTRO] → IDLE
   
   Example: One-shot animation with setup and teardown.


6. **Intro Only (no loop, no outro)**
   
   Playback flow::
   
       START → [INTRO] → IDLE
   
   Example: Quick animation that plays once and stops.


7. **Solo Sections**
   
   - **Loop only**: Repeating animation, stops immediately
   - **Outro only**: Ending animation (unusual)
   - **Intro only**: One-shot animation


Implementation Details
----------------------

Animation Analysis
^^^^^^^^^^^^^^^^^^^
The handler includes ``_analyze_animation_structure()`` method that detects which
sections are present in a descriptor:

.. code-block:: python

    structure = handler._analyze_animation_structure(descriptor, animation_file)
    # Returns: {
    #   "has_intro": bool,
    #   "has_loop": bool,
    #   "has_outro": bool
    # }

This analysis also validates the ``play_once`` flag and logs warnings if conflicts are detected.


Play Animation Logic
^^^^^^^^^^^^^^^^^^^^
When ``play_animation()`` is called:

1. Load descriptor and analyze structure
2. Determine effective loop behavior:
   
   - If has intro/outro (structured): 
     
     - If also has loop → loop=True
     - Else → loop=False (play once through structure)
     - play_once flag is ignored with warning
   
   - Else if only loop + play_once flag:
     
     - loop=False (plays once only, doesn't repeat)
   
   - Else if only loop:
     
     - loop=True (repeats normally)
   
   - Else:
     
     - Use provided loop parameter

3. Send animation command with descriptor to WebUI
4. WebUI uses frame ranges to play correct sections
5. No rotation task started for structured animations


Stop Animation Logic
^^^^^^^^^^^^^^^^^^^^
When ``stop_animation()`` is called:

1. Check if animation has ``outro`` section
2. If has outro:
   
   - Send animation command to play outro
   - Calculate duration based on frame count (approx 30fps)
   - Wait for outro to complete
   - Then transition to Idle

3. If no outro:
   
   - Immediately transition to Idle


WebUI Integration
-----------------
The WebUI receives animation commands with this structure:

.. code-block:: python

    {
        "type": "animation",
        "animation": "/skins/Rei/animations/Thinking.fbx",
        "loop": true,
        "state": "think",
        "descriptor": {
            "intro": {"start_frame": 0, "end_frame": 20},
            "loop": {"start_frame": 21, "end_frame": 120},
            "outro": {"start_frame": 121, "end_frame": 160}
        }
    }

The WebUI uses this information to:
- Play specific frame ranges
- Handle looping logic for the loop section
- Prepare outro frames for graceful stopping


Optional ``animation_state`` payload (facial state)
-----------------------------------------------

Alongside ``type: "animation"`` commands, the backend may include an **optional** rich ``animation_state``
object. This is backward compatible: if absent, the WebUI behaves as before.

``animation_state`` is designed for a **hybrid** approach:

- The backend can provide suggestions (descriptor ``expressions`` / ``blink`` / ``eye_movement``) and the
  current emotion snapshot (``emotions``).
- The WebUI applies facial changes by resolving logical keys via the active skin's ``persona.json``
  (``blendshape_map``), so different skins can map emotions/visemes/blendshapes differently.

Minimal example (shortened):

.. code-block:: json

    {
      "type": "animation",
      "state": "write",
      "animation": "/skins/Rei/animations/Write/Texting.fbx",
      "descriptor": { "loop": {"start_frame": 0, "end_frame": 120} },
      "animation_state": {
        "action": "write",
        "phase": "loop",
        "animation": "/skins/Rei/animations/Write/Texting.fbx",
        "descriptor": { "loop": {"start_frame": 0, "end_frame": 120} },
        "clip": { "name": "Texting", "duration": 4.0, "fps": 30 },
        "timing": { "started_at": "2025-12-20T20:00:00Z", "time_in_clip": 0.0, "current_frame": 0 },
        "expressions": [],
        "blink": { "auto": true, "rate_s": 3.5, "intensity": 0.6 },
        "eye_movement": { "auto": true, "saccade_rate_s": 2 },
        "emotions": { "dominant": "happy", "values": { "happy": 7.5, "calm": 5.2 } },
        "lipsync": false
      }
    }

Notes:

- ``lipsync`` is a boolean consent flag only (default: ``false`` when not present).
- ``expressions.targets`` uses logical keys; the WebUI resolves them using the skin mapping.


Emotion overlay (client-side)
-----------------------------

When ``animation_state.emotions`` is present, the WebUI may apply a short "emotion overlay" facial pose:

- pick the strongest emotion from ``emotions.values`` (ties are broken randomly)
- wait a small random delay **after action start**
- apply the corresponding face for a random duration that scales with the emotion intensity

This is intentionally **not** tied to WRITING specifically, because plugins may override or bypass the
writing phase/action. The mapping is done through ``persona.json`` under ``blendshape_map.emotions``.


Optional ``animation_state`` payload (facial state)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

In addition to the legacy fields (``state``, ``animation``, ``descriptor``), the backend may attach a richer
``animation_state`` object to the WebSocket payload. This is **optional** and fully backward compatible.

Note: the lightweight animation state summary broadcast (used for state synchronization) will also try to
enrich the ``animation_state`` with runtime emotions when an Emotion Manager plugin is available.

Typical use cases:

- Provide a single structured snapshot for facial controllers (expressions, blink, eye movement)
- Expose emotional state to the UI (for skin-specific mapping)
- Declare lip-sync consent via a simple boolean flag

Example (shortened)::

    {
      "type": "animation",
      "state": "think",
      "animation": "/skins/Rei/animations/Think/Thinking.fbx",
      "descriptor": { ... },
      "animation_state": {
        "action": "think",
        "phase": "loop",
        "animation": "/skins/Rei/animations/Think/Thinking.fbx",
        "descriptor": { ... },
        "clip": { "name": "Thinking", "duration": 2.34, "fps": 30 },
        "timing": { "started_at": "2025-12-17T20:00:00Z", "time_in_clip": 1.2, "current_frame": 36 },
        "expressions": [ ... ],
        "blink": { "auto": true, "rate_s": 3.5, "intensity": 0.6 },
        "eye_movement": { "auto": true, "saccade_rate_s": 2 },
        "emotions": { "dominant": "happy", "values": { "happy": 7.5, "calm": 5.2 } },
        "lipsync": false
      }
    }

Notes:

- ``lipsync`` is a boolean consent flag only (default: ``false`` when not provided).
- Blink defaults are tuned to a human-like frequency (~15–20 blinks/min), i.e. roughly one blink every 3–4 seconds.
- The WebUI emits browser events when a rich ``animation_state`` is received:

  - ``synth_animation_state_updated`` (detail: full state)
  - ``synth_animation_lipsync_changed`` (detail: ``{ lipsync: boolean }``)


Backward Compatibility
----------------------
- Animations without descriptors work as before (use provided loop parameter)
- The legacy ``play_once`` flag is still supported
  
  - With intro/outro: ignored (warning logged)
  - With loop only: plays loop once
  - Without structured sections: plays animation once

- Existing animations continue to work unchanged


Creating New Animations
------------------------
To create an animation with intro/loop/outro:

1. Create the FBX animation with:
   
   - Intro frames: setup/transition frames
   - Loop frames: repeating motion frames
   - Outro frames: wind-down/transition frames

2. Create a `.fbx.json` descriptor:

   .. code-block:: json

       {
         "intro": {
           "start_frame": 0,
           "end_frame": 29
         },
         "loop": {
           "start_frame": 30,
           "end_frame": 119
         },
         "outro": {
           "start_frame": 120,
           "end_frame": 149
         }
       }

3. Save both files in the same directory:
   
   - ``animations/state/Name.fbx``
   - ``animations/state/Name.fbx.json``


Testing
-------
Run the animation flow tests:

.. code-block:: bash

    python test_animation_flow.py

Tests verify:
- Descriptor loading
- Structure analysis
- Loop behavior determination
- play_once flag handling
- Outro playback

Animation State (server → WebUI)
-------------------------------

The backend may include an optional ``animation_state`` object in the WebSocket payload. This object provides fine-grained instructions and the current emotional state for client-side facial animation. Example schema (abridged):

.. code-block:: json

    {
      "animation_state": {
        "action": "think",
        "phase": "loop",
        "descriptor": { ... },
        "clip": { "name": "Thinking", "duration": 2.34, "fps": 30 },
        "timing": { "started_at": "2025-12-17T20:00:00Z", "time_in_clip": 1.2, "current_frame": 36 },
        "expressions": [ { "start_frame":0, "end_frame":15, "targets": { "eyes.closed": 0.1, "mouth.O": 0.02 }, "source": "server", "priority": 10 } ],
        "blink": { "auto": true, "rate_s": 4, "intensity": 0.6 },
        "eye_movement": { "auto": true, "saccade_rate_s": 2 },
        "emotions": { "dominant": "happy", "values": { "happy": 7.5, "calm": 5.2 } },
        "lipsync": false
      }
    }

Notes:

- ``animation_state`` is optional and preserved for backward compatibility if missing.
- ``lipsync`` is a boolean flag (default ``false``) — it is a signal that lip‑sync may be enabled by a consumer, it does not automatically start lip‑sync processing.
- The WebUI is responsible for resolving expression targets to per‑skin blendshapes using ``skins/<skin>/persona.json`` (``blendshape_map``) and applying smoothing locally.

Per-skin persona mapping
^^^^^^^^^^^^^^^^^^^^^^^^^

Place mappings in ``skins/<skin>/persona.json`` under the ``blendshape_map`` key. Example:

.. code-block:: json

    {
      "blendshape_map": {
         "happy": "Smile",
         "mouth.O": "Vowel_O"
      },
      "emotion_speed": { "default": 6.0, "decay": 4.0 }
    }

The WebUI will fetch ``/skins/<skin>/persona.json`` and apply the mapping when resolving targets from ``animation_state.expressions``.

Testing & manual QA
^^^^^^^^^^^^^^^^^^^^

 - A small manual test harness is available at ``core/webui_templates/animation_face_test.html`` to exercise face expressions and verify that ``blendShapeProxy.setValue`` is invoked.
 - The WebUI template now emits global events ``synth_animation_state_updated`` and ``synth_animation_lipsync_changed`` that can be used by other consumers.
- Graceful stopping
- Various animation combinations
