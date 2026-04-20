=====================
VRM Avatar Animations
=====================

Overview
========

The Synthetic Heart WebUI includes a sophisticated VRM avatar animation system that synchronizes visual feedback with the AI's internal states. The system supports multiple animation states and automatically transitions between them based on the AI's activity.

Animation States
================

Idle State
----------

The idle animation (``Happy Idle.fbx``) is the default state when the AI is not processing or speaking. This animation:

- Replaces the default T-pose with a natural, relaxed stance
- Loads automatically when a VRM model is loaded
- Loops continuously until interrupted by another state

**Technical Implementation:**

- Uses Mixamo FBX format converted to VRM-compatible animation tracks
- Applied via Three.js AnimationMixer with crossfade transitions
- Located at: ``res/synth_webui/animations/Happy Idle.fbx``

Talking State
-------------

The talking animation (``talking.fbx``) activates when the AI generates text responses. The system:

- Estimates speech duration based on word count (~150 words per minute)
- Automatically transitions from idle to talking with a 0.5-second crossfade
- Returns to idle state after the estimated duration

**Duration Calculation:**

.. code-block:: javascript

    const wordCount = text.trim().split(/\s+/).length;
    const estimatedDuration = (wordCount / 150) * 60; // seconds

**Usage Example:**

.. code-block:: javascript

    // Trigger talking animation
    if (window.VRMAnimations) {
        window.VRMAnimations.startTalking(responseText);
    }

Thinking State (Placeholder)
-----------------------------

The thinking state activates when the message chain is processing. Currently implemented as a placeholder:

- Uses the idle animation while the AI processes requests
- Prepared for future custom thinking animation integration
- Sets ``isProcessing`` flag to prevent animation conflicts

**Future Implementation:**

When a thinking animation is added (e.g., ``thinking.fbx``), the system will automatically:

- Load it during initialization
- Crossfade to it when processing starts
- Return to idle when processing completes

Animation System Architecture
==============================

Loading Pipeline
----------------

1. **VRM Model Load:** When a VRM model is loaded via the WebUI:
   
   .. code-block:: javascript

       // Create AnimationMixer
       currentMixer = new THREE.AnimationMixer(vrm.scene);
       
       // Load default animations
       await loadDefaultAnimations(vrm);

2. **Mixamo Conversion:** FBX animations are converted to VRM-compatible format:

   - Bone mapping via ``mixamoVRMRigMap``
   - Quaternion retargeting for proper rotation
   - Hip height adjustment for scale compatibility

3. **Action Setup:** Each animation is configured as a Three.js AnimationAction:

   .. code-block:: javascript

       idleAction = currentMixer.clipAction(idleClip);
       idleAction.play();

State Management
----------------

The animation system maintains several state flags:

- ``currentVRM``: Reference to loaded VRM model
- ``currentMixer``: Three.js AnimationMixer instance
- ``isProcessing``: Boolean flag for thinking state
- ``isSpeaking``: Boolean flag for talking state

**Transition Logic:**

- Only one animation plays at a time
- Crossfade duration: 0.5 seconds
- Prevents overlapping state changes

Integration with Message Chain
===============================

The animation system integrates with the core message chain through global functions:

.. code-block:: javascript

    window.VRMAnimations = {
        startThinking,    // Called when chain starts processing
        stopThinking,     // Called when chain completes
        startTalking,     // Called when response is generated
        stopTalking       // Called after estimated speech duration
    };

**Implementation in Message Handler:**

.. code-block:: javascript

    // When AI starts processing
    window.VRMAnimations?.startThinking();
    
    // When AI generates response
    const response = await generateResponse(message);
    window.VRMAnimations?.stopThinking();
    window.VRMAnimations?.startTalking(response);

3D Environment
==============

The WebUI displays a persistent 3D environment even without a VRM model loaded:

Floor and Grid
--------------

- **Floor:** 10x10m plane with dark material (``0x2a2a2a``)
- **Grid Helper:** 20x20 grid for depth perception
- **Lighting:** Ambient + directional key/fill lights

**Technical Details:**

