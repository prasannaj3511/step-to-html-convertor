"""Walk the XDE document: enumerate unique parts, synthesise colours, gather stats.

Note on the viewer's model tree: we deliberately do *not* ship a sidecar copy of
the assembly hierarchy.  The glTF node graph produced from this document already
*is* the assembly tree, so the viewer reconstructs it from the loaded scene.
That keeps the two representations from drifting apart when gltfpack rewrites
the graph.
"""

from __future__ import annotations

import colorsys
import hashlib
from collections import Counter
from dataclasses import dataclass, field

from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.Quantity import Quantity_Color, Quantity_TOC_sRGB
from OCP.TCollection import TCollection_AsciiString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label, TDF_LabelSequence, TDF_Tool
from OCP.TopoDS import TopoDS_Shape
from OCP.XCAFDoc import (
    XCAFDoc_ColorCurv,
    XCAFDoc_ColorGen,
    XCAFDoc_ColorSurf,
    XCAFDoc_ColorTool,
    XCAFDoc_DocumentTool,
    XCAFDoc_ShapeTool,
)

from .logutil import log


@dataclass
class Part:
    """A unique piece of geometry in the document (referenced by >=1 instances)."""

    label: TDF_Label
    shape: TopoDS_Shape
    name: str
    entry: str
    instances: int = 1
    faces: int = 0
    diag: float = 0.0
    bbox: tuple[float, float, float, float, float, float] | None = None
    triangles: int = 0
    nodes: int = 0
    mesh_seconds: float = 0.0


@dataclass
class SceneInfo:
    parts: list[Part] = field(default_factory=list)
    instance_count: int = 0
    placements: int = 0
    assembly_count: int = 0
    free_shapes: int = 0
    had_colors: bool = False
    colored_parts: int = 0
    synthesized_parts: int = 0
    components_by_target: dict[str, list[TDF_Label]] = field(default_factory=dict)


def label_entry(label: TDF_Label) -> str:
    """Stable textual id for an OCAF label, e.g. `0:1:1:37`."""
    s = TCollection_AsciiString()
    TDF_Tool.Entry_s(label, s)
    return s.ToCString()


def label_name(label: TDF_Label, fallback: str = "") -> str:
    attr = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), attr):
        try:
            return attr.Get().ToExtString()
        except Exception:
            return str(attr.Get())
    return fallback


def _sanitize(name: str) -> str:
    name = (name or "").strip()
    # SolidWorks tends to emit trailing configuration suffixes and stray nulls.
    name = name.replace("\x00", "").strip()
    return name


def _components(label: TDF_Label) -> list[TDF_Label]:
    seq = TDF_LabelSequence()
    XCAFDoc_ShapeTool.GetComponents_s(label, seq, False)
    return [seq.Value(i) for i in range(1, seq.Length() + 1)]


def _referred(component: TDF_Label) -> TDF_Label | None:
    ref = TDF_Label()
    if XCAFDoc_ShapeTool.GetReferredShape_s(component, ref):
        return ref
    return None


def expand_placements(
    roots: list[TDF_Label],
) -> tuple[Counter, int, int, dict[str, list[TDF_Label]]]:
    """Expand the assembly graph into per-part placement counts.

    A part nested three assemblies deep, where each level is itself used twice,
    is drawn eight times.  Counting raw component labels would report one.  The
    per-assembly result is memoised so shared subassemblies are expanded once.

    Also returns the component (instance) labels pointing at each part, because
    that is where some CAD systems hang the colour of a recoloured instance.
    """
    memo: dict[str, Counter] = {}
    components_seen = 0
    assemblies_seen: set[str] = set()
    components_by_target: dict[str, list[TDF_Label]] = {}

    def walk(label: TDF_Label) -> Counter:
        nonlocal components_seen
        entry = label_entry(label)
        cached = memo.get(entry)
        if cached is not None:
            return cached
        if not XCAFDoc_ShapeTool.IsAssembly_s(label):
            result = Counter({entry: 1})
            memo[entry] = result
            return result

        assemblies_seen.add(entry)
        memo[entry] = Counter()  # guards against a malformed cyclic reference
        result: Counter = Counter()
        for comp in _components(label):
            components_seen += 1
            ref = _referred(comp)
            target = ref if ref is not None else comp
            components_by_target.setdefault(label_entry(target), []).append(comp)
            result += walk(target)
        memo[entry] = result
        return result

    total: Counter = Counter()
    for root in roots:
        total += walk(root)
    return total, components_seen, len(assemblies_seen), components_by_target


def collect_parts(doc) -> SceneInfo:
    """Enumerate every *unique* shape label plus how many times it is placed."""
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())

    all_labels = TDF_LabelSequence()
    st.GetShapes(all_labels)

    free_labels = TDF_LabelSequence()
    st.GetFreeShapes(free_labels)
    roots = [free_labels.Value(i) for i in range(1, free_labels.Length() + 1)]

    info = SceneInfo(free_shapes=len(roots))

    placements, components_seen, assembly_count, comps_by_target = expand_placements(roots)
    info.instance_count = components_seen
    info.assembly_count = assembly_count
    info.components_by_target = comps_by_target
    ref_counts = dict(placements)

    seen: set[str] = set()
    for i in range(1, all_labels.Length() + 1):
        lab = all_labels.Value(i)
        if XCAFDoc_ShapeTool.IsReference_s(lab) or XCAFDoc_ShapeTool.IsAssembly_s(lab):
            continue
        if not XCAFDoc_ShapeTool.IsSimpleShape_s(lab):
            continue
        entry = label_entry(lab)
        if entry in seen:
            continue
        seen.add(entry)
        shape = XCAFDoc_ShapeTool.GetShape_s(lab)
        if shape.IsNull():
            continue
        nm = _sanitize(label_name(lab)) or f"Part_{len(info.parts) + 1}"
        info.parts.append(
            Part(
                label=lab,
                shape=shape,
                name=nm,
                entry=entry,
                instances=max(1, ref_counts.get(entry, 1)),
            )
        )

    info.placements = sum(ref_counts.values())
    log(
        f"scene: {len(info.parts)} unique part(s), {info.instance_count} component link(s) "
        f"-> {info.placements} placement(s), {info.assembly_count} assembly node(s), "
        f"{info.free_shapes} root(s)"
    )
    return info


