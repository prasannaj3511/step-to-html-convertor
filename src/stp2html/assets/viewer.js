/**
 * iSTP2HTML viewer
 *
 * Renders the glTF produced by the Python converter.  Three things matter for
 * response time on large assemblies and they drive most of the design here:
 *
 *  1. Render on demand.  A CAD viewer is idle most of the time; drawing 5,000
 *     meshes at 60 Hz while nothing moves is pure waste.  We only draw when
 *     something actually changed.
 *  2. Adaptive resolution.  While the user is dragging, pixel ratio drops; it
 *     snaps back the moment they stop, so motion stays smooth without giving up
 *     final image sharpness.
 *  3. Shared geometry.  The exporter emits one mesh per unique part referenced
 *     by many nodes, and GLTFLoader preserves that sharing, so GPU memory tracks
 *     unique geometry rather than placement count.
 */

import * as THREE from 'three';
import { TrackballControls } from 'three/addons/controls/TrackballControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { MeshoptDecoder } from 'three/addons/libs/meshopt_decoder.module.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

const CFG = window.__ISTP2HTML__ || {};
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const stageEl = $('#stage') || $('#viewer');

/* ------------------------------------------------------------------ state */

const state = {
  parts: [],              // { id, name, object, meshes[], tris, color, visible }
  partByObject: new Map(),
  root: null,
  bounds: new THREE.Box3(),
  center: new THREE.Vector3(),
  radius: 1,
  selected: null,
  isolated: false,
  explode: 0,
  needsRender: true,
  interacting: false,
  interactTimer: 0,
  dprFull: Math.min(window.devicePixelRatio || 1, 2),
  dprLow: Math.min(window.devicePixelRatio || 1, 2) * 0.6,
  edgesBuilt: false,
  edgeGroup: null,
  measure: { active: false, points: [], objects: [] },
  clip: { enabled: false, axis: 'x', pos: 1, flip: false, plane: null },
  stats: { tris: 0, meshes: 0, geoms: 0, materials: 0 },
  frameTimes: [],
  lastFrame: performance.now(),
  lastRenderAt: 0,
  lastCalls: 0,
  lastTris: 0,
  renderCount: 0,
  shading: CFG.shading === 'shaded-wire' ? 'shaded' : (CFG.shading || 'shaded'),
  shadedWire: CFG.shading === 'shaded-wire',
  machineTransparent: false,
  // --- bottom dock, mirroring the portal's GLB viewer toolbar ---
  navMode: 'rotate',        // gesture the LEFT mouse button performs
  // Per-part transparency accumulates: making one part see-through leaves the
  // previous ones see-through, so several can be compared at once.
  transparentParts: new Set(),
  explodeEnabled: false,    // the dock toggle; the amount below survives turning it off
  explodeValue: 0.5,
  selectVisibleActive: false,
  visibleSelection: [],     // parts highlighted by "Select Visible", kept apart from
                            // `selected` so isolate/part-transparency still mean one part
  zoomedToSelection: false,
  sidebarOpen: true,
  // Isolate is a MODE, not a one-shot: it follows the selection, and the backup
  // is what "everything was visible like this before isolating" looked like, so
  // leaving isolate does not silently un-hide parts the user hid by hand.
  isolateBackup: null,
  tone: CFG.tone || 'neutral',
  modelIndex: CFG.modelIndex || 0,
  homeView: null,
};

/* --------------------------------------------------------------- renderer */

const canvas = $('#canvas') || $('#glcanvas');
const renderer = new THREE.WebGLRenderer({
  canvas,
  antialias: true,
  alpha: false,
  powerPreference: 'high-performance',
  stencil: false,
  logarithmicDepthBuffer: false,
});
renderer.setPixelRatio(state.dprFull);
renderer.outputColorSpace = THREE.SRGBColorSpace;
// Default to the Khronos PBR Neutral curve rather than ACES Filmic. ACES looks
// cinematic but visibly desaturates and warms saturated colours, so a part the
// designer painted pure red renders noticeably off. Neutral keeps assigned
// colours close to their nominal value while still taming highlights.
renderer.toneMapping = THREE.NeutralToneMapping;
renderer.toneMappingExposure = 1.0;
renderer.localClippingEnabled = true;
renderer.info.autoReset = false;

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 10000);

// TrackballControls (not OrbitControls): it doesn't pin the camera's "up" to
// world Y, so dragging can tumble the model over the poles and roll it about
// the view axis - free rotation in every direction, not just azimuth/elevation.
const controls = new TrackballControls(camera, canvas);
// The damping factor and the speed values only make sense read together.
// While a gesture's start point chases its end point, TrackballControls
// re-applies the remaining delta every frame, so a pan drag travels an
// effective panSpeed/damping and a wheel notch compounds by zoomSpeed/damping
// - but rotation uses damping only for the after-release coast, which decays
// by sqrt(1 - damping) per frame. That coupling is how the shipped 0.12/0.9
// pair panned at 7.5x ("too sensitive") while rotation swung for over a
// second after mouseup; it is also why staticMoving=true was the wrong cure -
// it fixed the swing but collapsed pan to a flat 0.3x and reduced wheel zoom
// to single weak steps. Strong damping keeps the smoothing and kills the
// swing: at 0.4 the coast sheds to ~5% inside twelve frames (~200ms).
controls.staticMoving = false;
controls.dynamicDampingFactor = 0.4;
controls.rotateSpeed = 1.2;
// Effective gains at 0.4 damping: pan 0.7/0.4 = 1.75x of the base distance
// scale (a quarter of the old 7.5x), zoom exponent 2.0/0.4 = 5 (was 8.3).
controls.zoomSpeed = 2.0;
controls.panSpeed = 0.7;
controls.mouseButtons = {
  LEFT: THREE.MOUSE.ROTATE,
  MIDDLE: THREE.MOUSE.DOLLY,
  RIGHT: THREE.MOUSE.PAN,
};

/* --------------------------------------------------------------- lighting */

let envRT = null;

function setupEnvironment() {
  const pmrem = new THREE.PMREMGenerator(renderer);
  pmrem.compileEquirectangularShader();
  envRT = pmrem.fromScene(new RoomEnvironment(), 0.04);
  scene.environment = envRT.texture;
  pmrem.dispose();
}

// A soft key/fill/rim trio on top of the room IBL. Directional lights give the
// crisp shading that makes machined edges readable; the IBL alone looks flat.
const keyLight = new THREE.DirectionalLight(0xffffff, 1.7);
const fillLight = new THREE.DirectionalLight(0xffffff, 0.6);
const rimLight = new THREE.DirectionalLight(0xffffff, 0.5);
const ambient = new THREE.AmbientLight(0xffffff, 0.25);
scene.add(keyLight, fillLight, rimLight, ambient);

const THEMES = {
  dark: { bg: 0x14171c, grid1: 0x3a4250, grid2: 0x252b34 },
  light: { bg: 0xffffff, grid1: 0xc7d1dd, grid2: 0xe4e9f0 },
};

function applyTheme(name) {
  const t = THEMES[name] || THEMES.dark;
  document.documentElement.dataset.theme = name;
  scene.background = new THREE.Color(t.bg);
  const themeSelect = $('#opt-theme');
  if (themeSelect) themeSelect.value = name;
  if (grid) {
    grid.material.color.setHex(t.grid1);
    grid.material.opacity = name === 'light' ? 0.5 : 0.35;
  }
  requestRender();
}

let grid = null;

/* ----------------------------------------------------------------- helpers */

function requestRender() { state.needsRender = true; }

function toast(msg, ms = 1700) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove('show'), ms);
}

function fmtInt(n) { return (n | 0).toLocaleString(); }

/**
 * Scene units are metres (glTF's convention, which the exporter converts to);
 * CFG.unitScale takes them back to whatever unit the CAD file was authored in.
 */
function fmtLen(sceneUnits) {
  const u = CFG.units || 'mm';
  const v = sceneUnits * (CFG.unitScale || 1000);
  const a = Math.abs(v);
  if (u === 'mm' && a >= 1000) return (v / 1000).toFixed(3) + ' m';
  if (a >= 100) return v.toFixed(1) + ' ' + u;
  if (a >= 1) return v.toFixed(2) + ' ' + u;
  if (a >= 0.01) return v.toFixed(3) + ' ' + u;
  return v.toExponential(2) + ' ' + u;
}

function fmtBytes(n) {
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + ' ' + units[i];
}

/* ------------------------------------------------------------------ loader */

const loaderEl = $('#loader');
const loaderBar = $('#loader .bar > i');
const loaderSub = $('#loader .sub');
const loaderTitle = $('#loader .title');

function setProgress(pct, text) {
  loaderBar.style.width = Math.max(0, Math.min(100, pct)) + '%';
  if (text) loaderSub.textContent = text;
}

function failLoad(err) {
  loaderEl.classList.add('error');
  loaderTitle.textContent = 'Could not load the model';
  loaderSub.textContent = String(err && err.message ? err.message : err);
  $('#loader .hint').classList.remove('hidden');
  console.error(err);
}

const gltfLoader = new GLTFLoader();
gltfLoader.setMeshoptDecoder(MeshoptDecoder);

function showLoader(title) {
  loaderEl.classList.remove('done', 'error');
  loaderEl.style.display = '';
  $('#loader .hint').classList.add('hidden');
  if (title) loaderTitle.textContent = title;
  setProgress(0, 'starting…');
}

function hideLoader() {
  loaderEl.classList.add('done');
  // Kept in the DOM rather than removed: switching models reuses it.
  setTimeout(() => {
    if (loaderEl.classList.contains('done')) loaderEl.style.display = 'none';
  }, 400);
}

async function loadModel(url = CFG.model) {
  setProgress(2, 'requesting model');

  const gltf = await new Promise((resolve, reject) => {
    gltfLoader.load(
      url,
      resolve,
      (evt) => {
        if (evt.lengthComputable && evt.total) {
          const pct = (evt.loaded / evt.total) * 88;
          setProgress(2 + pct, `${fmtBytes(evt.loaded)} / ${fmtBytes(evt.total)}`);
        } else {
          setProgress(2 + Math.min(80, evt.loaded / 400000), fmtBytes(evt.loaded) + ' downloaded');
        }
      },
      reject
    );
  });

  setProgress(92, 'preparing scene');
  await new Promise((r) => requestAnimationFrame(r));
  buildScene(gltf.scene);
  setProgress(100, 'ready');
  hideLoader();
}

/**
 * Release everything the previous model owned.
 *
 * WebGL resources are not garbage collected with their JS wrappers, so skipping
 * this would leak a model's worth of VRAM on every switch.
 */
