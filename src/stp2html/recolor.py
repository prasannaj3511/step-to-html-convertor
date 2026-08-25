"""Restore per-instance colours that OpenCASCADE's glTF writer discards.

XDE can attach a colour to a *component* label, meaning "this particular
placement of the shared part is painted differently" - the usual way a CAD
system records a recoloured instance of a library part.  `RWGltf_CafWriter`
resolves only the part-level style, so every placement comes out wearing the
part's colour and the override is silently lost.  Verified against OCCT 7.9 with
merged and unmerged faces, both name formats, and the explicit-roots overload.

The repair is cheap because glTF separates a `mesh` (which owns the material)
from the `node` that places it.  Giving one placement its own colour means
duplicating the *mesh* JSON entry - a few hundred bytes that re-reference the
very same accessors - and pointing it at a new material.  No geometry is copied
and the binary chunk is untouched.

Safety: the glTF node graph and the XDE tree are walked in lockstep and every
name is compared before anything is written.  On the first mismatch the file is
left exactly as OCCT wrote it, so a future change in OCCT's traversal order
degrades to "no instance colours" rather than to wrong colours.
"""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path

from OCP.Quantity import Quantity_Color
from OCP.TDF import TDF_Label, TDF_LabelSequence
from OCP.XCAFDoc import (
    XCAFDoc_ColorGen,
    XCAFDoc_ColorSurf,
    XCAFDoc_ColorTool,
    XCAFDoc_DocumentTool,
    XCAFDoc_ShapeTool,
)

from .logutil import log, warn
from .scene import label_name

_TYPES = (XCAFDoc_ColorSurf, XCAFDoc_ColorGen)

# STEP's auto-generated occurrence ids, e.g. NAUO12.
_AUTO_OCCURRENCE_RE = re.compile(r"^NAUO\d+$", re.IGNORECASE)
_JSON_CHUNK = 0x4E4F534A
_BIN_CHUNK = 0x004E4942
_GLB_MAGIC = 0x46546C67


def _color_of(label: TDF_Label) -> tuple[float, float, float] | None:
    """Linear RGB attached directly to `label`, if any."""
    col = Quantity_Color()
    for t in _TYPES:
        if XCAFDoc_ColorTool.GetColor_s(label, t, col):
            return (col.Red(), col.Green(), col.Blue())
    return None


def _components(label: TDF_Label) -> list[TDF_Label]:
    seq = TDF_LabelSequence()
    XCAFDoc_ShapeTool.GetComponents_s(label, seq, False)
    return [seq.Value(i) for i in range(1, seq.Length() + 1)]


def _referred(component: TDF_Label) -> TDF_Label | None:
    ref = TDF_Label()
    if XCAFDoc_ShapeTool.GetReferredShape_s(component, ref):
        return ref
    return None


def _expected_tree(doc) -> list[dict]:
    """Depth-first expansion mirroring how OCCT emits glTF nodes.

    Each entry records the name OCCT should have written and the colour that
    ought to be shown there, with component styles overriding part styles and
    both overriding whatever the enclosing assembly carries.
    """
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    roots = TDF_LabelSequence()
    st.GetFreeShapes(roots)

    out: list[dict] = []

    def visit(label: TDF_Label, product: str, instance: str, inherited, override) -> None:
        own = override if override is not None else _color_of(label)
        effective = own if own is not None else inherited
        out.append(
            {
                "product": product,
                "instance": instance,
                # What we would rather show: the occurrence name when the CAD
                # system gave the component one, otherwise the part name.
                "best": instance or product,
                "color": effective,
                "children": [],
            }
        )
        node = out[-1]

        if XCAFDoc_ShapeTool.IsAssembly_s(label):
            for comp in _components(label):
                ref = _referred(comp)
                target = ref if ref is not None else comp
                node["children"].append(len(out))
                visit(
                    target,
                    label_name(target, ""),
                    _clean_instance_name(label_name(comp, "")),
                    effective,
                    _color_of(comp),
                )

    for i in range(1, roots.Length() + 1):
        root = roots.Value(i)
        visit(root, label_name(root, ""), "", None, None)

    return out


