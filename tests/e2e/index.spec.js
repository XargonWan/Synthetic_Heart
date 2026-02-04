const { test, expect } = require('@playwright/test');

const BASE = process.env.WEBUI_BASE || 'http://127.0.0.1:8080';

test('index title contains brand name', async ({ page }) => {
  await page.goto(BASE, { waitUntil: 'networkidle' });
  await expect(page).toHaveTitle(/Synthetic Heart/);
});
