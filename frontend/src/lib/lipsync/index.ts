import type { VisemeWeights } from '@/composables/vrm/face'
import type { LipsyncData } from '@/services/protocol'

/**
 * Pluggable mouth-driving strategy.
 *
 * The active implementation is amplitude-based (mirrors the legacy viewer's
 * analyser approach) because no SyntH TTS engine currently returns
 * precomputed phoneme timings (`VoxBase.get_lipsync_data` → None). The
 * interface exists so a precomputed-frames driver or a wlipsync
 * (vowel-classification) driver can slot in without touching callers —
 * that's also the phase-2 realtime-audio seam.
 */
export interface LipSyncDriver {
  /** Wire the driver to the audio graph for one utterance. */
  attach: (source: AudioNode, context: AudioContext, meta?: LipsyncData | null) => void
  /** Sample current viseme weights (called once per render frame). */
  sample: () => VisemeWeights
  detach: () => void
}

const MOUTH_OPEN_CAP = 0.7 // legacy viewer caps mouth-open at 0.7
const SMOOTHING = 0.35

export class AnalyserLipSyncDriver implements LipSyncDriver {
  private analyser: AnalyserNode | null = null
  private buffer: Uint8Array<ArrayBuffer> | null = null
  private smoothed = 0

  attach(source: AudioNode, context: AudioContext): void {
    this.detach()
    const analyser = context.createAnalyser()
    analyser.fftSize = 256
    analyser.smoothingTimeConstant = 0.2
    source.connect(analyser)
    this.analyser = analyser
    this.buffer = new Uint8Array(analyser.frequencyBinCount)
  }

  sample(): VisemeWeights {
    if (!this.analyser || !this.buffer)
      return {}
    this.analyser.getByteTimeDomainData(this.buffer)
    // RMS of the time-domain signal → mouth openness.
    let sumSquares = 0
    for (const v of this.buffer) {
      const centered = (v - 128) / 128
      sumSquares += centered * centered
    }
    const rms = Math.sqrt(sumSquares / this.buffer.length)
    // Perceptual curve: quiet speech still moves the mouth.
    const target = Math.min(MOUTH_OPEN_CAP, Math.sqrt(rms) * 1.6)
    this.smoothed += (target - this.smoothed) * SMOOTHING
    return { A: this.smoothed < 0.02 ? 0 : this.smoothed }
  }

  detach(): void {
    this.analyser?.disconnect()
    this.analyser = null
    this.buffer = null
    this.smoothed = 0
  }
}
