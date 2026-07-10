<script setup lang="ts">
import type { SpeakerInfo, VoiceEngineInfo } from '@/services/voice-config'

import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

import {
  fetchConfigValues,
  fetchSpeakers,
  fetchVoiceComponents,
  setConfigValue,
  setSubsystemModel,
  voiceSampleUrl,
} from '@/services/voice-config'

const loading = ref(true)
const error = ref<string | null>(null)
const busy = ref(false)

const voxEngines = ref<VoiceEngineInfo[]>([])
const aurisEngines = ref<VoiceEngineInfo[]>([])
const activeVox = ref<string | null>(null)
const activeAuris = ref<string | null>(null)
const voxCurrentModel = ref('')

const speakers = ref<SpeakerInfo[]>([])
// Engines following the `<ENGINE>_VOICE` config convention (kitten today) get
// a persistable voice choice; engines without the key are preview-only.
const voiceConfigKey = computed(() =>
  activeVox.value ? `${activeVox.value.toUpperCase()}_VOICE` : null,
)
const currentVoice = ref<string | undefined>(undefined)
const voicePersistable = ref(false)
const previewing = ref<string | null>(null)

const activeVoxEngine = computed(() => voxEngines.value.find(e => e.name === activeVox.value))
const voxModels = computed(() => activeVoxEngine.value?.available_models ?? [])
const selectedModel = ref('')

let previewAudio: HTMLAudioElement | null = null

function stopPreview(): void {
  if (previewAudio) {
    previewAudio.pause()
    previewAudio = null
  }
  previewing.value = null
}

async function refresh(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const components = await fetchVoiceComponents()
    voxEngines.value = components.vox
    aurisEngines.value = components.auris
    activeVox.value = components.vox.find(e => e.active)?.name ?? components.vox[0]?.name ?? null
    activeAuris.value = components.auris.find(e => e.active)?.name ?? null
    voxCurrentModel.value = components.voxCurrentModel
  }
  catch {
    error.value = 'Voice engines unavailable — is the backend reachable?'
  }
  finally {
    loading.value = false
  }
}

async function loadVoices(): Promise<void> {
  speakers.value = []
  voicePersistable.value = false
  currentVoice.value = undefined
  const engine = activeVox.value
  const key = voiceConfigKey.value
  if (!engine || !key)
    return
  try {
    const [list, config] = await Promise.all([fetchSpeakers(engine), fetchConfigValues([key])])
    // Engine may have been switched again while we were fetching.
    if (engine !== activeVox.value)
      return
    speakers.value = list
    voicePersistable.value = key in config && config[key] !== undefined
    currentVoice.value = config[key]
  }
  catch {
    speakers.value = []
  }
}

function syncSelectedModel(): void {
  const models = voxModels.value
  const preferred = voxCurrentModel.value || activeVoxEngine.value?.default_model || ''
  selectedModel.value = models.includes(preferred) ? preferred : (models[0] ?? '')
}

async function selectEngine(subsystem: 'vox' | 'auris', name: string): Promise<void> {
  const current = subsystem === 'vox' ? activeVox : activeAuris
  if (busy.value || current.value === name)
    return
  busy.value = true
  stopPreview()
  try {
    await setConfigValue(subsystem === 'vox' ? 'ACTIVE_VOX_ENGINE' : 'ACTIVE_AURIS_ENGINE', name)
    current.value = name
    await refresh()
  }
  catch {
    error.value = `Failed to switch ${subsystem === 'vox' ? 'TTS' : 'STT'} engine`
  }
  finally {
    busy.value = false
  }
}

/** Clicking a voice previews it, and persists the choice when the engine supports it. */
async function selectVoice(speaker: SpeakerInfo): Promise<void> {
  stopPreview()
  if (activeVox.value) {
    previewing.value = speaker.code
    previewAudio = new Audio(voiceSampleUrl(activeVox.value, speaker.code))
    previewAudio.addEventListener('ended', () => stopPreview())
    previewAudio.play().catch(() => stopPreview())
  }
  if (voicePersistable.value && voiceConfigKey.value && currentVoice.value !== speaker.code) {
    try {
      await setConfigValue(voiceConfigKey.value, speaker.code)
      currentVoice.value = speaker.code
    }
    catch {
      error.value = 'Failed to save voice'
    }
  }
}

