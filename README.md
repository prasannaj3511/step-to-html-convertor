# iSTP2HTML

Convert STEP (ISO 10303-21) CAD files into optimized HTML viewers that open in
any modern browser, load fast, and keep the geometry faithful to the original
B-rep.

On the sample assembly in `input-stp/`:

| | |
|---|---|
| Input | `13110-PLI1.STEP` — 340.6 MB, 3.8 M entities, SolidWorks 2024 / AP203 |
| Output | **17.7 MB** `.glb` + a 10 KB HTML viewer — or one **24.6 MB** standalone `.html` |
| Reduction | **19×** smaller than the STEP |
| Conversion | ~3 minutes end to end, ~1 GB peak RAM |
| Geometry | 725 unique parts, 5,038 placements, 4.1 M unique / 18.8 M rendered triangles |
| In the browser | **1.6 s** to first frame, **48–54 fps** while orbiting, **0** redraws when idle |

---

## Quick start

```powershell
# one-time setup
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
cd tools; npm install; cd ..

# pick files and settings in a window
.\.venv\Scripts\python.exe -m stp2html --gui

# ...or convert everything in input-stp/ into output-html/
.\.venv\Scripts\python.exe -m stp2html

# view it
cd output-html; python serve.py
```

### The desktop launcher

`--gui` opens a small window for the common case of "convert this batch of
files":

- **Add files…** multi-selects with Ctrl/Shift-click; **Add folder…** pulls in
  every `.step`/`.stp` beneath a directory, recursively.
- The list supports multi-select for removal, and shows each file's size.
- Quality, colour scheme, units, single-file output and the output folder are
  all set from the same window.
- Conversion runs on a worker thread, streaming the same log the CLI prints, so
  a multi-minute job on a large assembly stays responsive.

Everything it does is available from the CLI; it exists so you do not have to
remember flag names.

`pip install -e .` puts a `stp2html` command on the venv's path too, so
`.\.venv\Scripts\stp2html.exe --quality ultra` works as well.

`serve.py` opens the viewer in your browser. A local server is needed because
browsers block `file://` pages from reading the model file — or use
`--single-file` (below) to get one self-contained `.html` that needs no server.

---

## How it works

```
 .STEP ──▶ OCCT XDE ──▶ adaptive ──▶ glTF-binary ──▶ meshopt ──▶ HTML + .glb
          (assembly)   tessellation                  (gltfpack)
```

**1. Read with XDE, not as a flat shape.** `STEPCAFControl_Reader` keeps the
assembly hierarchy, part names and colours. Unique part geometry is stored once
and referenced by many component labels, so the 725 distinct parts in the sample
cover all 5,038 placements. Translators that aren't needed for rendering (GD&T,
materials, saved views, SHUO) are switched off — on a 3.8 M-entity file they
dominate both runtime and peak memory.

**2. Tessellate per part, not globally.** A single linear deflection is wrong for
real assemblies: a value fine enough for an M3 screw buries a large weldment in
triangles, and a value tuned for the weldment turns every fastener into a
faceted lump. Each unique part instead gets a deflection scaled to its own
bounding-box diagonal, clamped to a sane absolute band. Triangulation is cached
on the shared `TShape`, so meshing the unique parts covers every placement.

**3. Export glTF with real mesh reuse.** Faces of one part merge into a single
primitive (one draw call per part rather than one per B-rep face), indices drop
to 16-bit where possible, and OCCT shares the binary accessors between duplicate
meshes — so file size tracks *unique* geometry, not placement count. CAD Z-up is
converted to glTF Y-up, and the unit scale is stated explicitly so a model
authored in inches doesn't silently come out 25.4× too small.

**4. Compress with meshopt.** `gltfpack` quantizes vertices and applies
`EXT_meshopt_compression` — 109 MB → 17.7 MB on the sample. meshopt was chosen
over Draco because it decodes roughly an order of magnitude faster in the
browser, and time-to-first-frame matters more than the last few percent of size.

**5. Repair the colours OCCT drops.** XDE can attach a colour to a *component*
label — "this placement of the shared part is painted differently", the usual way
a recoloured instance is recorded. `RWGltf_CafWriter` resolves only part-level
styles, so those overrides are silently lost. After export the glTF node graph
and the XDE tree are walked in lockstep and the missing colours are applied by
duplicating the affected `mesh` entries, which re-reference the same accessors —
so no geometry is copied. Every name is verified before anything is written; on
a mismatch the file is left exactly as OCCT wrote it.

**6. Ship a self-contained viewer.** three.js is vendored into the output folder
(no CDN), so the result works on an air-gapped machine, an intranet share or a
USB stick.

### Why it stays responsive

