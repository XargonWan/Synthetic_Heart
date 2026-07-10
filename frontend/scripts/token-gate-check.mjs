// Verifies the frontend half of the /api/audio/upload token gate end-to-end:
// with SYNTH_WEBUI_API_TOKEN set on the backend and the token configured in
// the stage's settings store (localStorage), a real mic utterance cycle
// (VAD speech_start -> speech_end, injected like barge-in-check.mjs since a
// fake device can't produce genuine speech) makes the app POST the recorded
// clip to /api/audio/upload WITH ?token= and get past the 401 gate.
//
// Run against a token-gated scratch backend, e.g.:
//   SYNTH_WEBUI_TLS=0 SYNTH_WEBUI_HTTP_PORT=8092 SYNTH_WEBUI_API_TOKEN=gatetest \
//     uv run python scripts/run_webui.py
//   node scripts/token-gate-check.mjs http://127.0.0.1:8092 gatetest
import { chromium } from 'playwright'

const base = process.argv[2] ?? 'http://127.0.0.1:8092'
const token = process.argv[3] ?? 'gatetest'

const browser = await chromium.launch({
  args: [
    '--autoplay-policy=no-user-gesture-required',
    '--use-fake-device-for-media-stream',
    '--use-fake-ui-for-media-stream',
  ],
})
const page = await browser.newPage()
page.on('pageerror', err => console.log('[pageerror]', err.message))

// Configure the token exactly as the Settings drawer would persist it, and
// capture the /api/audio/stream WebSocket so we can inject VAD frames.
await page.addInitScript((tok) => {
  localStorage.setItem('synth-stage/api-token', tok)
  const OrigWebSocket = window.WebSocket
  window.WebSocket = new Proxy(OrigWebSocket, {
    construct(target, args) {
      const ws = new target(...args)
      if (String(args[0]).includes('/api/audio/stream'))
        window.__capturedAudioStreamWs = ws
      return ws
    },
  })
}, token)

const uploadRequest = page.waitForRequest(
  req => req.url().includes('/api/audio/upload') && req.method() === 'POST',
  { timeout: 20000 },
)

await page.goto(`${base}/stage/`, { waitUntil: 'domcontentloaded' })
await page.waitForFunction(() => !!window.__stage?.mic, { timeout: 8000 })

await page.evaluate(() => { void window.__stage.mic.start() })
await page.waitForFunction(() => window.__stage.mic.state === 'listening', { timeout: 8000 })

// One synthetic utterance: speech_start begins the MediaRecorder capture,
// speech_end stops it and triggers the upload.
await page.evaluate(() => {
  window.__capturedAudioStreamWs.onmessage({ data: JSON.stringify({ type: 'vad', signal: 'speech_start' }) })
})
await page.waitForTimeout(800)
await page.evaluate(() => {
  window.__capturedAudioStreamWs.onmessage({ data: JSON.stringify({ type: 'vad', signal: 'speech_end' }) })
})

const req = await uploadRequest
const resp = await req.response()
const url = req.url()
const status = resp?.status()
console.log('upload request URL:', url)
console.log('upload response status:', status)

const hasToken = url.includes(`token=${encodeURIComponent(token)}`)
console.log(
  hasToken && status !== 401
    ? 'PASS: recorded clip was POSTed with ?token= and got past the auth gate'
    : `FAIL: ${hasToken ? '' : 'token missing from upload URL; '}${status === 401 ? 'server rejected with 401' : ''}`,
)

await browser.close()
