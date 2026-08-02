// Verifies the settings-drawer Voice section end-to-end against a live
// backend: engine lists come from the real GET /api/components, voice pills
// from GET /api/vox/speakers, previews from GET /api/vox/sample, and clicking
// pills issues the right writes.
//
// Config WRITES (POST /api/config, POST /api/components/vox/model) are ALWAYS
// intercepted and fulfilled with 200 instead of hitting the server: backends
// share the production DB, so letting a test click actually flip
// ACTIVE_VOX_ENGINE would reconfigure the production instance. The captured
// payloads are asserted instead. Safe to run against production.
//
// Scenario A: raw live data — engine pills render, clicking a non-active
//   engine POSTs ACTIVE_VOX_ENGINE, and (because the write is mocked) the
//   subsequent refresh reverts the highlight to the server's real active
//   engine.
// Scenario B: GET /api/components is rewritten to flag `kitten` active so the
//   voice grid renders (the real active engine may have no speakers) —
//   speakers and the preview sample still come from the real backend.
//
//   node scripts/voice-settings-check.mjs http://127.0.0.1:8008
import { chromium } from 'playwright'

const base = process.argv[2] ?? 'http://127.0.0.1:8008'
const browser = await chromium.launch({ args: ['--autoplay-policy=no-user-gesture-required'] })

async function setupPage(context, { rewriteActive } = {}) {
  const page = await context.newPage()
  page.on('pageerror', err => console.log('[pageerror]', err.message))

  const state = { configWrites: [], modelWrites: [], sampleRequests: [] }
  await page.route('**/api/config', async (route) => {
    if (route.request().method() === 'POST') {
      state.configWrites.push(route.request().postDataJSON())
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
      return
    }
    await route.fallback()
  })
  await page.route('**/api/components/vox/model', async (route) => {
    state.modelWrites.push(route.request().postDataJSON())
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })
  if (rewriteActive) {
    await page.route('**/api/components', async (route) => {
      if (route.request().method() !== 'GET') {
        await route.fallback()
        return
      }
      const resp = await route.fetch()
      const data = await resp.json()
      for (const e of data.vox ?? []) e.active = e.name === rewriteActive
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) })
    })
  }
  page.on('request', (req) => {
    if (req.url().includes('/api/vox/sample'))
      state.sampleRequests.push(req.url())
  })

  await page.goto(`${base}/stage/`, { waitUntil: 'domcontentloaded' })
  await page.click('[title="Settings"]')
  const voiceSection = page.locator('section', { has: page.locator('h3', { hasText: 'Voice' }) })
  await voiceSection.waitFor({ timeout: 10000 })
  return { page, voiceSection, state }
}

let allPass = true
function check(label, ok, detail = '') {
  allPass = allPass && !!ok
  console.log(`${ok ? 'PASS' : 'FAIL'}: ${label}${detail ? ` — ${detail}` : ''}`)
}

// ── Scenario A: live data, engine switch write ─────────────────────────────
{
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } })
  const { page, voiceSection, state } = await setupPage(context)

  const ttsPills = voiceSection.locator('div:has(> span:text-is("Text to speech")) > div > button')
  const sttPills = voiceSection.locator('div:has(> span:text-is("Speech to text")) > div > button')
  await ttsPills.first().waitFor({ timeout: 10000 })
  const ttsNames = (await ttsPills.allTextContents()).map(s => s.trim())
  const sttNames = (await sttPills.allTextContents()).map(s => s.trim())
  console.log('TTS engines rendered:', ttsNames)
  console.log('STT engines rendered:', sttNames)
  check('engine lists render from live /api/components', ttsNames.length > 0 && sttNames.length > 0)

  const activeBefore = await ttsPills.evaluateAll(els =>
    els.find(el => el.className.includes('bg-primary'))?.textContent?.trim() ?? null)
  const pillCount = await ttsPills.count()
  let clicked = null
  for (let i = 0; i < pillCount; i++) {
    const cls = await ttsPills.nth(i).getAttribute('class')
    if (!cls.includes('bg-primary')) {
      clicked = (await ttsPills.nth(i).textContent()).trim()
      await ttsPills.nth(i).click()
      break
    }
  }
  await page.waitForTimeout(800)
  const engineWrite = state.configWrites.find(w => w?.key === 'ACTIVE_VOX_ENGINE')
  check('clicking a non-active engine POSTs ACTIVE_VOX_ENGINE', engineWrite && engineWrite.value,
    `clicked ${clicked} -> ${JSON.stringify(engineWrite)}`)
  const activeAfter = await ttsPills.evaluateAll(els =>
    els.find(el => el.className.includes('bg-primary'))?.textContent?.trim() ?? null)
  check('highlight reverts to server truth when the write does not stick', activeAfter === activeBefore,
    `before=${activeBefore} after=${activeAfter}`)
  await context.close()
}

// ── Scenario B: kitten flagged active -> voice grid + preview + persist ────
{
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } })
  const { page, voiceSection, state } = await setupPage(context, { rewriteActive: 'kitten' })

  const voicePills = voiceSection.locator('div.grid > button')
  await voicePills.first().waitFor({ timeout: 10000 }).catch(() => {})
  const voiceCount = await voicePills.count()
  const voiceNames = (await voicePills.allTextContents()).map(s => s.trim().replace(/[▶♪]\s*$/, '').trim())
  console.log('voices rendered:', voiceNames)
  check('voice grid renders from live /api/vox/speakers', voiceCount > 0)

  if (voiceCount > 0) {
    // Click a voice that is NOT the currently persisted one.
    const selectedIdx = await voicePills.evaluateAll(els =>
      els.findIndex(el => el.className.includes('bg-primary')))
    const target = selectedIdx === 0 ? 1 : 0
    await voicePills.nth(target).click()
    await page.waitForTimeout(1200)
    check('clicking a voice requests a live preview sample', state.sampleRequests.length > 0,
      state.sampleRequests[0])
    const voiceWrite = state.configWrites.find(w => String(w?.key ?? '').endsWith('_VOICE'))
    check('clicking a voice persists the engine voice key', voiceWrite !== undefined,
      JSON.stringify(voiceWrite))
  }

  await page.screenshot({ path: 'scripts/voice-settings.png' })
  console.log('screenshot: scripts/voice-settings.png')
  await context.close()
}

console.log(allPass ? 'ALL PASS' : 'SOME CHECKS FAILED')
await browser.close()
