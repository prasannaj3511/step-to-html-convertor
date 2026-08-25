/**
 * Exercises every interactive viewer feature and reports whether each one
 * changed the scene the way it should. Complements test_viewer.mjs, which
 * covers loading and frame rate.
 *
 *   node test_features.mjs <url> [--headed]
 */
import { chromium } from 'playwright';

const URL_ = process.argv[2] || 'http://127.0.0.1:8138/index.html';
const HEADED = process.argv.includes('--headed');

const browser = await chromium.launch({
  headless: !HEADED,
  args: HEADED
    ? ['--ignore-gpu-blocklist']
    : ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
});
const page = await browser.newPage({ viewport: { width: 1500, height: 900 } });

const errors = [];
page.on('pageerror', (e) => errors.push(`[pageerror] ${e.message.split('\n')[0]}`));
page.on('console', (m) => { if (m.type() === 'error') errors.push(`[console] ${m.text().slice(0, 200)}`); });
page.on('dialog', (d) => d.accept());   // accept the heavy-model edge warning

await page.goto(URL_, { waitUntil: 'load', timeout: 180000 });
await page.waitForFunction(() => document.querySelector('#loader')?.classList.contains('done'), { timeout: 600000 });
await page.waitForTimeout(1500);

const results = {};
const check = async (name, fn) => {
  const before = errors.length;
  try {
    results[name] = await fn();
  } catch (e) {
    results[name] = `THREW: ${e.message.split('\n')[0]}`;
  }
  if (errors.length > before) results[name] += ` (+${errors.length - before} console errors)`;
};

const stats = () => page.evaluate(() => window.iSTP2HTML.stats());
const visibleCount = () =>
  page.evaluate(() => window.iSTP2HTML.state.parts.filter((p) => p.object.visible).length);

// --- selection -------------------------------------------------------------
await check('select', async () => {
  await page.evaluate(() => {
    const api = window.iSTP2HTML;
    api.selectPart(api.state.parts[Math.min(1, api.state.parts.length - 1)]);
  });
  await page.waitForTimeout(300);
  const name = await page.evaluate(() => window.iSTP2HTML.state.selected?.name);
  const shown = await page.evaluate(() => document.querySelector('#sel-info')?.innerText.includes('Triangles'));
  return `selected=${name} infoPanelPopulated=${shown}`;
});

// --- isolate / hide / show all --------------------------------------------
await check('isolate', async () => {
  const before = await visibleCount();
  await page.click('#btn-isolate');
  await page.waitForTimeout(500);
  const after = await visibleCount();
  return `visible ${before} -> ${after} (expect fewer)`;
});

await check('showAll', async () => {
  await page.click('#btn-showall');
  await page.waitForTimeout(400);
  return `visible=${await visibleCount()}`;
});

await check('hide', async () => {
  await page.evaluate(() => window.iSTP2HTML.selectPart(window.iSTP2HTML.state.parts[0]));
  const before = await visibleCount();
  await page.click('#btn-hide');
  await page.waitForTimeout(300);
  const after = await visibleCount();
  await page.click('#btn-showall');
  return `visible ${before} -> ${after} (expect one fewer)`;
});

// --- filter ----------------------------------------------------------------
await check('filter', async () => {
  const name = await page.evaluate(() => window.iSTP2HTML.state.parts[0].name.slice(0, 4));
  await page.fill('#tree-search', name);
  await page.waitForTimeout(600);
  const matched = await visibleCount();
  await page.fill('#tree-search', '');
  await page.waitForTimeout(500);
  return `query="${name}" matched=${matched} restored=${await visibleCount()}`;
});

// --- explode ---------------------------------------------------------------
await check('explode', async () => {
  // Measure in world space. Local coordinates are meaningless to compare here:
  // gltfpack quantizes positions and compensates with a per-node scale, so a
  // small world offset can read as a huge number in a node's local frame.
  const worldPos = () =>
    page.evaluate(() => {
      const api = window.iSTP2HTML;
      const p = api.state.parts[Math.min(1, api.state.parts.length - 1)];
      const v = new (Object.getPrototypeOf(api.state.center).constructor)();
      p.object.getWorldPosition(v);
      return { pos: v.toArray(), radius: api.state.radius };
    });

  const before = await worldPos();
  const setSlider = async (v) => {
    await page.locator('#opt-explode').evaluate((el, val) => {
      el.value = val;
      el.dispatchEvent(new Event('input', { bubbles: true }));
    }, String(v));
    await page.waitForTimeout(600);
  };

  await setSlider(0.6);
  const moved = await worldPos();
  await setSlider(0);
  const back = await worldPos();

  const d = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
  const shift = d(before.pos, moved.pos);
  return `movedBy=${(shift / before.radius).toFixed(2)}x radius, `
    + `returnedError=${(d(before.pos, back.pos) / before.radius).toExponential(1)}x radius`;
});