def _clean_instance_name(name: str) -> str:
    """Keep a component name only when it tells the reader something.

    Two forms carry no information and must not displace the product name:

    * `=>[0:1:1:2]` - OCCT's placeholder for a component with no name.
    * `NAUO17` - the auto-generated id STEP gives a NEXT_ASSEMBLY_USAGE_-
      OCCURRENCE. SolidWorks emits these for every occurrence, so preferring
      them would replace a tree full of real part numbers with `NAUO1`,
      `NAUO2`, ... - which is what the sample assembly does.
    """
    name = (name or "").strip()
    if not name or name.startswith("=>["):
        return ""
    if _AUTO_OCCURRENCE_RE.match(name):
        return ""
    return name


def _read_glb(path: Path) -> tuple[dict, bytes]:
    data = path.read_bytes()
    magic, version, _total = struct.unpack_from("<III", data, 0)
    if magic != _GLB_MAGIC or version != 2:
        raise ValueError("not a glTF 2.0 binary file")
    offset = 12
    gltf: dict | None = None
    binary = b""
    while offset + 8 <= len(data):
        length, kind = struct.unpack_from("<II", data, offset)
        payload = data[offset + 8 : offset + 8 + length]
        if kind == _JSON_CHUNK:
            gltf = json.loads(payload.decode("utf-8"))
        elif kind == _BIN_CHUNK:
            binary = payload
        offset += 8 + length
    if gltf is None:
        raise ValueError("glTF binary has no JSON chunk")
    return gltf, binary


def _write_glb(path: Path, gltf: dict, binary: bytes) -> None:
    js = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    js += b" " * (-len(js) % 4)                 # chunks must be 4-byte aligned
    bn = binary + b"\x00" * (-len(binary) % 4)

    total = 12 + 8 + len(js) + (8 + len(bn) if bn else 0)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<III", _GLB_MAGIC, 2, total))
        fh.write(struct.pack("<II", len(js), _JSON_CHUNK))
        fh.write(js)
        if bn:
            fh.write(struct.pack("<II", len(bn), _BIN_CHUNK))
            fh.write(bn)


def _srgb_hex(linear: tuple[float, float, float]) -> str:
    def enc(v: float) -> float:
        v = max(0.0, min(1.0, v))
        return v * 12.92 if v <= 0.0031308 else 1.055 * (v ** (1 / 2.4)) - 0.055

    return "#%02X%02X%02X" % tuple(round(enc(c) * 255) for c in linear)


