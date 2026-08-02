<script setup lang="ts">
import type { VRM } from '@pixiv/three-vrm'
import type { SceneHost } from '@/composables/vrm/scene'

import { useElementSize } from '@vueuse/core'
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'

import type { AvatarDriver } from '@/composables/vrm/avatar-driver'

import type { BlendshapeMap } from '@/composables/vrm/face'

import { createAvatarDriver } from '@/composables/vrm/avatar-driver'
import { EyeSaccadeDriver } from '@/composables/vrm/eye-saccade'
import { FacialDriver } from '@/composables/vrm/face'
import { disposeVrm, loadVrm } from '@/composables/vrm/loader'
import { CAMERA_PRESETS, createSceneHost } from '@/composables/vrm/scene'
import LoadingScreen from '@/components/system/LoadingScreen.vue'
import { fetchFullState, fetchSkins } from '@/services/karada-rest'
import { useAudioStore } from '@/stores/audio'
import { useAvatarStore } from '@/stores/avatar'
import { useConnectionStore } from '@/stores/connection'
import { useSettingsStore } from '@/stores/settings'

const canvasRef = ref<HTMLCanvasElement | null>(null)
const containerRef = ref<HTMLElement | null>(null)
const loadError = ref<string | null>(null)
const modelLoading = ref(false)
/** 0–1 during VRM download, null while retargeting animations afterward
 * (that phase has no measurable byte size). */
const loadProgress = ref<number | null>(0)
const loadLabel = ref('summoning…')

const audio = useAudioStore()
const avatar = useAvatarStore()
const connection = useConnectionStore()
const settings = useSettingsStore()

const host = shallowRef<SceneHost | null>(null)
const currentVrm = shallowRef<VRM | null>(null)
const driver = shallowRef<AvatarDriver | null>(null)
const facial = shallowRef<FacialDriver | null>(null)
const eyes = shallowRef<EyeSaccadeDriver | null>(null)

async function loadBlendshapeMap(modelUrl: string): Promise<BlendshapeMap> {
  try {
    const skinName = /\/skins\/([^/]+)\//.exec(modelUrl)?.[1]?.toLowerCase()
    const skins = await fetchSkins()
    const skin = skins.find(s => s.name.toLowerCase() === skinName) ?? skins[0]
    const persona = skin?.persona as { blendshape_map?: BlendshapeMap } | undefined
    return persona?.blendshape_map ?? {}
  }
  catch {
    return {}
  }
}

const { width, height } = useElementSize(containerRef)

let loadSequence = 0

async function switchModel(url: string): Promise<void> {
  const sceneHost = host.value
  if (!sceneHost)
    return
  const seq = ++loadSequence
  modelLoading.value = true
  loadError.value = null
  loadProgress.value = 0
  loadLabel.value = 'downloading model…'
  try {
    const vrm = await loadVrm(url, (fraction) => {
      if (seq === loadSequence)
        loadProgress.value = fraction
    })
    if (seq !== loadSequence) {
      // A newer model swap superseded this load.
      disposeVrm(vrm)
      return
    }
    loadProgress.value = null
    loadLabel.value = 'preparing animations…'
    if (currentVrm.value) {
      driver.value?.dispose()
      driver.value = null
      facial.value?.dispose()
      facial.value = null
      eyes.value = null
      sceneHost.avatarRoot.remove(currentVrm.value.scene)
      disposeVrm(currentVrm.value)
    }
    sceneHost.avatarRoot.add(vrm.scene)
    currentVrm.value = vrm

    const newFacial = new FacialDriver(vrm, await loadBlendshapeMap(url))
    newFacial.setFaceValues(avatar.faceValues)
    newFacial.setOverlay(avatar.expressionTargets)

    // The driver preloads idle + replays the server's current animation, so
    // the model is added already animating (no visible T-pose).
    const newDriver = await createAvatarDriver(vrm, {
      modelUrl: url,
      onDescriptorState: state => newFacial.setDescriptorState(state),
    })
    if (seq !== loadSequence) {
      newDriver.dispose()
      newFacial.dispose()
      return
    }
    facial.value = newFacial
    driver.value = newDriver
    eyes.value = new EyeSaccadeDriver(vrm)
  }
  catch (err) {
    if (seq === loadSequence)
      loadError.value = err instanceof Error ? err.message : String(err)
  }
  finally {
    if (seq === loadSequence)
      modelLoading.value = false
  }
}

