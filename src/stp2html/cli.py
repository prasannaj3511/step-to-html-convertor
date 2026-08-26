"""Command-line entry point for iSTP2HTML."""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from . import __version__
from .gltf import PackOptions
from .logutil import error, log, set_quiet
from .meshing import QUALITY_PRESETS
from .pipeline import ConvertOptions, convert_file

STEP_SUFFIXES = {".step", ".stp", ".p21", ".stpz"}
DEFAULT_ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    quality_help = "; ".join(f"{k} ({v.label})" for k, v in QUALITY_PRESETS.items())
    p = argparse.ArgumentParser(
        prog="stp2html",
        description="Convert STEP (ISO 10303-21) CAD files into optimized, "
        "self-contained HTML viewers.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "inputs",
        nargs="*",
        help="STEP files or directories to convert (default: ./input-stp)",
    )
    p.add_argument(
        "-o", "--output", type=Path, default=None,
        help="output directory (default: ./output-html)",
    )
    p.add_argument("-q", "--quality", default="high", choices=list(QUALITY_PRESETS),
                   help=f"tessellation preset - {quality_help}")
    p.add_argument("--color-scheme", default="auto",
                   choices=["auto", "neutral", "palette", "metal", "uniform", "preserve", "none"],
                   help="how to colour parts that carry NO colour in the file. "
                        "auto = neutral grey if the file has any colours, otherwise a "
                        "distinguishable palette; neutral = always grey; palette/metal = "
                        "always synthesise; preserve/none = leave uncoloured. "
                        "Authored colours are never overridden except by 'uniform'")
    p.add_argument("--units", default="MM", choices=["MM", "M", "INCH", "CM"],
                   help="length unit to normalise the model to")
    p.add_argument("--title", default=None, help="title shown in the viewer")

    g = p.add_argument_group("output")
    g.add_argument("--single-file", action="store_true",
                   help="inline every asset into one portable .html (larger, no server needed)")
    g.add_argument("--no-html", action="store_true",
                   help="only produce the optimized .glb + manifest, skip the viewer")
    g.add_argument("--html-only", action="store_true",
                   help="regenerate the viewer from models already in the output "
                        "directory, without re-reading any STEP file")
    g.add_argument("--theme", default="light", choices=["dark", "light"],
                   help="initial viewer theme")
    g.add_argument("--part-metrics", action="store_true",
                   help="include a per-part breakdown in the JSON manifest")
    g.add_argument("--keep-intermediate", action="store_true",
                   help="keep the uncompressed .raw.glb next to the final model")

    o = p.add_argument_group("optimization")
    o.add_argument("--compression", default="cc", choices=["cc", "c", "none"],
                   help="meshopt compression level ('cc' = highest ratio)")
    o.add_argument("--position-bits", type=int, default=16,
                   help="position quantization bits per mesh (16 = effectively lossless)")
    o.add_argument("--normal-bits", type=int, default=10,
                   help="normal quantization bits")
    o.add_argument("--simplify", type=float, default=1.0,
                   help="mesh simplification ratio (1.0 = off, 0.5 = half the triangles)")
    o.add_argument("--simplify-error", type=float, default=0.01,
                   help="maximum simplification deviation as a fraction of mesh size")
    o.add_argument("--gpu-instancing", action="store_true",
                   help="emit EXT_mesh_gpu_instancing - far fewer draw calls, but "
                        "repeated parts stop being individually selectable")
    o.add_argument("--no-pack", action="store_true", help="skip gltfpack entirely")
    o.add_argument("--gltfpack", default=None, help="path to the gltfpack executable")
    o.add_argument("--no-merge-faces", action="store_true",
                   help="keep one primitive per B-rep face (many more draw calls)")
    o.add_argument("--serial", action="store_true", help="disable multi-threading")

    p.add_argument("--gui", action="store_true",
                   help="open the desktop launcher: multi-select STEP files and "
                        "pick quality/colour/output settings, then convert")
    p.add_argument("--quiet", action="store_true", help="suppress progress output")
    p.add_argument("--version", action="version", version=f"iSTP2HTML {__version__}")
    return p


