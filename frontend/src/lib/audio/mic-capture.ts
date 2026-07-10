/**
 * Microphone capture → 16 kHz mono s16le PCM chunks, the wire format of
 * SyntH's `/api/audio/stream` endpoint.
 *
 * An AudioWorklet accumulates input samples; the main thread receives
 * Float32 blocks, downsamples from the context rate, and converts to s16le.
 * (Downsampling by linear interpolation matches what the legacy webui's
 * capture path produces — the backend's Silero VAD is tolerant of it.)
 */

export const MIC_TARGET_SAMPLE_RATE = 16000

// ~128 ms of audio per posted block at 48 kHz — small enough for responsive
// VAD, large enough to keep message overhead negligible.
const WORKLET_BLOCK_SAMPLES = 6144

const WORKLET_SOURCE = `
class MicCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super()
    this._buffer = new Float32Array(${WORKLET_BLOCK_SAMPLES})
    this._offset = 0
  }
  process(inputs) {
    const channel = inputs[0] && inputs[0][0]
    if (!channel) return true
    let i = 0
    while (i < channel.length) {
      const space = this._buffer.length - this._offset
      const copy = Math.min(space, channel.length - i)
      this._buffer.set(channel.subarray(i, i + copy), this._offset)
      this._offset += copy
      i += copy
      if (this._offset === this._buffer.length) {
        this.port.postMessage(this._buffer.slice(0))
        this._offset = 0
      }
    }
    return true
  }
}
registerProcessor('mic-capture', MicCaptureProcessor)
`

function downsampleTo16k(input: Float32Array, inputRate: number): Float32Array {
  if (inputRate === MIC_TARGET_SAMPLE_RATE)
    return input
  const ratio = inputRate / MIC_TARGET_SAMPLE_RATE
  const outLength = Math.floor(input.length / ratio)
  const out = new Float32Array(outLength)
  for (let i = 0; i < outLength; i++) {
    const pos = i * ratio
    const idx = Math.floor(pos)
    const frac = pos - idx
    const a = input[idx] ?? 0
    const b = input[idx + 1] ?? a
    out[i] = a + (b - a) * frac
  }
  return out
}

function floatToS16le(input: Float32Array): ArrayBuffer {
  const out = new DataView(new ArrayBuffer(input.length * 2))
  for (let i = 0; i < input.length; i++) {
    const clamped = Math.max(-1, Math.min(1, input[i] ?? 0))
    out.setInt16(i * 2, clamped < 0 ? clamped * 0x8000 : clamped * 0x7FFF, true)
  }
  return out.buffer
}

export interface MicCapture {
  stop: () => void
}

/**
 * Start streaming 16kHz PCM from an existing MediaStream to `onChunk` — used
 * to feed the VAD signal socket. Does not acquire its own getUserMedia
 * stream, so callers can share one mic stream between this and a
 * MediaRecorder (see stores/mic.ts).
 */
export async function startMicCapture(
  stream: MediaStream,
  onChunk: (pcm: ArrayBuffer) => void,
): Promise<MicCapture> {
  const context = new AudioContext()
  const workletUrl = URL.createObjectURL(
    new Blob([WORKLET_SOURCE], { type: 'application/javascript' }),
  )
  try {
    await context.audioWorklet.addModule(workletUrl)
  }
  finally {
    URL.revokeObjectURL(workletUrl)
  }

  const source = context.createMediaStreamSource(stream)
  const node = new AudioWorkletNode(context, 'mic-capture')
  node.port.onmessage = (event: MessageEvent<Float32Array>) => {
    const downsampled = downsampleTo16k(event.data, context.sampleRate)
    onChunk(floatToS16le(downsampled))
  }
  source.connect(node)
  // Worklets need a destination path to run in some browsers; route through
  // a zero-gain node so the mic is never audible.
  const silence = context.createGain()
  silence.gain.value = 0
  node.connect(silence)
  silence.connect(context.destination)

  return {
    stop: () => {
      node.port.onmessage = null
      source.disconnect()
      node.disconnect()
      silence.disconnect()
      // Stream ownership stays with the caller (shared with MediaRecorder).
      void context.close()
    },
  }
}