- **Render on demand.** A CAD viewer is idle most of the time; drawing thousands
  of meshes at 60 Hz while nothing moves is pure waste. Frames are drawn only
  when something actually changes, so an idle tab costs ~0 GPU.
- **Adaptive resolution.** Pixel ratio drops while you drag and snaps back the
  moment you stop — smooth motion without giving up final sharpness.
- **Shared geometry.** GPU memory tracks unique parts, not placements.
- **Lazy tree.** Branches build their DOM rows on first expand, so a
  5,000-placement assembly doesn't create 5,000 rows up front.

---

## Viewer features

| | |
|---|---|
| Navigate | Free rotate in any direction (LMB), pan (RMB), zoom (wheel), 7 standard views, fit-to-selection |
| Structure | Lazy assembly tree with per-node visibility, colour swatches, name filter |
| Inspect | Click to select, isolate, hide, per-part triangle count and bounding size |
| **Switch models** | When several STEP files are converted together, a **Model** selector in the toolbar swaps between them in place — no page reload, and the previous model's GPU memory is released |
| Section | Live X/Y/Z clipping plane with flip |
| Measure | Point-to-point distance with ΔX/ΔY/ΔZ breakdown |
| Colour | **Shaded** or **Flat** display, plus a tone-mapping selector — see below |
| Presentation | Explode slider, edge overlay, light/dark theme, metalness/roughness/exposure/FOV, PNG snapshot |
| Diagnostics | Live FPS, draw calls, visible vs. total triangles |

Press <kbd>?</kbd> in the viewer for the full shortcut list.

### Seeing the colours the CAD file actually specifies

Shading is not colour-neutral: lighting, metalness and any tone curve all move a
pixel away from the RGB assigned in CAD. Two settings control this.

- **Shading → Flat** renders surfaces unlit with the tone curve off, so what you
  see is *exactly* the authored colour. Verified: a part written as `#FF0000`
  reads back as `#FF0000` in the framebuffer, bit for bit.
- **Tone mapping** defaults to Khronos **Neutral** rather than ACES Filmic. ACES
  looks cinematic but visibly desaturates saturated colours; Neutral keeps them
  close to nominal while still taming highlights. **None** is also available.

Colour handling is verified against a generated corpus covering all three places
a STEP file can attach appearance:

| Where the colour lives | Result |
|---|---|
| Part label | 10/10 exact |
| Sub-shape (per-face) labels | 6/6 exact |
| Component (per-instance) labels | 5/5 exact — needs the repair step above |
| Rendered pixels, flat mode | bit-exact |

---

## Command line

```
python -m stp2html [inputs...] [-o OUTPUT] [options]
```

Inputs may be files or directories; with none given it reads `input-stp/`.

**Quality**

| Preset | Deflection | Use |
|---|---|---|
| `draft` | 4.0e-3 × diagonal | fastest, visibly faceted |
| `medium` | 1.5e-3 | balanced |
| `high` *(default)* | 6.0e-4 | smooth, recommended |
| `ultra` | 2.5e-4 | near-CAD fidelity, large output |

**Frequently used options**

| Option | Effect |
|---|---|
| `--gui` | open the desktop launcher (multi-select + settings) |
| `-q, --quality` | tessellation preset (above) |
| `--single-file` | one portable `.html`, model base64-inlined, no server needed |
| `--color-scheme` | `auto` (default), `neutral`, `palette`, `metal`, `uniform`, `preserve`, `none` |
| `--simplify R` | decimate to ratio R (e.g. `0.5`) for very heavy models |
| `--gpu-instancing` | `EXT_mesh_gpu_instancing` — far fewer draw calls, but repeated parts stop being individually selectable |
| `--position-bits N` | position quantization (default 16 ≈ lossless) |
| `--html-only` | regenerate the viewer from an existing conversion, no re-read |
| `--part-metrics` | add a per-part breakdown to the JSON manifest |
| `--theme` | initial viewer theme (`dark` / `light`) |
| `--units` | normalise to `MM` / `CM` / `M` / `INCH` |

**Where the sample's colours come from.** `13110-PLI1.STEP` contains **zero**
appearance entities — no `COLOUR_RGB`, no `STYLED_ITEM`, nothing (verified by
scanning all 3.8 M entities). That is normal for a SolidWorks AP203 export: the
colours you see in SolidWorks live in the part documents, not in the STEP. So
the colours in the viewer are ours, not the file's.

`--color-scheme auto` therefore adapts to what the file actually carries:

- **File has colours** → authored colours are used as-is; parts that lack one get
  a neutral grey, so the designer's intent stays the thing you see.
- **File has no colours at all** → a single grey would render a 725-part assembly
  as one unreadable blob, so each part gets a distinguishable colour derived from
  a hash of its name (stable across runs).