onMounted(async () => {
  const canvas = canvasRef.value
  if (!canvas)
    return

  const sceneHost = createSceneHost(canvas)
  host.value = sceneHost
  sceneHost.applyCameraPreset(CAMERA_PRESETS[settings.cameraPreset])

  sceneHost.onFrame((delta) => {
    driver.value?.update(delta)
    facial.value?.setVisemes(audio.sampleVisemes())
    facial.value?.update(delta)
    eyes.value?.update(delta)
    currentVrm.value?.update(delta)
  })

  connection.connect()

  // Initial avatar state comes from REST (works even before the WS handshake
  // finishes and regardless of which socket variant we ride).
  try {
    const state = await fetchFullState()
    if (!avatar.model && state.vrm_model?.url) {
      avatar.model = {
        name: state.vrm_model.name ?? '',
        url: state.vrm_model.url,
      }
    }
    if (state.face_values)
      avatar.faceValues = state.face_values

    // Late join mid-utterance: replay the current TTS clip seeked to where
    // the server says it is (may be blocked by autoplay policy until the
    // first user gesture — that's fine, the next utterance will play).
    const currentAudio = state.audio as { url?: string, offset_s?: number } | null
    if (currentAudio?.url) {
      audio.scheduleTts(
        { type: 'tts-play', ...currentAudio, url: currentAudio.url },
        currentAudio.offset_s ?? 0,
      )
    }
  }
  catch (err) {
    loadError.value = `Backend unreachable: ${err instanceof Error ? err.message : err}`
  }
})

watch(() => avatar.model?.url, (url) => {
  if (url)
    void switchModel(url)
})

watch(() => avatar.animationEvent, (event) => {
  if (event && driver.value)
    void driver.value.applyAnimationEvent(event)
})

watch(() => avatar.preloadEvent, (event) => {
  if (event && driver.value)
    driver.value.applyPreloadEvent(event)
})

watch(() => avatar.faceValues, (values) => {
  facial.value?.setFaceValues(values)
})

watch(() => avatar.expressionTargets, (targets) => {
  facial.value?.setOverlay(targets)
})

watch(() => settings.cameraPreset, (preset) => {
  host.value?.applyCameraPreset(CAMERA_PRESETS[preset])
})

watch([width, height], ([w, h]) => {
  if (w && h)
    host.value?.setSize(w, h)
})

onBeforeUnmount(() => {
  loadSequence++
  driver.value?.dispose()
  driver.value = null
  facial.value?.dispose()
  facial.value = null
  eyes.value = null
  if (currentVrm.value) {
    host.value?.avatarRoot.remove(currentVrm.value.scene)
    disposeVrm(currentVrm.value)
    currentVrm.value = null
  }
  host.value?.dispose()
  host.value = null
  connection.disconnect()
})
</script>

<template>
  <div ref="containerRef" class="relative h-full w-full">
    <canvas ref="canvasRef" class="block h-full w-full" />
    <LoadingScreen v-if="modelLoading" :progress="loadProgress" :label="loadLabel" />
    <div
      v-if="loadError"
      class="absolute bottom-4 left-1/2 max-w-lg -translate-x-1/2 rounded-lg bg-red-900/80 px-4 py-2 text-sm text-red-100"
    >
      {{ loadError }}
    </div>
  </div>
</template>

<style scoped>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.3s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
