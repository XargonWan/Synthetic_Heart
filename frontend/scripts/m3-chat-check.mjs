// M3 verification: chat UI send path + facial layer (descriptor eyes_closed
// during think must now close the eyes via the persona blendshape map).
import { chromium } from 'playwright'

const base = process.argv[2] ?? 'http://127.0.0.1:8088'
const outDir = process.argv[3] ?? '.'

const browser = await chromium.launch()
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

// 1. Send a chat message through the UI.
await page.fill('input[type="text"]', 'Hello from the new stage!')
await page.press('input[type="text"]', 'Enter')
await page.waitForTimeout(2000)
await page.screenshot({ path: `${outDir}/m3-chat.png` })

// 2. Fire think — descriptor closes the eyes from frame 20 (~0.7s in).
const resp = await fetch(`${base}/api/karada/action`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ action: 'think', priority: 10 }),
})
console.log(`POST think -> ${resp.status}`)
await page.waitForTimeout(4000)
await page.screenshot({ path: `${outDir}/m3-think-eyes.png` })

await browser.close()
console.log('--- console (filtered) ---')
for (const l of logs.slice(0, 40)) console.log(l)
