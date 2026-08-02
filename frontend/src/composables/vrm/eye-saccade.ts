import type { VRM } from '@pixiv/three-vrm'

import * as THREE from 'three'

/**
 * Idle eye saccades: small, natural-feeling random glances layered on top of
 * the VRM's lookAt target when nothing else (descriptor eye_movement, user
 * cursor) is driving gaze. Purely cosmetic — zero server involvement.
 */
export class EyeSaccadeDriver {
  private readonly target = new THREE.Vector3()
  private readonly basePoint: THREE.Vector3
  private timer = 0
  private nextSaccadeAt = this.randomInterval()

  constructor(private readonly vrm: VRM, distance = 1.2) {
    this.basePoint = new THREE.Vector3(0, 1.4, -distance)
    this.target.copy(this.basePoint)
    if (vrm.lookAt) {
      // We drive gaze manually every frame via lookAt(); autoUpdate would
      // otherwise fight us by tracking `.target` (which we leave unset).
      vrm.lookAt.autoUpdate = false
    }
  }

  update(delta: number): void {
    const lookAt = this.vrm.lookAt
    if (!lookAt)
      return

    this.timer += delta
    if (this.timer >= this.nextSaccadeAt) {
      this.timer = 0
      this.nextSaccadeAt = this.randomInterval()
      // Small horizontal/vertical offset — glances, not wide eye rolls.
      const dx = (Math.random() - 0.5) * 0.35
      const dy = (Math.random() - 0.5) * 0.18
      this.target.set(this.basePoint.x + dx, this.basePoint.y + dy, this.basePoint.z)
    }

    lookAt.lookAt(this.target)
  }

  private randomInterval(): number {
    return 1.5 + Math.random() * 3.0
  }
}
