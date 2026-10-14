"""
Combined PDF linker: embed portal links for
  1) all hyphenated part numbers anywhere on the page, and
  2) circled BOM item balloons (ITEM NO. -> PART NO. -> product_id).

Does not modify pdflink_all_parts.py or pdflink_balloons.py.

Usage:
  python pdflink_parts_and_balloons.py --input "path\\to\\pdfs" --output "path\\to\\linked"
  python pdflink_parts_and_balloons.py --input "..." --output "..." --no-report
  python pdflink_parts_and_balloons.py --input "..." --output "..." ^
      --link-base "http://localhost:3000/machines/371"
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import re
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from pdfminer.high_level import extract_pages
from pdfminer.layout import (
    LTAnno,
    LTChar,
    LTContainer,
    LTCurve,
    LTLine,
    LTRect,
    LTTextContainer,
    LTTextLine,
)
from PyPDF2 import PdfReader, PdfWriter
from PyPDF2.generic import AnnotationBuilder, ArrayObject, NameObject
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")
load_dotenv(SCRIPT_DIR.parent / "iEcom_server" / ".env", override=False)

MIN_BALLOON_SIZE = 10.0
MAX_BALLOON_SIZE = 80.0
MAX_ELLIPSE_ERROR = 0.08
MAX_ROW_Y_DIFFERENCE = 4.5
PART_PATTERN = re.compile(r"\b[A-Z0-9\-]{5,}\b", re.IGNORECASE)


@dataclass(frozen=True)
class TextItem:
    text: str
    bbox: tuple[float, float, float, float]

    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def y0(self) -> float:
        return self.bbox[1]

    @property
    def x1(self) -> float:
        return self.bbox[2]

    @property
    def y1(self) -> float:
        return self.bbox[3]

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass(frozen=True)
class BomBlock:
    item_header: TextItem
    part_header: TextItem
    description_header: TextItem | None


@dataclass(frozen=True)
class BomRow:
    item_no: int
    part_number: str
    block_number: int


@dataclass(frozen=True)
class EllipseCandidate:
    bbox: tuple[float, float, float, float]
    shape_error: float

    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def y0(self) -> float:
        return self.bbox[1]

    @property
    def x1(self) -> float:
        return self.bbox[2]

    @property
    def y1(self) -> float:
        return self.bbox[3]

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass(frozen=True)
class Balloon:
    item_no: int
    text_bbox: tuple[float, float, float, float]
    circle_bbox: tuple[float, float, float, float]
    shape_error: float


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().strip("'").strip('"').strip()
    return value or None


def _env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = _clean(os.getenv(name))
        if value:
            return value
    return default


def decrypt_aes_cbc(encrypted_hex: str, iv_hex: str, key_hex: str) -> str:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = bytes.fromhex(key_hex)
    iv = bytes.fromhex(iv_hex)
    encrypted = bytes.fromhex(encrypted_hex)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    padded = cipher.decryptor().update(encrypted) + cipher.decryptor().finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


def candidate_db_passwords() -> list[tuple[str, str]]:
    passwords: list[tuple[str, str]] = []
    encrypted = _env("DB_PASS_ENCRYPTED")
    iv = _env("DB_PASS_IV")
    key = _env("ENCRYPTION_KEY")
    if encrypted and iv and key:
        try:
            passwords.append(
                ("iEcom_server encrypted DB_PASS", decrypt_aes_cbc(encrypted, iv, key))
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to decrypt DB_PASS_ENCRYPTED: {exc}") from exc

    plain = _env("PG_PASSWORD", "DB_PASSWORD", "DB_PASS")
    if plain:
        passwords.append(("pipelines/plain PG_PASSWORD", plain))

    if not passwords:
        raise RuntimeError(
            "No DB password found. Set PG_PASSWORD in iEcom_data_pipelines/.env "
            "or DB_PASS_ENCRYPTED + DB_PASS_IV + ENCRYPTION_KEY in iEcom_server/.env"
        )
    return passwords


def build_engine(logger: logging.Logger | None = None):
    host = _env("PG_HOST", "DB_HOST", default="localhost")
    user = _env("PG_USER", "DB_USER", "DB_USERNAME", default="postgres")
    port = int(_env("PG_PORT", "DB_PORT", default="5432"))
    database = _env("PG_DATABASE", "DB_NAME", "DB_DATABASE", default="bpa")

    last_error: Exception | None = None
    for source, password in candidate_db_passwords():
        conn_url = URL.create(
            "postgresql+psycopg2",
            username=user,
            password=password,
            host=host,
            port=port,
            database=database,
        )
        engine = create_engine(conn_url, connect_args={"connect_timeout": 10})
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            if logger:
                logger.info("DB auth succeeded via %s", source)
            return engine, {
                "host": host,
                "user": user,
                "port": port,
                "database": database,
            }
        except Exception as exc:
            last_error = exc
            engine.dispose()
            if logger:
                logger.warning("DB auth failed via %s", source)

    raise RuntimeError(f"Could not connect to PostgreSQL: {last_error}")


def load_product_map(engine) -> dict[str, int]:
    with engine.connect() as connection:
        frame = pd.read_sql(
            text("SELECT product_id, product_number FROM boms"),
            connection,
        )
    return {
        str(row["product_number"]).strip(): int(row["product_id"])
        for _, row in frame.iterrows()
        if str(row["product_number"]).strip()
        and str(row["product_number"]).strip().lower() != "nan"
    }


def setup_logging(output_folder: Path) -> logging.Logger:
    log_path = output_folder / "linking_process_parts_and_balloons.log"
    logger = logging.getLogger("pdflink_parts_and_balloons")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console_handler)
    return logger


def looks_like_part_number(token: str) -> bool:
    token = token.strip()
    if "-" not in token or len(token) < 5 or not re.search(r"\d", token):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+", token))


def iter_layout_objects(root):
    yield root
    if isinstance(root, LTContainer):
        for child in root:
            yield from iter_layout_objects(child)


def collect_text_items(page_layout) -> list[TextItem]:
    items: list[TextItem] = []
    for obj in iter_layout_objects(page_layout):
        if not isinstance(obj, LTTextLine):
            continue
        value = " ".join(obj.get_text().split())
        if value:
            items.append(TextItem(value, tuple(float(v) for v in obj.bbox)))
    return items


def iter_line_matches(text_line, pattern):
    glyphs = []
    for obj in text_line:
        if isinstance(obj, LTChar):
            glyphs.append((obj.get_text(), obj.bbox))
        elif isinstance(obj, LTAnno):
            glyphs.append((obj.get_text(), None))
        else:
            text_value = getattr(obj, "get_text", lambda: "")()
            bbox = getattr(obj, "bbox", None)
            for char in text_value:
                glyphs.append((char, bbox))

    line_text = "".join(char for char, _ in glyphs).rstrip("\n")
    for match in pattern.finditer(line_text):
        matched_boxes = [
            bbox for _, bbox in glyphs[match.start() : match.end()] if bbox
        ]
        if not matched_boxes:
            continue
        x0 = min(bbox[0] for bbox in matched_boxes)
        y0 = min(bbox[1] for bbox in matched_boxes)
        x1 = max(bbox[2] for bbox in matched_boxes)
        y1 = max(bbox[3] for bbox in matched_boxes)
        yield match.group(), (x0, y0, x1, y1)


def normalize_header(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", value.upper()).strip()


def find_bom_blocks(items: list[TextItem]) -> list[BomBlock]:
    item_headers = [
        item
        for item in items
        if normalize_header(item.text) in {"ITEM", "ITEM NO", "ITEM NUMBER"}
    ]
    part_headers = [
        item
        for item in items
        if normalize_header(item.text) in {"PART", "PART NO", "PART NUMBER"}
    ]
    description_headers = [
        item for item in items if normalize_header(item.text) == "DESCRIPTION"
    ]

    blocks: list[BomBlock] = []
    seen: set[tuple[int, int, int]] = set()
    for part_header in part_headers:
        left_headers = [
            item
            for item in item_headers
            if item.x1 < part_header.x0
            and abs(item.cy - part_header.cy) <= MAX_ROW_Y_DIFFERENCE
        ]
        if not left_headers:
            continue
        item_header = min(left_headers, key=lambda item: part_header.x0 - item.x1)

        # DESCRIPTION may sit to either side of PART # depending on the
        # table template, so it is matched by row alignment only, and is
        # optional: some tables don't carry a DESCRIPTION header at all.
        row_description_headers = [
            item
            for item in description_headers
            if abs(item.cy - part_header.cy) <= MAX_ROW_Y_DIFFERENCE
        ]
        description_header = (
            min(row_description_headers, key=lambda item: abs(item.cx - part_header.cx))
            if row_description_headers
            else None
        )

        key = (round(item_header.cx), round(part_header.cx), round(part_header.cy))
        if key in seen:
            continue
        seen.add(key)
        blocks.append(BomBlock(item_header, part_header, description_header))

    blocks.sort(key=lambda block: (block.part_header.y0, block.item_header.x0))
    return blocks


def extract_bom_rows(
    items: list[TextItem], blocks: list[BomBlock]
) -> tuple[list[BomRow], dict[int, str], dict[int, list[str]]]:
    rows: list[BomRow] = []
    seen_rows: set[tuple[int, str, int, int]] = set()

    for block_number, block in enumerate(blocks, start=1):
        # Column order (ITEM/DESCRIPTION/PART#) varies by table template, so
        # boundaries are derived from actual header x-positions rather than
        # an assumed left-to-right order.
        columns: list[tuple[str, TextItem]] = [
            ("item", block.item_header),
            ("part", block.part_header),
        ]
        if block.description_header is not None:
            columns.append(("description", block.description_header))
        columns.sort(key=lambda pair: pair[1].x0)

        bounds: dict[str, tuple[float, float]] = {}
        for index, (name, header) in enumerate(columns):
            left = (
                (columns[index - 1][1].x1 + header.x0) / 2
                if index > 0
                else header.x0 - 12
            )
            right = (
                (header.x1 + columns[index + 1][1].x0) / 2
                if index + 1 < len(columns)
                else header.x1 + 120
            )
            bounds[name] = (left, right)

        item_left, item_right = bounds["item"]
        part_left, part_right = bounds["part"]
        header_top = max(header.y1 for _, header in columns)

        item_candidates = [
            item
            for item in items
            if re.fullmatch(r"\d{1,4}", item.text)
            and item_left <= item.cx <= item_right
            and item.cy < header_top
        ]
        part_candidates = [
            item
            for item in items
            if looks_like_part_number(item.text)
            and part_left <= item.cx <= part_right
            and item.cy < header_top
        ]

        for item in item_candidates:
            matches = [
                part
                for part in part_candidates
                if abs(part.cy - item.cy) <= MAX_ROW_Y_DIFFERENCE
            ]
            if not matches:
                continue
            part = min(matches, key=lambda candidate: abs(candidate.cy - item.cy))
            item_no = int(item.text)
            key = (item_no, part.text, round(item.cy), block_number)
            if key in seen_rows:
                continue
            seen_rows.add(key)
            rows.append(
                BomRow(
                    item_no=item_no,
                    part_number=part.text,
                    block_number=block_number,
                )
            )

    parts_by_item: dict[int, set[str]] = {}
    for row in rows:
        parts_by_item.setdefault(row.item_no, set()).add(row.part_number)

    conflicts = {
        item_no: sorted(part_numbers)
        for item_no, part_numbers in parts_by_item.items()
        if len(part_numbers) > 1
    }
    bom_map = {
        item_no: next(iter(part_numbers))
        for item_no, part_numbers in parts_by_item.items()
        if len(part_numbers) == 1
    }
    rows.sort(key=lambda row: (row.item_no, row.block_number))
    return rows, bom_map, conflicts


def ellipse_shape_error(curve: LTCurve) -> float | None:
    width = float(curve.x1 - curve.x0)
    height = float(curve.y1 - curve.y0)
    if not (
        MIN_BALLOON_SIZE <= width <= MAX_BALLOON_SIZE
        and MIN_BALLOON_SIZE <= height <= MAX_BALLOON_SIZE
        and 0.5 <= width / height <= 2.0
    ):
        return None

    points = [(float(x), float(y)) for x, y in getattr(curve, "pts", [])]
    if len(points) < 8:
        return None
    closing_gap = math.hypot(
        points[0][0] - points[-1][0], points[0][1] - points[-1][1]
    )
    if closing_gap > max(width, height) * 0.05:
        return None

    cx = (curve.x0 + curve.x1) / 2
    cy = (curve.y0 + curve.y1) / 2
    normalized = [
        ((x - cx) / (width / 2), (y - cy) / (height / 2)) for x, y in points
    ]
    radii = [math.hypot(x, y) for x, y in normalized]
    if min(radii) < 0.70 or max(radii) > 1.30:
        return None
    if not (
        min(x for x, _ in normalized) <= -0.85
        and max(x for x, _ in normalized) >= 0.85
        and min(y for _, y in normalized) <= -0.85
        and max(y for _, y in normalized) >= 0.85
    ):
        return None

    error = sum(abs(radius - 1.0) for radius in radii) / len(radii)
    return error if error <= MAX_ELLIPSE_ERROR else None


def collect_ellipse_candidates(page_layout) -> list[EllipseCandidate]:
    candidates: list[EllipseCandidate] = []
    seen: set[tuple[int, int, int, int]] = set()
    for obj in iter_layout_objects(page_layout):
        if not isinstance(obj, LTCurve) or isinstance(obj, (LTLine, LTRect)):
            continue
        error = ellipse_shape_error(obj)
        if error is None:
            continue
        bbox = tuple(float(v) for v in obj.bbox)
        key = tuple(round(value * 10) for value in bbox)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(EllipseCandidate(bbox, error))
    return candidates


def text_is_centered_in_ellipse(item: TextItem, ellipse: EllipseCandidate) -> bool:
    tolerance = 0.5
    if not (
        ellipse.x0 - tolerance <= item.x0
        and item.x1 <= ellipse.x1 + tolerance
        and ellipse.y0 - tolerance <= item.y0
        and item.y1 <= ellipse.y1 + tolerance
    ):
        return False
    return (
        abs(item.cx - ellipse.cx) <= ellipse.width * 0.25
        and abs(item.cy - ellipse.cy) <= ellipse.height * 0.25
    )


def detect_balloons(
    items: list[TextItem], ellipses: list[EllipseCandidate]
) -> list[Balloon]:
    balloons: list[Balloon] = []
    seen: set[tuple[int, int, int, int, int]] = set()
    numeric_items = [item for item in items if re.fullmatch(r"\d{1,4}", item.text)]

    for item in numeric_items:
        matches = [
            ellipse
            for ellipse in ellipses
            if text_is_centered_in_ellipse(item, ellipse)
        ]
        if not matches:
            continue
        ellipse = min(
            matches,
            key=lambda candidate: (
                candidate.width * candidate.height,
                candidate.shape_error,
            ),
        )
        key = (int(item.text), *(round(value * 10) for value in ellipse.bbox))
        if key in seen:
            continue
        seen.add(key)
        balloons.append(
            Balloon(
                item_no=int(item.text),
                text_bbox=item.bbox,
                circle_bbox=ellipse.bbox,
                shape_error=ellipse.shape_error,
            )
        )

    balloons.sort(key=lambda balloon: (-balloon.circle_bbox[1], balloon.circle_bbox[0]))
    return balloons


def expand_rectangle(
    bbox: tuple[float, float, float, float], padding: float = 1.5
) -> tuple[float, float, float, float]:
    return (
        bbox[0] - padding,
        bbox[1] - padding,
        bbox[2] + padding,
        bbox[3] + padding,
    )


def collect_part_hits(page_layout) -> list[tuple[str, tuple[float, float, float, float]]]:
    hits: list[tuple[str, tuple[float, float, float, float]]] = []
    for element in page_layout:
        if not isinstance(element, LTTextContainer):
            continue
        for text_line in element:
            if not isinstance(text_line, LTTextLine):
                continue
            for match, bbox in iter_line_matches(text_line, PART_PATTERN):
                if looks_like_part_number(match):
                    hits.append((match, bbox))
    return hits


def write_excel_report(
    report_path: Path,
    log_path: Path,
    output_folder: Path,
    link_base: str,
    total: int,
    passed: int,
    failed: int,
    file_results: list[dict],
    part_rows: list[dict],
    balloon_rows: list[dict],
    logger: logging.Logger,
) -> None:
    parts_found = sum(1 for row in part_rows if row.get("id_found") == "Yes")
    parts_missing = sum(1 for row in part_rows if row.get("id_found") == "No")
    balloons_linked = sum(1 for row in balloon_rows if row.get("status") == "Linked")

    summary_df = pd.DataFrame(
        [
            {"Metric": "Total PDFs", "Value": total},
            {"Metric": "Passed", "Value": passed},
            {"Metric": "Failed", "Value": failed},
            {"Metric": "Link base", "Value": link_base},
            {
                "Metric": "Scope",
                "Value": "All part numbers on page + balloon callouts",
            },
            {"Metric": "Unique parts with product_id", "Value": parts_found},
            {"Metric": "Unique parts without product_id", "Value": parts_missing},
            {"Metric": "Balloon links written (unique events)", "Value": balloons_linked},
            {"Metric": "Log file", "Value": str(log_path)},
            {"Metric": "Output folder", "Value": str(output_folder)},
        ]
    )
    files_df = pd.DataFrame(
        file_results,
        columns=[
            "pdf_name",
            "status",
            "parts_found_in_db",
            "parts_missing_in_db",
            "part_links_written",
            "balloons_detected",
            "balloon_links_written",
            "total_links_written",
            "output_pdf",
            "error",
        ],
    )
    parts_df = pd.DataFrame(
        part_rows,
        columns=[
            "pdf_name",
            "part_number",
            "product_id",
            "link",
            "id_found",
            "page",
            "source",
        ],
    )
    balloons_df = pd.DataFrame(
        balloon_rows,
        columns=[
            "pdf_name",
            "page",
            "item_no",
            "part_number",
            "product_id",
            "link",
            "status",
        ],
    )

    try:
        with pd.ExcelWriter(report_path) as writer:
            summary_df.to_excel(writer, sheet_name="Summary", index=False)
            files_df.to_excel(writer, sheet_name="Files", index=False)
            parts_df.to_excel(writer, sheet_name="Parts", index=False)
            balloons_df.to_excel(writer, sheet_name="Balloons", index=False)
        logger.info("Excel summary saved: %s", report_path)
    except PermissionError:
        alt_path = report_path.with_name(report_path.stem + "_combined.xlsx")
        with pd.ExcelWriter(alt_path) as writer:
            summary_df.to_excel(writer, sheet_name="Summary", index=False)
            files_df.to_excel(writer, sheet_name="Files", index=False)
            parts_df.to_excel(writer, sheet_name="Parts", index=False)
            balloons_df.to_excel(writer, sheet_name="Balloons", index=False)
        logger.warning(
            "Could not overwrite %s (file may be open). Saved as %s",
            report_path,
            alt_path,
        )


def link_pdf(
    pdf_path: Path,
    output_pdf: Path,
    product_map: dict[str, int],
    product_set: set[str],
    link_base: str,
    logger: logging.Logger,
) -> tuple[dict, list[dict], list[dict]]:
    """
    Return (stats, part_rows, balloon_rows).
    Writes one output PDF with part-text links and balloon links.
    """
    annotations: list[tuple[int, tuple[float, float, float, float], str]] = []
    part_index: dict[str, dict] = {}
    balloon_rows: list[dict] = []
    balloons_detected = 0
    balloon_links = 0
    part_links = 0

    for page_num, layout in enumerate(extract_pages(str(pdf_path))):
        page_number = page_num + 1
        items = collect_text_items(layout)

        # --- Part numbers anywhere on the page ---
        for match, bbox in collect_part_hits(layout):
            if match not in part_index:
                if match in product_set:
                    product_id = product_map[match]
                    url = f"{link_base.rstrip('/')}/{product_id}"
                    part_index[match] = {
                        "pdf_name": pdf_path.name,
                        "part_number": match,
                        "product_id": product_id,
                        "link": url,
                        "id_found": "Yes",
                        "page": page_number,
                        "source": "part_text",
                    }
                    logger.info("PART %s -> %s", match, url)
                else:
                    part_index[match] = {
                        "pdf_name": pdf_path.name,
                        "part_number": match,
                        "product_id": "",
                        "link": "",
                        "id_found": "No",
                        "page": page_number,
                        "source": "part_text",
                    }
                    logger.info("PART %s -> NOT FOUND in boms", match)

            if match in product_set:
                product_id = product_map[match]
                url = f"{link_base.rstrip('/')}/{product_id}"
                annotations.append((page_num, bbox, url))
                part_links += 1

        # --- Balloons via ITEM NO -> PART NO ---
        blocks = find_bom_blocks(items)
        _rows, bom_map, conflicts = extract_bom_rows(items, blocks)
        ellipses = collect_ellipse_candidates(layout)
        balloons = detect_balloons(items, ellipses)
        balloons_detected += len(balloons)

        for balloon in balloons:
            part_number = ""
            product_id: int | str = ""
            url = ""
            status = ""

            if balloon.item_no in conflicts:
                status = "Conflicting BOM item"
            elif balloon.item_no not in bom_map:
                status = "Item not found in page BOM"
            else:
                part_number = bom_map[balloon.item_no]
                mapped_id = product_map.get(part_number)
                if mapped_id is None:
                    status = "Part not found in boms"
                else:
                    product_id = mapped_id
                    url = f"{link_base.rstrip('/')}/{mapped_id}"
                    status = "Linked"
                    annotations.append(
                        (page_num, expand_rectangle(balloon.circle_bbox), url)
                    )
                    balloon_links += 1

            balloon_rows.append(
                {
                    "pdf_name": pdf_path.name,
                    "page": page_number,
                    "item_no": balloon.item_no,
                    "part_number": part_number,
                    "product_id": product_id,
                    "link": url,
                    "status": status,
                }
            )
            logger.info(
                "BALLOON %s -> %s%s",
                balloon.item_no,
                status,
                f" ({part_number})" if part_number else "",
            )

    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()
    for i, page in enumerate(reader.pages):
        if page.get("/Annots"):
            page[NameObject("/Annots")] = ArrayObject()
        writer.add_page(page)
        for page_num, rect, url in annotations:
            if i == page_num:
                writer.add_annotation(i, AnnotationBuilder.link(rect, url=url))

    with open(output_pdf, "wb") as handle:
        writer.write(handle)

    part_rows = list(part_index.values())
    part_rows.sort(key=lambda row: (row["id_found"] != "Yes", row["part_number"]))
    stats = {
        "parts_found": sum(1 for row in part_rows if row["id_found"] == "Yes"),
        "parts_missing": sum(1 for row in part_rows if row["id_found"] == "No"),
        "part_links": part_links,
        "balloons_detected": balloons_detected,
        "balloon_links": balloon_links,
        "total_links": len(annotations),
    }
    return stats, part_rows, balloon_rows


def run_linking_process(
    input_folder: Path,
    output_folder: Path,
    link_base: str,
    write_report: bool = True,
) -> int:
    output_folder.mkdir(parents=True, exist_ok=True)
    logger = setup_logging(output_folder)
    log_path = output_folder / "linking_process_parts_and_balloons.log"
    report_path = output_folder / "pdf_linking_summary_parts_and_balloons.xlsx"

    logger.info("Script started (parts + balloons)")
    logger.info("Input : %s", input_folder)
    logger.info("Output: %s", output_folder)
    logger.info("Link base: %s", link_base)
    logger.info("Scope: all part numbers + balloon callouts")

    try:
        engine, db_meta = build_engine(logger)
        logger.info(
            "Connected host=%s database=%s user=%s",
            db_meta["host"],
            db_meta["database"],
            db_meta["user"],
        )
        product_map = load_product_map(engine)
        engine.dispose()
        product_set = set(product_map.keys())
        logger.info("Loaded %s product codes", len(product_set))

        pdfs = sorted(
            f.name for f in input_folder.iterdir() if f.suffix.lower() == ".pdf"
        )
        total = len(pdfs)
        passed = 0
        failed = 0
        file_results: list[dict] = []
        all_part_rows: list[dict] = []
        all_balloon_rows: list[dict] = []
        logger.info("Found %s PDFs", total)

        for filename in pdfs:
            try:
                logger.info("Processing %s", filename)
                pdf_path = input_folder / filename
                output_pdf = output_folder / filename
                stats, part_rows, balloon_rows = link_pdf(
                    pdf_path,
                    output_pdf,
                    product_map,
                    product_set,
                    link_base,
                    logger,
                )
                passed += 1
                file_results.append(
                    {
                        "pdf_name": filename,
                        "status": "Done",
                        "parts_found_in_db": stats["parts_found"],
                        "parts_missing_in_db": stats["parts_missing"],
                        "part_links_written": stats["part_links"],
                        "balloons_detected": stats["balloons_detected"],
                        "balloon_links_written": stats["balloon_links"],
                        "total_links_written": stats["total_links"],
                        "output_pdf": str(output_pdf),
                        "error": "",
                    }
                )
                all_part_rows.extend(part_rows)
                all_balloon_rows.extend(balloon_rows)
                logger.info(
                    "%s -> Done (parts_found=%s part_links=%s balloons=%s balloon_links=%s total=%s)",
                    filename,
                    stats["parts_found"],
                    stats["part_links"],
                    stats["balloons_detected"],
                    stats["balloon_links"],
                    stats["total_links"],
                )
            except Exception as pdf_err:
                failed += 1
                file_results.append(
                    {
                        "pdf_name": filename,
                        "status": "Failed",
                        "parts_found_in_db": 0,
                        "parts_missing_in_db": 0,
                        "part_links_written": 0,
                        "balloons_detected": 0,
                        "balloon_links_written": 0,
                        "total_links_written": 0,
                        "output_pdf": "",
                        "error": str(pdf_err),
                    }
                )
                logger.error("Failed %s: %s", filename, pdf_err)

        if write_report:
            logger.info("Writing Excel summary...")
            write_excel_report(
                report_path,
                log_path,
                output_folder,
                link_base,
                total,
                passed,
                failed,
                file_results,
                all_part_rows,
                all_balloon_rows,
                logger,
            )
        else:
            logger.info("Skipping Excel report (--no-report)")

        logger.info("Finished. passed=%s failed=%s", passed, failed)
        return 0 if failed == 0 else 2
    except Exception:
        logger.critical("Uncaught fatal error:\n%s", traceback.format_exc())
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Embed portal links for all part numbers on each PDF page "
            "and for circled BOM item balloons."
        )
    )
    parser.add_argument("--input", type=Path, required=True, help="Input PDF folder")
    parser.add_argument("--output", type=Path, required=True, help="Output PDF folder")
    parser.add_argument(
        "--link-base",
        default=_env(
            "PORTAL_MACHINE_BASE_URL",
            default="http://localhost:3000/machines/371",
        ),
        help="Portal base URL used as {link_base}/{product_id}",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="Skip writing the Excel summary report",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_folder = args.input.expanduser().resolve()
    output_folder = args.output.expanduser().resolve()
    if not input_folder.is_dir():
        print(f"Input folder not found: {input_folder}", file=sys.stderr)
        return 1
    return run_linking_process(
        input_folder,
        output_folder,
        args.link_base,
        write_report=not args.no_report,
    )


if __name__ == "__main__":
    raise SystemExit(main())