function disposeModel() {
  if (!state.root) return;

  const geometries = new Set();
  const materials = new Set();
  state.root.traverse((o) => {
    if (o.geometry) geometries.add(o.geometry);
    const mats = Array.isArray(o.material) ? o.material : [o.material];
    for (const m of mats) if (m) materials.add(m);
  });

  scene.remove(state.root);
  if (state.edgeGroup) {
    scene.remove(state.edgeGroup);
    const seen = new Set();
    state.edgeGroup.traverse((o) => {
      if (o.geometry && !seen.has(o.geometry)) { seen.add(o.geometry); o.geometry.dispose(); }
    });
    state.edgeGroup.children.forEach((c) => c.material?.dispose?.());
  }

  for (const g of geometries) g.dispose();
  for (const m of materials) {
    for (const key of Object.keys(m)) {
      const v = m[key];
      if (v && v.isTexture) v.dispose();
    }
    m.dispose();
  }
  for (const m of flatCache.values()) m.dispose();
  flatCache.clear();

  clearMeasure();
  state.root = null;
  state.parts = [];
  state.partByObject.clear();
  state.selected = null;
  state.isolated = false;
  state.edgesBuilt = false;
  state.edgeGroup = null;
  state.explode = 0;
  state.machineTransparent = false;
  state.frameTimes = [];
  // Everything below describes the model being torn down, so it cannot survive
  // into the next one. Part transparency lives on the shared HIGHLIGHT material
  // rather than on a mesh, so it needs clearing explicitly or it leaks across
  // model switches.
  state.transparentParts.clear();
  state.zoomedToSelection = false;
  state.selectVisibleActive = false;
  state.visibleSelection = [];
  state.isolateBackup = null;
  $('#btn-edges')?.classList.remove('active');
  syncDockState();
}

async function switchModel(index) {
  const list = CFG.models || [];
  const entry = list[index];
  if (!entry || index === state.modelIndex) return;

  showLoader(entry.title || entry.name);
  await new Promise((r) => requestAnimationFrame(r));

  // A single-file page carries its model inline and cannot fetch a sibling
  // .glb, so there the switcher navigates to the other page instead.
  if (!entry.model) {
    window.location.href = entry.page;
    return;
  }

  disposeModel();
  state.modelIndex = index;
  CFG.meta = entry.meta || CFG.meta;
  CFG.units = entry.units || CFG.units;
  CFG.unitScale = entry.unitScale || CFG.unitScale;

  document.title = entry.title || entry.name;
  const brandTitle = $('#brand-title');
  const brandSub = $('#brand-sub');
  const treeSearch = $('#tree-search');
  if (brandTitle) brandTitle.textContent = entry.title || entry.name;
  if (brandSub) brandSub.innerHTML = entry.subtitle || '';
  if (treeSearch) treeSearch.value = '';

  try {
    await loadModel(entry.model);
    // Shading is a viewer preference, so carry it across the switch.
    if (state.shading === 'flat') setShading('flat');
    if (state.shadedWire) await setShadedWire(true);
    if (state.machineTransparent) applyMachineTransparency(true);
    toast(`Loaded ${entry.title || entry.name}`);
  } catch (err) {
    failLoad(err);
  }
}

/* ------------------------------------------------------- scene preparation */

const MAT_DEFAULTS = { metalness: 0.12, roughness: 0.52 };

const TONE_MAPPINGS = {
  none: THREE.NoToneMapping,
  neutral: THREE.NeutralToneMapping,
  aces: THREE.ACESFilmicToneMapping,
};

const flatCache = new Map();

/** Unlit twin of a shaded material, so the assigned colour renders literally. */
function flatVariant(mat) {
  let flat = flatCache.get(mat);
  if (!flat) {
    flat = new THREE.MeshBasicMaterial({
      color: mat.color ? mat.color.clone() : new THREE.Color(0xb7bcc4),
      side: mat.side,
      transparent: mat.transparent,
      opacity: mat.opacity,
    });
    flatCache.set(mat, flat);
  }
  return flat;
}

/**
 * Switch between physically-shaded and flat display.
 *
 * "Flat" exists because shading is not colour-neutral: lighting, metalness and
 * any tone curve all move the pixel away from the RGB the designer assigned in
 * CAD. In flat mode the surface is unlit and the tone curve is off, so what you
 * see is exactly the authored colour - which is what you want when checking a
 * model against a part spec.
 */
function setShading(mode) {
  clearSelection();
  state.shading = mode;
  state.root.traverse((o) => {
    if (!o.isMesh) return;
    if (!o.userData._shaded) o.userData._shaded = o.material;
    const src = o.userData._shaded;
    o.material = mode === 'flat'
      ? (Array.isArray(src) ? src.map(flatVariant) : flatVariant(src))
      : src;
  });
  renderer.toneMapping = mode === 'flat'
    ? THREE.NoToneMapping
    : (TONE_MAPPINGS[state.tone] ?? THREE.NeutralToneMapping);
  if (state.machineTransparent) applyMachineTransparency(true);
  forEachMaterial((m) => { m.needsUpdate = true; });
  updateSelectionUI();
  requestRender();
}

function syncShadedWireUI() {
  const opt = $('#opt-shaded-wire');
  if (opt) opt.checked = !!state.shadedWire;
}

async function setShadedWire(enabled) {
  if (enabled && state.shading !== 'shaded') {
    const shadingSelect = $('#opt-shading');
    if (shadingSelect) shadingSelect.value = 'shaded';
    setShading('shaded');
  }
  state.shadedWire = enabled;
  if (enabled) {
    if (!state.edgesBuilt) await buildEdges();
    if (state.edgeGroup) state.edgeGroup.visible = true;
  } else if (state.edgeGroup) {
    state.edgeGroup.visible = false;
  }
  $('#btn-edges')?.classList.toggle('active', !!state.edgeGroup?.visible);
  syncShadedWireUI();
  requestRender();
}

function setToneMapping(name) {
  state.tone = name;
  if (state.shading !== 'flat') {
    renderer.toneMapping = TONE_MAPPINGS[name] ?? THREE.NeutralToneMapping;
    forEachMaterial((m) => { m.needsUpdate = true; });
  }
  requestRender();
}

function applyMachineTransparency(enabled) {
  if (!state.root) return;
  clearSelection();
  state.root.traverse((o) => {
    if (!o.isMesh) return;
    const mats = Array.isArray(o.material) ? o.material : [o.material];
    for (const m of mats) {
      if (!m) continue;
      if (!m.userData._machineSurface) {
        m.userData._machineSurface = {
          transparent: !!m.transparent,
          opacity: typeof m.opacity === 'number' ? m.opacity : 1,
          depthWrite: 'depthWrite' in m ? !!m.depthWrite : true,
        };
      }
      const base = m.userData._machineSurface;
      m.transparent = enabled ? true : base.transparent;
      m.opacity = enabled ? 0.2 : base.opacity;
      if ('depthWrite' in m) m.depthWrite = enabled ? false : base.depthWrite;
      m.needsUpdate = true;
    }
  });
  state.machineTransparent = enabled;
  syncDockState();
  updateSelectionUI();
  requestRender();
}

function toggleMachineTransparency() {
  applyMachineTransparency(!state.machineTransparent);
  toast(state.machineTransparent ? 'Machine transparent' : 'Machine view restored');
}

/*
 * Per-part transparency.
 *
 * The materials that arrive from glTF are shared between parts, so flipping the
 * flags in place would turn every part that happens to use the same material
 * see-through at once. Each material therefore gets ONE transparent twin, cached
 * on the original and reused, and a part is made transparent by swapping which
 * of the pair its meshes wear. That is what lets several parts be transparent
 * simultaneously without any of them interfering.
 */
function solidOf(mat) {
  return (mat && mat.userData && mat.userData._solidSource) || mat;
}

function transparentVariant(mat) {
  if (!mat) return mat;
  const solid = solidOf(mat);
  if (solid.userData._transparentTwin) return solid.userData._transparentTwin;
  const twin = solid.clone();
  twin.transparent = true;
  twin.opacity = PART_TRANSPARENT_OPACITY;
  twin.depthWrite = false;
  twin.userData = { ...twin.userData, _solidSource: solid };
  solid.userData._transparentTwin = twin;
  return twin;
}

function isPartTransparent(part) {
  return !!part && state.transparentParts.has(part);
}

function setPartTransparency(part, enabled) {
  if (!part) return;
  if (enabled) state.transparentParts.add(part);
  else state.transparentParts.delete(part);

  const selected = state.selected === part;
  for (const m of part.meshes) {
    if (selected) {
      // Wearing a highlight: swap which highlight, and rewrite the stash so the
      // choice survives being deselected later.
      m.material = enabled ? HIGHLIGHT_TRANSPARENT : HIGHLIGHT;
      if (m.userData._mat) {
        m.userData._mat = enabled ? transparentVariant(m.userData._mat) : solidOf(m.userData._mat);
      }
    } else {
      m.material = enabled ? transparentVariant(m.material) : solidOf(m.material);
    }
  }
  syncDockState();
  requestRender();
}

function togglePartTransparency() {
  const part = state.selected;
  if (!part) { toast('Select a part first'); return; }
  const next = !isPartTransparent(part);
  setPartTransparency(part, next);
  const total = state.transparentParts.size;
  toast(next
    ? `${part.name} transparent (${total} part${total === 1 ? '' : 's'})`
    : `${part.name} solid`);
}

/** Used when a model is torn down, and by "Show all" as a full reset. */
function clearAllPartTransparency() {
  for (const part of [...state.transparentParts]) setPartTransparency(part, false);
  state.transparentParts.clear();
}

/**
 * Names three.js invents for glTF entities that carry none of their own:
 * `mesh_4`, `mesh_4_instance_2`, and `mesh_4_1` for the second primitive of a
 * split multi-material mesh.  The exporter deliberately leaves glTF meshes
 * unnamed so the *node* name (the real CAD part name) wins, which means any name
 * of this shape is generated plumbing rather than something to show the user.
 *
 * Getting the multi-primitive form wrong is what turns one per-face-coloured
 * part into six bogus entries in the model tree.
 */
const AUTO_NAME_RE = /^(?:mesh|node|primitive)_\d+(?:_\d+)*(?:_instance_\d+)?$/;

function isMeaningfulName(name) {
  return !!name && name.trim().length > 0 && !AUTO_NAME_RE.test(name);
}

/**
 * gltfpack keeps named nodes but re-parents the actual mesh onto an unnamed
 * child, so "the part the user clicked" is the nearest meaningfully-named
 * ancestor of the mesh, not the mesh's own parent.
 */
function nearestNamedAncestor(obj, stopAt) {
  let cur = obj;
  while (cur && cur !== stopAt) {
    if (isMeaningfulName(cur.name)) return cur;
    cur = cur.parent;
  }
  return stopAt;
}

