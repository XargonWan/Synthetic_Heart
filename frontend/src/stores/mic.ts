import type { MicCapture } from '@/lib/audio/mic-capture'

import { defineStore } from 'pinia'
import { ref } from 'vue'

import { MIC_TARGET_SAMPLE_RATE, startMicCapture } from '@/lib/audio/mic-capture'
import { VoiceRecorder } from '@/lib/audio/voice-recorder'
import { AudioStreamClient } from '@/services/audio-stream'
import { transcribeClip } from '@/services/audio-upload'
import { useAudioStore } from './audio'
import { useChatStore } from './chat'
import { useConnectionStore } from './connection'

export type MicState = 'off' | 'starting' | 'listening' | 'error'

/**
 * Voice input, mirroring the legacy webui's ambient-VAD flow
 * (res/synth_webui/js/chat-window.mjs `_startSileroVAD`/`stopRecordingAndSend`):
 *
 *   mic stream ─┬─▶ 16kHz PCM ──▶ /api/audio/stream (VAD signal only:
 *               │                 speech_start/speech_end, no transcript)
 *               └─▶ MediaRecorder (per-utterance clip)
 *
 * On `speech_start` a recording segment begins; on `speech_end` it stops and
 * the clip is POSTed to `/api/audio/upload` for the actual transcript, which
 * is then sent over `/ws` exactly like a typed message.
 *
 * Half-duplex guard: mic frames are suppressed while the avatar is speaking
 * (`audio.speaking`) so its own voice is never fed back into VAD/STT. This
 * suppression point is also the phase-2 barge-in seam.
 */
export const useMicStore = defineStore('mic', () => {
  const state = ref<MicState>('off')
  const userSpeaking = ref(false)
  const transcribing = ref(false)
  const error = ref<string | null>(null)

  let stream: MediaStream | null = null
  let pcmCapture: MicCapture | null = null
  let vadClient: AudioStreamClient | null = null
  let recorder: VoiceRecorder | null = null
  let uploadAbort: AbortController | null = null

  const audio = useAudioStore()
  const chat = useChatStore()
  const connection = useConnectionStore()

  async function submitClip(): Promise<void> {
    const rec = recorder
    if (!rec)
      return
    const blob = await rec.stop()
    if (!blob)
      return
    uploadAbort?.abort()
    uploadAbort = new AbortController()
    transcribing.value = true
    try {
      const text = await transcribeClip(blob, uploadAbort.signal)
      if (text && connection.sendText(text, true))
        chat.addLocalUserMessage(text)
    }
    catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError')
        return
      error.value = err instanceof Error ? err.message : String(err)
    }
    finally {
      uploadAbort = null
      transcribing.value = false
    }
  }

  async function start(): Promise<void> {
    if (state.value === 'listening' || state.value === 'starting')
      return
    state.value = 'starting'
    error.value = null

    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      recorder = new VoiceRecorder(stream)

      const client = new AudioStreamClient({
        onVad: (signal) => {
          userSpeaking.value = signal === 'speech_start'
          if (signal === 'speech_start') {
            recorder?.start()
          }
          else {
            void submitClip()
          }
        },
        onError: (detail) => {
          error.value = detail
        },
        onClose: () => {
          if (state.value === 'listening')
            void stop()
        },
      }, MIC_TARGET_SAMPLE_RATE)
      await client.open()
      vadClient = client

      pcmCapture = await startMicCapture(stream, (pcm) => {
        // Half-duplex: don't feed the avatar's own voice back into VAD.
        if (!audio.speaking)
          vadClient?.sendPcm(pcm)
      })

      state.value = 'listening'
    }
    catch (err) {
      error.value = err instanceof Error ? err.message : String(err)
      state.value = 'error'
      await teardown()
    }
  }

  async function teardown(): Promise<void> {
    pcmCapture?.stop()
    pcmCapture = null
    vadClient?.close()
    vadClient = null
    await recorder?.stop()
    recorder = null
    stream?.getTracks().forEach(track => track.stop())
    stream = null
    userSpeaking.value = false
  }

  async function stop(): Promise<void> {
    await teardown()
    if (state.value !== 'error')
      state.value = 'off'
  }

  async function toggle(): Promise<void> {
    if (state.value === 'listening')
      await stop()
    else
      await start()
  }

  return { state, userSpeaking, transcribing, error, start, stop, toggle }
})
