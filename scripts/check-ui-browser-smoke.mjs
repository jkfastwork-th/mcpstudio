import { chromium } from 'playwright';

const baseURL = process.env.HIRDA_BASE_URL || 'http://127.0.0.1:8100/';
const browser = await chromium.launch({headless:true, executablePath:process.env.CHROMIUM_PATH || '/snap/bin/chromium'});
try {
  const page = await browser.newPage();
  const response = await page.goto(baseURL, {waitUntil:'domcontentloaded', timeout:10000});
  if (!response || !response.ok()) {
    throw new Error(`HIRDA page failed: ${response?.status() ?? 'no response'}`);
  }
  const title = await page.title();
  const buttons = await page.locator('button').count();
  const nav = await page.locator('.primary-nav a[data-view]').count();
  console.log(JSON.stringify({ok:true,title,buttons,nav}));
} finally {
  await browser.close();
}