// --- section ---------------------------------------------------------------
await check('section', async () => {
  await page.click('#btn-section');
  await page.waitForTimeout(200);
  const full = (await stats()).visibleTriangles;
  await page.locator('#clip-pos').evaluate((el) => {
    el.value = '0.5';
    el.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await page.waitForTimeout(700);
  const planes = await page.evaluate(() => window.iSTP2HTML.renderer.clippingPlanes.length);
  await page.locator('#clip-on').uncheck();
  await page.waitForTimeout(300);
  const off = await page.evaluate(() => window.iSTP2HTML.renderer.clippingPlanes.length);
  await page.click('#btn-section');
  return `planesWhenOn=${planes} planesWhenOff=${off} trisBefore=${full}`;
});

// --- measure ---------------------------------------------------------------
await check('measure', async () => {
  await page.click('#btn-measure');
  await page.waitForTimeout(200);
  const box = await page.locator('#canvas').boundingBox();
  await page.mouse.click(box.x + box.width * 0.45, box.y + box.height * 0.55);
  await page.waitForTimeout(400);
  await page.mouse.click(box.x + box.width * 0.55, box.y + box.height * 0.45);
  await page.waitForTimeout(500);
  const out = await page.evaluate(() => document.querySelector('#measure-out')?.innerText.trim());
  await page.click('#btn-measure');
  return out ? `distance="${out.split('\n')[0]}"` : 'no measurement produced (clicks may have missed geometry)';
});

// --- edges -----------------------------------------------------------------
await check('edges', async () => {
  const t = Date.now();
  await page.click('#btn-edges');
  await page.waitForFunction(() => window.iSTP2HTML.state.edgesBuilt === true, { timeout: 300000 });
  await page.waitForTimeout(500);
  const segs = await page.evaluate(() => window.iSTP2HTML.state.edgeGroup?.children.length ?? 0);
  return `builtIn=${((Date.now() - t) / 1000).toFixed(1)}s lineSegments=${segs}`;
});

// --- theme -----------------------------------------------------------------
await check('theme', async () => {
  // The theme control lives in the Settings panel, which starts closed.
  await page.click('#btn-settings');
  await page.waitForTimeout(250);
  await page.selectOption('#opt-theme', 'light');
  await page.waitForTimeout(400);
  const t = await page.evaluate(() => document.documentElement.dataset.theme);
  const bg = await page.evaluate(() => '#' + window.iSTP2HTML.scene.background.getHexString());
  await page.selectOption('#opt-theme', 'dark');
  await page.waitForTimeout(250);
  const backTo = await page.evaluate(() => document.documentElement.dataset.theme);
  await page.click('#btn-settings');
  return `switchedTo=${t} sceneBg=${bg} restored=${backTo}`;
});

// --- standard views --------------------------------------------------------
await check('views', async () => {
  const seen = [];
  for (const k of ['1', '5', '3', '0']) {
    await page.keyboard.press(k);
    await page.waitForTimeout(500);
    seen.push(await page.evaluate(() => window.iSTP2HTML.camera.position.toArray().map((v) => +v.toFixed(2)).join(',')));
  }
  return `distinctPositions=${new Set(seen).size}/4`;
});

// --- snapshot --------------------------------------------------------------
await check('snapshot', async () => {
  const dl = page.waitForEvent('download', { timeout: 30000 }).catch(() => null);
  await page.click('#btn-snap');
  const d = await dl;
  return d ? `downloaded=${d.suggestedFilename()}` : 'no download event';
});

console.log(JSON.stringify({ url: URL_, results, errors: errors.slice(0, 20) }, null, 2));
await browser.close();
