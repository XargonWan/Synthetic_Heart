// Visual smoke check for SyntH Stage: loads the app, waits for the VRM to
// render, captures console + a screenshot.
import { chromium } from 'playwright'

const url = process.argv[2] ?? 'http://127.0.0.1:8088/stage/'
const shot = process.argv[3] ?? 'stage-check.png'
const waitMs = Number(process.argv[4] ?? 15000)

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })

const logs = []
page.on('console', msg => logs.push(`[${msg.type()}] ${msg.text()}`))
page.on('pageerror', err => logs.push(`[pageerror] ${err.message}`))

await page.goto(url, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(waitMs)

// Probe canvas: non-blank pixels indicate the model rendered.
const canvasInfo = await page.evaluate(() => {
  const canvas = document.querySelector('canvas')
  if (!canvas)
    return { present: false }
  return { present: true, w: canvas.width, h: canvas.height }
})

await page.screenshot({ path: shot })
await browser.close()

console.log('canvas:', JSON.stringify(canvasInfo))
console.log('--- console output ---')
for (const l of logs.slice(0, 60)) console.log(l)
