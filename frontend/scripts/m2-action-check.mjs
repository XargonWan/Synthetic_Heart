// M2 verification: open the stage, then fire a Karada action server-side and
// capture before/during screenshots to confirm the descriptor state machine
// (intro→loop) and WS animation events work end to end.
import { chromium } from 'playwright'

const base = process.argv[2] ?? 'http://127.0.0.1:8088'
const outDir = process.argv[3] ?? '.'
const action = process.argv[4] ?? 'think'

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
const logs = []
page.on('console', (msg) => {
  const t = msg.text()
  if (!/GPU stall|Context Lost|Context Restored/.test(t))
    logs.push(`[${msg.type()}] ${t}`)
})
page.on('pageerror', err => logs.push(`[pageerror] ${err.message}`))

await page.goto(`${base}/stage/`, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(9000)
await page.screenshot({ path: `${outDir}/m2-before.png` })

const resp = await fetch(`${base}/api/karada/action`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ action, priority: 10 }),
})
console.log(`POST /api/karada/action ${action} -> ${resp.status}`)

await page.waitForTimeout(1500)
await page.screenshot({ path: `${outDir}/m2-during-intro.png` })
await page.waitForTimeout(3500)
await page.screenshot({ path: `${outDir}/m2-during-loop.png` })

await browser.close()
console.log('--- console (filtered) ---')
for (const l of logs.slice(0, 40)) console.log(l)
