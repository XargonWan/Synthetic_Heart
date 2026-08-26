// Karada v2 animation engine — ported from res/synth_webui/js/vrm-animation-engine.mjs
// (same repo). Semantics preserved: server sends only state + descriptor +
// started_at; the client owns intro→loop→outro, crossfades, and idle fallback.
// One deliberate change: section subclips use THREE.AnimationUtils.subclip
// (which re-bases keyframe times to zero) instead of the legacy trim()-based
// helper, so loop/outro sections play their actual frames.
import type { DescriptorData } from '@/services/karada-rest'

import * as THREE from 'three'

export interface PlayAnimationParams {
  state: string
  descriptor: DescriptorData | null
  descriptorId: string | null
  startedAt: number | string
  clip: THREE.AnimationClip
}

/** Rich state forwarded to the facial layer (blink/eye/lipsync/expressions
 * declared in the descriptor). Mirrors the legacy `applyAnimationState` input. */
export interface DescriptorFacialState {
  action: string
  phase: string
  descriptor: DescriptorData | null
  fps: number
  startedAtMs: number
  expressions: Array<Record<string, unknown>> | null
  blink: unknown
  eyeMovement: unknown
  lipsync: boolean
}

export interface AnimationEngineOptions {
  mixer: THREE.AnimationMixer
  /** Resolve the idle fallback clip (may return null while not yet cached). */
  resolveIdleClip: () => THREE.AnimationClip | null
  /** Receives descriptor facial data on every state change (M3+ facial layer). */
  onDescriptorState?: (state: DescriptorFacialState) => void
  onStateChange?: (state: string, descriptorId: string | null) => void
}

const CROSSFADE_DURATION = 0.3 // seconds

const IDLE_FALLBACK_STATE = 'idle'

// Persistent low-weight base idle: bones NOT keyed by the current foreground
// action fall back to the VRM bind pose (T-pose) when nothing else drives
// them. A base idle running at a small weight floor keeps the whole skeleton
// driven at all times, so no transient T-pose can appear between animations.
const BASE_IDLE_MIN_WEIGHT = 0.15
const BASE_IDLE_FLOOR_WEIGHT = 0.12

function parseStartedAt(startedAt: number | string): number {
  if (typeof startedAt === 'number' && Number.isFinite(startedAt))
    return startedAt < 1e12 ? startedAt * 1000 : startedAt
  if (typeof startedAt === 'string' && startedAt.trim() !== '') {
    const numeric = Number(startedAt)
    if (Number.isFinite(numeric))
      return numeric < 1e12 ? numeric * 1000 : numeric
    const parsed = new Date(startedAt).getTime()
    if (Number.isFinite(parsed))
      return parsed
  }
  return Date.now()
}

export class KaradaAnimationEngine {
  private readonly mixer: THREE.AnimationMixer
  private readonly resolveIdleClip: () => THREE.AnimationClip | null
  private readonly onDescriptorState?: (state: DescriptorFacialState) => void
  private readonly onStateChange?: (state: string, descriptorId: string | null) => void

  private currentAction: THREE.AnimationAction | null = null
  private currentClip: THREE.AnimationClip | null = null
  private currentDescriptor: DescriptorData | null = null
  private currentState: string | null = null
  private currentDescriptorId: string | null = null
  private currentStartedAtKey: number | string | null = null
  private animationStartedAt = 0
  private animationClock = 0
  private currentSection: 'intro' | 'loop' | 'outro' = 'loop'
  private sectionStartTime = 0

  private baseIdleAction: THREE.AnimationAction | null = null
  private baseIdleClip: THREE.AnimationClip | null = null
  private baseIdleFloorTimer: ReturnType<typeof setTimeout> | null = null
  private returnToIdleTimer: ReturnType<typeof setTimeout> | null = null

  constructor(options: AnimationEngineOptions) {
    this.mixer = options.mixer
    this.resolveIdleClip = options.resolveIdleClip
    this.onDescriptorState = options.onDescriptorState
    this.onStateChange = options.onStateChange
    this.ensureBaseIdle()
  }

  get state(): string | null {
    return this.currentState
  }

  /** Call every frame (before mixer.update). Advances the descriptor state
   * machine (intro→loop hand-off is clock-driven). */
  update(): void {
    if (!this.animationStartedAt) {
      this.animationClock = 0
    }
    else {
      this.animationClock = (Date.now() - this.animationStartedAt) / 1000
    }
    this.updateDescriptorStateMachine()
  }