def patch_instance_colors(glb_path: Path, doc) -> int:
    """Apply component-level colours to `glb_path`. Returns how many nodes changed."""
    try:
        gltf, binary = _read_glb(glb_path)
    except (OSError, ValueError, struct.error) as exc:
        warn(f"could not reopen glTF to apply instance colours: {exc}")
        return 0

    nodes = gltf.get("nodes") or []
    meshes = gltf.get("meshes") or []
    materials = gltf.get("materials") or []
    scenes = gltf.get("scenes") or []
    if not nodes or not meshes:
        return 0

    expected = _expected_tree(doc)
    if not expected:
        return 0

    # --- walk both trees together, verifying as we go -----------------------
    scene_roots = scenes[0].get("nodes", []) if scenes else []
    expected_roots = [i for i, e in enumerate(expected) if _is_root(expected, i)]
    if len(scene_roots) != len(expected_roots):
        return 0

    pairs: list[tuple[int, dict]] = []

    def match(node_idx: int, exp_idx: int) -> bool:
        node = nodes[node_idx]
        exp = expected[exp_idx]
        written = (node.get("name") or "").strip()
        # Accept whichever name OCCT chose. The structural check below is what
        # actually guarantees we are looking at the right node; the name test
        # only guards against a wholesale change in traversal order.
        if written and written not in (exp["product"], exp["instance"]) \
                and not written.startswith("=>["):
            return False
        pairs.append((node_idx, exp))
        # OCCT emits one named child per component; unnamed children just carry
        # the mesh and have no counterpart in the XDE tree.
        named_kids = [c for c in node.get("children", []) if (nodes[c].get("name") or "").strip()]
        if len(named_kids) != len(exp["children"]):
            return False
        return all(match(c, e) for c, e in zip(named_kids, exp["children"]))

    for node_idx, exp_idx in zip(scene_roots, expected_roots):
        if not match(node_idx, exp_idx):
            log("instance colours: glTF layout did not match the document; leaving colours as exported")
            return 0

    # --- apply -------------------------------------------------------------
    mat_by_color: dict[tuple, int] = {}
    mesh_variants: dict[tuple[int, int], int] = {}
    changed = 0
    renamed = 0

    for node_idx, exp in pairs:
        node = nodes[node_idx]

        # Prefer the occurrence name, which distinguishes placements of a shared
        # part. OCCT can only be asked for one name format globally, and its
        # instance-first mode emits internal label references for unnamed
        # components - so choose per node here instead.
        best = exp["best"]
        written = (node.get("name") or "").strip()
        if best and best != written:
            node["name"] = best
            renamed += 1
        elif written.startswith("=>["):
            node["name"] = exp["product"] or written

        color = exp["color"]
        if color is None:
            continue

        # A node's mesh may hang off an unnamed child (OCCT does this for
        # components); follow one level down to find it.
        target_idx = node_idx
        if "mesh" not in node:
            kids = [c for c in node.get("children", []) if "mesh" in nodes[c] and not nodes[c].get("name")]
            if len(kids) != 1:
                continue
            target_idx = kids[0]
        target = nodes[target_idx]
        mesh_idx = target.get("mesh")
        if mesh_idx is None:
            continue

        prims = meshes[mesh_idx].get("primitives", [])
        # Multiple primitives means per-face colours, which are already correct
        # and must not be flattened to a single instance colour.
        if len(prims) != 1:
            continue

        current = prims[0].get("material")
        want = (round(color[0], 6), round(color[1], 6), round(color[2], 6))
        if current is not None and current < len(materials):
            have = materials[current].get("pbrMetallicRoughness", {}).get("baseColorFactor")
            if have and tuple(round(v, 6) for v in have[:3]) == want:
                continue  # already the right colour

        mat_idx = mat_by_color.get(want)
        if mat_idx is None:
            base = materials[current] if current is not None and current < len(materials) else {}
            new_mat = json.loads(json.dumps(base)) if base else {"doubleSided": True}
            pbr = new_mat.setdefault("pbrMetallicRoughness", {})
            alpha = 1.0
            if pbr.get("baseColorFactor") and len(pbr["baseColorFactor"]) > 3:
                alpha = pbr["baseColorFactor"][3]
            pbr["baseColorFactor"] = [color[0], color[1], color[2], alpha]
            new_mat["name"] = f"inst_{len(materials)}"
            materials.append(new_mat)
            mat_idx = len(materials) - 1
            mat_by_color[want] = mat_idx

        key = (mesh_idx, mat_idx)
        variant = mesh_variants.get(key)
        if variant is None:
            # Duplicate only the mesh's JSON: accessors, and therefore all the
            # vertex data, stay shared with the original.
            clone = json.loads(json.dumps(meshes[mesh_idx]))
            clone["primitives"][0]["material"] = mat_idx
            meshes.append(clone)
            variant = len(meshes) - 1
            mesh_variants[key] = variant

        target["mesh"] = variant
        changed += 1

    if not changed and not renamed:
        return 0

    gltf["materials"] = materials
    gltf["meshes"] = meshes
    _write_glb(glb_path, gltf, binary)
    if changed:
        log(
            f"instance colours: repainted {changed} placement(s) that OCCT exported "
            f"with the part colour ({len(mat_by_color)} distinct colour(s))"
        )
    if renamed:
        log(f"instance names: applied {renamed} occurrence name(s) to the model tree")
    return changed


def _is_root(expected: list[dict], idx: int) -> bool:
    for e in expected:
        if idx in e["children"]:
            return False
    return True
