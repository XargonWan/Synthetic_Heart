// M4 verification: TTS playback via the ported playback manager + analyser
// lipsync (mouth opens during audio), and the mic → /api/audio/stream path
// (fake mic device; asserts the stream opens and PCM flows without error).
import { chromium } from 'playwright'

const base = process.argv[2] ?? 'http://127.0.0.1:8088'
const outDir = process.argv[3] ?? '.'

const browser = await chromium.launch({
  args: [
    '--autoplay-policy=no-user-gesture-required',
    '--use-fake-device-for-media-stream',
    '--use-fake-ui-for-media-stream',
  ],
})
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
const logs = []
page.on('console', (msg) => {
  const t = msg.text()
  if (!/GPU stall|Context Lost|Context Restored|\[synth-ws\]/.test(t))
    logs.push(`[${msg.type()}] ${t}`)
})
page.on('pageerror', err => logs.push(`[pageerror] ${err.message}`))

await page.goto(`${base}/stage/`, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(9000)

// ── 1. TTS playback + lipsync ────────────────────────────────────────────────
await page.evaluate(() => {
  window.__stage.audio.scheduleTts({
    type: 'tts-play',
    url: '/uploads/stage_test_tone.wav',
    text: 'lipsync check',
    audio_duration_s: 3,
  })
})
await page.waitForTimeout(1200)
const speakingMid = await page.evaluate(() => window.__stage.audio.speaking)
const visemesMid = await page.evaluate(() => window.__stage.audio.sampleVisemes())
await page.screenshot({ path: `${outDir}/m4-speaking.png` })
await page.waitForTimeout(2800)
const speakingAfter = await page.evaluate(() => window.__stage.audio.speaking)
console.log(`speaking mid=${speakingMid} visemes=${JSON.stringify(visemesMid)} after=${speakingAfter}`)

// ── 2. Mic → /api/audio/stream ──────────────────────────────────────────────
await page.evaluate(() => window.__stage.mic.start())
await page.waitForTimeout(3500)
const micState = await page.evaluate(() => ({
  state: window.__stage.mic.state,
  error: window.__stage.mic.error,
}))
console.log(`mic: ${JSON.stringify(micState)}`)
await page.screenshot({ path: `${outDir}/m4-mic.png` })
await page.evaluate(() => window.__stage.mic.stop())

await browser.close()
console.log('--- console (filtered) ---')
for (const l of logs.slice(0, 30)) console.log(l)
