// Repro script for the three reported /stage issues:
//   1. theme hue slider has no visible effect
//   2. skin switch does nothing on the stage
//   3. mic: "Cannot read properties of undefined (reading 'getUserMedia')"
// Usage: node scripts/repro-issues.mjs [base] [outDir]
import { chromium } from 'playwright'

const base = process.argv[2] ?? 'http://127.0.0.1:8088'
const outDir = process.argv[3] ?? '.'

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
const wsFrames = []
page.on('websocket', (ws) => {
  ws.on('framereceived', (frame) => {
    try {
      const data = JSON.parse(frame.payload)
      wsFrames.push(data.type)
    }
    catch {}
  })
})
page.on('pageerror', err => console.log('[pageerror]', err.message))

await page.goto(`${base}/stage/`, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(8000)

// ── mic environment ─────────────────────────────────────────────
const micEnv = await page.evaluate(() => ({
  secureContext: window.isSecureContext,
  mediaDevices: !!navigator.mediaDevices,
}))
console.log('mic env @', base, '->', JSON.stringify(micEnv))

// ── theme hue ───────────────────────────────────────────────────
const probeColor = () => page.evaluate(() => {
  let el = document.getElementById('hue-probe')
  if (!el) {
    el = document.createElement('div')
    el.id = 'hue-probe'
    el.className = 'bg-primary-500'
    document.body.appendChild(el)
  }
  return {
    color: getComputedStyle(el).backgroundColor,
    hueVar: getComputedStyle(document.documentElement).getPropertyValue('--chromatic-hue'),
    inlineHue: document.documentElement.style.getPropertyValue('--chromatic-hue'),
  }
})

await page.click('button[title="Settings"]')
await page.waitForTimeout(500)
const before = await probeColor()
await page.fill('input[type="range"]', '30')
await page.dispatchEvent('input[type="range"]', 'input')
await page.waitForTimeout(400)
const after = await probeColor()
console.log('hue before:', JSON.stringify(before))
console.log('hue after :', JSON.stringify(after))
console.log('hue slider effective:', before.color !== after.color ? 'YES' : 'NO — colors identical')
await page.screenshot({ path: `${outDir}/repro-hue.png` })

// ── skin switch ─────────────────────────────────────────────────
const stateBefore = await page.evaluate(async () =>
  (await (await fetch('/api/karada/state')).json()).vrm_model)
console.log('model before:', JSON.stringify(stateBefore))

// click the first skin tile that is NOT the active one
const skinButtons = page.locator('aside .grid button')
const count = await skinButtons.count()
console.log('skin tiles:', count)
const framesBeforeClick = wsFrames.length
let clicked = null
for (let i = 0; i < count; i++) {
  const btn = skinButtons.nth(i)
  const cls = await btn.getAttribute('class')
  if (!cls?.includes('ring-primary-400')) {
    clicked = (await btn.innerText()).trim().split('\n')[0]
    await btn.click()
    break
  }
}
console.log('clicked skin:', clicked)
await page.waitForTimeout(6000)
const framesAfterClick = wsFrames.slice(framesBeforeClick)

const stateAfter = await page.evaluate(async () =>
  (await (await fetch('/api/karada/state')).json()).vrm_model)
console.log('model after :', JSON.stringify(stateAfter))
console.log('ws frames after click:', JSON.stringify(framesAfterClick))
console.log('vrm_model broadcast received:', framesAfterClick.includes('vrm_model') ? 'YES' : 'NO')
await page.screenshot({ path: `${outDir}/repro-skin.png` })

await browser.close()
