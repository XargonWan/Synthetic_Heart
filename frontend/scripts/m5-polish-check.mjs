import { chromium } from 'playwright'

const base = process.argv[2] ?? 'http://127.0.0.1:8088'
const outDir = process.argv[3] ?? '.'

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
const logs = []
page.on('console', (msg) => {
  const t = msg.text()
  if (!/GPU stall|Context Lost|Context Restored/.test(t))
    logs.push(`[${msg.type()}] ${t}`)
})
page.on('pageerror', err => logs.push(`[pageerror] ${err.message}`))

// 1. Loading screen — screenshot immediately, before the model finishes.
await page.goto(`${base}/stage/`, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(300)
await page.screenshot({ path: `${outDir}/m5-loading.png` })
await page.waitForTimeout(9000)

// 2. Settings drawer + skin selector.
await page.click('button[title="Settings"]')
await page.waitForTimeout(600)
await page.screenshot({ path: `${outDir}/m5-settings.png` })

// 3. Theme hue slider changes the primary color.
await page.fill('input[type="range"]', '10')
await page.dispatchEvent('input[type="range"]', 'input')
await page.waitForTimeout(300)
await page.screenshot({ path: `${outDir}/m5-hue.png` })

await browser.close()

// 4. Transparent mode — fresh page.
const browser2 = await chromium.launch()
const page2 = await browser2.newPage({ viewport: { width: 800, height: 600 } })
await page2.goto(`${base}/stage/?transparent=1`, { waitUntil: 'domcontentloaded' })
await page2.waitForTimeout(9000)
await page2.screenshot({ path: `${outDir}/m5-transparent.png`, omitBackground: true })
const bodyBg = await page2.evaluate(() => getComputedStyle(document.body).backgroundColor)
console.log('transparent body bg:', bodyBg)
await browser2.close()

console.log('--- console (filtered) ---')
for (const l of logs.slice(0, 30)) console.log(l)
