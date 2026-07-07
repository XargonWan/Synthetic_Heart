/**
 * Karada v2 Animation Engine
 *
 * Client-side animation engine that handles:
 * - Animation clock (local time based on server started_at)
 * - Descriptor-driven state machine (intro → loop → outro)
 * - Crossfade transitions between animations
 * - Pose safety and idle fallback
 *
 * Server ONLY sends: state + descriptor + started_at
 * Client decides HOW and WHEN to play (no server phase control)
 */

import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js';
import { AnimationUtils } from '/js/AnimationUtils.js';


// Module-scoped state
let mixer = null;
let currentAction = null;
let currentClip = null;
let currentDescriptor = null;
let currentState = null;
let currentDescriptorId = null;
let currentStartedAtKey = null;
let animationStartedAt = 0; // Local ms timestamp from server started_at
let animationClock = 0; // (Date.now() - animationStartedAt) / 1000

// Descriptor state machine
let currentSection = 'loop'; // 'intro', 'loop', 'outro'
let sectionStartTime = 0;
let isTransitioning = false;
let transitionFromAction = null;
let transitionToAction = null;

// Crossfade configuration
const CROSSFADE_DURATION = 0.3; // seconds

// Idle fallback
let idleAction = null;
let idleClip = null;
const IDLE_FALLBACK_STATE = 'idle';

// Callbacks for state changes
let onStateChangeCallback = null;
let onSectionChangeCallback = null;

/**
 * Initialize the animation engine
 * @param {THREE.AnimationMixer} mixerInstance - The Three.js animation mixer
 */
function initAnimationEngine(mixerInstance) {
    mixer = mixerInstance;
    console.log('[KaradaEngine] Initialized');
}

/**
 * Parse server timestamp to local animation start time
 * @param {string} startedAtIso - ISO timestamp from server
 * @returns {number} Local ms timestamp
 */
function parseStartedAt(startedAtIso) {
    try {
        if (typeof startedAtIso === 'number' && Number.isFinite(startedAtIso)) {
            return startedAtIso < 1e12 ? startedAtIso * 1000 : startedAtIso;
        }
        if (typeof startedAtIso === 'string' && startedAtIso.trim() !== '') {
            const numericValue = Number(startedAtIso);
            if (Number.isFinite(numericValue)) {
                return numericValue < 1e12 ? numericValue * 1000 : numericValue;
            }
        }
        return new Date(startedAtIso).getTime();
    } catch (e) {
        console.warn('[KaradaEngine] Failed to parse started_at:', e);
        return Date.now();
    }
}

/**
 * Update animation clock (call this in render loop)
 * @returns {number} Current animation time in seconds
 */
function updateAnimationClock() {
    if (!animationStartedAt) {
        animationClock = 0;
        return 0;
    }
    animationClock = (Date.now() - animationStartedAt) / 1000;
    return animationClock;
}

/**
 * Get current animation time
 * @returns {number} Animation time in seconds
 */
function getAnimationTime() {
    return animationClock;
}

/**
 * Create animation action from clip with descriptor info
 * @param {THREE.AnimationClip} clip - The animation clip
 * @param {Object} descriptor - Animation descriptor with intro/loop/outro
 * @returns {THREE.AnimationAction} Configured action
 */
function createActionFromClip(clip, descriptor) {
    if (!mixer || !clip) return null;

    const action = mixer.clipAction(clip);
    action.clampWhenFinished = true;
    action.loop = THREE.LoopRepeat;

    return action;
}

/**
 * Configure action for a specific descriptor section
 * @param {THREE.AnimationAction} action - The animation action
 * @param {Object} descriptor - Animation descriptor
 * @param {string} section - Section name ('intro', 'loop', 'outro')
 * @param {number} fps - Frames per second
 */
