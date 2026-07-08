/**
 * Client for the mic STT WebSocket (`/api/audio/stream`, core/webui.py).
 * Wire contract: first frame is a JSON config `{sample_rate, engine}`, then
 * binary s16le PCM chunks. Server replies with JSON events.
 *
 * IMPORTANT: on the default `vad`/`silero` engine, the server only ever
 * emits `ready` and `vad` (speech_start/speech_end) — it does not
 * transcribe. `partial`/`final` are only emitted when `engine` names a
 * bidirectional Live engine (core/live_registry.py); this client supports
 * that shape too, but SyntH's default configuration never exercises it —
 * the default voice path buffers the clip and POSTs it to
 * `/api/audio/upload` instead (see lib/audio/voice-recorder.ts).
 */

export interface AudioStreamEvents {
  onReady?: () => void
  onPartial?: (text: string) => void
  onFinal?: (text: string) => void
  onVad?: (signal: 'speech_start' | 'speech_end') => void
  onError?: (detail: string) => void
  onClose?: () => void
}

interface AudioStreamServerMessage {
  type: 'ready' | 'partial' | 'final' | 'vad' | 'error'
  text?: string
  signal?: string
  detail?: string
  [key: string]: unknown
}

export class AudioStreamClient {
  private ws: WebSocket | null = null

  constructor(
    private readonly events: AudioStreamEvents,
    private readonly sampleRate = 16000,
    private readonly engine = 'vad',
  ) {}

  get isOpen(): boolean {
    return this.ws?.readyState === WebSocket.OPEN
  }

  async open(): Promise<void> {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${window.location.host}/api/audio/stream`)
    ws.binaryType = 'arraybuffer'
    this.ws = ws

    await new Promise<void>((resolve, reject) => {
      ws.onopen = () => {
        ws.send(JSON.stringify({ sample_rate: this.sampleRate, engine: this.engine }))
        resolve()
      }
      ws.onerror = () => reject(new Error('audio stream connection failed'))
    })

    ws.onmessage = (event: MessageEvent<string>) => {
      let msg: AudioStreamServerMessage
      try {
        msg = JSON.parse(event.data)
      }
      catch {
        return
      }
      switch (msg.type) {
        case 'ready':
          this.events.onReady?.()
          break
        case 'partial':
          if (msg.text)
            this.events.onPartial?.(msg.text)
          break
        case 'final':
          if (msg.text)
            this.events.onFinal?.(msg.text)
          break
        case 'vad':
          if (msg.signal === 'speech_start' || msg.signal === 'speech_end')
            this.events.onVad?.(msg.signal)
          break
        case 'error':
          this.events.onError?.(msg.detail ?? 'audio stream error')
          break
      }
    }

    ws.onclose = () => {
      this.ws = null
      this.events.onClose?.()
    }
  }

  sendPcm(chunk: ArrayBuffer): void {
    if (this.isOpen)
      this.ws!.send(chunk)
  }

  close(): void {
    this.ws?.close()
    this.ws = null
  }
}
