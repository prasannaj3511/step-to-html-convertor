/**
 * Verify the in-viewer model switcher: it must load a different model in place,
 * rebuild the tree, and release the previous model's GPU resources.
 *
 *   node test_switch.mjs <url> [--headed]
 */
import { chromium } from 'playwright';

const URL_ = process.argv[2] || 'http://127.0.0.1:8139/index.html';
const HEADED = process.argv.includes('--headed');

const browser = await chromium.launch({
  headless: !HEADED,
  args: HEADED
    ? ['--ignore-gpu-blocklist']
    : ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
});
const page = await browser.newPage({ viewport: { width: 1500, height: 900 } });
const errors = [];
page.on('pageerror', (e) => errors.push(e.message.split('\n')[0]));
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text().slice(0, 160)); });

await page.goto(URL_, { waitUntil: 'load', timeout: 180000 });
await page.waitForFunction(() => document.querySelector('#loader')?.classList.contains('done'), { timeout: 300000 });
await page.waitForTimeout(1200);

const options = await page.evaluate(() =>
  [...document.querySelectorAll('#model-select option')].map((o) => o.textContent)
);
const switcherVisible = await page.isVisible('#model-switch');

const snapshot = () =>
  page.evaluate(() => {
    const s = window.iSTP2HTML.stats();
    return {
      parts: s.parts,
      tris: s.totalTriangles,
      geoms: s.uniqueGeometries,
      title: document.title,
      brand: document.querySelector('#brand-title')?.textContent,
      treeRows: document.querySelectorAll('.trow').length,
      glGeoms: window.iSTP2HTML.renderer.info.memory.geometries,
      glTex: window.iSTP2HTML.renderer.info.memory.textures,
      firstPart: window.iSTP2HTML.state.parts[0]?.name,
    };
  });

const results = [{ step: 'initial', ...(await snapshot()) }];

// Walk through several models via the dropdown.
for (const idx of [2, 6, 1, 0]) {
  await page.selectOption('#model-select', String(idx));
  await page.waitForFunction(
    () => document.querySelector('#loader')?.classList.contains('done'),
    { timeout: 180000 }
  );
  await page.waitForTimeout(1000);
  results.push({ step: `switched to #${idx}`, ...(await snapshot()) });
}

// Interact after switching to be sure the new model is fully wired up.
const afterUse = await page.evaluate(async () => {
  const api = window.iSTP2HTML;
  api.selectPart(api.state.parts[0]);
  await new Promise((r) => setTimeout(r, 300));
  return {
    selected: api.state.selected?.name,
    selInfoOk: document.querySelector('#sel-info')?.innerText.includes('Triangles'),
  };
});

console.log(JSON.stringify({
  url: URL_, switcherVisible, optionCount: options.length, options,
  results, afterUse, errors,
}, null, 2));

await browser.close();
