// Confirms the SettingsDrawer's "Access token" <input> actually round-trips
// through the Pinia store into localStorage and reconnects the socket live
// (the piece a pure store/localStorage check can't see).
import { chromium } from 'playwright'

const base = process.argv[2] ?? 'http://127.0.0.1:8091'
const token = process.argv[3] ?? 'stagetest123'

const browser = await chromium.launch()
const page = await browser.newPage()
await page.goto(`${base}/stage/`, { waitUntil: 'domcontentloaded' })

// Starts disconnected (no token, server requires one).
await page.waitForFunction(() => window.__stage?.connection?.status === 'reconnecting', { timeout: 8000 })
console.log('initial status:', await page.evaluate(() => window.__stage.connection.status))

await page.click('button[title="Settings"]')
await page.fill('input[type="password"]', token)
// blur to make sure the v-model.trim commit fires
await page.locator('input[type="password"]').blur()

const stored = await page.evaluate(() => window.localStorage.getItem('synth-stage/api-token'))
console.log('localStorage after typing token:', JSON.stringify(stored))

const status = await page.waitForFunction(() => window.__stage?.connection?.status === 'connected', { timeout: 8000 })
  .then(() => 'connected')
  .catch(() => 'did-not-reconnect')
console.log('status after entering correct token in the UI:', status)

await browser.close()
