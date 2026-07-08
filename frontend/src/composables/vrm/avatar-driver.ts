import type { VRM } from '@pixiv/three-vrm'
import type { VrmAnimationV2Message, VrmPreloadMessage } from '@/services/protocol'
import type { DescriptorFacialState } from './animation'

import * as THREE from 'three'

import {
  fetchAnimationManifest,
  fetchFullState,
  resolveAnimationDescriptor,
} from '@/services/karada-rest'

import { AnimationClipCache } from './animation-cache'
import { KaradaAnimationEngine } from './animation'

export interface AvatarDriver {
  mixer: THREE.AnimationMixer
  engine: KaradaAnimationEngine
  /** Feed a live `vrm_animation_v2` event. */
  applyAnimationEvent: (event: VrmAnimationV2Message) => Promise<void>
  /** Feed a `vrm_preload` cache-warming hint. */
  applyPreloadEvent: (event: VrmPreloadMessage) => void
  /** Advance clocks + mixer; call once per frame. */
  update: (delta: number) => void
  dispose: () => void
}

function skinNameFromModelUrl(url: string): string | null {
  const match = /\/skins\/([^/]+)\//.exec(url)
  return match?.[1] ?? null
}

/**
 * Binds one loaded VRM to the Karada animation protocol: owns the mixer, the
 * retargeted-clip cache, and the descriptor engine; resolves descriptor ids
 * via the Karada REST manifest.
 *
 * On startup it preloads the skin's idle clip (the engine's T-pose guard needs
 * it), then replays the server's current animation state so a late-joining
 * client lands mid-animation exactly like the legacy viewer.
 */
export async function createAvatarDriver(
  vrm: VRM,
  options: { modelUrl: string, onDescriptorState?: (s: DescriptorFacialState) => void } ,
): Promise<AvatarDriver> {
  const mixer = new THREE.AnimationMixer(vrm.scene)
  const cache = new AnimationClipCache(vrm)

  // ── idle preload ───────────────────────────────────────────────────────────
  const skin = skinNameFromModelUrl(options.modelUrl)?.toLowerCase() ?? null
  let idleUrl: string | null = null
  try {
    const manifest = await fetchAnimationManifest()
    const entries = Object.values(manifest.animations)
    const idles = entries.filter(e => e.state === 'idle' && e.animation_url)
    const skinIdle = idles.find(e => (e.skin ?? '').toLowerCase() === skin)
    idleUrl = (skinIdle ?? idles[0])?.animation_url ?? null
  }
  catch {
    // manifest unavailable — engine will run without an idle guard until the
    // first preload arrives
  }
  if (idleUrl)
    await cache.preload(idleUrl).catch(() => {})

  const engine = new KaradaAnimationEngine({
    mixer,
    resolveIdleClip: () => (idleUrl ? cache.getLoaded(idleUrl) : null),
    onDescriptorState: options.onDescriptorState,
  })

  async function applyAnimationEvent(event: VrmAnimationV2Message): Promise<void> {
    let animationUrl: string | null = null
    let descriptorData = null
    if (event.descriptor) {
      const entry = await resolveAnimationDescriptor(event.descriptor)
      animationUrl = entry?.animation_url ?? null
      descriptorData = entry?.descriptor_data ?? null
    }

    let clip = animationUrl ? await cache.get(animationUrl).catch(() => null) : null
    if (!clip && idleUrl) {
      // Unresolvable state (e.g. animation files removed) — fall back to idle
      // instead of freezing in the previous pose.
      clip = cache.getLoaded(idleUrl)
      if (!clip)
        return
      engine.transitionToIdle(clip)
      return
    }
    if (!clip)
      return

    engine.playAnimation({
      state: event.state,
      descriptor: descriptorData,
      descriptorId: event.descriptor ?? null,
      startedAt: event.started_at,
      clip,
    })
  }

  function applyPreloadEvent(event: VrmPreloadMessage): void {
    void (async () => {
      if (event.descriptor) {
        const entry = await resolveAnimationDescriptor(event.descriptor)
        if (entry?.animation_url)
          void cache.preload(entry.animation_url).catch(() => {})
      }
      else if (event.file) {
        void cache.preload(event.file).catch(() => {})
      }
    })()
  }

  // ── replay current server state (late join) ─────────────────────────────────
  try {
    const state = await fetchFullState()
    const anim = state.animation
    if (anim?.state && typeof anim.started_at === 'number') {
      await applyAnimationEvent({
        type: 'vrm_animation_v2',
        state: anim.state,
        descriptor: anim.descriptor ?? null,
        started_at: anim.started_at,
      })
    }
    else if (idleUrl) {
      const idle = cache.getLoaded(idleUrl)
      if (idle)
        engine.transitionToIdle(idle)
    }
  }
  catch {
    const idle = idleUrl ? cache.getLoaded(idleUrl) : null
    if (idle)
      engine.transitionToIdle(idle)
  }

  function update(delta: number): void {
    engine.update()
    mixer.update(delta)
  }

  function dispose(): void {
    engine.dispose()
    mixer.stopAllAction()
    mixer.uncacheRoot(vrm.scene)
  }

  return { mixer, engine, applyAnimationEvent, applyPreloadEvent, update, dispose }
}
