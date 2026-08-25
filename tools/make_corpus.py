"""Generate a corpus of STEP files covering the shapes real-world input takes.

Used to prove the converter is generic (schemas, units, structure) and, most
importantly, to check colour fidelity against exactly-known RGB values.
"""
import os
import sys

from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakeSphere,
    BRepPrimAPI_MakeTorus,
)
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
from OCP.gp import gp_Trsf, gp_Vec, gp_Pnt, gp_Dir, gp_Pln
from OCP.Interface import Interface_Static
from OCP.Quantity import Quantity_Color, Quantity_TOC_sRGB
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDocStd import TDocStd_Document
from OCP.TopLoc import TopLoc_Location
from OCP.TopAbs import TopAbs_FACE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_ColorSurf, XCAFDoc_DocumentTool

OUT = sys.argv[1] if len(sys.argv) > 1 else "."
os.makedirs(OUT, exist_ok=True)

# Exact sRGB values we will later look for in the rendered image.
PALETTE = [
    ("PureRed",    (1.0, 0.0, 0.0)),
    ("PureGreen",  (0.0, 1.0, 0.0)),
    ("PureBlue",   (0.0, 0.0, 1.0)),
    ("Yellow",     (1.0, 1.0, 0.0)),
    ("Cyan",       (0.0, 1.0, 1.0)),
    ("Magenta",    (1.0, 0.0, 1.0)),
    ("MidGrey",    (0.5, 0.5, 0.5)),
    ("White",      (1.0, 1.0, 1.0)),
    ("Orange",     (1.0, 0.5, 0.0)),
    ("Teal",       (0.0, 0.5, 0.5)),
]


def new_doc():
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("BinXCAF"))
    app.NewDocument(TCollection_ExtendedString("BinXCAF"), doc)
    return doc, XCAFDoc_DocumentTool.ShapeTool_s(doc.Main()), XCAFDoc_DocumentTool.ColorTool_s(doc.Main())


def name(label, text):
    TDataStd_Name.Set_s(label, TCollection_ExtendedString(text))


def write(doc, filename, schema="AP214IS"):
    Interface_Static.SetCVal_s("write.step.schema", schema)
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    path = os.path.join(OUT, filename)
    w.Write(path)
    print(f"  wrote {filename} ({os.path.getsize(path):,} bytes, schema={schema})")


# --------------------------------------------------------------- 1. part colours
def make_part_colours():
    doc, st, ct = new_doc()
    asm = st.NewShape()
    name(asm, "ColourGrid")
    for i, (nm, rgb) in enumerate(PALETTE):
        # Flat slabs in a row, each a large flat face pointing +Z at the camera.
        box = BRepPrimAPI_MakeBox(18.0, 18.0, 4.0).Shape()
        lab = st.AddShape(box, False)
        name(lab, nm)
        ct.SetColor(lab, Quantity_Color(*rgb, Quantity_TOC_sRGB), XCAFDoc_ColorSurf)
        t = gp_Trsf()
        t.SetTranslation(gp_Vec(i * 20.0, 0.0, 0.0))
        st.AddComponent(asm, lab, TopLoc_Location(t))
    st.UpdateAssemblies()
    write(doc, "colors_part_level.step")


# ------------------------------------------------------------- 2. face colours
def make_face_colours():
    doc, st, ct = new_doc()
    box = BRepPrimAPI_MakeBox(30.0, 30.0, 30.0).Shape()
    lab = st.AddShape(box, False)
    name(lab, "RainbowCube")
    ct.SetColor(lab, Quantity_Color(0.35, 0.35, 0.35, Quantity_TOC_sRGB), XCAFDoc_ColorSurf)
    exp = TopExp_Explorer(box, TopAbs_FACE)
    i = 0
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        sub = st.AddSubShape(lab, face)
        if not sub.IsNull():
            rgb = PALETTE[i % len(PALETTE)][1]
            ct.SetColor(sub, Quantity_Color(*rgb, Quantity_TOC_sRGB), XCAFDoc_ColorSurf)
        i += 1
        exp.Next()
    write(doc, "colors_face_level.step")
    print(f"     ({i} faces coloured individually)")


