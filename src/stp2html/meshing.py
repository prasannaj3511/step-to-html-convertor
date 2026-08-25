"""Adaptive tessellation of B-rep geometry.

The important idea here is *per-part* deflection.  A single global linear
deflection is wrong for real assemblies: a value fine enough for an M3 screw
buries a 2-metre weldment in triangles, and a value tuned for the weldment turns
every fastener into a faceted lump.  So each unique part gets a deflection scaled
to its own bounding-box diagonal, clamped into a sane absolute band.

Triangulation is cached on the underlying `TShape`, which every instance of a
part shares - so meshing the 914 unique parts covers all 3,300+ placements.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from OCP.BRep import BRep_Tool
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools
from OCP.IMeshTools import IMeshTools_Parameters
from OCP.TopAbs import TopAbs_FACE
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS

from .logutil import human_int, log, progress, warn
from .scene import SceneInfo, part_bbox


@dataclass(frozen=True)
class Quality:
    """A tessellation preset.

    `lin_factor` is relative to each part's bbox diagonal; `angle` is the maximum
    angular deviation in radians between the surface normal and the facet normal,
    which is what actually controls how round a small cylinder looks.
    """

    name: str
    lin_factor: float
    angle: float
    min_size_factor: float = 1e-5
    label: str = ""


QUALITY_PRESETS: dict[str, Quality] = {
    "draft": Quality("draft", 4.0e-3, 0.90, 2e-5, "fastest, visibly faceted"),
    "medium": Quality("medium", 1.5e-3, 0.55, 1e-5, "balanced"),
    "high": Quality("high", 6.0e-4, 0.32, 5e-6, "smooth, recommended"),
    "ultra": Quality("ultra", 2.5e-4, 0.20, 2e-6, "near-CAD fidelity, large"),
}


def count_triangulation(shape) -> tuple[int, int]:
    """Sum triangles and nodes currently attached to `shape`'s faces."""
    tris = nodes = 0
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    loc = TopLoc_Location()
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is not None:
            tris += tri.NbTriangles()
            nodes += tri.NbNodes()
        exp.Next()
    return tris, nodes


def count_faces(shape) -> int:
    n = 0
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        n += 1
        exp.Next()
    return n


def _deflection_for(diag: float, q: Quality, floor: float, ceiling: float) -> float:
    if diag <= 0.0 or not math.isfinite(diag):
        return floor
    return min(max(diag * q.lin_factor, floor), ceiling)


def tessellate(
    info: SceneInfo,
    quality: Quality,
    *,
    parallel: bool = True,
    clean_first: bool = False,
    defl_floor: float = 1.0e-3,
    defl_ceiling: float = 5.0,
    log_every: int = 10,
) -> dict:
    """Mesh every unique part with an adaptive deflection. Mutates `info` in place."""
    total_parts = len(info.parts)
    log(
        f"tessellating {total_parts} unique part(s) at quality='{quality.name}' "
        f"(lin={quality.lin_factor:g}x diag, ang={quality.angle:g} rad)"
    )

    t_start = time.time()
    total_tris = total_nodes = 0
    failures = 0

    for idx, part in enumerate(info.parts, start=1):
        try:
            xmin, ymin, zmin, xmax, ymax, zmax = part_bbox(part.shape)
            part.bbox = (xmin, ymin, zmin, xmax, ymax, zmax)
            diag = math.dist((xmin, ymin, zmin), (xmax, ymax, zmax))
            part.diag = diag
            part.faces = count_faces(part.shape)

            if clean_first:
                BRepTools.Clean_s(part.shape)

            params = IMeshTools_Parameters()
            params.Deflection = _deflection_for(diag, quality, defl_floor, defl_ceiling)
            params.Angle = quality.angle
            params.Relative = False
            params.InParallel = parallel
            params.MinSize = max(diag * quality.min_size_factor, 1.0e-5)
            params.AdjustMinSize = False
            params.ControlSurfaceDeflection = True
            params.CleanModel = True
            params.AllowQualityDecrease = True
            # Interior deflection can safely be looser than the boundary: the
            # silhouette is what the eye judges, and this trims a lot of triangles.
            params.DeflectionInterior = params.Deflection * 2.0
            params.AngleInterior = min(quality.angle * 2.0, 1.2)

            t0 = time.time()
            BRepMesh_IncrementalMesh(part.shape, params)
            part.mesh_seconds = time.time() - t0

            tris, nodes = count_triangulation(part.shape)
            part.triangles, part.nodes = tris, nodes
            total_tris += tris * part.instances
            total_nodes += nodes * part.instances
        except Exception as exc:  # keep going - one bad solid must not kill the run
            failures += 1
            warn(f"tessellation failed for '{part.name}' ({part.entry}): {exc}")

        progress(idx, total_parts, "parts meshed", every=log_every)

    unique_tris = sum(p.triangles for p in info.parts)
    unique_nodes = sum(p.nodes for p in info.parts)
    elapsed = time.time() - t_start

    log(
        f"meshed {human_int(unique_tris)} unique triangles "
        f"({human_int(unique_nodes)} verts) in {elapsed:.1f}s"
    )
    log(
        f"scene expands to {human_int(total_tris)} rendered triangles "
        f"across all instances (x{total_tris / max(unique_tris, 1):.1f} reuse factor)"
    )
    if failures:
        warn(f"{failures} part(s) failed to tessellate and will be missing")

    slowest = sorted(info.parts, key=lambda p: -p.mesh_seconds)[:5]
    if slowest and slowest[0].mesh_seconds > 1.0:
        log("slowest parts: " + ", ".join(f"{p.name} {p.mesh_seconds:.1f}s" for p in slowest))

    return {
        "unique_triangles": unique_tris,
        "unique_vertices": unique_nodes,
        "rendered_triangles": total_tris,
        "rendered_vertices": total_nodes,
        "mesh_seconds": elapsed,
        "failures": failures,
    }


def scene_bbox(info: SceneInfo) -> tuple[float, ...] | None:
    """Union of unique-part bounding boxes (in part-local space).

    This is only used for sanity reporting - the viewer computes the true world
    bounds from the loaded glTF, which accounts for instance placements.
    """
    boxes = [p.bbox for p in info.parts if p.bbox]
    if not boxes:
        return None
    xs0 = min(b[0] for b in boxes)
    ys0 = min(b[1] for b in boxes)
    zs0 = min(b[2] for b in boxes)
    xs1 = max(b[3] for b in boxes)
    ys1 = max(b[4] for b in boxes)
    zs1 = max(b[5] for b in boxes)
    return (xs0, ys0, zs0, xs1, ys1, zs1)
