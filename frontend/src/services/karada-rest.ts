/**
 * Typed client for the public Karada REST surface (`core/karada_api.py`).
 * All URLs are relative — same-origin in production (/stage) and proxied by
 * Vite in development.
 */

export interface KaradaAnimationState {
  state?: string
  descriptor?: string | null
  started_at?: number
  [key: string]: unknown
}

export interface KaradaFullState {
  vrm_model?: { name?: string, url?: string } | null
  animation?: KaradaAnimationState | null
  face_values?: Record<string, number> | null
  audio?: Record<string, unknown> | null
  [key: string]: unknown
}

export interface AnimationManifestEntry {
  id?: string
  state?: string
  skin?: string
  animation_file?: string
  animation_url?: string | null
  descriptor_data?: DescriptorData | null
  category?: string
  [key: string]: unknown
}

export interface AnimationManifest {
  version: number
  animations: Record<string, AnimationManifestEntry>
}

export interface DescriptorSection {
  start_frame: number
  end_frame: number
}

/** `.fbx.json` descriptor sidecar — intro/loop/outro frame ranges plus
 * facial metadata. Consumed by the animation engine (composables/vrm). */
export interface DescriptorData {
  fps?: number
  intro?: DescriptorSection | null
  loop?: DescriptorSection | null
  outro?: DescriptorSection | null
  play_once?: boolean
  lipsync?: boolean
  blink?: unknown
  eye_movement?: unknown
  expressions?: Array<Record<string, unknown>>
  [key: string]: unknown
}

export interface SkinInfo {
  name: string
  has_model?: boolean
  has_animations?: boolean
  persona?: Record<string, unknown> | null
  preview_url?: string | null
  [key: string]: unknown
}

async function getJson<T>(url: string): Promise<T> {
  const resp = await fetch(url, { cache: 'no-store' })
  if (!resp.ok)
    throw new Error(`GET ${url} -> ${resp.status}`)
  return await resp.json() as T
}

export async function fetchFullState(): Promise<KaradaFullState> {
  return getJson<KaradaFullState>('/api/karada/state')
}

export async function fetchAnimationManifest(): Promise<AnimationManifest> {
  return getJson<AnimationManifest>('/api/karada/animations/manifest')
}

export async function resolveDescriptor(descriptorId: string): Promise<AnimationManifestEntry | null> {
  try {
    return await getJson<AnimationManifestEntry>(
      `/api/karada/animations/resolve?descriptor_id=${encodeURIComponent(descriptorId)}`,
    )
  }
  catch {
    return null
  }
}

export async function fetchSkins(): Promise<SkinInfo[]> {
  return getJson<SkinInfo[]>('/api/karada/skins')
}

/**
 * Switch the active skin/persona. Lives on the webui router
 * (`POST /api/skins/{name}/activate`), not the Karada action endpoint —
 * `/api/karada/action` only accepts `AnimationState` values (idle, think,
 * touch, write, talk, skin_change) and merely plays the skin_change
 * *animation*, it does not swap the model. The server broadcasts the
 * resulting `vrm_model` event to every connected client, this one included.
 */
export async function activateSkin(name: string): Promise<void> {
  const resp = await fetch(`/api/skins/${encodeURIComponent(name)}/activate`, { method: 'POST' })
  if (!resp.ok)
    throw new Error(`POST /api/skins/${name}/activate -> ${resp.status}`)
}

export async function postAction(
  action: string,
  options: { priority?: number, context_id?: string, loop?: boolean } = {},
): Promise<void> {
  const resp = await fetch('/api/karada/action', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, ...options }),
  })
  if (!resp.ok)
    throw new Error(`POST /api/karada/action -> ${resp.status}`)
}

/**
 * Manifest cache with per-descriptor fallback resolution, mirroring
 * `_resolveKaradaAnimationDescriptor` in the legacy viewer.
 */
let manifestCache: AnimationManifest | null = null

export async function resolveAnimationDescriptor(
  descriptorId: string,
  forceRefresh = false,
): Promise<AnimationManifestEntry | null> {
  if (forceRefresh)
    manifestCache = null

  if (!manifestCache) {
    try {
      manifestCache = await fetchAnimationManifest()
    }
    catch {
      manifestCache = { version: 2, animations: {} }
    }
  }

  const cached = manifestCache.animations[descriptorId]
  if (cached)
    return cached

  const entry = await resolveDescriptor(descriptorId)
  if (entry)
    manifestCache.animations[descriptorId] = entry
  return entry
}
