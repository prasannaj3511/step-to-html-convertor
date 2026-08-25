/**
 * Colour-fidelity check.
 *
 * Reads the material colours three.js ended up with and compares them, channel
 * by channel, against the exact sRGB values written into the source STEP file.
 * Also samples rendered pixels in "flat" display mode, which is the mode that
 * is supposed to reproduce assigned colours literally.
 *
 *   node test_colors.mjs <base-url>
 */
import { chromium } from 'playwright';

const BASE = (process.argv[2] || 'http://127.0.0.1:8139').replace(/\/$/, '');
const HEADED = process.argv.includes('--headed');

// Must match PALETTE in the corpus generator.
const EXPECTED = {
  PureRed: '#ff0000', PureGreen: '#00ff00', PureBlue: '#0000ff',
  Yellow: '#ffff00', Cyan: '#00ffff', Magenta: '#ff00ff',
  MidGrey: '#808080', White: '#ffffff', Orange: '#ff8000', Teal: '#008080',
};

const browser = await chromium.launch({
  headless: !HEADED,
  args: HEADED
    ? ['--ignore-gpu-blocklist']
    : ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
});
const page = await browser.newPage({ viewport: { width: 1400, height: 850 } });
const errors = [];
page.on('pageerror', (e) => errors.push(e.message.split('\n')[0]));

async function load(name) {
  await page.goto(`${BASE}/${name}.html`, { waitUntil: 'load', timeout: 120000 });
  await page.waitForFunction(() => document.querySelector('#loader')?.classList.contains('done'), { timeout: 300000 });
  await page.waitForTimeout(800);
}

const out = {};

// ---------------------------------------------------------- part-level colours
await load('colors_part_level');
out.partLevel = await page.evaluate(() => {
  const res = {};
  for (const p of window.iSTP2HTML.state.parts) {
    const m = Array.isArray(p.meshes[0].material) ? p.meshes[0].material[0] : p.meshes[0].material;
    res[p.name] = '#' + m.color.getHexString();
  }
  return res;
});

// ---------------------------------------------------------- face-level colours
await load('colors_face_level');
out.faceLevel = await page.evaluate(() => {
  const seen = [];
  window.iSTP2HTML.state.root.traverse((o) => {
    if (!o.isMesh) return;
    const mats = Array.isArray(o.material) ? o.material : [o.material];
    for (const m of mats) seen.push('#' + m.color.getHexString());
  });
  return seen;
});

// ------------------------------------------------------ instance-level colours
await load('colors_instance_level');
out.instanceLevel = await page.evaluate(() => {
  const res = [];
  for (const p of window.iSTP2HTML.state.parts) {
    const m = Array.isArray(p.meshes[0].material) ? p.meshes[0].material[0] : p.meshes[0].material;
    res.push({ name: p.name, color: '#' + m.color.getHexString() });
  }
  return res;
});

// --------------------------------------------- rendered pixels in "flat" mode
await load('colors_part_level');
out.renderedFlat = await page.evaluate(async () => {
  const api = window.iSTP2HTML;
  if (!api.setShading) return { unsupported: true };
  api.setShading('flat');
  api.setView('front', false);
  await new Promise((r) => setTimeout(r, 900));

  const cvs = document.querySelector('#canvas');
  const gl = cvs.getContext('webgl2') || cvs.getContext('webgl');
  const w = cvs.width, h = cvs.height;
  const px = new Uint8Array(w * h * 4);
  // preserveDrawingBuffer is off, so the back buffer is undefined once the frame
  // is composited. Render and read in the same task to get real pixels.
  api.renderer.render(api.scene, api.camera);
  gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, px);

  // Histogram of opaque colours, ignoring the background.
  const counts = new Map();
  for (let i = 0; i < px.length; i += 4) {
    const hex = '#' + [px[i], px[i + 1], px[i + 2]]
      .map((v) => v.toString(16).padStart(2, '0')).join('');
    counts.set(hex, (counts.get(hex) || 0) + 1);
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 14)
    .map(([hex, n]) => ({ hex, px: n }));
});

// -------------------------------------------------------------------- verdict
const cmp = {};
let exact = 0, total = 0;
for (const [name, want] of Object.entries(EXPECTED)) {
  const got = out.partLevel[name];
  total++;
  if (got === want) exact++;
  cmp[name] = got === want ? `OK ${want}` : `MISMATCH want=${want} got=${got}`;
}

console.log(JSON.stringify({
  base: BASE,
  materialColourCheck: cmp,
  exactMatches: `${exact}/${total}`,
  faceLevelColours: out.faceLevel,
  instanceLevelColours: out.instanceLevel,
  renderedFlatPixels: out.renderedFlat,
  errors,
}, null, 2));

await browser.close();
