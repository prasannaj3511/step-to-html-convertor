"""Export the tessellated XDE document to glTF-binary, then optimize it for the web.

Two stages:

1. OCCT's `RWGltf_CafWriter` writes a plain .glb.  Faces belonging to one part are
   merged into a single primitive (one draw call per part instead of one per
   B-rep face), indices drop to 16-bit where a part is small enough, and unique
   part meshes are referenced by many nodes rather than duplicated.
2. `gltfpack` re-encodes it with meshopt: vertex/index quantization plus the
   EXT_meshopt_compression codec.  meshopt decodes roughly an order of magnitude
   faster than Draco in the browser, which matters far more than the last few
   percent of file size when the goal is time-to-first-frame.
"""

from __future__ import annotations

import json
import os
import sys
import shutil
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from OCP.Message import Message_ProgressRange
from OCP.RWGltf import RWGltf_CafWriter
from OCP.RWMesh import RWMesh_CoordinateSystem, RWMesh_NameFormat
from OCP.TCollection import TCollection_AsciiString
from OCP.TColStd import TColStd_IndexedDataMapOfStringString

from .logutil import human_bytes, log, warn


@dataclass
class PackOptions:
    enabled: bool = True
    compress: str = "cc"          # "c" = meshopt, "cc" = higher ratio, "" = none
    position_bits: int = 16       # per-mesh quantization; 16 == effectively lossless
    normal_bits: int = 10
    keep_named_nodes: bool = True
    keep_extras: bool = True
    gpu_instancing: bool = False  # EXT_mesh_gpu_instancing: fewer draw calls
    merge_instances: bool = False
    simplify: float = 1.0         # 1.0 = no simplification
    simplify_error: float = 0.01
    binary: str | None = None


def _ascii(s: str | os.PathLike) -> TCollection_AsciiString:
    return TCollection_AsciiString(str(s))


# Scale factor from a CAD length unit to metres, which is what glTF uses.
UNIT_TO_METRES = {"MM": 0.001, "CM": 0.01, "M": 1.0, "INCH": 0.0254}