def part_bbox(shape: TopoDS_Shape, *, use_triangulation: bool = False) -> tuple[float, ...]:
    box = Bnd_Box()
    box.SetGap(0.0)
    if use_triangulation:
        BRepBndLib.Add_s(shape, box, True)
    else:
        # `Add` with useTriangulation=False falls back to control points, which is
        # a fast conservative estimate - good enough to pick a mesh deflection.
        BRepBndLib.Add_s(shape, box, False)
    if box.IsVoid():
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    return box.Get()


def golden_hue(index: int) -> float:
    """Evenly spread hues using the golden angle so neighbours stay distinguishable."""
    return (index * 0.6180339887498949) % 1.0


def _color_for(name: str, index: int, scheme: str) -> tuple[float, float, float]:
    if scheme in ("uniform", "neutral"):
        # A darker engineering grey keeps unpainted machines readable on the
        # viewer's light background without needing edge overlays.
        return (0.46, 0.47, 0.49)
    # Hash the name so a given part keeps its colour across runs and across
    # exports of related files, rather than depending on traversal order.
    digest = hashlib.blake2b(name.encode("utf-8", "replace"), digest_size=8).digest()
    seed = int.from_bytes(digest, "big")
    if scheme == "metal":
        hue = 0.08 + ((seed % 1000) / 1000.0) * 0.08  # warm steel/brass band
        sat = 0.10 + ((seed >> 10) % 100) / 100.0 * 0.15
        val = 0.62 + ((seed >> 20) % 100) / 100.0 * 0.25
    else:  # "palette" - broad, mid-saturation engineering spread
        hue = golden_hue(index) if index else (seed % 1000) / 1000.0
        hue = ((seed % 997) / 997.0 * 0.35 + hue * 0.65) % 1.0
        sat = 0.34 + ((seed >> 10) % 100) / 100.0 * 0.26
        val = 0.58 + ((seed >> 20) % 100) / 100.0 * 0.30
    return colorsys.hsv_to_rgb(hue, sat, val)


_COLOR_TYPES = (XCAFDoc_ColorSurf, XCAFDoc_ColorGen, XCAFDoc_ColorCurv)


def _label_has_color(label: TDF_Label) -> bool:
    col = Quantity_Color()
    return any(XCAFDoc_ColorTool.GetColor_s(label, t, col) for t in _COLOR_TYPES)


def part_has_authored_color(part: Part, info: SceneInfo) -> bool:
    """Does this part already carry a colour anywhere XDE can express one?

    Checking only the part label is not enough, and getting this wrong is what
    makes a converted model come out the wrong colour.  A CAD system may attach
    appearance at three different levels:

      * the part label itself - the simple, common case;
      * a *sub-shape* label, i.e. per-face colours (SolidWorks/Creo/NX face
        colours, and what OCCT does for a lone face);
      * a *component* label, i.e. one instance of a shared library part
        recoloured in place.

    Miss any of those and the part looks unpainted, so we invent a colour and
    paint over the real one.
    """
    if _label_has_color(part.label):
        return True

    subs = TDF_LabelSequence()
    XCAFDoc_ShapeTool.GetSubShapes_s(part.label, subs)
    for i in range(1, subs.Length() + 1):
        if _label_has_color(subs.Value(i)):
            return True

    for comp in info.components_by_target.get(part.entry, ()):
        if _label_has_color(comp):
            return True

    return False


def apply_colors(doc, info: SceneInfo, scheme: str = "auto") -> None:
    """Fill in colours for parts that have none, without ever touching authored ones.

    `auto` adapts to the file: when the STEP carries appearance data the gaps are
    filled with a neutral grey, so the designer's colours stay the thing you see.
    When the file carries no appearance data at all - very common for AP203
    exports, including the sample in this repo - a single grey would render a
    900-part assembly as one unreadable blob, so parts get distinguishable
    colours derived from a hash of their name instead.
    """
    if scheme == "none":
        return

    ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())

    pool = TDF_LabelSequence()
    ct.GetColors(pool)
    info.had_colors = pool.Length() > 0

    authored = [part_has_authored_color(p, info) for p in info.parts]
    info.colored_parts = sum(authored)

    if info.had_colors:
        log(
            f"scene: file supplies {pool.Length()} colour(s); "
            f"{info.colored_parts}/{len(info.parts)} part(s) already coloured"
        )
    else:
        log("scene: file carries no appearance data")

    # Decide what an uncoloured part should get.
    if scheme == "auto":
        fill = "neutral" if info.had_colors else "palette"
    else:
        fill = scheme

    if fill == "preserve":
        return

    assigned = 0
    for idx, part in enumerate(info.parts):
        if authored[idx] and fill != "uniform":
            continue  # never override an authored colour
        r, g, b = _color_for(part.name, idx, fill)
        ct.SetColor(part.label, Quantity_Color(r, g, b, Quantity_TOC_sRGB), XCAFDoc_ColorSurf)
        assigned += 1

    info.synthesized_parts = assigned
    if assigned:
        what = "overrode" if fill == "uniform" else "filled"
        log(f"scene: {what} {assigned} part colour(s) using '{fill}'")
