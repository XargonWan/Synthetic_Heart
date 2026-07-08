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

    lipsync = new AnalyserLipSyncDriver()
    lipsync.attach(source, ctx, item.audio.meta.lipsync)

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
      lipsync?.detach()
      lipsync = null
      source.disconnect()
      speaking.value = false
    }
  }

  const manager = createPlaybackManager<TtsAudio>({
    play: playItem,
    maxVoices: 1,
    overflowPolicy: 'steal-oldest',
  })

  function scheduleTts(msg: TtsPlayMessage, offsetS = 0): void {
    if (!enabled.value || !msg.url)
      return
    lastError.value = null
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

  function stopAll(): void {
    manager.stopAll('user-stop')
  }

  return { speaking, enabled, lastError, scheduleTts, handleMessage, sampleVisemes, stopAll, ensureContext }
})
