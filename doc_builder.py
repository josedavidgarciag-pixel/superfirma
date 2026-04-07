"""
doc_builder.py — Superfirma
============================
Inserts processed ink images into the correct cells of a .docx template.

Cell lookup strategy:
  1. Iterate ALL tables in the document (skips logo/header tables).
  2. For each row, find a cell whose text matches a known keyword.
  3. Return the cell immediately to the RIGHT of the label cell.

Supported keyword variants (case-insensitive, partial match):
  Firma   → 'firma alumno/a', 'firma del alumno', 'firma alumno', 'firma'
  Nombre  → 'nombre y apellidos alumno/a', 'nombre y apellidos del alumno',
             'nombre y apellidos', 'nombre del alumno', 'nombre'

Auto-scaling:
  Reads real cell width from OOXML (DXA units, 1 inch = 1440 DXA).
  Scales the PNG proportionally to fill up to 90% of cell width
  while respecting a max-height cap (firma: 1.0", nombre: 0.85").
"""

import io
from docx import Document
from docx.shared import Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from PIL import Image


# ── Cell utilities ────────────────────────────────────────────────────────────

def _unique_cells(row):
    """Deduplicate merged cells in a row (python-docx repeats them)."""
    seen, cells = set(), []
    for cell in row.cells:
        key = id(cell._tc)
        if key not in seen:
            seen.add(key)
            cells.append(cell)
    return cells


def _cell_width_inches(cell, default: float = 2.0) -> float:
    """Read actual cell width from OOXML in inches. Falls back to default."""
    tcW = cell._tc.find('.//' + qn('w:tcW'))
    if tcW is None:
        return default
    w_type = tcW.get(qn('w:type'), '')
    try:
        w_val = int(tcW.get(qn('w:w'), 0))
    except (ValueError, TypeError):
        return default
    if w_type == 'dxa':
        return w_val / 1440.0
    return default


def _find_cell_right_of(table, keyword: str):
    """Return cell to the right of the first cell whose text contains keyword."""
    kw = keyword.strip().lower()
    for row in table.rows:
        unique = _unique_cells(row)
        for i, cell in enumerate(unique):
            if kw in cell.text.strip().lower():
                if i + 1 < len(unique):
                    return unique[i + 1]
    return None


def _find_in_all_tables(doc, *keywords):
    """Search all tables for any of the given keywords; return target cell."""
    for kw in keywords:
        for tbl in doc.tables:
            cell = _find_cell_right_of(tbl, kw)
            if cell is not None:
                return cell
    return None


# ── Image scaling ─────────────────────────────────────────────────────────────

def _scale_png(png_bytes: bytes, max_w_in: float, max_h_in: float):
    """
    Resize PNG to fit within (max_w_in × max_h_in) preserving aspect ratio.
    Returns (BytesIO ready to read, Inches width).
    """
    img = Image.open(io.BytesIO(png_bytes)).convert('RGBA')
    w_px, h_px = img.size

    # 96 px/inch used by python-docx for EMU conversion
    max_w_px = max_w_in * 96
    max_h_px = max_h_in * 96
    ratio    = min(max_w_px / max(w_px, 1), max_h_px / max(h_px, 1))
    new_w    = max(1, int(w_px * ratio))
    new_h    = max(1, int(h_px * ratio))

    img = img.resize((new_w, new_h), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf, Inches(new_w / 96)


# ── Cell insertion ────────────────────────────────────────────────────────────

def _insert_into_cell(cell, img_buf, img_width, alignment):
    """Clear cell content and insert image as a centred/left-aligned run."""
    # Remove all existing content
    for para in list(cell.paragraphs):
        for run in list(para.runs):
            run.text = ''
        para.clear()

    p = cell.paragraphs[0] if cell.paragraphs else cell.add_paragraph()
    p.alignment = alignment
    p.add_run().add_picture(img_buf, width=img_width)


# ── Public API ────────────────────────────────────────────────────────────────

def insert_images_into_docx(docx_bytes: bytes,
                             firma_png: bytes,
                             nombre_png: bytes) -> bytes:
    """
    Open docx_bytes, locate firma and nombre cells, insert scaled PNGs.
    Raises ValueError if either cell cannot be found.
    Returns modified docx as bytes.
    """
    doc    = Document(io.BytesIO(docx_bytes))
    errors = []

    # ── Firma ────────────────────────────────────────────────────────────────
    firma_cell = _find_in_all_tables(
        doc,
        'firma alumno/a', 'firma del alumno/a',
        'firma del alumno', 'firma alumno', 'firma'
    )
    if firma_cell is None:
        errors.append(
            "Celda de FIRMA no encontrada. "
            "El documento debe contener una celda con el texto 'FIRMA ALUMNO/A'."
        )
    else:
        cell_w     = _cell_width_inches(firma_cell, default=2.2)
        buf, width = _scale_png(firma_png, max_w_in=cell_w * 0.90, max_h_in=1.0)
        _insert_into_cell(firma_cell, buf, width, WD_ALIGN_PARAGRAPH.CENTER)

    # ── Nombre ───────────────────────────────────────────────────────────────
    nombre_cell = _find_in_all_tables(
        doc,
        'nombre y apellidos alumno/a', 'nombre y apellidos del alumno/a',
        'nombre y apellidos del alumno', 'nombre y apellidos',
        'nombre del alumno', 'nombre alumno', 'nombre'
    )
    if nombre_cell is None:
        errors.append(
            "Celda de NOMBRE no encontrada. "
            "El documento debe contener una celda con el texto 'NOMBRE Y APELLIDOS ALUMNO/A'."
        )
    else:
        cell_w     = _cell_width_inches(nombre_cell, default=5.5)
        buf, width = _scale_png(nombre_png, max_w_in=cell_w * 0.90, max_h_in=0.85)
        _insert_into_cell(nombre_cell, buf, width, WD_ALIGN_PARAGRAPH.LEFT)

    if errors:
        raise ValueError('\n'.join(errors))

    out = io.BytesIO()
    doc.save(out)
    out.seek(0)
    return out.read()