function configureSection(action, descriptor, section, fps = 30) {
    if (!action || !descriptor || !section) return;

    const sectionData = descriptor[section];
    if (!sectionData || typeof sectionData !== 'object') return;

    const startFrame = sectionData.start_frame || 0;
    const endFrame = sectionData.end_frame || 0;

    if (startFrame >= endFrame) return;

    // Use subclip to isolate the section
    const clip = action.getClip();
    if (!clip) return;

    // Store section info on action userData
    action.userData = action.userData || {};
    action.userData.section = section;
    action.userData.startFrame = startFrame;
    action.userData.endFrame = endFrame;
    action.userData.fps = fps;
    action.userData.duration = (endFrame - startFrame) / fps;
}

/**
 * Start playing a section of the current animation
 * @param {string} section - 'intro', 'loop', or 'outro'
 */
function playSection(section) {
    if (!currentAction || !currentDescriptor) return;

    const sectionData = currentDescriptor[section];
    if (!sectionData || typeof sectionData !== 'object') {
        // Section doesn't exist, fall back to loop
        if (section !== 'loop') {
            playSection('loop');
        }
        return;
    }

    const startFrame = sectionData.start_frame;
    const endFrame = sectionData.end_frame;
    const fps = currentDescriptor.fps || 30;

    if (typeof startFrame !== 'number' || typeof endFrame !== 'number') return;

    // Create subclip for this section
    const baseClip = currentAction.getClip();
    if (!baseClip) return;

    const sectionClip = AnimationUtils.subclip(
        baseClip,
        `${baseClip.name}_${section}`,
        startFrame,
        endFrame,
        fps
    );

    if (!sectionClip) return;

    // Configure the action
    const newAction = mixer.clipAction(sectionClip);
    newAction.loop = (section === 'loop') ? THREE.LoopRepeat : THREE.LoopOnce;
    newAction.clampWhenFinished = (section !== 'loop');

    // Fade in new section
    if (currentAction && currentAction !== newAction) {
        currentAction.fadeOut(CROSSFADE_DURATION);
    }

    newAction.reset();
    newAction.fadeIn(CROSSFADE_DURATION);
    newAction.play();

    currentAction = newAction;
    currentSection = section;
    sectionStartTime = animationClock;

    if (onSectionChangeCallback) {
        onSectionChangeCallback(section, animationClock);
    }

    console.debug(`[KaradaEngine] Playing section: ${section} (${startFrame}-${endFrame})`);
}

/**
 * Handle descriptor-driven state machine
 * Called each frame to update animation state based on descriptor
 */
function updateDescriptorStateMachine() {
    if (!currentDescriptor || !currentState || !currentAction) return;

    // Check if we have intro/loop/outro structure
    const hasIntro = currentDescriptor.intro && typeof currentDescriptor.intro === 'object';
    const hasLoop = currentDescriptor.loop && typeof currentDescriptor.loop === 'object';
    const hasOutro = currentDescriptor.outro && typeof currentDescriptor.outro === 'object';

    // If no structured descriptor, just play the full clip
    if (!hasIntro && !hasLoop && !hasOutro) return;

    const fps = currentDescriptor.fps || 30;

    // State machine logic
    if (currentSection === 'intro' && hasIntro) {
        // Check if intro is done
        const introDuration = ((currentDescriptor.intro.end_frame - currentDescriptor.intro.start_frame) / fps);
        if (animationClock - sectionStartTime >= introDuration) {
            // Transition to loop
            if (hasLoop) {
                playSection('loop');
            }
        }
    }
    // Note: outro is triggered externally via stopAnimation()
}

/**
 * Play a new animation (Karada v2 protocol)
 * @param {Object} params - Animation parameters
 * @param {string} params.state - Animation state name
 * @param {string} params.animationFile - Animation file name
 * @param {Object} params.descriptor - Animation descriptor
 * @param {string} params.startedAt - ISO timestamp when animation started
 * @param {string} params.animationId - Unique animation ID
 * @param {boolean} params.loop - Whether to loop (hint)
 * @param {THREE.AnimationClip} params.clip - Pre-loaded animation clip
 */