def write_glb(
    doc,
    out_path: Path,
    *,
    file_info: dict[str, str] | None = None,
    merge_faces: bool = True,
    split_indices_16: bool = True,
    parallel: bool = True,
    z_up: bool = True,
    units: str = "MM",
) -> Path:
    """Write `doc` to a binary glTF at `out_path`."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    writer = RWGltf_CafWriter(_ascii(out_path), True)  # True => .glb (binary)

    conv = writer.ChangeCoordinateSystemConverter()
    # CAD is Z-up; glTF is Y-up. Converting here means the viewer needs no
    # compensating root rotation, so world axes stay meaningful for sectioning.
    if z_up:
        conv.SetInputCoordinateSystem(RWMesh_CoordinateSystem.RWMesh_CoordinateSystem_Zup)
        conv.SetOutputCoordinateSystem(RWMesh_CoordinateSystem.RWMesh_CoordinateSystem_glTF)
    # State the unit conversion explicitly. OCCT leaves the input scale at -1
    # ("unknown") by default and then assumes millimetres, so a model authored in
    # inches would silently come out 25.4x too small.
    conv.SetInputLengthUnit(UNIT_TO_METRES.get(units.upper(), 0.001))
    conv.SetOutputLengthUnit(1.0)  # glTF measures in metres

    writer.SetMergeFaces(merge_faces)
    writer.SetSplitIndices16(split_indices_16)
    writer.SetParallel(parallel)
    writer.SetToEmbedTexturesInGlb(True)
    writer.SetForcedUVExport(False)
    try:
        # Product name first. The instance-first format looks tempting - it
        # distinguishes placements of a shared part - but when a component is
        # unnamed (the common case) OCCT falls back to an internal label
        # reference like "=>[0:1:1:2]", which is meaningless in a model tree.
        # Occurrence names are recovered later, in recolor.patch_instance_colors,
        # where the component labels are available directly.
        writer.SetNodeNameFormat(RWMesh_NameFormat.RWMesh_NameFormat_ProductOrInstance)
        # Leave meshes anonymous. three.js falls back to the *mesh* name when a
        # node has none, and gltfpack re-parents meshes onto unnamed child nodes -
        # so a named mesh would shadow the real part name in the viewer's tree.
        writer.SetMeshNameFormat(RWMesh_NameFormat.RWMesh_NameFormat_Empty)
    except Exception as exc:  # binding differences across OCCT builds
        warn(f"could not set glTF name format ({exc}); using defaults")

    info_map = TColStd_IndexedDataMapOfStringString()
    for key, value in (file_info or {}).items():
        if value:
            info_map.Add(_ascii(key), _ascii(str(value)[:512]))

    log(f"writing glTF-binary -> {out_path.name}")
    t0 = time.time()
    ok = writer.Perform(doc, info_map, Message_ProgressRange())
    if not ok:
        raise RuntimeError(f"glTF export failed for {out_path}")
    size = out_path.stat().st_size
    log(f"wrote {out_path.name} ({human_bytes(size)}) in {time.time() - t0:.1f}s")
    return out_path


def find_gltfpack(explicit: str | None = None) -> str | None:
    """Locate a gltfpack executable: explicit > project-vendored > PATH."""
    if explicit:
        p = Path(explicit)
        return str(p) if p.exists() else (shutil.which(explicit) or None)

    is_windows = sys.platform.startswith("win")
    local_candidates = (
        "gltfpack.cmd", "gltfpack"
    ) if is_windows else (
        "gltfpack", "gltfpack.cmd"
    )

    here = Path(__file__).resolve()
    for root in list(here.parents)[:5]:
        for rel in (
            Path("tools") / "node_modules" / ".bin",
            Path("node_modules") / ".bin",
        ):
            for name in local_candidates:
                candidate = root / rel / name
                if candidate.exists():
                    return str(candidate)

    return shutil.which("gltfpack")


GLB_MAGIC = 0x46546C67  # 'glTF'


def is_valid_glb(path: Path) -> bool:
    """Cheap structural check: GLB header plus a parseable JSON chunk."""
    try:
        with open(path, "rb") as fh:
            header = fh.read(12)
            if len(header) < 12:
                return False
            magic, version, total = struct.unpack("<III", header)
            if magic != GLB_MAGIC or version != 2:
                return False
            if total > path.stat().st_size:
                return False
            chunk_header = fh.read(8)
            if len(chunk_header) < 8:
                return False
            length, kind = struct.unpack("<II", chunk_header)
            if kind != 0x4E4F534A:  # 'JSON'
                return False
            doc = json.loads(fh.read(length).decode("utf-8"))
            return bool(doc.get("asset")) and "meshes" in doc
    except (OSError, ValueError, struct.error):
        return False


def optimize_glb(src: Path, dst: Path, opts: PackOptions) -> tuple[Path, dict]:
    """Run gltfpack over `src`. Falls back to copying `src` if gltfpack is absent."""
    stats = {"packed": False, "input_bytes": src.stat().st_size, "output_bytes": 0}

    if not opts.enabled:
        if src != dst:
            shutil.copy2(src, dst)
        stats["output_bytes"] = dst.stat().st_size
        return dst, stats

    exe = find_gltfpack(opts.binary)
    if not exe:
        warn(
            "gltfpack not found - shipping the uncompressed glTF. "
            "Install it with `npm install gltfpack` inside iSTP2HTML/tools "
            "for ~5-10x smaller output and faster loads."
        )
        if src != dst:
            shutil.copy2(src, dst)
        stats["output_bytes"] = dst.stat().st_size
        return dst, stats

    cmd: list[str] = [exe, "-i", str(src), "-o", str(dst)]
    if opts.compress:
        cmd.append(f"-{opts.compress}")
    cmd += ["-vp", str(opts.position_bits), "-vn", str(opts.normal_bits)]
    if opts.keep_named_nodes:
        cmd.append("-kn")
    if opts.keep_extras:
        cmd.append("-ke")
    if opts.gpu_instancing:
        cmd.append("-mi")
    if opts.merge_instances:
        cmd.append("-mm")
    if opts.simplify < 1.0:
        cmd += ["-si", f"{opts.simplify:g}", "-se", f"{opts.simplify_error:g}", "-slb"]

    log("optimizing: " + " ".join(Path(c).name if os.sep in c else c for c in cmd))
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)

    # Judge gltfpack by its output, not its exit code. The npm wrapper is known
    # to trip a libuv assertion while tearing down the Node process on Windows -
    # after it has already written a perfectly good file. Discarding that result
    # would silently cost ~6x in download size, so validate the .glb instead and
    # only fall back when it is genuinely unusable.
    produced_valid = dst.exists() and is_valid_glb(dst)
    if not produced_valid:
        warn(f"gltfpack produced no usable output (exit {proc.returncode}); using unoptimized glTF")
        if proc.stderr:
            warn(proc.stderr.strip()[:2000])
        if src != dst:
            shutil.copy2(src, dst)
        stats["output_bytes"] = dst.stat().st_size
        return dst, stats

    if proc.returncode != 0:
        stats["exit_anomaly"] = proc.returncode
        warn(
            f"gltfpack exited with {proc.returncode} but wrote a valid glTF "
            f"({human_bytes(dst.stat().st_size)}); keeping it"
        )

    tail = (proc.stdout or "").strip().splitlines()
    for line in tail[-6:]:
        log("  gltfpack: " + line.strip())

    stats["packed"] = True
    stats["output_bytes"] = dst.stat().st_size
    ratio = stats["input_bytes"] / max(stats["output_bytes"], 1)
    log(
        f"optimized {human_bytes(stats['input_bytes'])} -> "
        f"{human_bytes(stats['output_bytes'])} ({ratio:.1f}x) in {time.time() - t0:.1f}s"
    )
    return dst, stats
