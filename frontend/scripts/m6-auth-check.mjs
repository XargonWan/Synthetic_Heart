// M6 verification: token-gated /ws and /api/karada/ws reject connections
// without the right token and accept them with it.
import { chromium } from 'playwright'

const base = process.argv[2] ?? 'http://127.0.0.1:8088'
const token = process.argv[3] ?? 'stagetest123'

const browser = await chromium.launch()
const page = await browser.newPage()

async function tryWs(path, withToken) {
  const url = withToken
    ? `${base.replace('http', 'ws')}${path}?token=${token}`
    : `${base.replace('http', 'ws')}${path}`
  return page.evaluate((u) => {
    return new Promise((resolve) => {
      const ws = new WebSocket(u)
      const timer = setTimeout(() => { ws.close(); resolve('timeout') }, 4000)
      ws.onopen = () => { clearTimeout(timer); resolve('open') }
      ws.onclose = (ev) => { clearTimeout(timer); resolve(`closed:${ev.code}`) }
      ws.onerror = () => {}
    })
  }, url)
}

await page.goto(`${base}/stage/?token=${token}`, { waitUntil: 'domcontentloaded' })

console.log('/ws no token:', await tryWs('/ws', false))
console.log('/ws with token:', await tryWs('/ws', true))
console.log('/api/karada/ws no token:', await tryWs('/api/karada/ws', false))
console.log('/api/karada/ws with token:', await tryWs('/api/karada/ws', true))

await browser.close()