function playAnimation(params) {
    const { state, animationFile, descriptor, descriptorId, startedAt, loop, clip } = params;

    console.debug(`[KaradaEngine] playAnimation: state=${state}, descriptor=${descriptorId || 'unknown'}`);

    // Skip if the canonical Karada tuple is unchanged.
    if (
        currentState === state
        && currentDescriptorId === (descriptorId || null)
        && currentStartedAtKey === startedAt
    ) {
        console.debug('[KaradaEngine] Same animation already playing, skipping');
        return;
    }

    // Store new state
    currentState = state;
    currentDescriptor = descriptor || null;
    currentDescriptorId = descriptorId || null;
    currentStartedAtKey = startedAt;
    animationStartedAt = parseStartedAt(startedAt);
    animationClock = (Date.now() - animationStartedAt) / 1000;

    // Use provided clip
    if (!clip) {
        console.warn('[KaradaEngine] No clip provided for animation');
        return;
    }

    // Stop current action with fade out
    if (currentAction) {
        currentAction.fadeOut(CROSSFADE_DURATION);
    }

    // A section-less descriptor with `play_once: true` (e.g. touch/Surprised)
    // must play ONCE and return to idle, never loop. Idle itself is always a
    // looping state regardless of any descriptor flag.
    const playOnce = !!(descriptor && descriptor.play_once) && state !== IDLE_FALLBACK_STATE;

    // Create new action
    currentClip = clip;
    currentAction = mixer.clipAction(clip);
    currentAction.loop = THREE.LoopRepeat;
    currentAction.clampWhenFinished = true;

    // Start playing appropriate section
    if (descriptor && (descriptor.intro || descriptor.loop || descriptor.outro)) {
        // Has descriptor structure - start with intro if available, else loop
        if (descriptor.intro && typeof descriptor.intro === 'object') {
            playSection('intro');
        } else if (descriptor.loop && typeof descriptor.loop === 'object') {
            playSection('loop');
        } else {
            // Just play the full clip
            currentAction.reset();
            currentAction.fadeIn(CROSSFADE_DURATION);
            currentAction.play();
            currentSection = 'loop';
        }
    } else if (playOnce) {
        // One-shot, section-less clip: play a single time then fall back to idle.
        currentAction.loop = THREE.LoopOnce;
        currentAction.clampWhenFinished = true;
        currentAction.reset();
        currentAction.fadeIn(CROSSFADE_DURATION);
        currentAction.play();
        currentSection = 'loop';
        _scheduleReturnToIdle(currentAction);
    } else {
        // No descriptor structure - just play the clip
        currentAction.reset();
        currentAction.fadeIn(CROSSFADE_DURATION);
        currentAction.play();
        currentSection = 'loop';
    }

    // Karada v2: the engine owns the skeletal clip, but facial expressions
    // (eyes_closed, blink, eye_movement, lipsync) declared in the descriptor
    // are driven by the AnimationHandler's per-frame expression ticker. Forward
    // the resolved descriptor as a rich animation state so the handler injects
    // and evaluates those expressions against the local animation clock. Without
    // this, the descriptor's `expressions` never reach the VRM blendshapes.
    _forwardDescriptorExpressions(state, descriptor, startedAt);

    if (onStateChangeCallback) {
        onStateChangeCallback(state, descriptorId || null);
    }
}

/**
 * Forward a resolved descriptor's facial data to the AnimationHandler so its
 * per-frame expression ticker applies eyes_closed / blink / eye_movement /
 * lipsync to the VRM. This is the single point where every play path (WS,
 * bootstrap, debug) converges with the resolved state + descriptor.
 * @param {string} stateName - Logical animation state (e.g. 'think')
 * @param {Object} descriptor - Resolved descriptor (.fbx.json contents)
 * @param {string|number} startedAt - Server started_at for the animation clock
 */
