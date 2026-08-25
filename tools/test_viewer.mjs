/**
 * Headless/headed smoke + performance test for the generated viewer.
 *
 *   node test_viewer.mjs <url> <screenshot-dir> [--headed]
 *
 * `--headed` runs against the machine's real GPU, which is the only way to get
 * a meaningful frame rate for a large assembly - the headless SwiftShader
 * rasteriser is orders of magnitude slower than any real device.
 */
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';

const URL_ = process.argv[2] || 'http://127.0.0.1:8137/index.html';
const OUT = process.argv[3] || './shots';
const HEADED = process.argv.includes('--headed');

fs.mkdirSync(OUT, { recursive: true });

const swArgs = ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'];
const gpuArgs = ['--ignore-gpu-blocklist', '--enable-gpu-rasterization', '--enable-zero-copy'];

const browser = await chromium.launch({
  headless: !HEADED,
  args: [...(HEADED ? gpuArgs : swArgs), '--disable-dev-shm-usage'],
});
const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });

const logs = [];
page.on('console', (m) => logs.push(`[${m.type()}] ${m.text()}`));
page.on('pageerror', (e) => logs.push(`[pageerror] ${e.message}`));
page.on('requestfailed', (r) => logs.push(`[reqfail] ${r.url()} :: ${r.failure()?.errorText}`));
page.on('crash', () => logs.push('[crash] renderer process crashed'));
page.on('close', () => logs.push('[close] page closed'));

// Results are printed even if a later step takes the browser down, so a crash
// in the section test does not throw away the load and frame-rate numbers.
const result = { url: URL_, headed: HEADED, logs };
let printed = false;
const report = () => {
  if (printed) return;
  printed = true;
  result.logs = logs.slice(0, 40);
  console.log(JSON.stringify(result, null, 2));
};
process.on('exit', report);

const shot = async (name) => {
  try {
    await page.screenshot({ path: path.join(OUT, name), timeout: 120000 });
  } catch (e) {
    logs.push(`[shot-fail] ${name}: ${e.message}`);
  }
};

const step = async (name, fn, fallback = null) => {
  try {
    return await fn();
  } catch (e) {
    logs.push(`[step-fail] ${name}: ${e.message.split('\n')[0]}`);
    return fallback;
  }
};

const t0 = Date.now();
await page.goto(URL_, { waitUntil: 'load', timeout: 180000 });

let ready = false;
try {
  await page.waitForFunction(
    () => {
      const l = document.querySelector('#loader');
      return !l || l.classList.contains('done') || l.classList.contains('error');
    },
    { timeout: 600000 }
  );
  ready = true;
} catch {
  logs.push('[timeout] loader never finished');
}
const loadMs = Date.now() - t0;
const errored = await page.evaluate(() => !!document.querySelector('#loader.error'));
Object.assign(result, { ready, errored, loadMs });

await page.waitForTimeout(2500);

const info = await page.evaluate(() => {
  const c = document.querySelector('#canvas');
  const gl = c && (c.getContext('webgl2') || c.getContext('webgl'));
  let renderer = 'n/a';
  if (gl) {
    const d = gl.getExtension('WEBGL_debug_renderer_info');
    renderer = d ? gl.getParameter(d.UNMASKED_RENDERER_WEBGL) : 'masked';
  }
  return {
    hud: document.querySelector('#hud')?.innerText,
    canvas: c ? `${c.width}x${c.height}` : 'none',
    treeRows: document.querySelectorAll('.trow').length,
    glRenderer: renderer,
    loaderSub: document.querySelector('#loader .sub')?.textContent,
    title: document.title,
  };
});

result.info = info;
await shot('01-initial.png');

await step('info-panel', async () => {
  await page.click('#btn-info');
  await page.waitForTimeout(400);
});
await shot('02-info.png');

await step('select', async () => {
  const box = await page.locator('#canvas').boundingBox();
  if (!box) return;
  const t = Date.now();
  await page.mouse.click(box.x + box.width * 0.5, box.y + box.height * 0.45);
  await page.waitForTimeout(900);
  result.pickMs = Date.now() - t - 900;
});
await shot('03-selected.png');
result.sel = (
  await step('sel-text', () => page.evaluate(() => document.querySelector('#sel-info')?.innerText), '')
)?.slice(0, 300);

// Orbit and count real presented frames.
const orbit = await step('orbit', () => page.evaluate(async () => {
  const cvs = document.querySelector('#canvas');
  const r = cvs.getBoundingClientRect();
  const cx = r.left + r.width / 2;
  const cy = r.top + r.height / 2;
  const ev = (type, x, y, buttons) =>
    cvs.dispatchEvent(
      new PointerEvent(type, {
        clientX: x, clientY: y, buttons, button: 0,
        bubbles: true, pointerId: 1, isPrimary: true, pointerType: 'mouse',
      })
    );

  let frames = 0;
  let stop = false;
  const count = () => { frames++; if (!stop) requestAnimationFrame(count); };
  requestAnimationFrame(count);

  ev('pointerdown', cx, cy, 1);
  const t = performance.now();
  for (let i = 0; i < 90; i++) {
    ev('pointermove', cx + Math.sin(i / 7) * 300, cy + Math.cos(i / 11) * 110, 1);
    await new Promise((res) => setTimeout(res, 16));
  }
  const dt = performance.now() - t;
  ev('pointerup', cx, cy, 0);
  stop = true;
  return { frames, ms: Math.round(dt), fps: +(frames / (dt / 1000)).toFixed(1) };
}));

result.orbit = orbit;
await shot('04-orbited.png');

// Idle cost: with render-on-demand the loop should coast without drawing.
result.idle = await step('idle', () => page.evaluate(async () => {
  const api = window.iSTP2HTML;
  // Orbit damping keeps drawing for a beat after the drag ends - that is
  // correct behaviour, so wait it out before sampling the truly-idle cost.
  await new Promise((r) => setTimeout(r, 3000));
  const before = api.stats().renderCount;
  const t = performance.now();
  let frames = 0;
  let stop = false;
  const count = () => { frames++; if (!stop) requestAnimationFrame(count); };
  requestAnimationFrame(count);
  await new Promise((r) => setTimeout(r, 2500));
  stop = true;
  const secs = (performance.now() - t) / 1000;
  return {
    rafPerSec: +(frames / secs).toFixed(1),
    rendersWhileIdle: api.stats().renderCount - before,
    hud: document.querySelector('#hud')?.innerText.split('\n')[0],
  };
}));

await step('section', async () => {
  await page.click('#btn-section');
  await page.waitForTimeout(250);
  await page.locator('#clip-pos').evaluate((el) => {
    el.value = '0.55';
    el.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await page.waitForTimeout(1500);
});
await shot('05-section.png');

await step('reset-view', async () => {
  await page.locator('#clip-on').uncheck();
  await page.click('#btn-section');
  await page.keyboard.press('0');
  await page.waitForTimeout(1500);
});
await shot('06-iso.png');

report();
await browser.close().catch(() => {});