.. code-block:: javascript

    // Floor setup
    const floorGeometry = new THREE.PlaneGeometry(10, 10);
    const floorMaterial = new THREE.MeshStandardMaterial({ 
        color: 0x2a2a2a, 
        roughness: 0.8,
        metalness: 0.2
    });

Camera and Controls
-------------------

- **Camera:** PerspectiveCamera (FOV: 30°)
- **Position:** (0, 1.4, 2.2) - optimal for humanoid viewing
- **Controls:** OrbitControls with damping enabled
- **Target:** (0, 1.2, 0) - centered on avatar chest height

Adding Custom Animations
=========================

To add new animations to the system:

1. **Export from Mixamo:**

   - Select your animation
   - Download as FBX format
   - Save to: ``res/synth_webui/animations/``

2. **Load in Code:**

   .. code-block:: javascript

       // In loadDefaultAnimations function
       const customClip = await loadMixamoAnimation(
           '/static/animations/custom.fbx', 
           vrm
       );
       customAction = currentMixer.clipAction(customClip);

3. **Create Trigger Function:**

   .. code-block:: javascript

       function startCustomAnimation() {
           if (!currentMixer || !customAction) return;
           
           // Crossfade from current animation
           idleAction.fadeOut(0.5);
           customAction.reset().fadeIn(0.5).play();
       }

4. **Expose Globally:**

   .. code-block:: javascript

       window.VRMAnimations.startCustom = startCustomAnimation;

File Locations
==============

Animation Assets
----------------

- ``res/synth_webui/animations/Happy Idle.fbx`` - Idle pose animation
- ``res/synth_webui/animations/talking.fbx`` - Talking animation
- ``res/synth_webui/js/mixamoVRMRigMap.js`` - Bone mapping definitions
- ``res/synth_webui/js/loadMixamoAnimation.js`` - Animation loader utility

Code Integration
----------------

- ``res/synth_webui/js/vrm-viewer.mjs`` - Main WebUI animation logic (frontend handler)
- Lines 2950-2960: Import statements for FBX and animation loaders
- Lines 3000-3020: Animation system initialization
- Lines 3170-3250: Animation state management functions

Troubleshooting
===============

Animation Not Loading
---------------------

**Symptoms:** VRM model loads but stays in T-pose

**Solutions:**

1. Check browser console for FBX loading errors
2. Verify animation files exist in ``res/synth_webui/animations/``
3. Ensure FBXLoader is imported correctly
4. Check VRM model has proper humanoid bone structure

Animation Doesn't Transition
-----------------------------

**Symptoms:** Animation gets stuck in one state

**Solutions:**

1. Check ``isProcessing`` and ``isSpeaking`` flags
2. Verify AnimationMixer.update() is called in render loop
3. Check crossfade timing (default: 0.5 seconds)
4. Look for JavaScript errors preventing state changes

Performance Issues
------------------

**Symptoms:** Choppy animation or low framerate

**Solutions:**

1. Reduce VRM model polygon count
2. Check if multiple animations are playing simultaneously
3. Verify GPU acceleration is enabled in browser
3. Monitor AnimationMixer update delta time

Browser Compatibility
=====================

The VRM animation system requires:

- **WebGL 2.0** support
- **ES6 Modules** support
- **Modern browser:** Chrome 90+, Firefox 88+, Safari 14+

**Tested Browsers:**

- ✅ Chrome 120+ (Recommended)
- ✅ Firefox 115+
- ✅ Edge 120+
- ✅ Safari 16+
- ⚠️ Mobile browsers (limited performance)

Future Enhancements
===================

Planned Features
----------------

1. **Thinking Animation:** Dedicated animation for processing state
2. **Emotion-based Animations:** Match animations to response sentiment
3. **Lip Sync:** Real-time lip synchronization with audio output
4. **Gesture System:** Hand/body gestures for emphasis
5. **Custom Animation Upload:** WebUI interface for animation management

Contributing
------------

To contribute new animations:

1. Export Mixamo animations in FBX format
2. Test with multiple VRM models for compatibility
3. Document animation purpose and trigger conditions
4. Submit pull request with animation files and integration code

See Also
========

- :doc:`usage` - General WebUI usage guide
- :doc:`architecture` - System architecture overview
- :doc:`interfaces` - Interface integration documentation