function buildScene(gltfRoot) {
  state.root = gltfRoot;

  const materials = new Set();
  const geometries = new Set();
  let tris = 0;
  let meshCount = 0;

  gltfRoot.traverse((o) => {
    if (!o.isMesh) return;
    meshCount++;
    geometries.add(o.geometry);

    const g = o.geometry;
    const count = g.index ? g.index.count : g.attributes.position.count;
    tris += count / 3;

    // OCCT writes only baseColorFactor, which leaves metallic at the glTF
    // default of 1.0 - every part would render as polished chrome. Nudge the
    // whole model to a plausible machined-surface response instead.
    const mats = Array.isArray(o.material) ? o.material : [o.material];
    for (const m of mats) {
      if (!m || materials.has(m)) continue;
      materials.add(m);
      if (m.isMeshStandardMaterial) {
        m.metalness = MAT_DEFAULTS.metalness;
        m.roughness = MAT_DEFAULTS.roughness;
      }
      // STEP/glTF exports can carry a reduced alpha that makes the whole
      // machine look washed out against a light background. Keep the viewer
      // solid by default unless a feature explicitly changes visibility.
      m.transparent = false;
      m.opacity = 1.0;
      if ('alphaTest' in m) m.alphaTest = 0;
      if ('depthWrite' in m) m.depthWrite = true;
      m.side = THREE.FrontSide;
      m.shadowSide = THREE.FrontSide;
      m.needsUpdate = true;
    }

    o.castShadow = false;
    o.receiveShadow = false;
    o.frustumCulled = true;

    if (!g.attributes.normal) g.computeVertexNormals();
    if (!g.boundingSphere) g.computeBoundingSphere();
  });

  state.stats = {
    tris: Math.round(tris),
    meshes: meshCount,
    geoms: geometries.size,
    materials: materials.size,
  };

  scene.add(gltfRoot);
  collectParts(gltfRoot);
  frameBounds();
  buildGrid();
  buildTreeUI();
  updateHud();
  updateModelInfo();
  requestRender();
}

function collectParts(root) {
  const byNode = new Map();

  root.traverse((o) => {
    if (!o.isMesh) return;
    const owner = nearestNamedAncestor(o, root);
    if (!byNode.has(owner)) byNode.set(owner, []);
    byNode.get(owner).push(o);
  });

  let id = 0;
  for (const [owner, meshes] of byNode) {
    let t = 0;
    for (const m of meshes) {
      const g = m.geometry;
      t += (g.index ? g.index.count : g.attributes.position.count) / 3;
    }
    const mat = Array.isArray(meshes[0].material) ? meshes[0].material[0] : meshes[0].material;
    const part = {
      id: id++,
      name: owner.name || `Part ${id}`,
      object: owner,
      meshes,
      tris: Math.round(t),
      color: mat && mat.color ? '#' + mat.color.getHexString() : '#888888',
      visible: true,
      home: owner.position.clone(),
    };
    state.parts.push(part);
    state.partByObject.set(owner, part);
    for (const m of meshes) state.partByObject.set(m, part);
  }
}

function frameBounds() {
  state.bounds.setFromObject(state.root);
  state.bounds.getCenter(state.center);
  const size = state.bounds.getSize(new THREE.Vector3());
  state.radius = Math.max(size.length() / 2, 1e-4);

  const r = state.radius;
  camera.near = Math.max(r / 5000, 1e-4);
  camera.far = r * 60;
  camera.updateProjectionMatrix();

  controls.minDistance = r * 0.005;
  controls.maxDistance = r * 40;

  const d = r * 3.2;
  keyLight.position.set(d, d * 1.2, d * 0.9);
  fillLight.position.set(-d, d * 0.4, -d * 0.6);
  rimLight.position.set(0, -d * 0.8, d * 0.5);
  for (const l of [keyLight, fillLight, rimLight]) l.target.position.copy(state.center);
  scene.add(keyLight.target, fillLight.target, rimLight.target);

  setView('iso', false);
  state.homeView = {
    position: camera.position.clone(),
    target: controls.target.clone(),
    up: camera.up.clone(),
  };
}

function buildGrid() {
  if (grid) { scene.remove(grid); grid.geometry.dispose(); grid.material.dispose(); }
  const size = state.radius * 6;
  // Round the division count to something that yields a human-friendly spacing.
  const step = Math.pow(10, Math.round(Math.log10(size / 20)));
  const divisions = Math.max(4, Math.min(200, Math.round(size / step)));
  grid = new THREE.GridHelper(size, divisions, 0x3a4250, 0x252b34);
  grid.material.transparent = true;
  grid.material.opacity = 0.35;
  grid.material.depthWrite = false;
  grid.position.set(state.center.x, state.bounds.min.y, state.center.z);
  grid.visible = CFG.showGrid !== false;
  scene.add(grid);
}

/* -------------------------------------------------------------- view setup */

const VIEW_DIRS = {
  iso: [1, 0.8, 1],
  front: [0, 0, 1],
  back: [0, 0, -1],
  left: [-1, 0, 0],
  right: [1, 0, 0],
  top: [0, 1, 0],
  bottom: [0, -1, 0],
};

const _corner = new THREE.Vector3();
const _right = new THREE.Vector3();
const _up = new THREE.Vector3();
const WORLD_UP = new THREE.Vector3(0, 1, 0);

/**
 * Exact camera distance needed to fit `box` when viewed along `dir`.
 *
 * Fitting the bounding *sphere* is the usual shortcut, but it wastes a lot of
 * frame on anything that isn't roughly cubic - a tall machine ends up as a small
 * object floating in empty space. Instead, project all eight corners onto the
 * camera basis and solve for the distance that brings the worst one just inside
 * both the horizontal and vertical frustum planes.
 */
function distanceToFit(box, dir, margin = 1.06) {
  const center = box.getCenter(new THREE.Vector3());
  const vTan = Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2);
  const hTan = vTan * Math.max(camera.aspect, 1e-3);

  _right.crossVectors(dir, WORLD_UP);
  if (_right.lengthSq() < 1e-8) _right.set(1, 0, 0); // looking straight up/down
  _right.normalize();
  _up.crossVectors(_right, dir).normalize();

  const min = box.min, max = box.max;
  let dist = 0;
  for (let i = 0; i < 8; i++) {
    _corner.set(
      i & 1 ? max.x : min.x,
      i & 2 ? max.y : min.y,
      i & 4 ? max.z : min.z
    ).sub(center);
    const depth = _corner.dot(dir);           // toward the camera
    const x = Math.abs(_corner.dot(_right));
    const y = Math.abs(_corner.dot(_up));
    dist = Math.max(dist, depth + x / hTan, depth + y / vTan);
  }
  const radius = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 1e-6);
  return Math.max(dist * margin, radius * 0.01);
}

function setView(name, animate = true) {
  // Preset views are canonical - snap the up vector back to world Y so a prior
  // free-roll drag (TrackballControls allows rolling, unlike OrbitControls)
  // doesn't leave "Front"/"Top"/etc. looking tilted.
  camera.up.copy(WORLD_UP);
  const dir = new THREE.Vector3(...(VIEW_DIRS[name] || VIEW_DIRS.iso)).normalize();
  const target = state.center.clone();
  const pos = target.clone().addScaledVector(dir, distanceToFit(state.bounds, dir));
  moveCamera(pos, target, animate);
}

function fitTo(box, animate = true) {
  if (!box || box.isEmpty()) return;
  camera.up.copy(WORLD_UP);
  const center = box.getCenter(new THREE.Vector3());
  let dir = camera.position.clone().sub(controls.target);
  if (dir.lengthSq() < 1e-12) dir.set(1, 0.8, 1);
  dir.normalize();
  moveCamera(center.clone().addScaledVector(dir, distanceToFit(box, dir)), center, animate);
}

let camAnim = null;

function moveCamera(pos, target, animate = true) {
  if (!animate) {
    camera.position.copy(pos);
    controls.target.copy(target);
    controls.update();
    requestRender();
    return;
  }
  camAnim = {
    t0: performance.now(),
    dur: 380,
    fromPos: camera.position.clone(),
    toPos: pos.clone(),
    fromTgt: controls.target.clone(),
    toTgt: target.clone(),
  };
  requestRender();
}

function goHome(animate = true) {
  if (!state.homeView) {
    fitTo(state.bounds, animate);
    return;
  }
  camera.up.copy(state.homeView.up);
  moveCamera(state.homeView.position, state.homeView.target, animate);
}

/**
 * Home is a full reset, not just a camera move.
 *
 * The portal's home button puts the machine back the way it arrived: every
 * filter released - machine and per-part transparency, isolate, explode,
 * section, hidden parts, the tree search - and only then the camera flown back
 * to its opening view. Leaving a filter on while the camera resets is what makes
 * the button feel broken: you press "reset" and the machine is still see-through.
 *
 * Rendering PREFERENCES are deliberately left alone - shading mode, theme, grid,
 * edges, tone, FOV. Those are settings the user went into a panel and chose, not
 * state they fell into by clicking around the model.
 *
 * Part transparency is cleared before machine transparency on purpose: a
 * transparent part wears a swapped-in material twin, and the machine restore
 * pass only touches the materials meshes are actually wearing at the time.
 */
function resetView(animate = true) {
  if (!state.root) { goHome(animate); return; }

  if (state.measure.active) toggleMeasure();
  else clearMeasure();

  const search = $('#tree-search');
  if (search) search.value = '';

  if (state.explodeEnabled) setExplodeEnabled(false);

  if (state.clip.enabled) {
    state.clip.enabled = false;
    const clipOn = $('#clip-on');
    if (clipOn) clipOn.checked = false;
    updateClipping();
  }

  clearSelection();
  markSelectedRow(null);
  showAll();                     // select-visible, isolate, part transparency, hidden parts
  if (state.machineTransparent) applyMachineTransparency(false);

  updateSelectionUI();
  goHome(animate);
  toast('View reset');
}

function stepCameraAnim(now) {
  if (!camAnim) return false;
  const k = Math.min(1, (now - camAnim.t0) / camAnim.dur);
  const e = k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2; // easeInOutCubic
  camera.position.lerpVectors(camAnim.fromPos, camAnim.toPos, e);
  controls.target.lerpVectors(camAnim.fromTgt, camAnim.toTgt, e);
  if (k >= 1) camAnim = null;
  return true;
}

/* --------------------------------------------------------------- selection */

const PART_TRANSPARENT_OPACITY = 0.35;

const HIGHLIGHT = new THREE.MeshStandardMaterial({
  color: 0x4da3ff,
  emissive: 0x11406e,
  metalness: 0.1,
  roughness: 0.4,
  side: THREE.FrontSide,
});

// A selected part wears HIGHLIGHT, so a selected part that is ALSO transparent
// needs a second highlight rather than mutated flags on the shared one - several
// parts can be transparent at once now, and only one of them is selected.
const HIGHLIGHT_TRANSPARENT = HIGHLIGHT.clone();
HIGHLIGHT_TRANSPARENT.transparent = true;
HIGHLIGHT_TRANSPARENT.opacity = PART_TRANSPARENT_OPACITY;
HIGHLIGHT_TRANSPARENT.depthWrite = false;