# --------------------------------------------------------- 3. instance colours
def make_instance_colours():
    doc, st, ct = new_doc()
    asm = st.NewShape()
    name(asm, "InstanceColours")
    cyl = BRepPrimAPI_MakeCylinder(6.0, 25.0).Shape()
    lab = st.AddShape(cyl, False)
    name(lab, "SharedPin")
    ct.SetColor(lab, Quantity_Color(0.6, 0.6, 0.6, Quantity_TOC_sRGB), XCAFDoc_ColorSurf)
    for i, (nm, rgb) in enumerate(PALETTE[:5]):
        t = gp_Trsf()
        t.SetTranslation(gp_Vec(i * 18.0, 0.0, 0.0))
        comp = st.AddComponent(asm, lab, TopLoc_Location(t))
        name(comp, f"Pin_{nm}")
        # Colour applied to the *component* label, not the part - a very common
        # pattern for CAD systems that recolour instances of a library part.
        ct.SetColor(comp, Quantity_Color(*rgb, Quantity_TOC_sRGB), XCAFDoc_ColorSurf)
    st.UpdateAssemblies()
    write(doc, "colors_instance_level.step")


# ------------------------------------------------------------- 4. deep nesting
def make_deep_nest():
    doc, st, ct = new_doc()
    leaf = st.AddShape(BRepPrimAPI_MakeBox(5.0, 5.0, 5.0).Shape(), False)
    name(leaf, "Leaf")
    ct.SetColor(leaf, Quantity_Color(0.9, 0.3, 0.1, Quantity_TOC_sRGB), XCAFDoc_ColorSurf)
    current = leaf
    for level in range(4):
        grp = st.NewShape()
        name(grp, f"Level{level}")
        for k in range(2):
            t = gp_Trsf()
            t.SetTranslation(gp_Vec(k * 9.0 * (level + 1), level * 7.0, 0.0))
            st.AddComponent(grp, current, TopLoc_Location(t))
        current = grp
    st.UpdateAssemblies()
    write(doc, "nested_deep.step")


# ------------------------------------------------------------ 5. single solid
def make_single_solid():
    doc, st, ct = new_doc()
    lab = st.AddShape(BRepPrimAPI_MakeTorus(20.0, 6.0).Shape(), False)
    name(lab, "LoneTorus")
    write(doc, "single_solid_nocolour.step")


# ------------------------------------------------------------ 6. open surface
def make_open_surface():
    doc, st, ct = new_doc()
    pln = gp_Pln(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))
    face = BRepBuilderAPI_MakeFace(pln, -25.0, 25.0, -25.0, 25.0).Face()
    lab = st.AddShape(face, False)
    name(lab, "OpenPlate")
    ct.SetColor(lab, Quantity_Color(0.2, 0.6, 0.9, Quantity_TOC_sRGB), XCAFDoc_ColorSurf)
    write(doc, "surface_only.step")


# ------------------------------------------------------------- 7. AP203 / 242
def make_schema_variants():
    doc, st, ct = new_doc()
    lab = st.AddShape(BRepPrimAPI_MakeSphere(12.0).Shape(), False)
    name(lab, "SchemaBall")
    ct.SetColor(lab, Quantity_Color(0.1, 0.8, 0.4, Quantity_TOC_sRGB), XCAFDoc_ColorSurf)
    write(doc, "schema_ap203.step", schema="AP203")

    doc2, st2, ct2 = new_doc()
    lab2 = st2.AddShape(BRepPrimAPI_MakeSphere(12.0).Shape(), False)
    name(lab2, "SchemaBall242")
    ct2.SetColor(lab2, Quantity_Color(0.1, 0.4, 0.9, Quantity_TOC_sRGB), XCAFDoc_ColorSurf)
    write(doc2, "schema_ap242.step", schema="AP242DIS")


# ------------------------------------------------------------------ 8. inches
def make_inch_units():
    Interface_Static.SetCVal_s("write.step.unit", "INCH")
    doc, st, ct = new_doc()
    # 2 x 1 x 0.5 inch block == 50.8 x 25.4 x 12.7 mm
    lab = st.AddShape(BRepPrimAPI_MakeBox(50.8, 25.4, 12.7).Shape(), False)
    name(lab, "InchBlock")
    ct.SetColor(lab, Quantity_Color(0.8, 0.6, 0.2, Quantity_TOC_sRGB), XCAFDoc_ColorSurf)
    write(doc, "units_inch.step", schema="AP214IS")
    Interface_Static.SetCVal_s("write.step.unit", "MM")


print("building STEP corpus in", OUT)
make_part_colours()
make_face_colours()
make_instance_colours()
make_deep_nest()
make_single_solid()
make_open_surface()
make_schema_variants()
make_inch_units()
print("done")
