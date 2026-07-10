import type { VRM } from '@pixiv/three-vrm'
import type * as THREE from 'three'

import { loadMixamoAnimation } from './retarget/load-mixamo-animation'

/**
 * Cache of retargeted animation clips, keyed by FBX URL.
 *
 * Clips are retargeted against a specific VRM's bone names, so the cache is
 * bound to one VRM instance — create a fresh cache on model swap.
 */
export class AnimationClipCache {
  private readonly clips = new Map<string, Promise<THREE.AnimationClip>>()
  private readonly resolved = new Map<string, THREE.AnimationClip>()

  constructor(private readonly vrm: VRM) {}

  /** Kick off (or reuse) a load for the given animation URL. */
  preload(url: string): Promise<THREE.AnimationClip> {
    let pending = this.clips.get(url)
    if (!pending) {
      pending = loadMixamoAnimation(url, this.vrm).then((clip) => {
        this.resolved.set(url, clip)
        return clip
      })
      // On failure, drop the entry so a later retry can succeed.
      pending.catch(() => this.clips.delete(url))
      this.clips.set(url, pending)
    }
    return pending
  }

  /** Synchronous lookup — only returns clips whose load already finished. */
  getLoaded(url: string): THREE.AnimationClip | null {
    return this.resolved.get(url) ?? null
  }

  async get(url: string): Promise<THREE.AnimationClip> {
    return this.preload(url)
  }
}