function highlightFor(part) {
  return state.transparentParts.has(part) ? HIGHLIGHT_TRANSPARENT : HIGHLIGHT;
}

function selectPart(part, { focus = false, fromTree = false } = {}) {
  if (state.selected === part) {
    if (focus) fitTo(new THREE.Box3().setFromObject(part.object));
    return;
  }
  // A sweep-highlight and a single selection both stash the original material on
  // the same key, and both mark tree rows `selected`. Picking a part ends the
  // sweep so neither can strip the other's state out from under it.
  if (state.selectVisibleActive) setSelectVisible(false);
  clearSelection();
  state.selected = part;
  if (part) {
    for (const m of part.meshes) {
      m.userData._mat = m.material;
      m.material = highlightFor(part);
    }
    if (focus) fitTo(new THREE.Box3().setFromObject(part.object));
    if (!fromTree) revealInTree(part);
  }
  updateSelectionUI();
  requestRender();
}

function clearSelection() {
  const p = state.selected;
  if (p) {
    for (const m of p.meshes) {
      if (m.userData._mat) { m.material = m.userData._mat; delete m.userData._mat; }
    }
  }
  state.selected = null;
  // Zoom-to-selection describes a camera state tied to a part that is no longer
  // selected. Transparency deliberately does NOT reset here: it belongs to the
  // part, not to the act of having it selected, which is what lets several parts
  // stay transparent while you move between them.
  state.zoomedToSelection = false;
}

const raycaster = new THREE.Raycaster();
raycaster.firstHitOnly = true;
const pointer = new THREE.Vector2();

