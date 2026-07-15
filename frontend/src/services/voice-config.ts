/**
 * Voice subsystem configuration client — TTS (Vox) and STT (Auris).
 *
 * These live on the legacy webui surface (`core/webui.py`), not the Karada
 * router; the request/payload shapes mirror what the legacy settings page
 * does (`res/synth_webui/js/main.js`):
 *   GET  /api/components                 engine registries (+active flags, models)
 *   GET  /api/config                     config registry items [{key, value, …}]
 *   POST /api/config                     {key, value} single-key update
 *   GET  /api/vox/speakers?engine=       [{code, name, language?}]
 *   GET  /api/vox/sample?engine=&speaker= WAV voice preview
 *   POST /api/components/vox/model       {engine, model}
 *
 * None of these are token-gated today, but every URL goes through
 * `withApiToken()` anyway — it is a no-op without a configured token and
 * keeps this client working if the backend gate ever widens.
 */
import { withApiToken } from '@/lib/api-token'

export interface VoiceEngineInfo {
  name: string
  display_name?: string
  description?: string
  label?: string
  active?: boolean
  available_models?: string[]
  default_model?: string | null
  [key: string]: unknown
}

export interface VoiceComponents {
  vox: VoiceEngineInfo[]
  auris: VoiceEngineInfo[]
  voxCurrentModel: string
  aurisCurrentModel: string
}

export interface SpeakerInfo {
  code: string
  name?: string
  language?: string
  [key: string]: unknown
}

async function getJson<T>(url: string): Promise<T> {
  const resp = await fetch(withApiToken(url))
  if (!resp.ok)
    throw new Error(`GET ${url} -> ${resp.status}`)
  return await resp.json() as T
}

async function postJson(url: string, body: unknown): Promise<void> {
  const resp = await fetch(withApiToken(url), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!resp.ok)
    throw new Error(`POST ${url} -> ${resp.status}`)
}

export async function fetchVoiceComponents(): Promise<VoiceComponents> {
  const data = await getJson<{
    vox?: VoiceEngineInfo[]
    auris?: VoiceEngineInfo[]
    vox_current_model?: string
    auris_current_model?: string
  }>('/api/components')
  return {
    vox: data.vox ?? [],
    auris: data.auris ?? [],
    voxCurrentModel: data.vox_current_model ?? '',
    aurisCurrentModel: data.auris_current_model ?? '',
  }
}

/**
 * Read config registry values for the given keys in one round trip.
 * Keys absent from the registry map to `undefined` — callers use that to
 * detect whether a per-engine setting (e.g. `KITTEN_VOICE`) exists at all.
 */
export async function fetchConfigValues(keys: string[]): Promise<Record<string, string | undefined>> {
  const data = await getJson<{ items?: Array<{ key?: string, value?: unknown }> }>('/api/config')
  const items = data.items ?? []
  const out: Record<string, string | undefined> = {}
  for (const key of keys) {
    const item = items.find(i => i.key === key)
    out[key] = item === undefined || item.value == null ? undefined : String(item.value)
  }
  return out
}

export async function setConfigValue(key: string, value: string): Promise<void> {
  await postJson('/api/config', { key, value })
}

export async function fetchSpeakers(engine: string): Promise<SpeakerInfo[]> {
  return getJson<SpeakerInfo[]>(`/api/vox/speakers?engine=${encodeURIComponent(engine)}`)
}

export function voiceSampleUrl(engine: string, speaker: string): string {
  return withApiToken(`/api/vox/sample?engine=${encodeURIComponent(engine)}&speaker=${encodeURIComponent(speaker)}`)
}

/** Persist the model choice for engines that expose `available_models`. */
export async function setSubsystemModel(subsystem: 'vox' | 'auris', engine: string, model: string): Promise<void> {
  await postJson(`/api/components/${subsystem}/model`, { engine, model })
}

// ── Vox per-language engine overrides ──────────────────────────────────────
// Global map VOX_LANGUAGE_OVERRIDES: { "<iso639-1>": { engine, model, voice } }.
// Lets the user route TTS to a different engine/model/voice per detected
// language (e.g. Italian -> Fish Audio + "maria", English -> Kitten + "luna").

export interface LanguageInfo {
  code: string
  en: string
  it: string
}

export interface VoxLanguageOverride {
  engine: string
  model: string
  voice: string
}

export type VoxLanguageOverrides = Record<string, VoxLanguageOverride>

export async function fetchLanguages(): Promise<LanguageInfo[]> {
  const data = await getJson<{ languages?: LanguageInfo[] }>('/api/languages')
  return data.languages ?? []
}

export async function getVoxLanguageOverrides(): Promise<VoxLanguageOverrides> {
  const values = await fetchConfigValues(['VOX_LANGUAGE_OVERRIDES'])
  const raw = values['VOX_LANGUAGE_OVERRIDES']
  if (!raw)
    return {}
  try {
    const parsed = JSON.parse(raw)
    return (parsed && typeof parsed === 'object') ? parsed as VoxLanguageOverrides : {}
  }
  catch {
    return {}
  }
}

export async function setVoxLanguageOverrides(map: VoxLanguageOverrides): Promise<void> {
  await postJson('/api/config', { key: 'VOX_LANGUAGE_OVERRIDES', value: JSON.stringify(map) })
}
