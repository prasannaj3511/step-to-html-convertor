"""Read a STEP file into an OCAF/XDE document, preserving assembly structure,
part names and colours.

XDE (eXtended Data Exchange) is what lets us keep the *assembly* rather than a
flattened soup of triangles: unique part geometry is stored once and referenced
by N component labels, each with its own placement.  That mapping survives all
the way into glTF as `mesh` reuse across `node`s, which is where most of the
output-size win comes from on large assemblies.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.Interface import Interface_Static
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDocStd import TDocStd_Document
from OCP.XCAFApp import XCAFApp_Application

from .logutil import log, warn

_STATUS_NAMES = {
    IFSelect_ReturnStatus.IFSelect_RetVoid: "nothing to transfer",
    IFSelect_ReturnStatus.IFSelect_RetError: "file error",
    IFSelect_ReturnStatus.IFSelect_RetFail: "transfer failed",
    IFSelect_ReturnStatus.IFSelect_RetStop: "transfer stopped",
}


@dataclass
class StepHeader:
    """Metadata lifted from the STEP HEADER section (cheap textual read)."""

    name: str = ""
    author: str = ""
    organization: str = ""
    originating_system: str = ""
    preprocessor: str = ""
    timestamp: str = ""
    schema: str = ""
    description: str = ""


def read_header(path: Path, max_bytes: int = 64_000) -> StepHeader:
    """Parse the STEP HEADER without loading the (potentially huge) DATA section."""
    hdr = StepHeader()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            blob = fh.read(max_bytes)
    except OSError as exc:  # pragma: no cover - unreadable file surfaces later anyway
        warn(f"could not read STEP header: {exc}")
        return hdr

    head = blob.split("ENDSEC", 1)[0]

    def _strings(section: str) -> list[str]:
        start = head.find(section)
        if start < 0:
            return []
        chunk = head[start : start + 4000]
        out, buf, inside = [], [], False
        i = 0
        while i < len(chunk):
            ch = chunk[i]
            if ch == "'":
                # '' is an escaped quote inside a STEP string
                if inside and i + 1 < len(chunk) and chunk[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                if inside:
                    out.append("".join(buf))
                    buf = []
                inside = not inside
            elif inside:
                buf.append(ch)
            elif ch == ";":
                break
            i += 1
        return out

    desc = _strings("FILE_DESCRIPTION")
    if desc:
        hdr.description = desc[0]

    fn = _strings("FILE_NAME")
    # FILE_NAME(name, time_stamp, author, organization, preprocessor, originating_system, authorisation)
    for idx, field in enumerate(
        ("name", "timestamp", "author", "organization", "preprocessor", "originating_system")
    ):
        if idx < len(fn):
            setattr(hdr, field, fn[idx])

    sch = _strings("FILE_SCHEMA")
    if sch:
        hdr.schema = sch[0]
    return hdr


def new_document() -> TDocStd_Document:
    """Create an empty XDE document bound to the XCAF application."""
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("BinXCAF"))
    app.NewDocument(TCollection_ExtendedString("BinXCAF"), doc)
    return doc


def configure_reader_statics(*, units: str = "MM", read_precision: float | None = None) -> None:
    """Tune the global XSTEP translation statics.

    `xstep.cascade.unit` forces the length unit of the resulting shapes, so a
    drawing authored in inches still lands in a predictable millimetre space.
    """
    Interface_Static.SetCVal_s("xstep.cascade.unit", units)
    # 0 = use the precision recorded in the file; 1 = force `read.precision.val`.
    if read_precision is None:
        Interface_Static.SetIVal_s("read.precision.mode", 0)
    else:
        Interface_Static.SetIVal_s("read.precision.mode", 1)
        Interface_Static.SetRVal_s("read.precision.val", read_precision)
    # Keep the shape hierarchy compact: we do not need per-subshape names, and on
    # a 4M-entity file they cost a lot of memory for no visual benefit.
    Interface_Static.SetIVal_s("read.stepcaf.subshapes.name", 0)
    # Everything else is left at OCCT's defaults on purpose: the shape-repr and
    # assembly-level statics change which representations get transferred, and
    # getting them wrong silently drops geometry.


def load_step(
    path: Path,
    *,
    units: str = "MM",
    read_colors: bool = True,
    read_names: bool = True,
    read_layers: bool = False,
    read_props: bool = False,
) -> TDocStd_Document:
    """Load `path` into a fresh XDE document.

    Everything that is not needed for rendering (GD&T, materials, saved views,
    SHUO) is switched off - on multi-hundred-megabyte files those translators
    dominate both runtime and peak memory.
    """
    configure_reader_statics(units=units)

    reader = STEPCAFControl_Reader()
    reader.SetColorMode(read_colors)
    reader.SetNameMode(read_names)
    reader.SetLayerMode(read_layers)
    reader.SetPropsMode(read_props)
    reader.SetGDTMode(False)
    reader.SetMatMode(False)
    reader.SetViewMode(False)
    reader.SetSHUOMode(False)

    log(f"reading STEP: {path.name}")
    status = reader.ReadFile(str(path))
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise RuntimeError(
            f"failed to read {path}: {_STATUS_NAMES.get(status, str(status))}"
        )

    roots = reader.NbRootsForTransfer()
    log(f"parsed OK - {roots} transferable root(s); building shapes")

    doc = new_document()
    if not reader.Transfer(doc):
        raise RuntimeError(f"STEP transfer produced no shapes for {path}")
    return doc
