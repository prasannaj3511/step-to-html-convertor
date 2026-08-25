"""End-to-end conversion pipeline: STEP file -> optimized HTML viewer."""

from __future__ import annotations

import json
import platform
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .gltf import UNIT_TO_METRES, PackOptions, optimize_glb, write_glb
from .logutil import human_bytes, human_int, log, phase
from .meshing import QUALITY_PRESETS, Quality, scene_bbox, tessellate
from .recolor import patch_instance_colors
from .scene import apply_colors, collect_parts
from .step_io import load_step, read_header


@dataclass
class ConvertOptions:
    quality: str = "high"
    color_scheme: str = "auto"
    units: str = "MM"
    parallel: bool = True
    merge_faces: bool = True
    z_up: bool = True
    keep_intermediate: bool = False
    pack: PackOptions = field(default_factory=PackOptions)
    emit_html: bool = True
    single_file: bool = False
    title: str | None = None
    part_metrics: bool = False


def _quality(name: str) -> Quality:
    try:
        return QUALITY_PRESETS[name]
    except KeyError:
        raise SystemExit(
            f"unknown quality '{name}'; choose one of {', '.join(QUALITY_PRESETS)}"
        )


def convert_file(src: Path, out_dir: Path, opts: ConvertOptions) -> dict:
    """Convert one STEP file. Returns a manifest dict describing the result."""
    t_start = time.time()
    stem = src.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    asset_dir = out_dir / "assets"
    model_dir = out_dir / "models"
    model_dir.mkdir(parents=True, exist_ok=True)

    quality = _quality(opts.quality)
    src_bytes = src.stat().st_size

    log("=" * 72)
    log(f"converting {src.name} ({human_bytes(src_bytes)})")
    log("=" * 72)

    header = read_header(src)
    if header.originating_system or header.preprocessor:
        log(f"authored by: {header.originating_system or '?'} / {header.preprocessor or '?'}")
    if header.schema:
        log(f"schema: {header.schema}")

    with phase("STEP parse + shape transfer"):
        doc = load_step(src, units=opts.units)

    with phase("scene analysis"):
        info = collect_parts(doc)
        if not info.parts:
            raise RuntimeError(f"no solid geometry found in {src.name}")
        apply_colors(doc, info, opts.color_scheme)

    with phase("tessellation"):
        mesh_stats = tessellate(info, quality, parallel=opts.parallel)

    raw_glb = model_dir / f"{stem}.raw.glb"
    final_glb = model_dir / f"{stem}.glb"

    file_info = {
        "Generator": f"iSTP2HTML {__version__}",
        "SourceFile": src.name,
        "SourceSystem": header.originating_system,
        "SourcePreprocessor": header.preprocessor,
        "SourceTimestamp": header.timestamp,
        "Schema": header.schema,
        "Quality": quality.name,
    }

    with phase("glTF export"):
        write_glb(
            doc,
            raw_glb,
            file_info=file_info,
            merge_faces=opts.merge_faces,
            parallel=opts.parallel,
            z_up=opts.z_up,
            units=opts.units,
        )
        # OCCT resolves only part-level styles, so any instance that was
        # recoloured in CAD would otherwise come out wearing the part's colour.
        patch_instance_colors(raw_glb, doc)

    with phase("mesh optimization"):
        _, pack_stats = optimize_glb(raw_glb, final_glb, opts.pack)

    if not opts.keep_intermediate and raw_glb.exists() and raw_glb != final_glb:
        raw_glb.unlink()

    bbox = scene_bbox(info)
    elapsed = time.time() - t_start

    manifest = {
        "generator": f"iSTP2HTML {__version__}",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": {
            "file": src.name,
            "bytes": src_bytes,
            "schema": header.schema,
            "originatingSystem": header.originating_system,
            "preprocessor": header.preprocessor,
            "timestamp": header.timestamp,
            "name": header.name,
        },
        "model": {
            "file": f"models/{final_glb.name}",
            "bytes": final_glb.stat().st_size,
            "units": opts.units,
            # glTF geometry is in metres; multiply by this to read values back in
            # the CAD unit above (1000 for mm).
            "unitScale": 1.0 / UNIT_TO_METRES.get(opts.units.upper(), 0.001),
            "upAxis": "Y",
            "compressed": pack_stats.get("packed", False),
        },
        "stats": {
            "uniqueParts": len(info.parts),
            "instances": info.instance_count,
            "placements": info.placements,
            "assemblies": info.assembly_count,
            "roots": info.free_shapes,
            "faces": sum(p.faces for p in info.parts),
            "uniqueTriangles": mesh_stats["unique_triangles"],
            "renderedTriangles": mesh_stats["rendered_triangles"],
            "uniqueVertices": mesh_stats["unique_vertices"],
            "meshFailures": mesh_stats["failures"],
            "quality": quality.name,
            "conversionSeconds": round(elapsed, 1),
            "rawGlbBytes": pack_stats.get("input_bytes", 0),
            "finalGlbBytes": pack_stats.get("output_bytes", 0),
            "hadAuthoredColors": info.had_colors,
        },
        "bbox": (
            {
                "min": [bbox[0], bbox[1], bbox[2]],
                "max": [bbox[3], bbox[4], bbox[5]],
            }
            if bbox
            else None
        ),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
    }

    if opts.part_metrics:
        manifest["parts"] = [
            {
                "name": p.name,
                "entry": p.entry,
                "instances": p.instances,
                "faces": p.faces,
                "triangles": p.triangles,
                "diagonal": round(p.diag, 4),
            }
            for p in sorted(info.parts, key=lambda x: -x.triangles)
        ]

    (model_dir / f"{stem}.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    log("-" * 72)
    log(
        f"DONE {src.name}: {human_int(len(info.parts))} unique parts / "
        f"{human_int(info.placements)} placements / "
        f"{human_int(mesh_stats['rendered_triangles'])} rendered triangles"
    )
    out_bytes = final_glb.stat().st_size
    ratio = src_bytes / max(out_bytes, 1)
    # Tiny inputs legitimately grow: a 4 KB STEP describing a torus becomes far
    # more bytes once tessellated. Say so rather than printing "0.3x smaller".
    change = f"{ratio:.1f}x smaller" if ratio >= 1 else f"{1 / ratio:.1f}x larger"
    log(
        f"     {human_bytes(src_bytes)} STEP -> {human_bytes(out_bytes)} GLB "
        f"({change}) in {elapsed:.1f}s"
    )
    log("-" * 72)

    manifest["_paths"] = {
        "glb": str(final_glb),
        "outDir": str(out_dir),
        "assetDir": str(asset_dir),
        "stem": stem,
    }
    return manifest
