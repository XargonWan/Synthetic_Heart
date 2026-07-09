import type { VisemeWeights } from '@/composables/vrm/face'
import type { LipSyncDriver } from '@/lib/lipsync'
import type { PlaybackItem } from '@/lib/pipelines-audio/playback-manager'
import type { ServerMessage, TtsPlayMessage } from '@/services/protocol'

import { defineStore } from 'pinia'
import { ref } from 'vue'

import { AnalyserLipSyncDriver } from '@/lib/lipsync'
import { createPlaybackManager } from '@/lib/pipelines-audio/playback-manager'

interface TtsAudio {
  url: string
  offsetS: number
  meta: TtsPlayMessage
}

let nextItemId = 1

/**
 * Avatar speech playback: queues `tts-play` events through the ported AIRI
 * playback manager (one voice; a new utterance steals the slot, matching the
 * legacy viewer's stop-previous behaviour), drives lipsync from the live
 * audio, and exposes speaking state for the half-duplex mic guard.
 */
export const useAudioStore = defineStore('audio', () => {
  const speaking = ref(false)
  const enabled = ref(true)
  const lastError = ref<string | null>(null)

  let context: AudioContext | null = null
  let lipsync: LipSyncDriver | null = null
  // Which item currently owns the shared `speaking`/`lipsync` state. Needed
  // because an aborted item's cleanup resolves via a Promise (see onAbort
  // below), which only runs as a *microtask* — a newer item can already be
  // synchronously mid-playback by the time a superseded item's `finally`
  // block executes (this happens on every turn interrupt: scheduleTts's
  // stopAll() + schedule() runs the new item's synchronous start before the
  // old item's abort-triggered continuation gets a turn). Without this
  // guard, the stale item's cleanup would clobber the new item's state,
  // e.g. `speaking` flips back to false right after the new clip started.
  let activeItemId: string | null = null

  function ensureContext(): AudioContext {
    if (!context)
      context = new AudioContext()
    if (context.state === 'suspended')
      void context.resume()
    return context
  }

  async function playItem(item: PlaybackItem<TtsAudio>, signal: AbortSignal): Promise<void> {
    const ctx = ensureContext()
    const audio = new Audio(item.audio.url)
    audio.crossOrigin = 'anonymous'
    if (item.audio.offsetS > 0)
      audio.currentTime = item.audio.offsetS

    const source = ctx.createMediaElementSource(audio)
    source.connect(ctx.destination)

    const itemLipsync: LipSyncDriver = new AnalyserLipSyncDriver()
    itemLipsync.attach(source, ctx, item.audio.meta.lipsync)

    activeItemId = item.id
    lipsync = itemLipsync
    speaking.value = true
    try {
      await audio.play()
      await new Promise<void>((resolve, reject) => {
        const cleanup = (): void => {
          audio.removeEventListener('ended', onEnded)
          audio.removeEventListener('error', onError)
          signal.removeEventListener('abort', onAbort)
        }
        const onEnded = (): void => {
          cleanup()
          resolve()
        }
        const onError = (): void => {
          cleanup()
          reject(new Error(`audio error for ${item.audio.url}`))
        }
        const onAbort = (): void => {
          cleanup()
          audio.pause()
          resolve()
        }
        audio.addEventListener('ended', onEnded)
        audio.addEventListener('error', onError)
        signal.addEventListener('abort', onAbort)
      })
    }
    finally {
      itemLipsync.detach()
      source.disconnect()
      if (activeItemId === item.id) {
        lipsync = null
        speaking.value = false
      }
    }
  }

  const manager = createPlaybackManager<TtsAudio>({
    play: playItem,
    maxVoices: 1,
    // 'queue' lets same-turn chunks (see turnKey below) play back-to-back
    // without cutting each other off; a genuinely new turn calls stopAll()
    // explicitly instead of relying on overflow to steal the slot.
    overflowPolicy: 'queue',
  })

  // Chunks of one sentence-streamed reply share `turn_id` (see
  // plugins/vox_plugin.py::_speak_chunked) and should queue after one
  // another. A single-shot reply has no turn_id, so its unique `url` stands
  // in as its turn key — every such message is its own turn, matching the
  // old steal-on-arrival behaviour exactly.
  let currentTurnKey: string | null = null

  function scheduleTts(msg: TtsPlayMessage, offsetS = 0): void {
    if (!enabled.value || !msg.url)
      return
    lastError.value = null

    const turnKey = msg.turn_id ?? msg.url
    if (turnKey !== currentTurnKey) {
      manager.stopAll('new-turn')
      currentTurnKey = turnKey
    }

    manager.schedule({
      id: `tts-${nextItemId++}`,
      streamId: 'synth-tts',
      intentId: 'speech',
      segmentId: msg.url,
      sequence: nextItemId,
      priority: 10,
      text: msg.text ?? '',
      special: null,
      audio: { url: msg.url, offsetS, meta: msg },
      createdAt: Date.now(),
    })
  }

  function handleMessage(msg: ServerMessage): void {
    if (msg.type === 'tts-play')
      scheduleTts(msg, msg.offset_s ?? 0)
  }

  /** Sampled by the Stage render loop and fed to the facial driver. */
  function sampleVisemes(): VisemeWeights {
    return lipsync?.sample() ?? {}
  }

  function stopAll(reason = 'user-stop'): void {
    manager.stopAll(reason)
  }

  return { speaking, enabled, lastError, scheduleTts, handleMessage, sampleVisemes, stopAll, ensureContext }
})