function pickAt(clientX, clientY) {
  const rect = canvas.getBoundingClientRect();
  pointer.x = ((clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const hits = raycaster.intersectObject(state.root, true);
  for (const h of hits) {
    if (!h.object.visible) continue;
    if (state.clip.enabled && state.clip.plane && state.clip.plane.distanceToPoint(h.point) < 0) continue;
    return h;
  }
  return null;
}

/* -------------------------------------------------------------- model tree */

let treeRootEl = null;

// Expand the first couple of levels so the assembly is legible on arrival, but
// never auto-expand a level so wide it would build thousands of rows up front.
const AUTO_EXPAND_DEPTH = 2;
const AUTO_EXPAND_MAX_CHILDREN = 80;

function shouldAutoExpand(depth, childCount) {
  return depth < AUTO_EXPAND_DEPTH && childCount > 0 && childCount <= AUTO_EXPAND_MAX_CHILDREN;
}

function buildTreeUI() {
  treeRootEl = $('#tree-wrap');
  treeRootEl.textContent = '';
  const frag = document.createDocumentFragment();

  // The glTF scene root carries no name; showing it as "(unnamed)" just adds a
  // dead level, so start from its children unless it is a real named node.
  if (state.root.name && state.root.name.trim()) {
    renderTreeNode(state.root, frag, 0, true);
  } else {
    for (const child of childEntries(state.root)) {
      renderTreeNode(child, frag, 0, true);
    }
  }
  treeRootEl.appendChild(frag);
}

function childEntries(obj) {
  // Collapse gltfpack's unnamed mesh wrappers so the tree shows the CAD
  // structure rather than the exporter's plumbing.
  const out = [];
  for (const c of obj.children) {
    if (c.isLight || c === grid) continue;
    if (isMeaningfulName(c.name)) {
      out.push(c);
    } else {
      // Unnamed wrapper (or an auto-named mesh): splice its children into this
      // level rather than showing an exporter artefact as a tree row.
      out.push(...childEntries(c));
    }
  }
  return out;
}

function renderTreeNode(obj, parentEl, depth, expanded) {
  const kids = childEntries(obj);
  const part = state.partByObject.get(obj);

  const node = document.createElement('div');
  node.className = 'tnode';

  const row = document.createElement('div');
  row.className = 'trow';
  row.style.paddingLeft = 4 + depth * 13 + 'px';
  row.dataset.uuid = obj.uuid;

  const caret = document.createElement('span');
  caret.className = 'tcaret' + (kids.length ? '' : ' leaf') + (expanded ? ' open' : '');
  caret.textContent = '▶';

  const eye = document.createElement('span');
  eye.className = 'teye';
  eye.textContent = obj.visible ? '◉' : '○';
  eye.title = 'Toggle visibility';

  const name = document.createElement('span');
  name.className = 'tname';
  name.textContent = obj.name || '(unnamed)';
  name.title = obj.name || '';

  row.append(caret, eye);
  if (part) {
    const sw = document.createElement('span');
    sw.className = 'tswatch';
    sw.style.background = part.color;
    row.append(sw);
  }
  row.append(name);

  if (kids.length) {
    const count = document.createElement('span');
    count.className = 'tcount';
    count.textContent = kids.length;
    row.append(count);
  }

  node.append(row);

  const childBox = document.createElement('div');
  childBox.className = 'tkids';
  if (!expanded) childBox.classList.add('hidden');
  node.append(childBox);

  let built = false;
  const build = () => {
    if (built) return;
    built = true;
    const frag = document.createDocumentFragment();
    for (const k of kids) {
      renderTreeNode(k, frag, depth + 1, shouldAutoExpand(depth + 1, childEntries(k).length));
    }
    childBox.appendChild(frag);
  };
  if (expanded && kids.length) build();

  caret.addEventListener('click', (e) => {
    e.stopPropagation();
    if (!kids.length) return;
    build();
    const open = childBox.classList.toggle('hidden');
    caret.classList.toggle('open', !open);
  });

  eye.addEventListener('click', (e) => {
    e.stopPropagation();
    setObjectVisible(obj, !obj.visible);
    eye.textContent = obj.visible ? '◉' : '○';
    row.classList.toggle('dimmed', !obj.visible);
    requestRender();
  });

  row.addEventListener('click', () => {
    if (part) selectPart(part, { fromTree: true });
    else {
      clearSelection();
      updateSelectionUI();
      requestRender();
      fitTo(new THREE.Box3().setFromObject(obj));
    }
    markSelectedRow(row);
  });

  row.addEventListener('dblclick', () => {
    fitTo(new THREE.Box3().setFromObject(obj));
  });

  obj.userData._treeRow = row;
  obj.userData._treeExpand = () => { build(); childBox.classList.remove('hidden'); caret.classList.add('open'); };

  parentEl.appendChild(node);
}

function markSelectedRow(row) {
  $$('.trow.selected').forEach((r) => r.classList.remove('selected'));
  if (row) {
    row.classList.add('selected');
    row.scrollIntoView({ block: 'nearest' });
  }
}

function revealInTree(part) {
  const chain = [];
  let cur = part.object;
  while (cur && cur !== state.root) { chain.push(cur); cur = cur.parent; }
  chain.reverse();
  for (const o of chain) if (o.userData._treeExpand) o.userData._treeExpand();
  markSelectedRow(part.object.userData._treeRow);
}

function setObjectVisible(obj, visible) {
  obj.visible = visible;
  const part = state.partByObject.get(obj);
  if (part) part.visible = visible;
}

/* ------------------------------------------------------------------ filter */

function applyFilter(query) {
  if (state.selectVisibleActive) setSelectVisible(false);   // filtering rewrites visibility
  const q = query.trim().toLowerCase();
  if (!q) {
    for (const p of state.parts) setObjectVisible(p.object, true);
    $$('.trow').forEach((r) => r.classList.remove('dimmed'));
    $$('.teye').forEach((e) => { e.textContent = '◉'; });
    requestRender();
    toast('Filter cleared');
    return;
  }
  let hits = 0;
  for (const p of state.parts) {
    const match = p.name.toLowerCase().includes(q);
    setObjectVisible(p.object, match);
    if (match) hits++;
    const row = p.object.userData._treeRow;
    if (row) {
      row.classList.toggle('dimmed', !match);
      const eye = row.querySelector('.teye');
      if (eye) eye.textContent = match ? '◉' : '○';
    }
  }
  requestRender();
  toast(`${hits} part${hits === 1 ? '' : 's'} match "${query.trim()}"`);
}

/* -------------------------------------------------------------- visibility */

function showAll() {
  // The sweep highlights "the parts visible right now", so any change to what is
  // visible invalidates it.
  if (state.selectVisibleActive) setSelectVisible(false);
  state.isolated = false;
  state.isolateBackup = null;
  clearAllPartTransparency();
  state.root.traverse((o) => { if (o !== grid) o.visible = true; });
  for (const p of state.parts) p.visible = true;
  $$('.trow').forEach((r) => r.classList.remove('dimmed'));
  $$('.teye').forEach((e) => { e.textContent = '◉'; });
  syncDockState();
  requestRender();
}

/**
 * Isolate is a MODE that follows the selection, not a one-shot hide.
 *
 * While it is on, whatever is selected is what you see; pick a different part and
 * isolation moves to it. Losing the selection ends the mode outright: clicking
 * empty space brings the machine back AND releases the button, so the next part
 * you pick is simply selected instead of being silently re-isolated. That is the
 * portal's behaviour, and it is why clicking empty space no longer strands you
 * inside a lone part with no way out but re-selecting it.
 *
 * The backup is what makes leaving isolate safe: restoring means "put visibility
 * back how it was", not "show everything", so parts hidden by hand before
 * isolating stay hidden afterwards.
 */
function applyIsolateState() {
  if (!state.root) return;
  const wanted = state.isolated && !!state.selected;
  // Deselecting is what turns the mode off - not just what suspends its effect.
  if (state.isolated && !state.selected) state.isolated = false;

  if (wanted) {
    if (!state.isolateBackup) {
      state.isolateBackup = new Map();
      state.root.traverse((o) => state.isolateBackup.set(o, o.visible));
    }
    const keep = new Set();
    let cur = state.selected.object;
    while (cur) { keep.add(cur); cur = cur.parent; }
    state.selected.object.traverse((o) => keep.add(o));
    state.root.traverse((o) => { o.visible = keep.has(o); });
    state.root.visible = true;
  } else if (state.isolateBackup) {
    state.root.traverse((o) => {
      if (state.isolateBackup.has(o)) o.visible = state.isolateBackup.get(o);
    });
    state.isolateBackup = null;
  }
  requestRender();
}

function isolateSelected() {
  // Matches the portal, where the button is simply disabled without a selection.
  if (!state.selected) { toast('Select a part first'); return; }
  state.isolated = !state.isolated;
  applyIsolateState();
  syncDockState();
  if (state.isolated) {
    fitTo(new THREE.Box3().setFromObject(state.selected.object));
    toast('Isolated ' + state.selected.name);
  } else {
    toast('Isolation off');
  }
}

function hideSelected() {
  if (!state.selected) { toast('Select a part first'); return; }
  const p = state.selected;
  setObjectVisible(p.object, false);
  const row = p.object.userData._treeRow;
  if (row) {
    row.classList.add('dimmed');
    const eye = row.querySelector('.teye');
    if (eye) eye.textContent = '○';
  }
  clearSelection();
  updateSelectionUI();
  requestRender();
  toast('Hid ' + p.name);
}

/* ----------------------------------------------------------------- explode */

/**
 * Push every part outward along the vector from the model centre to that part's
 * own centre, which keeps concentric stacks readable instead of scattering them.
 * Always recomputed from the stored home transform rather than applied
 * incrementally, so dragging the slider back and forth cannot accumulate drift.
 */
function setExplodeStable(factor) {
  state.explode = factor;
  const wp = new THREE.Vector3();
  const dirWorld = new THREE.Vector3();
  for (const p of state.parts) {
    p.object.position.copy(p.home);
    // getWorldPosition refreshes this object's world matrix (and its ancestors'),
    // so an explicit subtree update here would just be wasted work per part.
    p.object.getWorldPosition(wp);
    dirWorld.copy(wp).sub(state.center);
    if (dirWorld.lengthSq() < 1e-9) continue;
    dirWorld.normalize().multiplyScalar(factor * state.radius * 0.85);
    const parent = p.object.parent;
    if (!parent) { p.object.position.copy(p.home).add(dirWorld); continue; }
    const a = parent.worldToLocal(wp.clone());
    const b = parent.worldToLocal(wp.clone().add(dirWorld));
    p.object.position.copy(p.home).add(b.sub(a));
  }
  state.root.updateMatrixWorld(true);
  requestRender();
}

/* ---------------------------------------------------------------- clipping */

function updateClipping() {
  const c = state.clip;
  $('#btn-section').classList.toggle('active', c.enabled);
  if (!c.enabled) {
    renderer.clippingPlanes = [];
    c.plane = null;
    requestRender();
    return;
  }
  const min = state.bounds.min, max = state.bounds.max;
  const lo = c.axis === 'x' ? min.x : c.axis === 'y' ? min.y : min.z;
  const hi = c.axis === 'x' ? max.x : c.axis === 'y' ? max.y : max.z;
  const pad = (hi - lo) * 0.02 || 1e-3;
  const at = THREE.MathUtils.lerp(lo - pad, hi + pad, c.pos);

  // three.js keeps the half-space where `normal . p + constant > 0`.
  // Unflipped we want to keep everything *below* the cut, so the normal points
  // back down the axis. That way the slider at its maximum keeps the whole
  // model, and dragging it down sweeps material away - which is what a user
  // expects from a section control.
  const axis = new THREE.Vector3(
    c.axis === 'x' ? 1 : 0,
    c.axis === 'y' ? 1 : 0,
    c.axis === 'z' ? 1 : 0
  );
  const plane = c.flip
    ? new THREE.Plane(axis.clone(), -at)
    : new THREE.Plane(axis.clone().negate(), at);

  c.plane = plane;
  renderer.clippingPlanes = [plane];
  requestRender();
}

/* ----------------------------------------------------------------- measure */

const measureMat = new THREE.LineBasicMaterial({ color: 0xffb454, depthTest: false });

function clearMeasure() {
  for (const o of state.measure.objects) {
    scene.remove(o);
    o.geometry?.dispose();
  }
  state.measure.objects = [];
  state.measure.points = [];
  const measureOut = $('#measure-out');
  if (measureOut) measureOut.textContent = '';
  requestRender();
}

function addMeasurePoint(pt) {
  const m = state.measure;
  m.points.push(pt.clone());

  const dotGeo = new THREE.SphereGeometry(state.radius * 0.006, 12, 8);
  const dot = new THREE.Mesh(dotGeo, new THREE.MeshBasicMaterial({ color: 0xffb454, depthTest: false }));
  dot.position.copy(pt);
  dot.renderOrder = 999;
  scene.add(dot);
  m.objects.push(dot);

  if (m.points.length === 2) {
    const geo = new THREE.BufferGeometry().setFromPoints(m.points);
    const line = new THREE.Line(geo, measureMat);
    line.renderOrder = 999;
    scene.add(line);
    m.objects.push(line);

    const d = m.points[0].distanceTo(m.points[1]);
    const delta = m.points[1].clone().sub(m.points[0]);
    const measureOut = $('#measure-out');
    if (measureOut) {
      measureOut.innerHTML =
        `<b>${fmtLen(d)}</b><br>` +
        `<span style="color:var(--text-faint)">dX ${fmtLen(Math.abs(delta.x))} &nbsp; ` +
        `dY ${fmtLen(Math.abs(delta.y))} &nbsp; dZ ${fmtLen(Math.abs(delta.z))}</span>`;
    }
    m.points = [];
    // Next click starts a fresh pair but keeps the previous line on screen.
    m.objects = m.objects.slice();
  }
  requestRender();
}

/* -------------------------------------------------------------------- edges */

/**
 * Build crease edges once per *unique* geometry, then attach a LineSegments to
 * every node that uses it.  On a 5,000-placement assembly built from 725 unique
 * parts this is ~7x less work than doing it per placement.
 */
async function buildEdges(thresholdDeg = 28) {
  if (state.edgesBuilt) {
    state.edgeGroup.visible = !state.edgeGroup.visible;
    $('#btn-edges')?.classList.toggle('active', state.edgeGroup.visible);
    state.shadedWire = !!state.edgeGroup.visible;
    syncShadedWireUI();
    requestRender();
    return;
  }

  // Crease extraction walks every triangle and allocates a line segment per
  // kept edge, so on a very heavy model it is worth a lot of seconds and a lot
  // of memory. Make that the user's call rather than silently freezing the tab.
  if (state.stats.tris > 3_000_000) {
    const est = Math.ceil(state.stats.tris / 1_500_000);
    const ok = window.confirm(
      `This model has ${fmtInt(state.stats.tris)} triangles.\n\n` +
      `Building the edge overlay may take around ${est}–${est * 4} seconds ` +
      `and will noticeably increase memory use.\n\nBuild it now?`
    );
    if (!ok) return;
  }

  const meshes = [];
  state.root.traverse((o) => { if (o.isMesh) meshes.push(o); });

  const byGeom = new Map();
  for (const m of meshes) {
    if (!byGeom.has(m.geometry)) byGeom.set(m.geometry, []);
    byGeom.get(m.geometry).push(m);
  }

  const group = new THREE.Group();
  group.name = '__edges__';
  const mat = new THREE.LineBasicMaterial({
    color: document.documentElement.dataset.theme === 'light' ? 0x33404f : 0x0b0e12,
    transparent: true,
    opacity: 0.55,
  });

  const total = byGeom.size;
  let done = 0;
  const t0 = performance.now();

  for (const [geom, users] of byGeom) {
    let edgeGeo = null;
    try {
      edgeGeo = new THREE.EdgesGeometry(geom, thresholdDeg);
    } catch (e) {
      done++;
      continue;
    }
    if (edgeGeo.attributes.position && edgeGeo.attributes.position.count) {
      for (const u of users) {
        const seg = new THREE.LineSegments(edgeGeo, mat);
        seg.applyMatrix4(u.matrixWorld);
        seg.frustumCulled = true;
        group.add(seg);
      }
    } else {
      edgeGeo.dispose();
    }
    done++;
    if (done % 24 === 0) {
      toast(`Building edges ${Math.round((done / total) * 100)}%`, 600);
      await new Promise((r) => setTimeout(r, 0));
    }
  }

  scene.add(group);
  state.edgeGroup = group;
  state.edgesBuilt = true;
  state.edgeGroup.visible = true;
  $('#btn-edges')?.classList.add('active');
  state.shadedWire = true;
  syncShadedWireUI();
  requestRender();
  toast(`Edges built in ${((performance.now() - t0) / 1000).toFixed(1)}s`);
}

/* --------------------------------------------------------------------- HUD */

function updateHud() {
  // The stats readout is optional chrome - the portal builds ship without the
  // #hud element at all, and this also runs from collectParts before boot.
  const hud = $('#hud');
  if (!hud) return;
  const s = state.stats;
  // Draw-call and triangle counts are captured at render time: rendering is on
  // demand, so reading renderer.info here would report whatever the last frame
  // happened to leave behind.
  const idle = performance.now() - state.lastRenderAt > 900;
  const fps = idle || !state.frameTimes.length
    ? 'idle'
    : String(Math.round(1000 / (state.frameTimes.reduce((a, b) => a + b, 0) / state.frameTimes.length)));

  hud.innerHTML =
    `<span class="k">fps  </span>${String(fps).padStart(5)}\n` +
    `<span class="k">draw </span>${String(fmtInt(state.lastCalls)).padStart(5)}\n` +
    `<span class="k">vis  </span>${String(fmtInt(state.lastTris)).padStart(5)}\n` +
    `<span class="k">tris </span>${fmtInt(s.tris)}\n` +
    `<span class="k">parts</span> ${fmtInt(state.parts.length)}`;
}

function updateSelectionUI() {
  const p = state.selected;
  // Every path that changes what is selected ends up here, which makes it the
  // one place the dock's selection-gated buttons need re-rendering from - and the
  // one place isolate needs to follow the selection to its new target, or release
  // when there is no longer one.
  applyIsolateState();
  syncDockState();
  const el = $('#sel-info');
  if (!p) {
    el.innerHTML = '<span style="color:var(--text-faint)">No selection &mdash; click a part</span>';
    return;
  }
  const box = new THREE.Box3().setFromObject(p.object);
  const size = box.getSize(new THREE.Vector3());
  el.innerHTML =
    `<dl class="kv">` +
    `<dt>Name</dt><dd>${escapeHtml(p.name)}</dd>` +
    `<dt>Triangles</dt><dd>${fmtInt(p.tris)}</dd>` +
    `<dt>Meshes</dt><dd>${p.meshes.length}</dd>` +
    `<dt>Size</dt><dd>${fmtLen(size.x)} &times; ${fmtLen(size.y)} &times; ${fmtLen(size.z)}</dd>` +
    `<dt>Colour</dt><dd><span class="tswatch" style="display:inline-block;background:${p.color}"></span> ${p.color}</dd>` +
    `</dl>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function updateModelInfo() {
  const m = CFG.meta || {};
  const st = m.stats || {};
  const src = m.source || {};
  $('#model-info').innerHTML =
    `<dl class="kv">` +
    `<dt>Source</dt><dd>${escapeHtml(src.file || CFG.title || '')}</dd>` +
    `<dt>CAD system</dt><dd>${escapeHtml(src.originatingSystem || 'unknown')}</dd>` +
    `<dt>Schema</dt><dd>${escapeHtml((src.schema || '').split('{')[0].trim() || 'n/a')}</dd>` +
    `<dt>STEP size</dt><dd>${src.bytes ? fmtBytes(src.bytes) : 'n/a'}</dd>` +
    `<dt>Model size</dt><dd>${m.model && m.model.bytes ? fmtBytes(m.model.bytes) : 'n/a'}</dd>` +
    `<dt>Unique parts</dt><dd>${fmtInt(st.uniqueParts || state.parts.length)}</dd>` +
    `<dt>Placements</dt><dd>${fmtInt(st.placements || state.stats.meshes)}</dd>` +
    `<dt>Triangles</dt><dd>${fmtInt(state.stats.tris)}</dd>` +
    `<dt>Geometries</dt><dd>${fmtInt(state.stats.geoms)} unique</dd>` +
    `<dt>Tessellation</dt><dd>${escapeHtml(st.quality || 'n/a')}</dd>` +
    `<dt>Units</dt><dd>${escapeHtml(CFG.units || 'mm')}</dd>` +
    `</dl>`;
}

/* ------------------------------------------------------------------ resize */

function resize() {
  const w = canvas.clientWidth || 1;
  const h = canvas.clientHeight || 1;
  if (canvas.width === w * renderer.getPixelRatio() && canvas.height === h * renderer.getPixelRatio()) return;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  controls.handleResize();
  requestRender();
}

const ro = new ResizeObserver(() => resize());
if (stageEl) ro.observe(stageEl);

/* ------------------------------------------------------------- render loop */

function markInteracting() {
  if (!state.interacting) {
    state.interacting = true;
    renderer.setPixelRatio(state.dprLow);
    resizeForce();
  }
  clearTimeout(state.interactTimer);
  state.interactTimer = setTimeout(() => {
    state.interacting = false;
    renderer.setPixelRatio(state.dprFull);
    resizeForce();
    requestRender();
  }, 220);
}

function resizeForce() {
  const w = canvas.clientWidth || 1;
  const h = canvas.clientHeight || 1;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  controls.handleResize();
  requestRender();
}

controls.addEventListener('start', markInteracting);
controls.addEventListener('change', () => { markInteracting(); requestRender(); });

function tick() {
  requestAnimationFrame(tick);

  const now = performance.now();
  let dirty = state.needsRender;

  if (stepCameraAnim(now)) dirty = true;
  if (controls.update()) dirty = true;

  if (!dirty) return;
  state.needsRender = false;

  renderer.info.reset();
  renderer.render(scene, camera);
  state.lastCalls = renderer.info.render.calls;
  state.lastTris = renderer.info.render.triangles;
  state.lastRenderAt = now;
  state.renderCount++;

  const dt = now - state.lastFrame;
  state.lastFrame = now;
  if (dt > 0 && dt < 500) {
    state.frameTimes.push(dt);
    if (state.frameTimes.length > 30) state.frameTimes.shift();
  }
}

/* ------------------------------------------------------------------- input */

let downPos = null;

canvas.addEventListener('pointerdown', (e) => {
  downPos = { x: e.clientX, y: e.clientY, t: performance.now(), b: e.button };
});

canvas.addEventListener('pointerup', (e) => {
  if (!downPos || e.button !== 0) { downPos = null; return; }
  const moved = Math.hypot(e.clientX - downPos.x, e.clientY - downPos.y);
  const held = performance.now() - downPos.t;
  downPos = null;
  if (moved > 4 || held > 400) return; // it was an orbit, not a click

  const hit = pickAt(e.clientX, e.clientY);

  if (state.measure.active) {
    if (hit) addMeasurePoint(hit.point);
    else toast('Click on the model to measure');
    return;
  }

  if (!hit) {
    clearSelection();
    updateSelectionUI();
    markSelectedRow(null);
    requestRender();
    return;
  }
  const part = state.partByObject.get(hit.object) ||
    state.partByObject.get(nearestNamedAncestor(hit.object, state.root));
  if (part) selectPart(part);
});

canvas.addEventListener('dblclick', (e) => {
  const hit = pickAt(e.clientX, e.clientY);
  if (hit) {
    const part = state.partByObject.get(hit.object) ||
      state.partByObject.get(nearestNamedAncestor(hit.object, state.root));
    if (part) { selectPart(part); fitTo(new THREE.Box3().setFromObject(part.object)); }
  } else {
    fitTo(state.bounds);
  }
});

window.addEventListener('keydown', (e) => {
  if (e.target.matches('input, textarea, select')) return;
  const k = e.key.toLowerCase();
  // Ahead of the map because Shift+F still lowercases to 'f', which would
  // otherwise fit the selection instead of toggling fullscreen.
  if (k === 'f' && e.shiftKey) { e.preventDefault(); toggleFullscreen(); return; }
  const map = {
    f: () => (state.selected ? fitTo(new THREE.Box3().setFromObject(state.selected.object)) : fitTo(state.bounds)),
    a: () => fitTo(state.bounds),
    '1': () => setView('front'), '2': () => setView('back'),
    '3': () => setView('left'), '4': () => setView('right'),
    '5': () => setView('top'), '6': () => setView('bottom'),
    '0': () => setView('iso'),
    i: () => $('#btn-isolate') && isolateSelected(),
    h: hideSelected,
    r: showAll,
    g: () => { grid.visible = !grid.visible; requestRender(); },
    m: () => $('#btn-measure') && toggleMeasure(),
    b: () => toggleSidebar(),
    escape: () => {
      if (state.measure.active) toggleMeasure();
      else { clearSelection(); updateSelectionUI(); markSelectedRow(null); requestRender(); }
    },
    '?': () => togglePanel('#panel-help'),
  };
  if (map[k]) { e.preventDefault(); map[k](); }
});

/* ---------------------------------------------------------------- controls */

function togglePanel(sel) {
  const el = $(sel);
  const wasHidden = el.classList.contains('hidden');
  $$('.panel').forEach((p) => { if (p !== el && p.id !== 'panel-info') p.classList.add('hidden'); });
  el.classList.toggle('hidden', !wasHidden);
  return wasHidden;
}

function setSidebarOpen(open) {
  state.sidebarOpen = !!open;
  $('#sidebar').classList.toggle('collapsed', !state.sidebarOpen);
  syncDockState();
  // The panel slides over 180ms and the canvas has to re-measure once it lands;
  // resizing mid-transition just gives the renderer a size it never keeps.
  setTimeout(resizeForce, 200);
}

function toggleSidebar() { setSidebarOpen(!state.sidebarOpen); }

function toggleMeasure() {
  const btnMeasure = $('#btn-measure');
  const panelMeasure = $('#panel-measure');
  if (!btnMeasure || !panelMeasure) return;
  state.measure.active = !state.measure.active;
  btnMeasure.classList.toggle('active', state.measure.active);
  panelMeasure.classList.toggle('hidden', !state.measure.active);
  canvas.style.cursor = state.measure.active ? 'crosshair' : '';
  if (!state.measure.active) clearMeasure();
  else toast('Click two points to measure');
}

function buildModelSwitcher() {
  const list = CFG.models || [];
  if (list.length < 2) return;   // nothing to switch between

  const wrap = $('#model-switch');
  const sel = $('#model-select');
  sel.textContent = '';
  list.forEach((m, i) => {
    const opt = document.createElement('option');
    opt.value = String(i);
    opt.textContent = m.title || m.name;
    if (i === state.modelIndex) opt.selected = true;
    sel.appendChild(opt);
  });
  wrap.classList.remove('hidden');
  sel.addEventListener('change', (e) => switchModel(parseInt(e.target.value, 10)));
}

/* ------------------------------------------------------------- bottom dock */

/*
 * The actions behind the GLB-parity toolbar, plus the one rule that holds this
 * section together: `syncDockState()` is the ONLY thing that writes a dock
 * button's glyph, label, tooltip, `active` class or `disabled` flag. The portal's
 * other HTML viewer learned this the hard way - a dozen scattered innerHTML
 * writes across one file kept desynchronising the same three toggles - so every
 * handler here mutates `state` and then calls the single renderer.
 */

/* Glyphs that swap at runtime. The static ones live in the template; these are
   the ones a toggle flips, so they have to exist as strings on this side.
   Sources are @mui/icons-material (24px viewBox) and react-icons, matching the
   portal's own imports. */
const ICONS = {
  navToPan: '<svg viewBox="0 0 24 24"><path d="M10 9h4V6h3l-5-5-5 5h3zm-1 1H6V7l-5 5 5 5v-3h3zm14 2-5-5v3h-3v4h3v3zm-9 3h-4v3H7l5 5 5-5h-3z"/></svg>',
  navToRotate: '<svg viewBox="0 0 24 24"><path d="M12 4V1L8 5l4 4V6c3.31 0 6 2.69 6 6 0 1.01-.25 1.97-.7 2.8l1.46 1.46C19.54 15.03 20 13.57 20 12c0-4.42-3.58-8-8-8m0 14c-3.31 0-6-2.69-6-6 0-1.01.25-1.97.7-2.8L5.24 7.74C4.46 8.97 4 10.43 4 12c0 4.42 3.58 8 8 8v3l4-4-4-4z"/></svg>',
  dropSolid: '<svg viewBox="0 0 256 256"><path d="M174,47.75a254.19,254.19,0,0,0-41.45-38.3,8,8,0,0,0-9.18,0A254.19,254.19,0,0,0,82,47.75C54.51,79.32,40,112.6,40,144a88,88,0,0,0,176,0C216,112.6,201.49,79.32,174,47.75Z"/></svg>',
  // @mui/icons-material Menu / MenuOpen - the portal's own drawer-toggle glyphs.
  menuClosed: '<svg viewBox="0 0 24 24"><path d="M3 18h18v-2H3zm0-5h18v-2H3zm0-7v2h18V6z"/></svg>',
  menuOpen: '<svg viewBox="0 0 24 24"><path d="M3 18h13v-2H3zm0-5h10v-2H3zm0-7v2h13V6zm18 9.59L17.42 12 21 8.41 19.59 7l-5 5 5 5z"/></svg>',
  dropTransparent: '<svg viewBox="0 0 16 16"><path d="M0 6.5a6.5 6.5 0 0 1 12.346-2.846 6.5 6.5 0 1 1-8.691 8.691A6.5 6.5 0 0 1 0 6.5m5.144 6.358a5.5 5.5 0 1 0 7.714-7.714 6.5 6.5 0 0 1-7.714 7.714m-.733-1.269q.546.226 1.144.33l-1.474-1.474q.104.597.33 1.144m2.614.386a5.5 5.5 0 0 0 1.173-.242L4.374 7.91a6 6 0 0 0-.296 1.118zm2.157-.672q.446-.25.838-.576L5.418 6.126a6 6 0 0 0-.587.826zm1.545-1.284q.325-.39.576-.837L6.953 4.83a6 6 0 0 0-.827.587l4.6 4.602Zm1.006-1.822q.183-.562.242-1.172L9.028 4.078q-.58.096-1.118.296l3.823 3.824Zm.186-2.642a5.5 5.5 0 0 0-.33-1.144 5.5 5.5 0 0 0-1.144-.33z"/></svg>',
  // @mui/icons-material Fullscreen / FullscreenExit.
  fullscreenEnter: '<svg viewBox="0 0 24 24"><path d="M7 14H5v5h5v-2H7zm-2-4h2V7h3V5H5zm12 7h-3v2h5v-5h-2zM14 5v2h3v3h2V5z"/></svg>',
  fullscreenExit: '<svg viewBox="0 0 24 24"><path d="M5 16h3v3h2v-5H5zm3-8H5v2h5V5H8zm6 11h2v-3h3v-2h-5zm2-11V5h-2v5h5V8z"/></svg>',
};

function setDockIcon(sel, svg) {
  const host = $(sel)?.querySelector('.dock-icon');
  if (host && host.innerHTML !== svg) host.innerHTML = svg;
}

/* ---- fullscreen ---- */

/*
 * Safari still ships these only under the webkit prefix, and iPhone Safari has
 * no element fullscreen at all - hence the capability probe rather than a
 * try/catch on click: a control that cannot work is better hidden than broken.
 *
 * The whole document goes fullscreen, not the canvas, so the dock and sidebar
 * come along; fullscreening the canvas alone would take the model and leave
 * every control behind.
 */
function fullscreenElement() {
  return document.fullscreenElement || document.webkitFullscreenElement || null;
}

function fullscreenSupported() {
  const el = document.documentElement;
  return !!(el.requestFullscreen || el.webkitRequestFullscreen);
}

function toggleFullscreen() {
  const el = document.documentElement;
  const request = el.requestFullscreen || el.webkitRequestFullscreen;
  const exit = document.exitFullscreen || document.webkitExitFullscreen;
  const active = !!fullscreenElement();
  if (active ? !exit : !request) return;
  // Browsers reject the request outside a user gesture, and an unhandled
  // rejection would surface in the console as an error the user cannot act on.
  Promise.resolve(active ? exit.call(document) : request.call(el)).catch(() =>
    toast('Fullscreen was blocked by the browser')
  );
}

function syncFullscreenButton() {
  const btn = $('#btn-fullscreen');
  if (!btn) return;
  const active = !!fullscreenElement();
  const label = active ? 'Exit Fullscreen' : 'Fullscreen';
  btn.title = label + ' (Shift+F)';
  btn.setAttribute('aria-label', label);
  btn.setAttribute('aria-pressed', active ? 'true' : 'false');
  setDockIcon('#btn-fullscreen', active ? ICONS.fullscreenExit : ICONS.fullscreenEnter);
}

/*
 * Fires for Esc and F11 too, not just the button, so the icon stays honest
 * however the mode was left. TrackballControls caches the canvas rect for its
 * drag maths, so resizeForce() - which calls handleResize() - has to run here
 * or rotation stays calibrated to the old viewport.
 */
function onFullscreenChange() {
  syncFullscreenButton();
  resizeForce();
}

/* ---- navigation mode ---- */

/**
 * Swaps what a left-drag does. The right button takes over the gesture the left
 * one gave up, so pan mode does not cost you the ability to rotate at all -
 * the toolbar looks the same either way, and nothing becomes unreachable.
 */
function setNavMode(mode) {
  state.navMode = mode === 'pan' ? 'pan' : 'rotate';
  const panning = state.navMode === 'pan';
  controls.mouseButtons.LEFT = panning ? THREE.MOUSE.PAN : THREE.MOUSE.ROTATE;
  controls.mouseButtons.RIGHT = panning ? THREE.MOUSE.ROTATE : THREE.MOUSE.PAN;
  syncDockState();
}

function toggleNavMode() {
  setNavMode(state.navMode === 'pan' ? 'rotate' : 'pan');
  toast(state.navMode === 'pan' ? 'Pan mode — drag to pan' : 'Rotate mode — drag to rotate');
}

/* ---- zoom to selection ---- */

function zoomToSelectionToggle() {
  if (!state.selected) return;
  if (state.zoomedToSelection) {
    fitTo(state.bounds);
    state.zoomedToSelection = false;
  } else {
    fitTo(new THREE.Box3().setFromObject(state.selected.object));
    state.zoomedToSelection = true;
  }
  syncDockState();
}

/* ---- select visible ---- */

/**
 * Highlights every part still visible, tracked separately from `selected`.
 *
 * This viewer's selection is single-part by design - isolate, part transparency
 * and the info panel all read `state.selected` - so promoting this to a real
 * multi-selection would ripple through all of them. Keeping the highlight in its
 * own list means "select visible" is a visual sweep that cannot confuse what
 * "the selected part" means.
 */
function setSelectVisible(active) {
  if (active === state.selectVisibleActive) return;
  if (active) {
    clearSelection();          // its highlight uses the same stash; don't double up
    markSelectedRow(null);
    state.visibleSelection = state.parts.filter((p) => p.visible && p.object.visible);
    for (const p of state.visibleSelection) {
      for (const m of p.meshes) {
        if (m.userData._mat) continue;
        m.userData._mat = m.material;
        m.material = highlightFor(p);
      }
      p.object.userData._treeRow?.classList.add('selected');
    }
  } else {
    for (const p of state.visibleSelection) {
      for (const m of p.meshes) {
        if (m.userData._mat) { m.material = m.userData._mat; delete m.userData._mat; }
      }
      p.object.userData._treeRow?.classList.remove('selected');
    }
    state.visibleSelection = [];
  }
  state.selectVisibleActive = active;
  updateSelectionUI();
  syncDockState();
  requestRender();
  if (active) toast(`${state.visibleSelection.length} visible part(s) selected`);
}

function toggleSelectVisible() { setSelectVisible(!state.selectVisibleActive); }

/* ---- show parent ---- */

function showParentFromSelection() {
  if (!state.selected) return;
  const parent = nearestNamedAncestor(state.selected.object.parent, state.root);
  const part = state.partByObject.get(parent);
  if (!part || part === state.selected) { toast('Already at the top of the assembly'); return; }
  selectPart(part);
  fitTo(new THREE.Box3().setFromObject(part.object));
}

/* ---- explode ---- */

// Recomputing every part's position is far more expensive than a frame, so a
// drag coalesces to one recompute per animation frame; both sliders share it.
let explodePending = null;
function queueExplode(v) {
  if (explodePending !== null) { explodePending = v; return; }
  explodePending = v;
  requestAnimationFrame(() => {
    const target = explodePending;
    explodePending = null;
    setExplodeStable(target);
  });
}

/**
 * The dock slider and the Settings-panel slider are two views of one number, so
 * this is the only writer: whichever the user drags, both inputs and the panel's
 * numeric read-out follow.
 */
function setExplodeAmount(factor) {
  state.explodeValue = factor;
  const dock = $('#dock-explode');
  const opt = $('#opt-explode');
  const val = $('#opt-explode-val');
  if (dock && dock.value !== String(factor)) dock.value = String(factor);
  if (opt && opt.value !== String(factor)) opt.value = String(factor);
  if (val) val.textContent = factor.toFixed(2);
  if (state.explodeEnabled) queueExplode(factor);
}

/**
 * The toggle is separate from the amount so turning explode off and on again
 * returns to where the slider was left, rather than resetting to zero.
 */
function setExplodeEnabled(enabled) {
  state.explodeEnabled = enabled;
  $('#explode-bar')?.classList.toggle('hidden', !enabled);
  queueExplode(enabled ? state.explodeValue : 0);
  syncDockState();
}

/* ---- more menu ---- */

function closeMoreMenu() {
  $('#dock-more-menu')?.classList.add('hidden');
  $('#btn-more-dock')?.setAttribute('aria-expanded', 'false');
}

function toggleMoreMenu() {
  const menu = $('#dock-more-menu');
  if (!menu) return;
  const opening = menu.classList.contains('hidden');
  menu.classList.toggle('hidden', !opening);
  $('#btn-more-dock')?.setAttribute('aria-expanded', String(opening));
  if (opening) syncDockState();
}

const MORE_ACTIONS = {
  part: togglePartTransparency,
  isolate: isolateSelected,
  info: () => $('#panel-info').classList.toggle('hidden'),
  settings: () => togglePanel('#panel-settings'),
  section: () => {
    const shown = togglePanel('#panel-section');
    if (shown) { $('#clip-on').checked = true; state.clip.enabled = true; updateClipping(); }
  },
  tree: toggleSidebar,
};

/* ---- the single renderer ---- */

function syncDockState() {
  const sel = !!state.selected;

  // Navigation: the glyph and tooltip name the mode you are about to GET, which
  // is why "pan mode" shows the pan arrows rather than the rotate loop.
  const panning = state.navMode === 'pan';
  setDockIcon('#btn-nav-mode', panning ? ICONS.navToRotate : ICONS.navToPan);
  const nav = $('#btn-nav-mode');
  if (nav) {
    nav.title = panning ? 'Rotate Mode' : 'Pan Mode';
    nav.setAttribute('aria-label', nav.title);
    nav.classList.toggle('active', panning);
  }

  $('#btn-explode-toggle')?.classList.toggle('active', state.explodeEnabled);
  const exp = $('#btn-explode-toggle');
  if (exp) exp.title = state.explodeEnabled ? 'Disable Explode Model' : 'Explode Model';

  const zoom = $('#btn-zoom-selected');
  if (zoom) {
    zoom.disabled = !sel;
    zoom.title = !sel
      ? 'Zoom to Selected (select a part first)'
      : (state.zoomedToSelection ? 'Zoom Out to Model' : 'Zoom to Selected');
  }

  // Machine / Part chips carry a droplet when solid and the transparency glyph
  // when transparent, the way the portal's chips do.
  const machine = $('#btn-theme-chip');
  if (machine) {
    machine.classList.toggle('active', state.machineTransparent);
    machine.title = state.machineTransparent
      ? 'Switch to Machine Solid'
      : 'Switch to Machine Transparent';
  }
  setDockIcon('#btn-theme-chip', state.machineTransparent ? ICONS.dropTransparent : ICONS.dropSolid);

  // The chip reports the SELECTED part's own transparency, not a global flag -
  // other parts may well be transparent at the same time.
  const selTransparent = isPartTransparent(state.selected);
  const part = $('#btn-part-chip');
  if (part) {
    part.disabled = !sel;
    part.classList.toggle('active', selTransparent);
    part.title = !sel
      ? 'Part transparency requires a selected part'
      : (selTransparent ? 'Switch selected part to Solid' : 'Switch selected part to Transparent');
  }
  setDockIcon('#btn-part-chip', selTransparent ? ICONS.dropTransparent : ICONS.dropSolid);

  const iso = $('#btn-isolate');
  if (iso) {
    iso.disabled = !sel;
    iso.classList.toggle('active', state.isolated);
    iso.title = !sel
      ? 'Isolate requires a selected part'
      : (state.isolated ? 'Disable Isolate' : 'Isolate selected part');
  }

  $('#btn-select-visible')?.classList.toggle('active', state.selectVisibleActive);
  const selVis = $('#btn-select-visible');
  if (selVis) selVis.title = state.selectVisibleActive ? 'Unselect Visible Parts' : 'Select Visible Parts';

  const parent = $('#btn-show-parent');
  if (parent) {
    parent.disabled = !sel;
    parent.title = sel ? 'Show Parent in View' : 'Show Parent requires a selected part';
  }

  // The compact-width menu rows mirror the pills they stand in for, disabled
  // state included - otherwise the action a phone user gets is the one the
  // toolbar refuses to give a desktop user.
  const menuPart = $('#dock-more-menu [data-action="part"]');
  if (menuPart) { menuPart.disabled = !sel; menuPart.classList.toggle('active', selTransparent); }
  const menuIso = $('#dock-more-menu [data-action="isolate"]');
  if (menuIso) { menuIso.disabled = !sel; menuIso.classList.toggle('active', state.isolated); }

  // Side-panel toggle: white with a blue glyph while the panel is shut, inverted
  // while it is open, so the button states the panel's state instead of merely
  // offering an action.
  const menuBtn = $('#btn-menu-toggle');
  if (menuBtn) {
    const open = state.sidebarOpen;
    const label = open ? 'Close Menu' : 'Open Menu';
    menuBtn.classList.toggle('active', open);
    menuBtn.setAttribute('aria-label', label);
    menuBtn.setAttribute('aria-expanded', String(open));
    setDockIcon('#btn-menu-toggle', open ? ICONS.menuOpen : ICONS.menuClosed);
    const tip = $('#menu-toggle-tip');
    if (tip) tip.textContent = label;
  }
}

function wireUI() {
  $('#btn-sidebar').addEventListener('click', toggleSidebar);
  $('#btn-fit')?.addEventListener('click', () => fitTo(state.bounds));

  $('#btn-menu-toggle')?.addEventListener('click', toggleSidebar);

  /* ---- bottom dock ---- */
  $('#btn-home')?.addEventListener('click', () => resetView());
  $('#btn-nav-mode')?.addEventListener('click', toggleNavMode);
  $('#btn-explode-toggle')?.addEventListener('click', () => setExplodeEnabled(!state.explodeEnabled));
  $('#dock-explode')?.addEventListener('input', (e) => setExplodeAmount(parseFloat(e.target.value)));
  $('#btn-zoom-selected')?.addEventListener('click', zoomToSelectionToggle);
  $('#btn-theme-chip')?.addEventListener('click', toggleMachineTransparency);
  $('#btn-part-chip')?.addEventListener('click', togglePartTransparency);
  $('#btn-isolate')?.addEventListener('click', isolateSelected);
  $('#btn-select-visible')?.addEventListener('click', toggleSelectVisible);
  $('#btn-show-parent')?.addEventListener('click', showParentFromSelection);
  $('#btn-help-dock')?.addEventListener('click', () => togglePanel('#panel-help'));

  if (fullscreenSupported()) {
    $('#btn-fullscreen')?.addEventListener('click', toggleFullscreen);
    document.addEventListener('fullscreenchange', onFullscreenChange);
    document.addEventListener('webkitfullscreenchange', onFullscreenChange);
    syncFullscreenButton();
  } else {
    $('#btn-fullscreen')?.classList.add('hidden');
  }

  $('#btn-more-dock')?.addEventListener('click', (e) => { e.stopPropagation(); toggleMoreMenu(); });
  $$('#dock-more-menu .dock-menu-item').forEach((item) =>
    item.addEventListener('click', () => {
      closeMoreMenu();
      MORE_ACTIONS[item.dataset.action]?.();
    })
  );
  // Click-away and Esc, so the menu behaves like the popup it imitates rather
  // than a panel you have to click the same button again to dismiss.
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#dock-more-menu, #btn-more-dock')) closeMoreMenu();
  });
  window.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeMoreMenu(); });

  $('#btn-hide').addEventListener('click', hideSelected);
  $('#btn-showall').addEventListener('click', showAll);
  $('#btn-edges')?.addEventListener('click', () => buildEdges());
  $('#btn-measure')?.addEventListener('click', toggleMeasure);
  $('#btn-settings').addEventListener('click', () => togglePanel('#panel-settings'));
  $('#btn-section').addEventListener('click', () => {
    const shown = togglePanel('#panel-section');
    if (shown) { $('#clip-on').checked = true; state.clip.enabled = true; updateClipping(); }
  });
  $('#btn-help').addEventListener('click', () => togglePanel('#panel-help'));
  $('#btn-info').addEventListener('click', () => $('#panel-info').classList.toggle('hidden'));
  $('#btn-snap')?.addEventListener('click', snapshot);

  $$('#viewcube button').forEach((b) => b.addEventListener('click', () => setView(b.dataset.view)));
  $$('.panel h3 button').forEach((b) =>
    b.addEventListener('click', () => b.closest('.panel').classList.add('hidden'))
  );

  const search = $('#tree-search');
  let searchTimer = 0;
  search.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => applyFilter(search.value), 220);
  });

  $('#opt-shading').addEventListener('change', (e) => {
    setShading(e.target.value);
    if (e.target.value !== 'shaded' && state.shadedWire) setShadedWire(false);
  });
  $('#opt-shaded-wire')?.addEventListener('change', (e) => { setShadedWire(e.target.checked); });
  $('#opt-tone').addEventListener('change', (e) => setToneMapping(e.target.value));
  $('#opt-theme').addEventListener('change', (e) => applyTheme(e.target.value));
  $('#opt-grid').addEventListener('change', (e) => { grid.visible = e.target.checked; requestRender(); });
  $('#opt-backface').addEventListener('change', (e) => {
    const side = e.target.checked ? THREE.FrontSide : THREE.DoubleSide;
    state.root.traverse((o) => {
      if (!o.isMesh) return;
      const mats = Array.isArray(o.material) ? o.material : [o.material];
      for (const m of mats) if (m) m.side = side;
    });
    HIGHLIGHT.side = side;
    requestRender();
  });
  $('#opt-metal').addEventListener('input', (e) => {
    const v = parseFloat(e.target.value);
    $('#opt-metal-val').textContent = v.toFixed(2);
    forEachMaterial((m) => { if (m.isMeshStandardMaterial) m.metalness = v; });
  });
  $('#opt-rough').addEventListener('input', (e) => {
    const v = parseFloat(e.target.value);
    $('#opt-rough-val').textContent = v.toFixed(2);
    forEachMaterial((m) => { if (m.isMeshStandardMaterial) m.roughness = v; });
  });
  $('#opt-exposure').addEventListener('input', (e) => {
    const v = parseFloat(e.target.value);
    $('#opt-exposure-val').textContent = v.toFixed(2);
    renderer.toneMappingExposure = v;
    requestRender();
  });
  // Frame-coalescing for the drag lives in queueExplode, shared with the dock slider.
  $('#opt-explode').addEventListener('input', (e) => {
    const v = parseFloat(e.target.value);
    setExplodeAmount(v);
    // Dragging this slider used to explode the model on its own. Now that the
    // dock owns an on/off toggle, moving it off zero implies "on" - otherwise
    // the slider would silently do nothing until you found the toolbar button.
    if (v > 0 && !state.explodeEnabled) setExplodeEnabled(true);
  });
  $('#opt-fov').addEventListener('input', (e) => {
    const v = parseFloat(e.target.value);
    $('#opt-fov-val').textContent = v.toFixed(0);
    camera.fov = v;
    camera.updateProjectionMatrix();
    requestRender();
  });

  $('#clip-on').addEventListener('change', (e) => { state.clip.enabled = e.target.checked; updateClipping(); });
  $('#clip-axis').addEventListener('change', (e) => { state.clip.axis = e.target.value; updateClipping(); });
  $('#clip-flip').addEventListener('change', (e) => { state.clip.flip = e.target.checked; updateClipping(); });
  $('#clip-pos').addEventListener('input', (e) => {
    state.clip.pos = parseFloat(e.target.value);
    $('#clip-pos-val').textContent = state.clip.pos.toFixed(2);
    updateClipping();
  });

  $('#measure-clear')?.addEventListener('click', clearMeasure);
}

function forEachMaterial(fn) {
  const seen = new Set();
  state.root.traverse((o) => {
    if (!o.isMesh) return;
    const mats = Array.isArray(o.material) ? o.material : [o.material];
    for (const m of mats) {
      if (!m || seen.has(m)) continue;
      seen.add(m);
      fn(m);
    }
  });
  requestRender();
}

function snapshot() {
  const prevRatio = renderer.getPixelRatio();
  // Supersample for a crisper capture, but cap it: on a multi-million-triangle
  // scene an unbounded ratio is a good way to lose the WebGL context.
  renderer.setPixelRatio(Math.min(2, (window.devicePixelRatio || 1) * 1.5));
  resizeForce();
  renderer.render(scene, camera);
  canvas.toBlob((blob) => {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = (CFG.title || 'model').replace(/\W+/g, '_') + '.png';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    renderer.setPixelRatio(prevRatio);
    resizeForce();
    toast('Snapshot saved');
  }, 'image/png');
}

/* -------------------------------------------------------------------- boot */

/**
 * Small diagnostics surface for debugging and automated checks. `renderCount`
 * is the useful one: because rendering is on demand, it should stop advancing
 * entirely once the view settles.
 */
window.iSTP2HTML = {
  state,
  scene,
  camera,
  renderer,
  controls,
  requestRender,
  selectPart,
  fitTo,
  setView,
  setShading,
  setToneMapping,
  stats: () => ({
    renderCount: state.renderCount,
    drawCalls: state.lastCalls,
    visibleTriangles: state.lastTris,
    totalTriangles: state.stats.tris,
    parts: state.parts.length,
    uniqueGeometries: state.stats.geoms,
    msSinceLastRender: Math.round(performance.now() - state.lastRenderAt),
  }),
};

async function boot() {
  // Light is the default because that is what the portal's GLB viewer is; the
  // dark theme stays available from Settings and from --theme.
  applyTheme(CFG.theme || 'light');
  setupEnvironment();
  wireUI();
  setNavMode(state.navMode);      // also performs the first full dock render
  setExplodeAmount(state.explodeValue);
  $('#opt-shading').value = state.shading;
  syncShadedWireUI();
  $('#opt-tone').value = state.tone;
  $('#opt-theme').value = CFG.theme || 'light';
  $('#opt-grid').checked = CFG.showGrid !== false;
  buildModelSwitcher();
  resize();
  tick();
  // The HUD must tick independently of the render loop: with rendering on
  // demand there are no frames at all while the user reads the screen.
  if ($('#hud')) setInterval(updateHud, 500);
  try {
    await loadModel();
    if (state.shading !== 'shaded') setShading(state.shading);
    if (state.shadedWire) await setShadedWire(true);
    if (state.machineTransparent) applyMachineTransparency(true);
    if (state.tone !== 'neutral') setToneMapping(state.tone);
  } catch (err) {
    failLoad(err);
  }
}

boot();
