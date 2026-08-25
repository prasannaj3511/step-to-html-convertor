"""Look specifically for every way a STEP file can carry appearance data."""
import re
import sys
from collections import Counter

path = sys.argv[1] if len(sys.argv) > 1 else r"D:\VSC Workspace\iSTP2HTML\input-stp\13110-PLI1.STEP"

COLOR_ENTITIES = [
    "COLOUR_RGB", "DRAUGHTING_PRE_DEFINED_COLOUR", "PRE_DEFINED_COLOUR",
    "STYLED_ITEM", "PRESENTATION_STYLE_ASSIGNMENT", "PRESENTATION_STYLE_BY_CONTEXT",
    "SURFACE_STYLE_USAGE", "SURFACE_SIDE_STYLE", "SURFACE_STYLE_FILL_AREA",
    "FILL_AREA_STYLE", "FILL_AREA_STYLE_COLOUR", "CURVE_STYLE",
    "MECHANICAL_DESIGN_GEOMETRIC_PRESENTATION_REPRESENTATION",
    "DRAUGHTING_MODEL", "PRESENTATION_LAYER_ASSIGNMENT",
    "SURFACE_STYLE_RENDERING", "SURFACE_STYLE_RENDERING_WITH_PROPERTIES",
    "SURFACE_STYLE_TRANSPARENT", "OVER_RIDING_STYLED_ITEM",
    "CONTEXT_DEPENDENT_OVER_RIDING_STYLED_ITEM", "MATERIAL_DESIGNATION",
]

pat = re.compile(rb"=\s*([A-Z_0-9]+)\s*\(")
counts = Counter()
with open(path, "rb") as f:
    while True:
        chunk = f.read(1 << 24)
        if not chunk:
            break
        tail = f.readline()
        if tail:
            chunk += tail
        for m in pat.finditer(chunk):
            counts[m.group(1).decode()] += 1

print(f"file: {path}")
print(f"total entities: {sum(counts.values()):,}\n")
print("-- appearance-related entities --")
any_found = False
for name in COLOR_ENTITIES:
    n = counts.get(name, 0)
    flag = "  <-- PRESENT" if n else ""
    if n:
        any_found = True
    print(f"  {name:<58} {n:>8,}{flag}")

print()
if any_found:
    print(">>> This file DOES carry appearance data.")
else:
    print(">>> No appearance data of any kind in this file.")

# Any entity whose name mentions colour/style/render at all.
print("\n-- any entity mentioning COLOUR/STYLE/RENDER --")
extras = {k: v for k, v in counts.items()
          if any(t in k for t in ("COLOUR", "COLOR", "STYLE", "RENDER", "VISUAL"))}
print("   ", extras or "(none)")
