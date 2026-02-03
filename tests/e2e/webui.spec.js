const { test, expect } = require('@playwright/test');

const BASE = process.env.WEBUI_BASE || 'http://127.0.0.1:8000';

test('page loads and exposes config', async ({ page }) => {
  await page.goto(BASE, { waitUntil: 'networkidle' });
  // Check that inline config exists
  const cfg = await page.evaluate(() => window.__SYNTH_CONFIG || null);
  expect(cfg).not.toBeNull();
  expect('RESPONSE_TIMEOUT' in cfg).toBeTruthy();
});

test('loads skins section and calls init if present', async ({ page }) => {
  await page.goto(BASE, { waitUntil: 'networkidle' });
  // Click the skins nav button
  await page.click('.nav-btn[data-tab="skins"]');
  // Wait for panel to be loaded
  await page.waitForSelector('#tab-skins .skins-grid, #tab-skins .card');
  const html = await page.innerHTML('#tab-skins');
  expect(html.length).toBeGreaterThan(10);
});
