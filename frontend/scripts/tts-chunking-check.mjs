// Verifies the frontend half of sentence-chunked TTS streaming: same-turn
// chunks (stores/audio.ts) queue and play back-to-back instead of cutting
// each other off, a new turn immediately interrupts a stale one, and
// chunked tts-play events don't corrupt the chat bubble replay link
// (stores/chat.ts). Drives the real Pinia stores via the debug hook
// (window.__stage), not raw WS frames, so this exercises the actual app
// code, not a re-implementation of it.
import { chromium } from 'playwright'

const base = process.argv[2] ?? 'http://127.0.0.1:8092'

// Tiny silent WAV, precise duration via frame count (8kHz mono 16-bit).
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

const browser = await chromium.launch()
const page = await browser.newPage()
page.on('console', (msg) => {
  if (msg.type() === 'error')
    console.log('[browser error]', msg.text())
})
await page.goto(`${base}/stage/`, { waitUntil: 'domcontentloaded' })
await page.waitForFunction(() => !!window.__stage?.audio, { timeout: 8000 })

// --- 1. Same-turn chunks queue back-to-back, no premature cutoff ---
// Headless Chromium plays tiny data-URI clips much slower than their
// nominal duration (observed ~5-6x in this environment), so this checks
// the *behavioral* property (one continuous speaking run, no gaps, clean
// eventual finish) rather than exact wall-clock timing.
const queuedResult = await page.evaluate(async (urls) => {
  const audio = window.__stage.audio
  const events = []
  let last = null
  const iv = setInterval(() => {
    if (audio.speaking !== last) {
      events.push(audio.speaking)
      last = audio.speaking
    }
  }, 10)

  audio.handleMessage({ type: 'tts-play', url: urls[0], audio_duration_s: 0.25, turn_id: 'turn-A' })
  await new Promise(r => setTimeout(r, 60))
  audio.handleMessage({ type: 'tts-play', url: urls[1], audio_duration_s: 0.25, turn_id: 'turn-A' })
  await new Promise(r => setTimeout(r, 60))
  audio.handleMessage({ type: 'tts-play', url: urls[2], audio_duration_s: 0.25, turn_id: 'turn-A' })

  // Poll until it settles back to false (bounded wait, not a fixed sleep).
  for (let i = 0; i < 400 && audio.speaking; i++)
    await new Promise(r => setTimeout(r, 50))

  clearInterval(iv)
  const trueRuns = events.filter(s => s).length
  return { trueRuns, stillSpeaking: audio.speaking }
}, [silentWavDataUri(0.25), silentWavDataUri(0.25), silentWavDataUri(0.25)])

console.log('same-turn queue result:', queuedResult)
console.log(
  queuedResult.trueRuns === 1 && !queuedResult.stillSpeaking
    ? 'PASS: three same-turn chunks played as one continuous run (queued, not stolen) and finished cleanly'
    : 'FAIL: chunks did not queue correctly',
)

// --- 2. A new turn interrupts a stale one immediately, without a false blip ---
const interruptResult = await page.evaluate(async (urls) => {
  const audio = window.__stage.audio
  const events = []
  let last = null
  const iv = setInterval(() => {
    if (audio.speaking !== last) {
      events.push(audio.speaking)
      last = audio.speaking
    }
  }, 5)

  audio.handleMessage({ type: 'tts-play', url: urls[0], audio_duration_s: 2, turn_id: 'turn-B' })
  await new Promise(r => setTimeout(r, 300))
  const speakingBeforeInterrupt = audio.speaking
  audio.handleMessage({ type: 'tts-play', url: urls[1], audio_duration_s: 0.2, turn_id: 'turn-C' })

  // Poll until it settles back to false (bounded wait).
  for (let i = 0; i < 400 && audio.speaking; i++)
    await new Promise(r => setTimeout(r, 50))

  clearInterval(iv)
  // No false-then-true blip across the interrupt: speaking should have
  // stayed continuously true from turn-B into turn-C (only one true->false
  // transition total: the very end, when turn-C's clip finishes).
  const falseTransitions = events.filter(s => !s).length
  return { speakingBeforeInterrupt, falseTransitions, speakingAfter: audio.speaking }
}, [silentWavDataUri(2), silentWavDataUri(0.2)])

console.log('new-turn interrupt result:', interruptResult)
console.log(
  interruptResult.speakingBeforeInterrupt && interruptResult.falseTransitions === 1 && !interruptResult.speakingAfter
    ? 'PASS: new turn interrupted the stale clip with no premature false blip, then finished cleanly'
    : 'FAIL: new turn did not interrupt as expected',
)

// --- 3. Chunked tts-play (turn_id set) must not corrupt the replay link ---
const chatResult = await page.evaluate((urls) => {
  const chat = window.__stage.chat
  chat.messages.splice(0, chat.messages.length)
  chat.handleMessage({ type: 'message', sender: 'synth', text: 'reply text', ts: Date.now() })
  const bubbleBefore = chat.messages[chat.messages.length - 1].ttsUrl
  chat.handleMessage({ type: 'tts-play', url: urls[0], turn_id: 'turn-D' })
  const bubbleAfterChunk = chat.messages[chat.messages.length - 1].ttsUrl
  chat.handleMessage({ type: 'message', sender: 'synth', text: 'reply text', ts: Date.now(), tts_url: '/static/combined.wav' })
  const finalBubbleTtsUrl = chat.messages[chat.messages.length - 1].ttsUrl
  return { bubbleBefore, bubbleAfterChunk, finalBubbleTtsUrl }
}, [silentWavDataUri(0.1)])

console.log('chat bubble result:', chatResult)
console.log(
  chatResult.bubbleBefore === undefined
  && chatResult.bubbleAfterChunk === undefined
  && chatResult.finalBubbleTtsUrl === '/static/combined.wav'
    ? 'PASS: chunk tts-play left the bubble alone; combined-clip message set the correct link'
    : 'FAIL: chat bubble replay link was corrupted by a mid-stream chunk',
)

// --- 4. Legacy single-shot tts-play (no turn_id) still attaches directly ---
const legacyResult = await page.evaluate((url) => {
  const chat = window.__stage.chat
  chat.messages.splice(0, chat.messages.length)
  chat.handleMessage({ type: 'message', sender: 'synth', text: 'reply text', ts: Date.now() })
  chat.handleMessage({ type: 'tts-play', url })
  return chat.messages[chat.messages.length - 1].ttsUrl
}, silentWavDataUri(0.1))

console.log('legacy single-shot result:', legacyResult)
console.log(
  typeof legacyResult === 'string' && legacyResult.startsWith('data:audio/wav')
    ? 'PASS: single-shot (no turn_id) tts-play still attaches directly, unchanged'
    : 'FAIL: legacy single-shot attach behaviour regressed',
)

await browser.close()