async function selectModel(model: string): Promise<void> {
  if (!activeVox.value || busy.value)
    return
  busy.value = true
  try {
    await setSubsystemModel('vox', activeVox.value, model)
    selectedModel.value = model
  }
  catch {
    error.value = 'Failed to save TTS model'
  }
  finally {
    busy.value = false
  }
}

watch(activeVox, () => {
  void loadVoices()
  syncSelectedModel()
})

onMounted(async () => {
  await refresh()
  await loadVoices()
  syncSelectedModel()
})
onUnmounted(stopPreview)

function engineLabel(engine: VoiceEngineInfo): string {
  return engine.display_name || engine.name
}
</script>

<template>
  <div class="flex flex-col gap-3">
    <p v-if="loading" class="text-xs text-white/40">
      Loading voice engines…
    </p>
    <p v-else-if="error" class="text-xs text-red-300">
      {{ error }}
      <button class="ml-1 underline hover:text-red-200" @click="refresh">
        retry
      </button>
    </p>

    <template v-if="!loading">
      <!-- TTS -->
      <div v-if="voxEngines.length" class="flex flex-col gap-1.5">
        <span class="text-[11px] text-white/40">Text to speech</span>
        <div class="flex flex-wrap gap-1.5">
          <button
            v-for="engine in voxEngines"
            :key="engine.name"
            class="rounded-lg px-2 py-1 text-xs transition"
            :class="engine.name === activeVox
              ? 'bg-primary-500/80 text-white'
              : 'bg-white/10 text-white/70 hover:bg-white/20'"
            :disabled="busy"
            :title="engine.description || engine.label"
            @click="selectEngine('vox', engine.name)"
          >
            {{ engineLabel(engine) }}
          </button>
        </div>

        <template v-if="speakers.length">
          <span class="mt-1 text-[11px] text-white/40">
            Voice <span v-if="!voicePersistable" class="text-white/25">(preview only)</span>
          </span>
          <div class="grid grid-cols-2 gap-1.5">
            <button
              v-for="speaker in speakers"
              :key="speaker.code"
              class="group flex items-center justify-between gap-1 rounded-lg px-2 py-1 text-left text-xs transition"
              :class="voicePersistable && speaker.code === currentVoice
                ? 'bg-primary-500/80 text-white'
                : 'bg-white/10 text-white/70 hover:bg-white/20'"
              @click="selectVoice(speaker)"
            >
              <span class="truncate">{{ speaker.name || speaker.code }}</span>
              <span
                class="shrink-0 text-[10px]"
                :class="previewing === speaker.code ? 'animate-pulse text-white' : 'text-white/40 group-hover:text-white/70'"
              >
                {{ previewing === speaker.code ? '♪' : '▶' }}
              </span>
            </button>
          </div>
        </template>

        <template v-if="voxModels.length">
          <span class="mt-1 text-[11px] text-white/40">Model</span>
          <select
            :value="selectedModel"
            :disabled="busy"
            class="w-full rounded-lg bg-white/10 px-2 py-1.5 text-xs text-white focus:outline-none focus:ring-1 focus:ring-primary-400 [&>option]:bg-neutral-900"
            @change="selectModel(($event.target as HTMLSelectElement).value)"
          >
            <option v-for="model in voxModels" :key="model" :value="model">
              {{ model }}
            </option>
          </select>
        </template>
      </div>

      <!-- STT -->
      <div v-if="aurisEngines.length" class="flex flex-col gap-1.5">
        <span class="text-[11px] text-white/40">Speech to text</span>
        <div class="flex flex-wrap gap-1.5">
          <button
            v-for="engine in aurisEngines"
            :key="engine.name"
            class="rounded-lg px-2 py-1 text-xs transition"
            :class="engine.name === activeAuris
              ? 'bg-primary-500/80 text-white'
              : 'bg-white/10 text-white/70 hover:bg-white/20'"
            :disabled="busy"
            :title="engine.description || engine.label"
            @click="selectEngine('auris', engine.name)"
          >
            {{ engineLabel(engine) }}
          </button>
        </div>
      </div>

      <p v-if="!voxEngines.length && !aurisEngines.length && !error" class="text-xs text-white/40">
        No voice engines registered on this server.
      </p>
    </template>
  </div>
</template>
