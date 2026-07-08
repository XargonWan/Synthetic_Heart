import type {
  ServerMessage,
  VrmAnimationV2Message,
  VrmPreloadMessage,
} from '@/services/protocol'

import { defineStore } from 'pinia'
import { ref, shallowRef } from 'vue'

export interface VrmModelInfo {
  name: string
  url: string
}

/**
 * Pure avatar state fed by the WS dispatch. The Stage scene subscribes to
 * these refs and drives the renderer — no three.js objects live in here.
 */
export const useAvatarStore = defineStore('avatar', () => {
  const model = ref<VrmModelInfo | null>(null)

  /** Last animation event; the animation composable consumes transitions. */
  const animationEvent = shallowRef<VrmAnimationV2Message | null>(null)

  /** Last preload hint from the server (cache-warming). */
  const preloadEvent = shallowRef<VrmPreloadMessage | null>(null)

  const faceValues = ref<Record<string, number>>({})

  /** Facial-expression overlay (replace semantics — one active source). */
  const expressionTargets = ref<Record<string, number> | null>(null)

  function handleMessage(msg: ServerMessage): void {
    switch (msg.type) {
      case 'vrm_model':
        if (msg.url)
          model.value = { name: msg.name, url: msg.url }
        break
      case 'vrm_animation_v2':
        animationEvent.value = msg
        break
      case 'vrm_preload':
        preloadEvent.value = msg
        break
      case 'vrm_face':
        faceValues.value = msg.values ?? {}
        break
      case 'vrm_expression_set':
        expressionTargets.value = msg.targets && typeof msg.targets === 'object'
          ? { ...msg.targets }
          : msg.name
            ? { [msg.name]: Number(msg.intensity) || 0 }
            : {}
        break
      case 'vrm_expression_clear':
        expressionTargets.value = null
        break
    }
  }

  return { model, animationEvent, preloadEvent, faceValues, expressionTargets, handleMessage }
})