  playAnimation(params: PlayAnimationParams): void {
    const { state, descriptor, descriptorId, startedAt, clip } = params

    // Skip if the canonical Karada tuple is unchanged.
    if (
      this.currentState === state
      && this.currentDescriptorId === (descriptorId || null)
      && this.currentStartedAtKey === startedAt
    ) {
      return
    }

    this.clearReturnToIdle()
    this.currentState = state
    this.currentDescriptor = descriptor ?? null
    this.currentDescriptorId = descriptorId || null
    this.currentStartedAtKey = startedAt
    this.animationStartedAt = parseStartedAt(startedAt)
    this.animationClock = (Date.now() - this.animationStartedAt) / 1000

    this.ensureBaseIdle()

    const previousAction = this.currentAction

    // A section-less descriptor with `play_once: true` (e.g. touch/Surprised)
    // must play ONCE and return to idle. Idle itself always loops.
    const playOnce = !!descriptor?.play_once && state !== IDLE_FALLBACK_STATE

    this.currentClip = clip
    this.currentAction = this.mixer.clipAction(clip)
    this.currentAction.loop = THREE.LoopRepeat
    this.currentAction.clampWhenFinished = true

    if (descriptor && (descriptor.intro || descriptor.loop || descriptor.outro)) {
      // Structured descriptor — start with intro when present, else loop.
      // playSection crossfades internally; fade the previous action out here
      // to avoid a lingering overlay.
      if (previousAction && previousAction !== this.currentAction)
        previousAction.fadeOut(CROSSFADE_DURATION)
      if (descriptor.intro) {
        this.playSection('intro')
      }
      else if (descriptor.loop) {
        this.playSection('loop')
      }
      else {
        this.startForegroundAction(this.currentAction, previousAction)
        this.currentSection = 'loop'
        this.scheduleBaseIdleFloorDrop()
      }
    }
    else if (playOnce) {
      this.currentAction.loop = THREE.LoopOnce
      this.currentAction.clampWhenFinished = true
      this.startForegroundAction(this.currentAction, previousAction)
      this.currentSection = 'loop'
      this.scheduleBaseIdleFloorDrop()
      this.scheduleReturnToIdle(this.currentAction)
    }
    else {
      this.startForegroundAction(this.currentAction, previousAction)
      this.currentSection = 'loop'
      this.scheduleBaseIdleFloorDrop()
    }

    this.forwardDescriptorState(state, descriptor)
    this.onStateChange?.(state, descriptorId || null)
  }

  /** Play outro if the descriptor has one, then fall back to idle. */
  stopAnimation(onComplete?: () => void): void {
    const goIdle = (): void => {
      const idle = this.resolveIdleClip()
      if (idle)
        this.transitionToIdle(idle)
      onComplete?.()
    }

    if (!this.currentAction || !this.currentDescriptor || !this.currentState) {
      goIdle()
      return
    }

    const outro = this.currentDescriptor.outro
    if (outro && this.currentSection !== 'outro') {
      this.playSection('outro')
      const fps = this.currentDescriptor.fps || 30
      const outroDuration = (outro.end_frame - outro.start_frame) / fps
      setTimeout(() => {
        if (this.currentState === IDLE_FALLBACK_STATE) {
          onComplete?.()
          return
        }
        goIdle()
      }, outroDuration * 1000 + 100)
    }
    else {
      goIdle()
    }
  }

  transitionToIdle(idleClip: THREE.AnimationClip): void {
    this.ensureBaseIdle()
    if (this.baseIdleAction && this.baseIdleClip === idleClip) {
      // Promote the persistent base idle to foreground instead of layering a
      // second idle action on the mixer.
      if (this.baseIdleFloorTimer) {
        clearTimeout(this.baseIdleFloorTimer)
        this.baseIdleFloorTimer = null
      }
      if (this.currentAction && this.currentAction !== this.baseIdleAction)
        this.currentAction.fadeOut(CROSSFADE_DURATION)
      this.baseIdleAction.enabled = true
      this.baseIdleAction.setEffectiveWeight(1.0)
      this.baseIdleAction.play()
      this.currentAction = this.baseIdleAction
    }
    else {
      const prevAction = this.currentAction
      const idleAction = this.mixer.clipAction(idleClip)
      idleAction.loop = THREE.LoopRepeat
      this.startForegroundAction(idleAction, prevAction)
      this.currentAction = idleAction
      this.scheduleBaseIdleFloorDrop()
    }

    this.currentState = IDLE_FALLBACK_STATE
    this.currentSection = 'loop'
    this.currentDescriptor = null
    this.currentDescriptorId = null
    this.currentStartedAtKey = null
    this.animationStartedAt = Date.now()
    this.animationClock = 0
    this.forwardDescriptorState(IDLE_FALLBACK_STATE, null)
  }

