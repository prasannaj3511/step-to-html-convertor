"""Generate the HTML viewer and vendor its JavaScript dependencies.

Everything is copied locally - no CDN - so the output folder works on an
air-gapped machine, an intranet share, or a USB stick.

Two output shapes are supported:

* folder mode (default): `index.html` + `assets/` + `models/`.  The .glb streams
  in progressively, which is what you want for a 17 MB model.
* `--single-file`: one .html with the model base64-inlined and every JS module
  turned into a blob URL.  It opens straight from `file://` with no server, at
  the cost of ~33% size and no streaming.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
from pathlib import Path

from . import __version__
from .logutil import human_bytes, log, warn

ASSET_DIR = Path(__file__).resolve().parent / "assets"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SINGLE_FILE_TEMPLATE = ASSET_DIR / "template_single_file.html"
DEFAULT_TEMPLATE = ASSET_DIR / "template.html"

# Entry points; their relative imports are followed automatically.
THREE_ENTRIES = [
    "build/three.module.min.js",
    "examples/jsm/controls/TrackballControls.js",
    "examples/jsm/loaders/GLTFLoader.js",
    "examples/jsm/libs/meshopt_decoder.module.js",
    "examples/jsm/environments/RoomEnvironment.js",
]

# Matches the specifier in `import x from 'y'`, `import 'y'`, `export * from 'y'`
# and `import('y')`.  Anchoring on the `from`/`import` keyword immediately before
# the string keeps this robust against minified bundles, where the binding list
# between `import` and `from` can run to many kilobytes.
_IMPORT_RE = re.compile(
    r"""(?P<head>\bfrom\s*|\bimport\s*\(?\s*)(?P<q>['"])(?P<spec>[^'"]+)(?P=q)"""
)


def find_three_package() -> Path | None:
    for root in (PROJECT_ROOT, Path.cwd()):
        for cand in (
            root / "tools" / "node_modules" / "three",
            root / "node_modules" / "three",
        ):
            if (cand / "build" / "three.module.min.js").exists():
                return cand
    return None


def _iter_specifiers(source: str):
    for m in _IMPORT_RE.finditer(source):
        yield m.group("spec")


def _normalize_rel(base: Path, spec: str) -> str:
    """Resolve a relative module specifier against `base` purely lexically.

    `Path.resolve()` is wrong here: these are virtual package-relative paths, and
    resolving them would drag in the real filesystem root.
    """
    norm: list[str] = []
    for part in Path(base, spec).parts:
        if part == ".":
            continue
        if part == "..":
            if norm:
                norm.pop()
            continue
        norm.append(part)
    return Path(*norm).as_posix() if norm else ""


def collect_module_graph(pkg: Path, entries: list[str]) -> dict[str, str]:
    """Return {relative_path: source} for every module reachable from `entries`."""
    out: dict[str, str] = {}
    queue = list(entries)
    while queue:
        rel = queue.pop()
        rel = str(Path(rel).as_posix())
        if rel in out:
            continue
        path = pkg / rel
        if not path.exists():
            warn(f"three.js module missing: {rel}")
            continue
        src = path.read_text(encoding="utf-8")
        out[rel] = src
        base = Path(rel).parent
        for spec in _iter_specifiers(src):
            if spec.startswith("."):
                queue.append(_normalize_rel(base, spec))
    return out


def vendor_three(dest: Path) -> dict[str, str]:
    """Copy the reachable three.js module graph into `dest`. Returns the graph."""
    pkg = find_three_package()
    if pkg is None:
        raise SystemExit(
            "three.js was not found. Run `npm install three` inside "
            f"{PROJECT_ROOT / 'tools'} and convert again."
        )
    graph = collect_module_graph(pkg, THREE_ENTRIES)
    for rel, src in graph.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(src, encoding="utf-8")
    total = sum(len(s.encode("utf-8")) for s in graph.values())
    log(f"vendored {len(graph)} three.js module(s) ({human_bytes(total)}) from {pkg}")
    return graph


IMPORT_MAP = {
    "imports": {
        "three": "./assets/three/build/three.module.min.js",
        "three/addons/": "./assets/three/examples/jsm/",
    }
}


def _render(template: str, mapping: dict[str, str]) -> str:
    for key, value in mapping.items():
        template = template.replace("{{" + key + "}}", value)
    return template


def _single_file_css(css: str) -> str:
    """Return the inline stylesheet for the legacy-style single-file shell."""
    return css + """

body.legacy-single-file #app {
  position: fixed;
  inset: 0;
  display: flex;
  flex-direction: column;
}

body.legacy-single-file #viewercontainer {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  overflow: hidden;
}

body.legacy-single-file #viewercontainer.row {
  margin-left: 0;
  margin-right: 0;
}

body.legacy-single-file #viewerTools {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
}

body.legacy-single-file #treeview {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
}

body.legacy-single-file #canvas_picker,
body.legacy-single-file #svgWrapper,
body.legacy-single-file #visibilityMenu,
body.legacy-single-file #measureAlert,
body.legacy-single-file #commentPanel,
body.legacy-single-file #measureControls,
body.legacy-single-file #clipControls {
  position: absolute;
  z-index: 7;
}

body.legacy-single-file #canvas_picker,
body.legacy-single-file #svgWrapper {
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
}

body.legacy-single-file #svgWrapper {
  display: none;
}

body.legacy-single-file #visibilityMenu,
body.legacy-single-file #commentPanel,
body.legacy-single-file #measureControls,
body.legacy-single-file #clipControls {
  display: none;
}

body.legacy-single-file #measureAlert {
  right: 10px;
  bottom: 10px;
  min-width: 220px;
  max-width: 320px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-elev);
  color: var(--text);
  box-shadow: var(--shadow);
}

body.legacy-single-file #measureAlert strong {
  display: block;
  margin-bottom: 6px;
}

body.legacy-single-file #bomModal {
  display: none;
}

body.legacy-single-file #viewer {
  flex: 1 1 auto;
  position: relative;
  min-width: 0;
  min-height: 0;
  background: #ffffff;
  border-radius: 32px;
  overflow: hidden;
}

body.legacy-single-file #glcanvas {
  display: block;
  width: 100%;
  height: 100%;
  touch-action: none;
  background: #ffffff;
}

body.legacy-single-file #viewerTools .tb-group {
  display: flex;
  align-items: center;
  gap: 2px;
}

body.legacy-single-file #viewerTools .tb-sep {
  width: 1px;
  height: 22px;
  background: var(--border);
  margin: 0 4px;
}

body.legacy-single-file #viewerTools .spacer {
  flex: 1 1 auto;
}

body.legacy-single-file {
  --bg: #f7f7f5;
  --bg-elev: rgba(255, 255, 255, 0.96);
  --bg-elev-2: #ffffff;
  --border: #e2e2de;
  --text: #2e2e2c;
  --text-dim: #686865;
  --text-faint: #9a9a96;
  --accent: #1d99d6;
  --accent-dim: #1d99d6;
  --shadow: 0 18px 42px rgba(27, 39, 51, 0.12);
  font-family: Poppins, "Avenir Next", "Segoe UI", sans-serif;
  background:
    radial-gradient(circle at 14% 12%, rgba(29, 153, 214, 0.08), transparent 18%),
    linear-gradient(180deg, #ffffff 0%, #f8f8f6 82%, #f3f3ef 100%);
  color: var(--text);
}

body.legacy-single-file #app {
  padding: 8px 18px 68px;
}

body.legacy-single-file #topbar {
  position: fixed;
  top: 8px;
  left: 18px;
  right: 18px;
  height: 56px;
  padding: 0;
  background: transparent;
  border-bottom: none;
  pointer-events: none;
}

body.legacy-single-file #viewerTools {
  position: relative;
  height: 56px;
  pointer-events: none;
}

body.legacy-single-file #viewerTools > * {
  pointer-events: auto;
}

body.legacy-single-file #topbar .brand {
  position: absolute;
  left: 50%;
  top: 18px;
  transform: translateX(-50%);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  text-align: center;
}

body.legacy-single-file #brand-title {
  font-size: 20px;
  font-weight: 700;
  letter-spacing: -0.03em;
}

body.legacy-single-file #topbar .brand small {
  font-size: 11px;
  color: var(--text-dim);
  font-weight: 500;
}

body.legacy-single-file .tb#btn-sidebar {
  position: absolute;
  left: 6px;
  top: 16px;
  width: 50px;
  height: 50px;
  padding: 0;
  border: none;
  border-radius: 14px;
  background: linear-gradient(180deg, #2ba9ea 0%, #1d99d6 100%);
  color: #fff;
  box-shadow: 0 12px 28px rgba(29, 153, 214, 0.32);
  font-size: 0;
}

body.legacy-single-file .tb#btn-sidebar::before {
  content: "\\2630";
  font-size: 22px;
  line-height: 50px;
}

body.legacy-single-file .tb#btn-help {
  position: absolute;
  right: 0;
  top: 18px;
  width: 44px;
  height: 44px;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: var(--text-dim);
  font-size: 0;
  box-shadow: none;
}

body.legacy-single-file .tb#btn-help::before {
  content: "\\00d7";
  font-size: 34px;
  line-height: 44px;
}

body.legacy-single-file .tb#btn-help:hover {
  background: rgba(0, 0, 0, 0.04);
  color: var(--text);
}

body.legacy-single-file #viewerTools .tb-sep,
body.legacy-single-file #viewerTools #model-switch,
body.legacy-single-file #viewerTools .spacer,
body.legacy-single-file #viewerTools .tb-group {
  position: static;
}

body.legacy-single-file #viewerTools .tools-primary,
body.legacy-single-file #viewerTools .tools-secondary,
body.legacy-single-file #viewerTools .tools-tertiary {
  position: fixed;
  left: 50%;
  transform: translateX(-50%);
  bottom: 18px;
  background: rgba(255, 255, 255, 0.94);
  border: 1px solid rgba(225, 225, 220, 0.95);
  border-radius: 22px;
  box-shadow: 0 12px 30px rgba(21, 31, 43, 0.12);
  padding: 8px;
  gap: 6px;
  backdrop-filter: blur(14px);
  z-index: 12;
  width: max-content;
  max-width: calc(100vw - 48px);
}

body.legacy-single-file #viewerTools .tools-primary {
  margin-left: -386px;
}

body.legacy-single-file #viewerTools .tools-secondary {
  margin-left: 0;
}

body.legacy-single-file button.tb,
body.legacy-single-file .tb {
  min-width: 56px;
  min-height: 56px;
  padding: 12px 16px;
  border: 1px solid rgba(224, 224, 219, 0.95);
  border-radius: 22px;
  background: #fff;
  color: var(--text);
  box-shadow: 0 4px 10px rgba(0, 0, 0, 0.05);
  font-size: 15px;
  font-weight: 600;
}

body.legacy-single-file button.tb:hover,
body.legacy-single-file .tb:hover {
  background: #f7fbfe;
  border-color: #bfe3f6;
}

body.legacy-single-file button.tb.active,
body.legacy-single-file .tb.active {
  background: linear-gradient(180deg, #2ba9ea 0%, #1d99d6 100%);
  border-color: #1d99d6;
  color: #fff;
}

/* Emoji glyphs for the legacy shell's top-bar buttons.

   Every selector below is scoped to `.tb` on purpose. These rules key on ids
   alone, and several of those ids (#btn-home, #btn-theme-chip, #btn-part-chip,
   #btn-isolate) now belong to the bottom dock, which draws real SVG icons. An
   unscoped `#btn-home { font-size: 0 }` reaches straight into the dock and
   silently blanks its labels, so the `.tb` keeps these aimed at the top bar and
   nowhere else. */
body.legacy-single-file .tb#btn-fit::before { content: "\\2302"; }
body.legacy-single-file .tb#btn-home::before { content: "\\2302"; }
body.legacy-single-file .tb#btn-pan::before { content: "\\2725"; }
body.legacy-single-file .tb#btn-expand::before { content: "\\2197"; }
body.legacy-single-file .tb#btn-isolate::before { content: "\\29c9"; }
body.legacy-single-file .tb#btn-hide::before { content: "\\2297"; }
body.legacy-single-file .tb#btn-showall::before { content: "\\2637"; }
body.legacy-single-file .tb#btn-edges::before { content: "\\25a3"; }
body.legacy-single-file .tb#btn-section::before { content: "\\2014"; }
body.legacy-single-file .tb#btn-measure::before { content: "\\2194"; }
body.legacy-single-file .tb#btn-snap::before { content: "\\29c9"; }
body.legacy-single-file .tb#btn-info::before { content: "\\2611"; }
body.legacy-single-file .tb#btn-settings::before { content: "\\22ee"; }
body.legacy-single-file .tb#btn-theme-chip::before { content: "\\1f4a7"; }
body.legacy-single-file .tb#btn-part-chip::before { content: "\\1f4a7"; }

body.legacy-single-file .tb#btn-fit,
body.legacy-single-file .tb#btn-home,
body.legacy-single-file .tb#btn-pan,
body.legacy-single-file .tb#btn-expand,
body.legacy-single-file .tb#btn-isolate,
body.legacy-single-file .tb#btn-hide,
body.legacy-single-file .tb#btn-showall,
body.legacy-single-file .tb#btn-edges,
body.legacy-single-file .tb#btn-section,
body.legacy-single-file .tb#btn-measure,
body.legacy-single-file .tb#btn-snap,
body.legacy-single-file .tb#btn-info,
body.legacy-single-file .tb#btn-settings,
body.legacy-single-file .tb#btn-theme-chip,
body.legacy-single-file .tb#btn-part-chip {
  font-size: 0;
}

body.legacy-single-file .tb#btn-fit::before,
body.legacy-single-file .tb#btn-home::before,
body.legacy-single-file .tb#btn-pan::before,
body.legacy-single-file .tb#btn-expand::before,
body.legacy-single-file .tb#btn-isolate::before,
body.legacy-single-file .tb#btn-hide::before,
body.legacy-single-file .tb#btn-showall::before,
body.legacy-single-file .tb#btn-edges::before,
body.legacy-single-file .tb#btn-section::before,
body.legacy-single-file .tb#btn-measure::before,
body.legacy-single-file .tb#btn-snap::before,
body.legacy-single-file .tb#btn-info::before,
body.legacy-single-file .tb#btn-settings::before,
body.legacy-single-file .tb#btn-theme-chip::before,
body.legacy-single-file .tb#btn-part-chip::before {
  font-size: 22px;
  line-height: 1;
}

body.legacy-single-file .tb#btn-home,
body.legacy-single-file .tb#btn-pan,
body.legacy-single-file .tb#btn-expand,
body.legacy-single-file .tb#btn-theme-chip,
body.legacy-single-file .tb#btn-part-chip,
body.legacy-single-file .tb#btn-isolate,
body.legacy-single-file .tb#btn-section,
body.legacy-single-file .tb#btn-measure {
  font-size: 15px;
}

body.legacy-single-file .tb#btn-home::before,
body.legacy-single-file .tb#btn-pan::before,
body.legacy-single-file .tb#btn-expand::before,
body.legacy-single-file .tb#btn-theme-chip::before,
body.legacy-single-file .tb#btn-part-chip::before,
body.legacy-single-file .tb#btn-section::before,
body.legacy-single-file .tb#btn-measure::before {
  margin-right: 8px;
  vertical-align: middle;
}

body.legacy-single-file .tb#btn-home,
body.legacy-single-file .tb#btn-pan,
body.legacy-single-file .tb#btn-expand,
body.legacy-single-file .tb#btn-theme-chip,
body.legacy-single-file .tb#btn-part-chip,
body.legacy-single-file .tb#btn-isolate,
body.legacy-single-file .tb#btn-section,
body.legacy-single-file .tb#btn-measure {
  min-width: 118px;
}

body.legacy-single-file .tb#btn-theme-chip {
  color: var(--accent);
}

body.legacy-single-file .tb#btn-part-chip,
body.legacy-single-file .tb#btn-isolate {
  background: #e4e4e4;
  border-color: #dddddd;
  color: #8e8e8e;
  box-shadow: none;
}

body.legacy-single-file .tb#btn-fit,
body.legacy-single-file .tb#btn-info,
body.legacy-single-file .tb#btn-help,
body.legacy-single-file .tb#btn-settings,
body.legacy-single-file .tb#btn-sidebar,
body.legacy-single-file .tb#btn-snap,
body.legacy-single-file .tb#btn-edges,
body.legacy-single-file .tb#btn-hide,
body.legacy-single-file .tb#btn-showall {
  min-width: 56px;
}

body.legacy-single-file .tb#btn-help,
body.legacy-single-file .tb#btn-settings,
body.legacy-single-file .tb#btn-sidebar {
  width: 56px;
  height: 56px;
  padding: 0;
  border-radius: 22px;
  background: #fff;
  box-shadow: 0 4px 10px rgba(0, 0, 0, 0.05);
  color: var(--accent);
  border: 1px solid rgba(224, 224, 219, 0.95);
}

body.legacy-single-file .tb#btn-info,
body.legacy-single-file .tb#btn-help,
body.legacy-single-file .tb#btn-settings {
  position: static;
  min-width: 56px;
  width: 56px;
  height: 56px;
  padding: 0;
  font-size: 0;
}

body.legacy-single-file .tb#btn-info::before,
body.legacy-single-file .tb#btn-help::before,
body.legacy-single-file .tb#btn-settings::before {
  display: block;
  text-align: center;
  line-height: 56px;
}

body.legacy-single-file .tb#btn-help::before {
  content: "?";
  font-size: 24px;
  line-height: 1;
}

body.legacy-single-file .tb#btn-sidebar {
  position: fixed;
  left: 18px;
  top: 18px;
  z-index: 16;
}

body.legacy-single-file .tb#btn-sidebar::before {
  content: "\\2630";
  font-size: 24px;
  line-height: 1;
}

body.legacy-single-file .tb#btn-settings::before {
  font-size: 28px;
}

body.legacy-single-file #viewercontainer {
  position: fixed;
  inset: 72px 14px 18px 14px;
  display: block;
}

body.legacy-single-file #sidebar {
  position: absolute;
  top: 4px;
  left: 4px;
  bottom: 118px;
  width: 494px;
  border: 1px solid rgba(222, 222, 218, 0.95);
  border-radius: 18px;
  background: rgba(255, 255, 255, 0.96);
  box-shadow: var(--shadow);
  overflow: hidden;
  z-index: 10;
}

body.legacy-single-file #sidebar.collapsed {
  margin-left: 0;
  transform: translateX(-110%);
  transition: transform 0.22s ease;
}

body.legacy-single-file #treeview {
  padding: 30px 22px 14px;
}

body.legacy-single-file .legacy-tabs {
  display: flex;
  align-items: center;
  gap: 24px;
  padding-bottom: 10px;
  border-bottom: 1px solid #dadad4;
  margin-bottom: 14px;
  overflow: visible;
}

body.legacy-single-file .legacy-tab {
  border: none;
  background: transparent;
  color: #333432;
  font-size: 15px;
  font-weight: 700;
  line-height: 1.2;
  padding: 0 0 8px;
  border-bottom: 3px solid transparent;
  border-radius: 0;
  box-shadow: none;
  min-width: 0;
  min-height: 0;
  white-space: nowrap;
}

body.legacy-single-file .legacy-tab.active {
  color: #232321;
  border-bottom-color: var(--accent);
}

body.legacy-single-file .legacy-side-title {
  font-size: 18px;
  font-weight: 800;
  margin-bottom: 8px;
}

body.legacy-single-file .side-head {
  padding: 0 0 10px;
  border-bottom: none;
}

body.legacy-single-file .side-head input[type="search"] {
  height: 42px;
  border-radius: 14px;
  background: #ffffff;
  border-color: #e7e7e3;
  padding: 0 14px;
  font-family: inherit;
}

body.legacy-single-file #tree-wrap {
  padding: 2px 2px 10px;
}

body.legacy-single-file .trow {
  gap: 10px;
  padding: 8px 10px;
  border-radius: 16px;
  margin-bottom: 2px;
}

body.legacy-single-file .trow.selected {
  background: linear-gradient(180deg, #2ba9ea 0%, #1d99d6 100%);
  box-shadow: 0 12px 24px rgba(29, 153, 214, 0.22);
}

body.legacy-single-file .tswatch {
  display: none;
}

body.legacy-single-file .tname {
  font-size: 15px;
  font-weight: 700;
  line-height: 1.2;
}

body.legacy-single-file .tcount {
  display: none;
}

body.legacy-single-file #viewer {
  position: absolute;
  inset: 0;
}

body.legacy-single-file #glcanvas {
  border-radius: 0;
  background: transparent;
}

body.legacy-single-file #overlay-tl {
  left: 22px;
  bottom: 94px;
  top: auto;
}

body.legacy-single-file #overlay-br {
  right: 22px;
  bottom: 104px;
}

body.legacy-single-file #hud,
body.legacy-single-file #axis-note {
  opacity: 0.85;
}

body.legacy-single-file #viewcube {
  top: auto;
  right: auto;
  left: 18px;
  bottom: 42px;
  grid-template-columns: repeat(3, 18px);
  gap: 2px;
}

body.legacy-single-file #viewcube button {
  width: 18px;
  height: 18px;
  padding: 0;
  border: none;
  background: transparent;
  color: transparent;
  box-shadow: none;
}

body.legacy-single-file #viewcube button:nth-child(1) { border-bottom: 2px solid #405cff; }
body.legacy-single-file #viewcube button:nth-child(2) { border-left: 2px solid #3ebd66; }
body.legacy-single-file #viewcube button:nth-child(3) { border-right: 2px solid #ff6b6b; }

body.legacy-single-file .panel {
  background: rgba(255, 255, 255, 0.96);
  border: 1px solid rgba(224, 224, 219, 0.95);
  color: var(--text);
  border-radius: 20px;
}

body.legacy-single-file #panel-settings,
body.legacy-single-file #panel-section,
body.legacy-single-file #panel-help {
  top: 82px;
  right: 22px;
}

body.legacy-single-file #panel-info {
  left: auto;
  right: 22px;
  bottom: 112px;
}

body.legacy-single-file #loader {
  background: rgba(247, 247, 245, 0.94);
}

body.legacy-single-file #toast {
  bottom: 96px;
  background: rgba(255, 255, 255, 0.96);
  color: var(--text);
  border-color: rgba(224, 224, 219, 0.95);
}

body.legacy-single-file .legacy-disclaimer {
  position: fixed;
  left: 18px;
  right: 120px;
  bottom: 8px;
  color: #7f7f7a;
  font-size: 12px;
  text-align: left;
  pointer-events: none;
}

@media (max-width: 1180px) {
  body.legacy-single-file #sidebar {
    width: 380px;
  }

  body.legacy-single-file #viewerTools .tools-primary {
    margin-left: -296px;
  }

  body.legacy-single-file #viewerTools .tools-tertiary {
    margin-left: 304px;
  }
}

@media (max-width: 900px) {
  body.legacy-single-file #sidebar {
    left: 0;
    width: min(88vw, 360px);
    top: 0;
    bottom: 96px;
  }

  body.legacy-single-file #topbar .brand {
    top: 18px;
    width: calc(100% - 120px);
  }

  body.legacy-single-file #brand-title {
    font-size: 16px;
  }

  body.legacy-single-file #viewerTools .tools-primary,
  body.legacy-single-file #viewerTools .tools-secondary,
  body.legacy-single-file #viewerTools .tools-tertiary {
    left: 50%;
    margin-left: 0;
    transform: translateX(-50%) scale(0.92);
    bottom: 12px;
  }

}
"""


def _model_entry(manifest: dict, *, page: str, single_file: bool) -> dict:
    """One row for the viewer's model switcher."""
    stats = manifest.get("stats", {})
    model = manifest.get("model", {})
    return {
        "name": manifest["source"]["file"],
        "title": manifest["source"]["file"],
        "subtitle": _subtitle(manifest),
        "page": page,
        # A single-file page has its model inlined and cannot fetch a sibling
        # .glb, so it navigates to the other page instead of swapping in place.
        "model": None if single_file else model.get("file"),
        "units": (model.get("units") or "MM").lower(),
        "unitScale": model.get("unitScale", 1000.0),
        "meta": {
            "source": manifest.get("source", {}),
            "model": model,
            "stats": stats,
        },
    }


def _viewer_config(
    manifest: dict,
    *,
    model_href: str,
    title: str,
    theme: str,
    show_grid: bool = True,
    shading: str = "shaded",
) -> dict:
    stats = manifest.get("stats", {})
    return {
        "model": model_href,
        "title": title,
        "units": (manifest.get("model", {}).get("units") or "MM").lower(),
        # glTF stores metres; the viewer multiplies by this before showing any
        # length so dimensions read in the model's own CAD unit.
        "unitScale": manifest.get("model", {}).get("unitScale", 1000.0),
        "theme": theme,
        "showGrid": show_grid,
        "shading": shading,
        "meta": {
            "source": manifest.get("source", {}),
            "model": manifest.get("model", {}),
            "stats": {
                "uniqueParts": stats.get("uniqueParts"),
                "placements": stats.get("placements"),
                "instances": stats.get("instances"),
                "quality": stats.get("quality"),
                "uniqueTriangles": stats.get("uniqueTriangles"),
                "renderedTriangles": stats.get("renderedTriangles"),
                "conversionSeconds": stats.get("conversionSeconds"),
            },
        },
    }


def _subtitle(manifest: dict) -> str:
    st = manifest.get("stats", {})
    parts = st.get("uniqueParts") or 0
    placements = st.get("placements") or st.get("instances") or 0
    tris = st.get("renderedTriangles") or 0
    return (
        f"{parts:,} parts &middot; {placements:,} placements &middot; "
        f"{tris / 1e6:.1f}M triangles"
    )


def _display_title(title: str) -> str:
    text = str(title or "").strip()
    lower = text.lower()
    for suffix in (".step", ".stp", ".p21"):
        if lower.endswith(suffix):
            return text[: -len(suffix)]
    return text


def _escape(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_page(
    manifest: dict,
    *,
    title: str,
    single_file: bool,
    graph: dict[str, str] | None,
    css: str,
    js: str,
    template: str,
    out_dir: Path,
    theme: str = "light",
    models: list[dict] | None = None,
    model_index: int = 0,
) -> str:
    glb_rel = manifest["model"]["file"]
    display_title = _display_title(title)

    if single_file:
        glb_path = out_dir / glb_rel
        raw = glb_path.read_bytes()
        model_href = "data:model/gltf-binary;base64," + base64.b64encode(raw).decode("ascii")
        style = f"<style>\n{_single_file_css(css)}\n</style>"
        importmap = _blob_bootstrap(graph or {}, js)
        script = ""
        page_theme = "light"
        show_grid = False
    else:
        model_href = glb_rel
        style = '<link rel="stylesheet" href="assets/viewer.css">'
        importmap = (
            '<script type="importmap">\n'
            + json.dumps(IMPORT_MAP, indent=2)
            + "\n</script>"
        )
        script = '<script type="module" src="assets/viewer.js"></script>'
        page_theme = theme
        show_grid = True

    cfg = _viewer_config(
        manifest,
        model_href=model_href,
        title=display_title,
        theme=page_theme,
        show_grid=show_grid,
    )
    if models and len(models) > 1:
        cfg["models"] = models
        cfg["modelIndex"] = model_index
    config_tag = (
        "<script>window.__ISTP2HTML__ = "
        + json.dumps(cfg, separators=(",", ":"))
        + ";</script>"
    )

    return _render(
        template,
        {
            "VERSION": __version__,
            "TITLE": _escape(display_title),
            "TITLE_HTML": _escape(display_title),
            "SUBTITLE": _subtitle(manifest),
            "UNITS": _escape((manifest.get("model", {}).get("units") or "MM").lower()),
            "STYLE": style,
            "CONFIG": config_tag,
            "IMPORTMAP": importmap,
            "SCRIPT": script,
        },
    )


def _rewrite_relative_specifiers(rel: str, src: str) -> str:
    """Turn every relative import into a `vfs:` bare specifier.

    Blob URLs carry no path, so a relative import inside a blob-hosted module has
    nothing to resolve against.  Rewriting them to flat, absolute keys that the
    import map knows about is what makes single-file mode work.
    """
    base = Path(rel).parent

    def repl(m: re.Match) -> str:
        spec = m.group("spec")
        if not spec.startswith("."):
            return m.group(0)
        key = "vfs:" + _normalize_rel(base, spec)
        return f"{m.group('head')}{m.group('q')}{key}{m.group('q')}"

    return _IMPORT_RE.sub(repl, src)


def _blob_bootstrap(graph: dict[str, str], viewer_js: str) -> str:
    """Emit a script that turns the module graph into blob URLs + an import map."""
    modules = {
        rel: _rewrite_relative_specifiers(rel, src) for rel, src in graph.items()
    }
    modules["__viewer__.js"] = _rewrite_relative_specifiers("__viewer__.js", viewer_js)

    payload = json.dumps(modules)
    aliases = json.dumps(
        {
            "three": "build/three.module.min.js",
            "three/addons/controls/TrackballControls.js": "examples/jsm/controls/TrackballControls.js",
            "three/addons/loaders/GLTFLoader.js": "examples/jsm/loaders/GLTFLoader.js",
            "three/addons/libs/meshopt_decoder.module.js": "examples/jsm/libs/meshopt_decoder.module.js",
            "three/addons/environments/RoomEnvironment.js": "examples/jsm/environments/RoomEnvironment.js",
        }
    )

    return f"""<script>
(function () {{
  var SOURCES = {payload};
  var ALIASES = {aliases};
  var urls = {{}};
  for (var rel in SOURCES) {{
    urls[rel] = URL.createObjectURL(new Blob([SOURCES[rel]], {{ type: 'text/javascript' }}));
  }}
  var imports = {{}};
  for (var rel in urls) imports['vfs:' + rel] = urls[rel];
  for (var alias in ALIASES) imports[alias] = urls[ALIASES[alias]];

  // The import map must be in the document before the first module evaluates.
  var im = document.createElement('script');
  im.type = 'importmap';
  im.textContent = JSON.stringify({{ imports: imports }});
  document.currentScript.after(im);

  var entry = document.createElement('script');
  entry.type = 'module';
  entry.src = urls['__viewer__.js'];
  im.after(entry);
}})();
</script>"""


SERVE_PY = '''#!/usr/bin/env python3
"""Serve this folder over HTTP so the browser will load the model.

Opening index.html straight from disk trips the browser's file:// security
rules, which block reading the .glb. Running this script sidesteps that.

    python serve.py            # then open the printed address
    python serve.py 9000       # use a specific port
"""
import http.server
import os
import socketserver
import sys
import webbrowser

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
os.chdir(os.path.dirname(os.path.abspath(__file__)))


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".glb": "model/gltf-binary",
        ".gltf": "model/gltf+json",
        ".js": "text/javascript",
        ".mjs": "text/javascript",
    }

    def end_headers(self):
        # These let the browser cache the (immutable) model between reloads.
        self.send_header("Cache-Control", "public, max-age=3600")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\\n" % (fmt % args))


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


with Server(("127.0.0.1", PORT), Handler) as httpd:
    url = f"http://127.0.0.1:{PORT}/"
    print(f"iSTP2HTML viewer serving at {url}")
    print("Press Ctrl+C to stop.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\\nstopped")
'''

SERVE_CMD = """@echo off
REM Convenience launcher for the viewer on Windows.
python "%~dp0serve.py" %*
"""


def write_viewer(
    out_dir: Path,
    manifests: list[dict],
    *,
    title: str | None = None,
    single_file: bool = False,
    theme: str = "light",
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    template_path = SINGLE_FILE_TEMPLATE if single_file else DEFAULT_TEMPLATE
    template = template_path.read_text(encoding="utf-8")
    css = (ASSET_DIR / "viewer.css").read_text(encoding="utf-8")
    js = (ASSET_DIR / "viewer.js").read_text(encoding="utf-8")

    graph: dict[str, str] | None = None
    if single_file:
        pkg = find_three_package()
        if pkg is None:
            raise SystemExit("three.js not found; cannot build a single-file viewer")
        graph = collect_module_graph(pkg, THREE_ENTRIES)
    else:
        assets = out_dir / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        vendor_three(assets / "three")
        shutil.copy2(ASSET_DIR / "viewer.css", assets / "viewer.css")
        shutil.copy2(ASSET_DIR / "viewer.js", assets / "viewer.js")
        (out_dir / "serve.py").write_text(SERVE_PY, encoding="utf-8")
        (out_dir / "serve.cmd").write_text(SERVE_CMD, encoding="utf-8")

    # Every page carries the whole model list so the viewer's switcher can move
    # between them without going back to an index page.
    model_list = [
        _model_entry(man, page=f"{man['_paths']['stem']}.html", single_file=single_file)
        for man in manifests
    ]

    pages: list[tuple[str, dict]] = []
    for idx, man in enumerate(manifests):
        stem = man["_paths"]["stem"]
        page_title = title or man["source"]["file"]
        html = build_page(
            man,
            title=page_title,
            single_file=single_file,
            graph=graph,
            css=css,
            js=js,
            template=template,
            out_dir=out_dir,
            theme=theme,
            models=model_list,
            model_index=idx,
        )
        name = f"{stem}.html"
        (out_dir / name).write_text(html, encoding="utf-8")
        pages.append((name, man))
        log(f"wrote {name} ({human_bytes(len((out_dir / name).read_bytes()))})")

    # index.html is always a real viewer, opened on the first model. With more
    # than one model the in-page switcher moves between them, so there is no
    # separate landing page to click through.
    shutil.copy2(out_dir / pages[0][0], out_dir / "index.html")

    log(f"viewer ready: {out_dir / 'index.html'}")
    if len(pages) > 1:
        log(f"  {len(pages)} models available from the 'Model' selector in the toolbar")
    if not single_file:
        log("open it via:  python serve.py   (inside the output folder)")