function _forwardDescriptorExpressions(stateName, descriptor, startedAt) {
    try {
        const handler = (typeof window !== 'undefined') ? window.animationHandler : null;
        if (!handler || typeof handler.applyAnimationState !== 'function') return;

        const hasRich = !!(
            descriptor
            && typeof descriptor === 'object'
            && (
                Array.isArray(descriptor.expressions)
                || descriptor.blink
                || descriptor.eye_movement
                || typeof descriptor.lipsync === 'boolean'
            )
        );

        // Anchor the expression timeline to the same clock the engine uses.
        const startedAtMs = animationStartedAt || Date.now();
        const startedAtIso = new Date(startedAtMs).toISOString();

        // States without a rich descriptor (e.g. `write`, which has no .fbx.json,
        // or a bare `idle`) still MUST reach the handler. Otherwise a previous
        // state that locked a persistent expression (e.g. think's eyes_closed,
        // which sets _eyesState.locked with a huge duration and suppresses the
        // blink loop) is never cleared: _lastAnimationState stays stuck on the
        // old state and the eyes remain shut during the new one. Forward a
        // minimal, expression-free state so applyAnimationState updates
        // _lastAnimationState to the new action and runs its smooth eyes reset
        // (the new state declares no eyes_closed, so the reset reopens them).
        if (!hasRich) {
            const minimalState = {
                action: stateName,
                animation: currentClip ? currentClip.name : null,
                phase: currentSection || 'loop',
                descriptor: (descriptor && typeof descriptor === 'object') ? descriptor : null,
                clip: { fps: (descriptor && descriptor.fps) ? descriptor.fps : 30 },
                timing: {
                    started_at: startedAtIso,
                    time_in_clip: 0,
                    current_frame: 0,
                },
                expressions: [],
                blink: null,
                eye_movement: null,
                lipsync: false,
                source: 'karada_engine_descriptor',
            };
            handler.applyAnimationState(minimalState);
            return;
        }

        const richState = {
            action: stateName,
            animation: currentClip ? currentClip.name : null,
            phase: currentSection || 'loop',
            descriptor: descriptor,
            clip: { fps: descriptor.fps || 30 },
            timing: {
                started_at: startedAtIso,
                time_in_clip: 0,
                current_frame: 0,
            },
            // Clone the descriptor's expressions array (shallow-copy each entry too).
            // applyAnimationState mutates state.expressions in place when it appends
            // persona_override entries; without this copy those overrides would be
            // pushed onto the shared, cached descriptor array and accumulate on every
            // replay (intro->loop, re-trigger), producing duplicate eyes_closed /
            // mouth_O entries and erratic facial state.
            expressions: Array.isArray(descriptor.expressions)
                ? descriptor.expressions.map(e => Object.assign({}, e))
                : null,
            blink: descriptor.blink || null,
            eye_movement: descriptor.eye_movement || null,
            lipsync: (typeof descriptor.lipsync === 'boolean') ? descriptor.lipsync : false,
            source: 'karada_engine_descriptor',
        };

        handler.applyAnimationState(richState);
    } catch (e) {
        console.warn('[KaradaEngine] Failed to forward descriptor expressions:', e);
    }
}

/**
 * Resolve the idle animation clip from the shared VRM animation cache so a
 * one-shot animation can fall back to idle without the server re-sending it.
 * @returns {THREE.AnimationClip|null}
 */
function _resolveIdleClip() {
    if (idleClip) return idleClip;
    try {
        if (typeof window !== 'undefined'
            && window.VRMAnimations
            && typeof window.VRMAnimations._getCachedAnimation === 'function') {
            return window.VRMAnimations._getCachedAnimation(IDLE_FALLBACK_STATE, null) || null;
        }
    } catch (e) { /* ignore */ }
    return null;
}

/**
 * Register a one-time `finished` handler on the mixer for a play-once action.
 * When that specific action completes, transition back to idle. A short timer
 * acts as a safety net in case the `finished` event is missed (e.g. the action
 * is replaced by a new state before it fires).
 * @param {THREE.AnimationAction} oneShotAction - The play-once action to watch
 */
