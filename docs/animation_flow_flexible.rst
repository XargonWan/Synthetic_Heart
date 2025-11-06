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
- Graceful stopping
- Various animation combinations
