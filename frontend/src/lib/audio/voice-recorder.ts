/**
 * Per-utterance audio capture for STT, mirroring the legacy webui's
 * MediaRecorder → `/api/audio/upload` flow (res/synth_webui/js/chat-window.mjs
 * `startRecording`/`stopRecordingAndSend`). SyntH's `/api/audio/stream`
 * endpoint on the default `vad`/`silero` engine only emits VAD
 * speech_start/speech_end signals — it does not transcribe — so the actual
 * clip has to be buffered and POSTed as a file once an utterance ends.
 */
export class VoiceRecorder {
  private recorder: MediaRecorder | null = null
  private chunks: Blob[] = []

  constructor(private readonly stream: MediaStream) {}

  get isRecording(): boolean {
    return this.recorder?.state === 'recording'
  }

  start(): void {
    if (this.isRecording)
      return
    const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
      ? 'audio/webm;codecs=opus'
      : MediaRecorder.isTypeSupported('audio/webm')
        ? 'audio/webm'
        : ''
    this.chunks = []
    this.recorder = mimeType ? new MediaRecorder(this.stream, { mimeType }) : new MediaRecorder(this.stream)
    this.recorder.ondataavailable = (e) => {
      if (e.data.size > 0)
        this.chunks.push(e.data)
    }
    this.recorder.start(100)
  }

  /** Stop and resolve the recorded clip (null if nothing was captured). */
  async stop(): Promise<Blob | null> {
    const recorder = this.recorder
    if (!recorder || recorder.state === 'inactive')
      return null
    this.recorder = null
    return new Promise((resolve) => {
      recorder.onstop = () => {
        const blob = this.chunks.length
          ? new Blob(this.chunks, { type: recorder.mimeType || 'audio/webm' })
          : null
        this.chunks = []
        resolve(blob && blob.size >= 512 ? blob : null)
      }
      recorder.stop()
    })
  }
}