  dispose(): void {
    this.clearReturnToIdle()
    if (this.baseIdleFloorTimer) {
      clearTimeout(this.baseIdleFloorTimer)
      this.baseIdleFloorTimer = null
    }
    this.mixer.stopAllAction()
  }

  // ── internals ──────────────────────────────────────────────────────────────

  private playSection(section: 'intro' | 'loop' | 'outro'): void {
    if (!this.currentAction || !this.currentDescriptor)
      return

    const sectionData = this.currentDescriptor[section]
    if (!sectionData) {
      if (section !== 'loop')
        this.playSection('loop')
      return
    }

    const { start_frame: startFrame, end_frame: endFrame } = sectionData
    const fps = this.currentDescriptor.fps || 30
    if (typeof startFrame !== 'number' || typeof endFrame !== 'number')
      return

    const baseClip = this.currentClip
    if (!baseClip)
      return

    const sectionClip = THREE.AnimationUtils.subclip(
      baseClip,
      `${baseClip.name}_${section}`,
      startFrame,
      endFrame,
      fps,
    )

    const newAction = this.mixer.clipAction(sectionClip)
    newAction.loop = section === 'loop' ? THREE.LoopRepeat : THREE.LoopOnce
    newAction.clampWhenFinished = section !== 'loop'

    // Keep the skeleton fully covered during the hand-off (no bind pose), and
    // use a true crossfade so total weight stays ~1.
    this.ensureBaseIdle()

    newAction.reset()
    if (this.currentAction !== newAction && this.currentAction.isRunning()) {
      newAction.play()
      this.currentAction.crossFadeTo(newAction, CROSSFADE_DURATION, false)
    }
    else {
      newAction.fadeIn(CROSSFADE_DURATION)
      newAction.play()
    }

    this.currentAction = newAction
    this.currentSection = section
    this.sectionStartTime = this.animationClock
    this.scheduleBaseIdleFloorDrop()
  }

  private updateDescriptorStateMachine(): void {
    if (!this.currentDescriptor || !this.currentState || !this.currentAction)
      return
    const { intro, loop, outro } = this.currentDescriptor
    if (this.currentSection === 'intro' && intro) {
      const fps = this.currentDescriptor.fps || 30
      const introDuration = (intro.end_frame - intro.start_frame) / fps
      if (this.animationClock - this.sectionStartTime >= introDuration) {
        if (loop) {
          this.playSection('loop')
        }
        else if (outro) {
          // No loop section (e.g. skin_change's "Look Around": intro+outro,
          // play_once). The intro is a one-shot that clamps on its last frame;
          // without a loop to hand off to, nothing advanced the state and the
          // action froze mid-pose. Drive intro → outro → idle so the animation
          // completes instead of clamping on the intro's final frame.
          this.playSection('outro')
          const outroDuration = (outro.end_frame - outro.start_frame) / fps
          setTimeout(() => {
            if (this.currentSection === 'outro' && this.currentState !== IDLE_FALLBACK_STATE) {
              const idle = this.resolveIdleClip()
              if (idle)
                this.transitionToIdle(idle)
            }
          }, outroDuration * 1000 + 100)
        }
        else {
          // Intro-only, no loop and no outro: fall straight back to idle
          // rather than clamping forever on the intro's last frame.
          const idle = this.resolveIdleClip()
          if (idle)
            this.transitionToIdle(idle)
        }
      }
    }
    // outro after a loop section is still triggered externally via stopAnimation()
  }

  /**
   * IMPORTANT: never use `fadeIn()` for the base idle — it resets the weight
   * to 0 first, which can expose a single-frame bind pose if this is briefly
   * the only driver. Raise the weight monotonically instead.
   */
  private ensureBaseIdle(minWeight = BASE_IDLE_MIN_WEIGHT): boolean {
    if (!this.baseIdleAction) {
      const clip = this.baseIdleClip ?? this.resolveIdleClip()
      if (!clip)
        return false // idle clip not cached yet; retried on next transition
      this.baseIdleClip = clip
      this.baseIdleAction = this.mixer.clipAction(clip)
    }
    const action = this.baseIdleAction
    action.enabled = true
    action.setLoop(THREE.LoopRepeat, Number.POSITIVE_INFINITY)
    action.clampWhenFinished = false
    action.setEffectiveWeight(Math.max(action.getEffectiveWeight() || 0, minWeight))
    action.play()
    return true
  }