function _scheduleReturnToIdle(oneShotAction) {
    if (!mixer || !oneShotAction) return;

    const clip = oneShotAction.getClip();
    const durationS = clip && Number.isFinite(clip.duration) ? clip.duration : 2.0;

    const onFinished = (event) => {
        if (event.action !== oneShotAction) return; // not our action
        mixer.removeEventListener('finished', onFinished);
        // Only fall back if this one-shot is still the active action; a newer
        // state may have already replaced it.
        if (currentAction === oneShotAction) {
            const clipIdle = _resolveIdleClip();
            if (clipIdle) {
                transitionToIdle(clipIdle);
            }
        }
    };

    mixer.addEventListener('finished', onFinished);

    // Safety net: if the event never arrives, force the fallback shortly after
    // the clip's natural duration (plus the crossfade).
    setTimeout(() => {
        mixer.removeEventListener('finished', onFinished);
        if (currentAction === oneShotAction) {
            const clipIdle = _resolveIdleClip();
            if (clipIdle) {
                transitionToIdle(clipIdle);
            }
        }
    }, (durationS + CROSSFADE_DURATION) * 1000 + 250);
}

/**
 * Stop current animation (play outro if available, then transition to idle)
 * @param {Function} onComplete - Callback when outro is complete
 */
function stopAnimation(onComplete) {
    if (!currentAction || !currentDescriptor || !currentState) {
        if (onComplete) onComplete();
        return;
    }

    // Check if we have an outro
    const hasOutro = currentDescriptor.outro && typeof currentDescriptor.outro === 'object';

    if (hasOutro && currentSection !== 'outro') {
        console.debug('[KaradaEngine] Playing outro before stopping');
        playSection('outro');

        // Schedule transition to idle after outro completes
        const fps = currentDescriptor.fps || 30;
        const outroDuration = ((currentDescriptor.outro.end_frame - currentDescriptor.outro.start_frame) / fps);

        setTimeout(() => {
            if (onComplete) onComplete();
        }, outroDuration * 1000 + 100);
    } else {
        // No outro, just stop
        if (onComplete) onComplete();
    }
}

/**
 * Transition to idle (fallback)
 * @param {THREE.AnimationClip} idleClipParam - Idle animation clip
 */
function transitionToIdle(idleClipParam) {
    if (!idleClipParam) return;

    console.debug('[KaradaEngine] Transitioning to idle');

    // Fade out current action
    if (currentAction) {
        currentAction.fadeOut(CROSSFADE_DURATION);
    }

    // Play idle
    idleClip = idleClipParam;
    idleAction = mixer.clipAction(idleClip);
    idleAction.loop = THREE.LoopRepeat;
    idleAction.reset();
    idleAction.fadeIn(CROSSFADE_DURATION);
    idleAction.play();

    currentAction = idleAction;
    currentState = IDLE_FALLBACK_STATE;
    currentSection = 'loop';
    currentDescriptor = null;
    currentDescriptorId = null;
    currentStartedAtKey = null;
    animationStartedAt = Date.now();
    animationClock = 0;
}

/**
 * Update function to call in render loop
 */
function updateEngine() {
    if (!mixer) return;

    updateAnimationClock();
    updateDescriptorStateMachine();
}

/**
 * Set callbacks
 */
function setOnStateChange(callback) {
    onStateChangeCallback = callback;
}

function setOnSectionChange(callback) {
    onSectionChangeCallback = callback;
}

/**
 * Get current state info
 */
function getEngineState() {
    return {
        state: currentState,
        section: currentSection,
        animationTime: animationClock,
        descriptorId: currentDescriptorId,
        isTransitioning,
    };
}

// Export public API
export {
    initAnimationEngine,
    playAnimation,
    stopAnimation,
    transitionToIdle,
    updateEngine,
    getEngineState,
    setOnStateChange,
    setOnSectionChange,
    getAnimationTime,
    CROSSFADE_DURATION,
};