Authored colours are never overridden — the one exception is `uniform`, which
explicitly forces every part to the same grey. Use `--color-scheme neutral` to
get plain CAD grey everywhere colour is missing, or `preserve` to add nothing.

---

## Output layout

Default (folder) mode:

```
output-html/
  index.html            viewer, opened on the first model
  13110-PLI1.html       per-model viewer (one per STEP file)
  serve.py / serve.cmd  local HTTP server
  models/
    13110-PLI1.glb      meshopt-compressed model
    13110-PLI1.json     manifest: stats, bbox, source metadata, per-part metrics
  assets/
    viewer.js/.css      viewer
    three/              vendored three.js modules
```

With several inputs you get one `.html` per STEP file plus a shared `models/`
folder, and the toolbar's **Model** selector moves between them.

## One HTML file per STEP, with no separate .glb

`--single-file` produces exactly that — a standalone `.html` with nothing beside
it:

```powershell
.\.venv\Scripts\python.exe -m stp2html --single-file
```

The model is base64-inlined as a `data:` URI and every JavaScript module is
turned into a blob URL wired together by a generated import map, because a
relative `import` inside a blob has no path to resolve against. The result opens
by double-clicking — no server, no CORS problem, nothing to copy alongside it.

The trade-offs are real, so pick deliberately:

| | Folder mode (default) | `--single-file` |
|---|---|---|
| Files | `.html` + `models/` + `assets/` | one `.html` |
| Size (this sample) | 17.7 MB `.glb` + 9 KB page | ~24 MB (base64 adds ~33%) |
| Needs a server | yes (`serve.py`) | no — opens from `file://` |
| Loading | streams, with a progress bar | whole file parsed before first paint |
| Several models | shared assets, in-place switching | each file standalone; the switcher navigates between pages |
| Browser caching | model cached separately from the page | re-downloads everything on any change |

Folder mode is the better default for a 17.7 MB model; `--single-file` is the
right answer for emailing one part to someone, or for a machine where you cannot
run a local server.

---

## Input coverage

The converter is schema- and structure-agnostic; nothing about the sample file
is special-cased. Verified against a generated corpus:

| Input | Result |
|---|---|
| AP203 (`CONFIG_CONTROL_DESIGN`) | OK |
| AP214 (`AUTOMOTIVE_DESIGN`) | OK |
| AP242 (`..._MIM_LF`) | OK |
| Millimetre and inch units | OK — normalised, dimensions read back correctly |
| Flat assembly | OK |
| 4-level nested assembly | OK — 8 component links expand to 16 placements |
| Single loose solid, no assembly | OK |
| Open shell / lone surface (no solid) | OK |
| Colours on parts / faces / instances | OK (see the colour table above) |
| No appearance data at all | OK — synthesised palette |
| 340 MB, 3.8 M entities, 5,038 placements | OK — 3 minutes, ~1 GB RAM |

Add more files to `input-stp/` (or point `--gui` at a folder) and they convert
together into one viewer.

### Reproducing these checks

```powershell
# generate the corpus of edge-case STEP files
.\.venv\Scripts\python.exe tools\make_corpus.py tools\corpus
.\.venv\Scripts\python.exe -m stp2html tools\corpus -o corpus-out
cd corpus-out; python serve.py 8139

# in another shell
cd tools
node test_colors.mjs   http://127.0.0.1:8139     # colour fidelity, channel by channel
node test_features.mjs http://127.0.0.1:8139/index.html   # every interactive feature
node test_switch.mjs   http://127.0.0.1:8139/index.html   # model switching + GPU cleanup
node test_viewer.mjs   http://127.0.0.1:8139/index.html shots --headed   # load time, FPS
```

`tools\scan_step_colors.py <file.step>` reports which appearance entities a STEP
file actually contains — useful when a model's colours are not what you expected.

## Requirements

- Python 3.10+ with `cadquery-ocp` (OpenCASCADE 7.9 bindings) and `numpy`
- Node.js, for `gltfpack` and the vendored three.js (`cd tools && npm install`)

Without `gltfpack` the pipeline still works but ships the uncompressed glTF —
roughly 6× larger — and warns.

Peak memory on the 340 MB sample is about 1 GB.

---

## Tuning for very large models

1. Try `--quality medium` first — often visually indistinguishable at assembly
   zoom levels and markedly lighter.
2. `--simplify 0.6 --simplify-error 0.005` decimates while bounding deviation.
3. `--gpu-instancing` collapses repeated placements into GPU instances; on the
   sample that is 5,038 draw calls down to a few hundred. The trade-off is that
   repeated parts are no longer individually selectable.