def gather_inputs(raw: list[str]) -> list[Path]:
    if not raw:
        default_dir = DEFAULT_ROOT / "input-stp"
        if not default_dir.is_dir():
            raise SystemExit(f"no inputs given and {default_dir} does not exist")
        raw = [str(default_dir)]

    files: list[Path] = []
    for item in raw:
        path = Path(item).expanduser()
        if path.is_dir():
            files.extend(
                sorted(
                    f for f in path.rglob("*")
                    if f.is_file() and f.suffix.lower() in STEP_SUFFIXES
                )
            )
        elif path.is_file():
            files.append(path)
        else:
            raise SystemExit(f"input not found: {path}")

    if not files:
        raise SystemExit("no STEP files found (looked for .step/.stp/.p21)")
    # De-duplicate while preserving order.
    seen, unique = set(), []
    for f in files:
        key = f.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def load_existing_manifests(out_dir: Path) -> list[dict]:
    """Re-read the manifests written by an earlier conversion run."""
    import json

    found = []
    for js in sorted((out_dir / "models").glob("*.json")):
        try:
            man = json.loads(js.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            error(f"skipping unreadable manifest {js.name}: {exc}")
            continue
        glb = out_dir / man.get("model", {}).get("file", "")
        if not glb.exists():
            error(f"skipping {js.name}: {glb.name} is missing")
            continue
        man["_paths"] = {
            "glb": str(glb),
            "outDir": str(out_dir),
            "assetDir": str(out_dir / "assets"),
            "stem": js.stem,
        }
        found.append(man)
    return found


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    set_quiet(args.quiet)

    out_dir = args.output or (DEFAULT_ROOT / "output-html")

    if args.gui:
        try:
            from .gui import run_gui
        except ImportError as exc:  # tkinter absent on some minimal installs
            error(f"the graphical launcher needs tkinter, which is unavailable: {exc}")
            return 1
        preselected = []
        if args.inputs:
            try:
                preselected = gather_inputs(args.inputs)
            except SystemExit:
                preselected = []
        return run_gui(preselected, out_dir)

    if args.html_only:
        from .html import write_viewer

        manifests = load_existing_manifests(out_dir)
        if not manifests:
            error(f"no converted models found in {out_dir / 'models'}")
            return 1
        log(f"rebuilding viewer for {len(manifests)} model(s) in {out_dir}")
        write_viewer(
            out_dir, manifests,
            title=args.title, single_file=args.single_file, theme=args.theme,
        )
        return 0

    files = gather_inputs(args.inputs)

    pack = PackOptions(
        enabled=not args.no_pack,
        compress="" if args.compression == "none" else args.compression,
        position_bits=args.position_bits,
        normal_bits=args.normal_bits,
        gpu_instancing=args.gpu_instancing,
        simplify=args.simplify,
        simplify_error=args.simplify_error,
        binary=args.gltfpack,
    )
    opts = ConvertOptions(
        quality=args.quality,
        color_scheme=args.color_scheme,
        units=args.units,
        parallel=not args.serial,
        merge_faces=not args.no_merge_faces,
        keep_intermediate=args.keep_intermediate,
        pack=pack,
        emit_html=not args.no_html,
        single_file=args.single_file,
        title=args.title,
        part_metrics=args.part_metrics,
    )

    log(f"iSTP2HTML {__version__}")
    log(f"{len(files)} input file(s) -> {out_dir}")

    manifests, failed = [], 0
    for src in files:
        try:
            manifests.append(convert_file(src, out_dir, opts))
        except Exception as exc:
            failed += 1
            error(f"{src.name}: {exc}")
            traceback.print_exc()

    if opts.emit_html and manifests:
        from .html import write_viewer

        write_viewer(
            out_dir, manifests,
            title=opts.title, single_file=opts.single_file, theme=args.theme,
        )

    if failed:
        error(f"{failed} of {len(files)} file(s) failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
