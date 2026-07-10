import type { VRM } from '@pixiv/three-vrm'
import type { DescriptorFacialState } from './animation'

/**
 * Facial layer: composes every face-driving source each frame and applies the
 * result to the VRM's expressionManager.
 *
 * Layer order (low → high):
 *  1. face values      — server emotion state (`vrm_face`)
 *  2. descriptor       — frame-windowed expressions from the animation
 *                        descriptor (e.g. think's eyes_closed at frame 20+)
 *  3. overlay          — `vrm_expression_set` facial expressions (replace
 *                        semantics: one active source)
 *  4. visemes          — lipsync mouth shapes (M4), applied last
 *
 * Logical target names (eyes_closed, mouth_smile, …) are translated to VRM
 * expression names through the skin's persona `blendshape_map`; names already
 * matching a VRM expression pass through unchanged.
 */

export interface BlendshapeMap {
  [logicalName: string]: string | Record<string, Record<string, number>> | undefined
  visemes?: Record<string, Record<string, number>>
}

export type VisemeWeights = Partial<Record<'A' | 'I' | 'U' | 'E' | 'O', number>>

interface DescriptorExpressionEntry {
  start_frame?: number
  end_frame?: number
  targets?: Record<string, number>
  [key: string]: unknown
}

const BLINK_MIN_INTERVAL_S = 2.5
const BLINK_MAX_INTERVAL_S = 6.0
const BLINK_CLOSE_S = 0.08
const BLINK_HOLD_S = 0.04
const BLINK_OPEN_S = 0.1

export class FacialDriver {
  private faceValues: Record<string, number> = {}
  private overlay: Record<string, number> | null = null
  private descriptorState: DescriptorFacialState | null = null
  private visemes: VisemeWeights = {}

  private blinkTimer = 0
  private nextBlinkAt = this.randomBlinkInterval()
  private blinkPhase: 'idle' | 'closing' | 'hold' | 'opening' = 'idle'
  private blinkPhaseTime = 0
  private blinkWeight = 0

  /** Names we drove last frame — zeroed before each re-apply so dropped
   * sources release their expressions. */
  private drivenNames = new Set<string>()

  constructor(
    private readonly vrm: VRM,
    private readonly blendshapeMap: BlendshapeMap,
  ) {}

  setFaceValues(values: Record<string, number>): void {
    this.faceValues = values ?? {}
  }

  setOverlay(targets: Record<string, number> | null): void {
    this.overlay = targets
  }

  setDescriptorState(state: DescriptorFacialState): void {
    this.descriptorState = state
  }

  setVisemes(weights: VisemeWeights): void {
    this.visemes = weights ?? {}
  }

  update(delta: number): void {
    const manager = this.vrm.expressionManager
    if (!manager)
      return

    // Compose all layers into one weight map (later layers overwrite).
    const composed = new Map<string, number>()

    for (const [name, value] of Object.entries(this.faceValues))
      this.accumulate(composed, name, value)

    for (const [name, value] of this.activeDescriptorTargets())
      this.accumulate(composed, name, value)

    if (this.overlay) {
      for (const [name, value] of Object.entries(this.overlay))
        this.accumulate(composed, name, value)
    }

    // Automatic blink loop, unless something else is already driving the
    // blink expression (descriptor eyes_closed, overlay, emotion).
    const blinkDriven = composed.has('blink')
    this.updateBlink(delta, blinkDriven)
    if (!blinkDriven && this.blinkWeight > 0)
      composed.set('blink', this.blinkWeight)

    // Visemes last — mouth shapes must win over emotion mouth targets.
    const visemeMap = this.blendshapeMap.visemes
    for (const [viseme, weight] of Object.entries(this.visemes)) {
      if (!weight)
        continue
      const targets = visemeMap?.[viseme]
      if (targets) {
        for (const [name, scale] of Object.entries(targets))
          composed.set(name, weight * scale)
      }
    }

    // Zero out everything we drove last frame, then apply the new weights.
    for (const name of this.drivenNames)
      manager.setValue(name, 0)
    this.drivenNames.clear()

    for (const [name, value] of composed) {
      if (manager.getExpression(name)) {
        manager.setValue(name, Math.max(0, Math.min(1, value)))
        this.drivenNames.add(name)
      }
    }
  }

  dispose(): void {
    const manager = this.vrm.expressionManager
    if (manager) {
      for (const name of this.drivenNames)
        manager.setValue(name, 0)
    }
    this.drivenNames.clear()
  }

  // ── internals ──────────────────────────────────────────────────────────────

  /** Translate a logical name via the blendshape map (pass-through when the
   * name is already a VRM expression) and store the strongest weight. */
  private accumulate(composed: Map<string, number>, name: string, value: number): void {
    const mapped = this.blendshapeMap[name]
    const resolved = typeof mapped === 'string' ? mapped : name
    composed.set(resolved, Number(value) || 0)
  }

  private activeDescriptorTargets(): Array<[string, number]> {
    const state = this.descriptorState
    if (!state?.expressions?.length)
      return []
    const clockS = (Date.now() - state.startedAtMs) / 1000
    const currentFrame = clockS * (state.fps || 30)
    const out: Array<[string, number]> = []
    for (const raw of state.expressions) {
      const entry = raw as DescriptorExpressionEntry
      const start = entry.start_frame ?? 0
      const end = entry.end_frame
      if (currentFrame < start)
        continue
      if (typeof end === 'number' && currentFrame > end)
        continue
      if (entry.targets) {
        for (const [name, value] of Object.entries(entry.targets))
          out.push([name, value])
      }
    }
    return out
  }

  private updateBlink(delta: number, suppressed: boolean): void {
    if (suppressed) {
      this.blinkPhase = 'idle'
      this.blinkWeight = 0
      return
    }
    switch (this.blinkPhase) {
      case 'idle':
        this.blinkTimer += delta
        if (this.blinkTimer >= this.nextBlinkAt) {
          this.blinkPhase = 'closing'
          this.blinkPhaseTime = 0
        }
        break
      case 'closing':
        this.blinkPhaseTime += delta
        this.blinkWeight = Math.min(1, this.blinkPhaseTime / BLINK_CLOSE_S)
        if (this.blinkPhaseTime >= BLINK_CLOSE_S) {
          this.blinkPhase = 'hold'
          this.blinkPhaseTime = 0
        }
        break
      case 'hold':
        this.blinkPhaseTime += delta
        this.blinkWeight = 1
        if (this.blinkPhaseTime >= BLINK_HOLD_S) {
          this.blinkPhase = 'opening'
          this.blinkPhaseTime = 0
        }
        break
      case 'opening':
        this.blinkPhaseTime += delta
        this.blinkWeight = Math.max(0, 1 - this.blinkPhaseTime / BLINK_OPEN_S)
        if (this.blinkPhaseTime >= BLINK_OPEN_S) {
          this.blinkPhase = 'idle'
          this.blinkWeight = 0
          this.blinkTimer = 0
          this.nextBlinkAt = this.randomBlinkInterval()
        }
        break
    }
  }

  private randomBlinkInterval(): number {
    return BLINK_MIN_INTERVAL_S + Math.random() * (BLINK_MAX_INTERVAL_S - BLINK_MIN_INTERVAL_S)
  }
}
