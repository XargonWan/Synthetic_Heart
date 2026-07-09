// Verifies barge-in end-to-end at the code level: getUserMedia requests
// echoCancellation/noiseSuppression/autoGainControl, and a VAD speech_start
// signal arriving while the avatar is speaking interrupts it immediately.
//
// Real acoustic echo cancellation (does the avatar's own voice actually stay
// below Silero's VAD threshold on real hardware) cannot be verified in a
// headless sandboxed browser — this only proves the app-side wiring is
// correct: the mic requests AEC, and the interrupt logic fires on the right
// signal. A synthetic `vad`/`speech_start` frame is injected by intercepting
// the real /api/audio/stream WebSocket (opened against a live backend) since
// nothing in this environment can produce genuine speech audio for a fake
// mic device to pick up.
import { chromium } from 'playwright'

const base = process.argv[2] ?? 'http://127.0.0.1:8092'

function silentWavDataUri(durationS) {
  const rate = 8000
  const frames = Math.round(rate * durationS)
  const dataSize = frames * 2
  const buf = Buffer.alloc(44 + dataSize)
  buf.write('RIFF', 0)
  buf.writeUInt32LE(36 + dataSize, 4)
  buf.write('WAVE', 8)
  buf.write('fmt ', 12)
  buf.writeUInt32LE(16, 16)
  buf.writeUInt16LE(1, 20)
  buf.writeUInt16LE(1, 22)
  buf.writeUInt32LE(rate, 24)
  buf.writeUInt32LE(rate * 2, 28)
  buf.writeUInt16LE(2, 32)
  buf.writeUInt16LE(16, 34)
  buf.write('data', 36)
  buf.writeUInt32LE(dataSize, 40)
  return `data:audio/wav;base64,${buf.toString('base64')}`
}

const browser = await chromium.launch({
  args: [
    '--autoplay-policy=no-user-gesture-required',
    '--use-fake-device-for-media-stream',
    '--use-fake-ui-for-media-stream',
  ],
})
const page = await browser.newPage()
page.on('pageerror', err => console.log('[pageerror]', err.message))

// Capture getUserMedia constraints and the /api/audio/stream WebSocket
// instance before any app code runs.
await page.addInitScript(() => {
  const origGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices)
  navigator.mediaDevices.getUserMedia = (constraints) => {
    window.__capturedConstraints = constraints
    return origGetUserMedia(constraints)
  }

  const OrigWebSocket = window.WebSocket
  window.WebSocket = new Proxy(OrigWebSocket, {
    construct(target, args) {
      const ws = new target(...args)
      if (String(args[0]).includes('/api/audio/stream'))
        window.__capturedAudioStreamWs = ws
      return ws
    },
  })
})

await page.goto(`${base}/stage/`, { waitUntil: 'domcontentloaded' })
await page.waitForFunction(() => !!window.__stage?.mic, { timeout: 8000 })

// Start the avatar "speaking" a long clip.
await page.evaluate((url) => {
  window.__stage.audio.handleMessage({ type: 'tts-play', url, audio_duration_s: 5, turn_id: 'turn-mic-test' })
}, silentWavDataUri(5))
await page.waitForFunction(() => window.__stage.audio.speaking === true, { timeout: 4000 })

// Start the mic.
await page.evaluate(() => { void window.__stage.mic.start() })
await page.waitForFunction(() => window.__stage.mic.state === 'listening', { timeout: 8000 })

const constraints = await page.evaluate(() => window.__capturedConstraints)
console.log('getUserMedia constraints:', JSON.stringify(constraints))
const aec = constraints?.audio
console.log(
  aec?.echoCancellation === true && aec?.noiseSuppression === true && aec?.autoGainControl === true
    ? 'PASS: getUserMedia requested echoCancellation/noiseSuppression/autoGainControl'
    : 'FAIL: expected AEC constraints not present',
)

// Confirm the real /api/audio/stream WS actually opened.
const wsReady = await page.evaluate(() => window.__capturedAudioStreamWs?.readyState)
console.log('audio-stream ws readyState (1 = OPEN):', wsReady)

// Simulate the server telling us the user started talking while the avatar
// is still speaking — this is the actual barge-in trigger.
const speakingBefore = await page.evaluate(() => window.__stage.audio.speaking)
await page.evaluate(() => {
  window.__capturedAudioStreamWs.onmessage({ data: JSON.stringify({ type: 'vad', signal: 'speech_start' }) })
})
await page.waitForFunction(() => window.__stage.audio.speaking === false, { timeout: 3000 }).catch(() => {})
const speakingAfter = await page.evaluate(() => window.__stage.audio.speaking)
const userSpeaking = await page.evaluate(() => window.__stage.mic.userSpeaking)

console.log('barge-in result:', { speakingBefore, speakingAfter, userSpeaking })
console.log(
  speakingBefore === true && speakingAfter === false && userSpeaking === true
    ? 'PASS: speech_start while avatar was speaking interrupted it immediately (barge-in)'
    : 'FAIL: barge-in did not interrupt the avatar as expected',
)

await browser.close()
