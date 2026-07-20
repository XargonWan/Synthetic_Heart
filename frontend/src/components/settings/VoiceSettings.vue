<script setup lang="ts">
import type { SpeakerInfo, VoiceEngineInfo } from '@/services/voice-config'

import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

import {
  fetchAurisModels,
  fetchConfigValues,
  fetchLanguages,
  fetchSpeakers,
  fetchVoiceComponents,
  getVoxLanguageOverrides,
  setConfigValue,
  setSubsystemModel,
  setVoxLanguageOverrides,
  downloadAurisModel,
  type LanguageInfo,
  type ModelCatalogEntry,
  type VoxLanguageOverride,
  type VoxLanguageOverrides,
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

// ── Vosk active-model selector (local Auris engine) ──
const aurisModels = ref<ModelCatalogEntry[]>([])
const selectedAurisModel = ref('')

async function loadAurisModels(): Promise<void> {
  aurisModels.value = []
  selectedAurisModel.value = ''
  if (activeAuris.value !== 'vosk')
    return
  try {
    const [list, config] = await Promise.all([fetchAurisModels(), fetchConfigValues(['VOSK_MODEL'])])
    if (activeAuris.value !== 'vosk')
      return
    aurisModels.value = list
    selectedAurisModel.value = config['VOSK_MODEL'] ?? ''
  }
  catch {
    aurisModels.value = []
  }
}

async function selectAurisModel(model: string): Promise<void> {
  if (busy.value)
    return
  busy.value = true
  try {
    await setConfigValue('VOSK_MODEL', model)
    selectedAurisModel.value = model
    const entry = aurisModels.value.find(m => m.model_id === model)
    if (entry && !entry.downloaded) {
      error.value = null
      await downloadAurisModel(model)
      // Refresh the catalog so the option flips to "downloaded" once done.
      await loadAurisModels()
    }
  }
  catch {
    error.value = 'Failed to save Vosk model'
  }
  finally {
    busy.value = false
  }
}

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
    await loadAurisModels()
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

watch(activeAuris, () => {
  void loadAurisModels()
})

// ── Vox per-language engine overrides ──────────────────────────────────────
const languages = ref<LanguageInfo[]>([])
const langOverrides = ref<VoxLanguageOverrides>({})
const langOverrideEditing = ref<string | null>(null)
const langOverrideEngine = ref('')
const langOverrideModel = ref('')
const langOverrideVoice = ref('')
const langOverrideVoices = ref<SpeakerInfo[]>([])
const langOverrideBusy = ref(false)

const langOverrideEngineModels = computed<string[]>(() => {
  const engine = voxEngines.value.find(e => e.name === langOverrideEngine.value)
  return engine?.available_models ?? []
})

function langLabel(code: string): string {
  const info = languages.value.find(l => l.code === code)
  return info ? `${info.it || info.en || code} (${code})` : code
}

async function loadLanguageOverrides(): Promise<void> {
  try {
    const [langs, map] = await Promise.all([fetchLanguages(), getVoxLanguageOverrides()])
    languages.value = langs
    langOverrides.value = map
  }
  catch {
    languages.value = []
    langOverrides.value = {}
  }
}

async function loadLangOverrideVoices(engine: string): Promise<void> {
  langOverrideVoices.value = []
  if (!engine || engine === 'disabled' || engine === 'kitten')
    return
  try {
    langOverrideVoices.value = await fetchSpeakers(engine)
  }
  catch {
    langOverrideVoices.value = []
  }
}

function openLangOverrideEditor(code: string | null): void {
  langOverrideEditing.value = code
  if (code && langOverrides.value[code]) {
    const entry = langOverrides.value[code]
    langOverrideEngine.value = entry.engine || ''
    langOverrideModel.value = entry.model || ''
    langOverrideVoice.value = entry.voice || ''
  }
  else {
    langOverrideEngine.value = ''
    langOverrideModel.value = ''
    langOverrideVoice.value = ''
  }
  void loadLangOverrideVoices(langOverrideEngine.value)
}

function closeLangOverrideEditor(): void {
  langOverrideEditing.value = null
}

async function saveLangOverride(): Promise<void> {
  const code = langOverrideEditing.value
  if (!code || !langOverrideEngine.value || langOverrideEngine.value === 'disabled') {
    error.value = 'Select a language and a non-disabled engine'
    return
  }
  langOverrideBusy.value = true
  try {
    const entry: VoxLanguageOverride = {
      engine: langOverrideEngine.value,
      model: langOverrideModel.value,
      voice: langOverrideVoice.value,
    }
    const next: VoxLanguageOverrides = { ...langOverrides.value, [code]: entry }
    await setVoxLanguageOverrides(next)
    langOverrides.value = next
    closeLangOverrideEditor()
  }
  catch {
    error.value = 'Failed to save language override'
  }
  finally {
    langOverrideBusy.value = false
  }
}

async function deleteLangOverride(code: string): Promise<void> {
  const next: VoxLanguageOverrides = { ...langOverrides.value }
  delete next[code]
  try {
    await setVoxLanguageOverrides(next)
    langOverrides.value = next
    if (langOverrideEditing.value === code)
      closeLangOverrideEditor()
  }
  catch {
    error.value = 'Failed to remove language override'
  }
}

watch(langOverrideEngine, (engine) => {
  langOverrideModel.value = ''
  void loadLangOverrideVoices(engine)
})

onMounted(async () => {
  await refresh()
  await loadVoices()
  syncSelectedModel()
  await loadLanguageOverrides()
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
        <span class="text-[11px] text-white/40">Vox — Text to speech</span>
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

        <!-- Per-language engine overrides -->
        <div class="mt-3 flex flex-col gap-1.5 border-t border-white/10 pt-3">
          <div class="flex items-center justify-between">
            <span class="text-[11px] text-white/40">Language overrides</span>
            <button
              class="rounded-lg bg-white/10 px-2 py-1 text-[11px] text-white/70 transition hover:bg-white/20"
              :disabled="busy || langOverrideBusy"
              @click="openLangOverrideEditor(null)"
            >
              ＋ Add language override
            </button>
          </div>
          <p class="text-[10px] text-white/30">
            Route TTS to a different engine / model / voice per detected language.
          </p>

          <div v-if="Object.keys(langOverrides).length" class="flex flex-col gap-1">
            <div
              v-for="(entry, code) in langOverrides"
              :key="code"
              class="flex items-center justify-between rounded-lg bg-white/5 px-2 py-1 text-xs"
            >
              <span class="truncate">
                <span class="font-semibold text-white/80">{{ langLabel(code) }}</span>
                <span class="ml-1 text-white/40">
                  → {{ entry.engine }}{{ entry.model ? ` · ${entry.model}` : '' }}{{ entry.voice ? ` · ${entry.voice}` : '' }}
                </span>
              </span>
              <span class="flex shrink-0 gap-1">
                <button
                  class="rounded bg-white/10 px-1.5 py-0.5 text-[10px] text-white/70 hover:bg-white/20"
                  @click="openLangOverrideEditor(code)"
                >
                  Edit
                </button>
                <button
                  class="rounded bg-red-500/20 px-1.5 py-0.5 text-[10px] text-red-200 hover:bg-red-500/30"
                  @click="deleteLangOverride(code)"
                >
                  Remove
                </button>
              </span>
            </div>
          </div>
          <p v-else class="text-[10px] text-white/30">
            No overrides configured — default engine is used for every language.
          </p>

          <!-- Editor -->
          <div
            v-if="langOverrideEditing !== null"
            class="mt-1 flex flex-col gap-1.5 rounded-lg border border-white/10 bg-white/5 p-2"
          >
            <div class="flex flex-wrap gap-1.5">
              <select
                :value="langOverrideEditing"
                :disabled="true"
                class="rounded-lg bg-white/10 px-2 py-1 text-xs text-white [&>option]:bg-neutral-900"
              >
                <option v-for="l in languages" :key="l.code" :value="l.code">
                  {{ l.it || l.en || l.code }} ({{ l.code }})
                </option>
              </select>
              <select
                v-model="langOverrideEngine"
                class="rounded-lg bg-white/10 px-2 py-1 text-xs text-white [&>option]:bg-neutral-900"
              >
                <option value="">— engine —</option>
                <option v-for="engine in voxEngines" :key="engine.name" :value="engine.name">
                  {{ engineLabel(engine) }}
                </option>
              </select>
              <select
                v-model="langOverrideModel"
                class="rounded-lg bg-white/10 px-2 py-1 text-xs text-white [&>option]:bg-neutral-900"
              >
                <option value="">(default model)</option>
                <option v-for="model in langOverrideEngineModels" :key="model" :value="model">
                  {{ model }}
                </option>
              </select>
              <select
                v-model="langOverrideVoice"
                class="rounded-lg bg-white/10 px-2 py-1 text-xs text-white [&>option]:bg-neutral-900"
              >
                <option value="">(default voice)</option>
                <option v-for="speaker in langOverrideVoices" :key="speaker.code" :value="speaker.code">
                  {{ speaker.name || speaker.code }}
                </option>
              </select>
            </div>
            <div class="flex gap-1.5">
              <button
                class="rounded-lg bg-primary-500/80 px-2 py-1 text-[11px] text-white hover:bg-primary-500"
                :disabled="langOverrideBusy"
                @click="saveLangOverride"
              >
                Save
              </button>
              <button
                class="rounded-lg bg-white/10 px-2 py-1 text-[11px] text-white/70 hover:bg-white/20"
                @click="closeLangOverrideEditor"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- STT -->
      <div v-if="aurisEngines.length" class="flex flex-col gap-1.5">
        <span class="text-[11px] text-white/40">Auris — Speech to text</span>
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
        <!-- Vosk active-model selector: ALL catalog models, pinned via VOSK_MODEL -->
        <div v-if="activeAuris === 'vosk'" class="mt-1 flex flex-col gap-1">
          <span class="text-[11px] text-white/40">Active Vosk model</span>
          <select
            v-if="aurisModels.length"
            class="rounded-lg bg-white/10 px-2 py-1 text-xs text-white/80"
            :value="selectedAurisModel"
            :disabled="busy"
            @change="selectAurisModel(($event.target as HTMLSelectElement).value)"
          >
            <option v-for="m in aurisModels" :key="m.model_id" :value="m.model_id">
              {{ m.display_name || m.model_id }}{{ m.downloaded ? '' : (m.downloading ? ' (downloading…)' : ' (not downloaded — select to download)') }}
            </option>
          </select>
          <span v-else class="text-[11px] text-white/30">No models available</span>
        </div>
      </div>

      <p v-if="!voxEngines.length && !aurisEngines.length && !error" class="text-xs text-white/40">
        No voice engines registered on this server.
      </p>
    </template>
  </div>
</template>