  /** Lower the base idle back to its floor only AFTER the incoming action has
   * fully faded in — earlier would re-expose the bind pose mid-crossfade. */
  private scheduleBaseIdleFloorDrop(
    targetWeight = BASE_IDLE_FLOOR_WEIGHT,
    delayMs = CROSSFADE_DURATION * 1000 + 120,
  ): void {
    if (!this.baseIdleAction)
      return
    if (this.baseIdleFloorTimer)
      clearTimeout(this.baseIdleFloorTimer)
    this.baseIdleFloorTimer = setTimeout(() => {
      this.baseIdleFloorTimer = null
      // Only lower the base idle when a DISTINCT foreground action is covering
      // the skeleton. When the current foreground action IS the base idle
      // (e.g. an `idle` state whose clip resolves to the same fallback idle
      // clip — three.js caches actions by clip, so `clipAction()` returns the
      // very same object), dropping it to the floor would leave the skeleton
      // at ~12% animation / ~88% bind pose = a partial T-pose. Keep it at full
      // weight in that case.
      if (this.baseIdleAction && this.currentAction !== this.baseIdleAction)
        this.baseIdleAction.setEffectiveWeight(targetWeight)
    }, delayMs)
  }

  /** Crossfade keeps combined foreground weight ~1 (independent
   * fadeOut+fadeIn can dip below full skeleton coverage). */
  private startForegroundAction(
    newAction: THREE.AnimationAction,
    prevAction: THREE.AnimationAction | null,
  ): void {
    newAction.reset()
    newAction.setEffectiveWeight(1.0)
    if (
      prevAction
      && prevAction !== newAction
      && prevAction !== this.baseIdleAction
      && prevAction.isRunning()
    ) {
      newAction.play()
      prevAction.crossFadeTo(newAction, CROSSFADE_DURATION, false)
    }
    else {
      newAction.fadeIn(CROSSFADE_DURATION)
      newAction.play()
    }
  }

  private scheduleReturnToIdle(oneShotAction: THREE.AnimationAction): void {
    const clip = oneShotAction.getClip()
    const durationS = Number.isFinite(clip.duration) ? clip.duration : 2.0

    const onFinished = (event: { action?: THREE.AnimationAction }): void => {
      if (event.action !== oneShotAction)
        return
      this.mixer.removeEventListener('finished', onFinished as never)
      if (this.currentAction === oneShotAction) {
        const idle = this.resolveIdleClip()
        if (idle)
          this.transitionToIdle(idle)
      }
    }
    this.mixer.addEventListener('finished', onFinished as never)

    // Safety net in case the finished event never arrives (action replaced).
    this.clearReturnToIdle()
    this.returnToIdleTimer = setTimeout(() => {
      this.returnToIdleTimer = null
      this.mixer.removeEventListener('finished', onFinished as never)
      if (this.currentAction === oneShotAction) {
        const idle = this.resolveIdleClip()
        if (idle)
          this.transitionToIdle(idle)
      }
    }, (durationS + CROSSFADE_DURATION) * 1000 + 250)
  }

  private clearReturnToIdle(): void {
    if (this.returnToIdleTimer) {
      clearTimeout(this.returnToIdleTimer)
      this.returnToIdleTimer = null
    }
  }

  /** Every state change reaches the facial layer — including descriptor-less
   * states — so a previous state's locked expression (e.g. think's
   * eyes_closed) is always released. Descriptor expressions are shallow-copied
   * because the facial layer mutates them in place. */
  private forwardDescriptorState(stateName: string, descriptor: DescriptorData | null): void {
    if (!this.onDescriptorState)
      return
    this.onDescriptorState({
      action: stateName,
      phase: this.currentSection,
      descriptor,
      fps: descriptor?.fps ?? 30,
      startedAtMs: this.animationStartedAt || Date.now(),
      expressions: Array.isArray(descriptor?.expressions)
        ? descriptor.expressions.map(e => ({ ...e }))
        : null,
      blink: descriptor?.blink ?? null,
      eyeMovement: descriptor?.eye_movement ?? null,
      lipsync: typeof descriptor?.lipsync === 'boolean' ? descriptor.lipsync : false,
    })
  }
}
