// Renders assets/*.svg to 2x PNGs with a transparent background, for READMEs: GitHub on mobile does not show the
// SVGs. Needs Playwright with Chromium (npm i -g playwright && npx playwright install chromium).
// Run after tools/make_assets.py: node tools/render_pngs.mjs
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assets = join(dirname(fileURLToPath(import.meta.url)), '..', 'assets');

const browser = await chromium.launch();
const page = await browser.newPage({ deviceScaleFactor: 2 });
for (const name of readdirSync(assets).filter((f) => f.endsWith('.svg'))) {
  await page.setContent(`<body style="margin:0;background:transparent">${readFileSync(join(assets, name), 'utf8')}</body>`);
  await page.locator('svg').screenshot({ path: join(assets, name.replace(/\.svg$/, '.png')), omitBackground: true });
  console.log(name.replace(/\.svg$/, '.png'));
}
await browser.close();
