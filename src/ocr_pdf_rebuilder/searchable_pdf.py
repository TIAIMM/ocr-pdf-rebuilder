"""Searchable OCR-layer PDF: original scans plus invisible text (render mode 3)."""

import hashlib
import os
import tempfile
from pathlib import Path

import fitz

from .component_runtime import ComponentRuntime
from .pipeline_config import *

SEARCHABLE_SKIP_CATEGORIES = frozenset({"Table", "Formula", "Picture"})
SEARCHABLE_MIN_RECT_POINTS = 1.0
SEARCHABLE_MIN_FONT_SIZE = 0.1
SEARCHABLE_VISUAL_SAMPLE_LIMIT = 12
SEARCHABLE_VISUAL_VALIDATION_DPI = 72
SEARCHABLE_INVISIBLE_RENDER_MODE = 3


def _searchable_rect_from_bbox(bbox, scale_x, scale_y):
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(value) for value in bbox)
    except (TypeError, ValueError):
        return None
    values = (x0, y0, x1, y1)
    if any(value != value or value in (float("inf"), float("-inf")) for value in values):
        return None
    return fitz.Rect(x0 * scale_x, y0 * scale_y, x1 * scale_x, y1 * scale_y)


def _searchable_clamp_rect(rect, page_rect):
    if rect is None:
        return None
    clamped = rect & page_rect
    if clamped.is_empty:
        return None
    if clamped.width < SEARCHABLE_MIN_RECT_POINTS:
        return None
    if clamped.height < SEARCHABLE_MIN_RECT_POINTS:
        return None
    return clamped


def _searchable_fit_size(font, text, rect):
    unit = font.text_length(text, fontsize=1.0)
    if unit <= 0:
        return None
    em_height = max(font.ascender - font.descender, 1.0)
    size = min(rect.height / em_height, rect.width / unit)
    if size < SEARCHABLE_MIN_FONT_SIZE:
        return None
    return size


def _searchable_place_text(writer, rect, text, fonts):
    text = (text or "").strip()
    if not text:
        return False
    font = fonts["cjk"] if has_cjk(text) else fonts["latin"]
    size = _searchable_fit_size(font, text, rect)
    if size is None:
        return False
    baseline_y = rect.y1 + font.descender * size
    writer.append((rect.x0, baseline_y), text, font=font, fontsize=size)
    return True


def _searchable_block_fallback_rects(block):
    rect = fitz.Rect(
        block["left"],
        block["top"],
        block["left"] + block["width"],
        block["top"] + block["height"],
    )
    lines = [line for line in str(block.get("text") or "").split("\n") if line.strip()]
    if not lines:
        return []
    if len(lines) == 1:
        return [(rect, lines[0])]
    row = rect.height / len(lines)
    return [
        (
            fitz.Rect(rect.x0, rect.y0 + index * row, rect.x1, rect.y0 + (index + 1) * row),
            lines[index],
        )
        for index in range(len(lines))
    ]


def _searchable_block_placements(block, page_rect):
    scale_x = block.get("bbox_scale_x", 1.0)
    scale_y = block.get("bbox_scale_y", 1.0)
    placements = []
    any_placed = False
    for line in block.get("line_info") or []:
        if not isinstance(line, dict):
            continue
        spans = line.get("spans") or []
        spans_with_bbox = [
            span
            for span in spans
            if isinstance(span, dict)
            and _searchable_rect_from_bbox(span.get("bbox"), scale_x, scale_y) is not None
        ]
        placed_in_line = False
        if spans_with_bbox:
            for span in spans_with_bbox:
                rect = _searchable_clamp_rect(
                    _searchable_rect_from_bbox(span.get("bbox"), scale_x, scale_y),
                    page_rect,
                )
                if rect and (span.get("text") or "").strip():
                    placements.append((rect, span["text"]))
                    placed_in_line = True
        else:
            rect = _searchable_clamp_rect(
                _searchable_rect_from_bbox(line.get("bbox"), scale_x, scale_y),
                page_rect,
            )
            if rect and (line.get("text") or "").strip():
                placements.append((rect, line["text"]))
                placed_in_line = True
        any_placed = any_placed or placed_in_line
    if not any_placed:
        return [
            (clamped, text)
            for rect, text in _searchable_block_fallback_rects(block)
            for clamped in [_searchable_clamp_rect(rect, page_rect)]
            if clamped
        ]
    return placements


def _searchable_page_placements(page, result):
    page_rect = page.rect
    placements = []
    skipped = 0
    for order, cell in enumerate(result.get("cells") or []):
        block = cell_to_block(
            cell,
            order,
            page_rect.width,
            page_rect.height,
            result.get("image_size"),
        )
        if block is None:
            continue
        if block["category"] in SEARCHABLE_SKIP_CATEGORIES:
            skipped += 1
            continue
        if not str(block.get("text") or "").strip():
            continue
        placements.extend(_searchable_block_placements(block, page_rect))
    return placements, skipped


def build_searchable_pdf(source_pdf_path, page_results, output_pdf_path, progress_callback=None):
    source_pdf_path = Path(source_pdf_path)
    output_pdf_path = Path(output_pdf_path)
    if not source_pdf_path.exists():
        raise RuntimeError(f"Source PDF not found: {source_pdf_path}")
    stats = {
        "pages": 0,
        "spans_placed": 0,
        "text_page_indexes": [],
        "pages_without_text": [],
        "skipped_category_cells": 0,
    }
    with fitz.open(source_pdf_path) as doc:
        stats["pages"] = doc.page_count
        fonts = {"cjk": fitz.Font("cjk"), "latin": fitz.Font("helv")}
        for page_index in range(doc.page_count):
            page = doc[page_index]
            placed = 0
            result = (page_results or {}).get(page_index)
            if result is not None and not result.get("blank_page"):
                placements, skipped = _searchable_page_placements(page, result)
                stats["skipped_category_cells"] += skipped
                if placements:
                    writer = fitz.TextWriter(page.rect)
                    for rect, text in placements:
                        if _searchable_place_text(writer, rect, text, fonts):
                            placed += 1
                    if placed:
                        writer.write_text(page, render_mode=SEARCHABLE_INVISIBLE_RENDER_MODE)
            if placed:
                stats["text_page_indexes"].append(page_index)
                stats["spans_placed"] += placed
            else:
                stats["pages_without_text"].append(page_index + 1)
            if progress_callback is not None:
                progress_callback(page_index + 1, doc.page_count)
        output_pdf_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{output_pdf_path.name}.",
            suffix=".tmp",
            dir=output_pdf_path.parent,
        )
        os.close(fd)
        try:
            doc.save(tmp_name, deflate=True, garbage=0)
            os.replace(tmp_name, output_pdf_path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise
    return stats


def searchable_visual_sample_indexes(page_count, limit=SEARCHABLE_VISUAL_SAMPLE_LIMIT):
    if page_count <= 0:
        return []
    count = min(limit, page_count)
    if count <= 1:
        return [0]
    return sorted(
        {round(index * (page_count - 1) / (count - 1)) for index in range(count)}
    )


def validate_searchable_pdf_text_presence(searchable_pdf_path, text_page_indexes, validation_scan=None):
    scan = validation_scan or scan_pdf_validation(searchable_pdf_path)
    counts = scan.get("text_char_counts") or []
    offenders = [
        page_index + 1
        for page_index in text_page_indexes
        if not (0 <= page_index < len(counts) and counts[page_index] > 0)
    ]
    if offenders:
        listed = ", ".join(str(page) for page in offenders[:40])
        suffix = f" (+{len(offenders) - 40} more)" if len(offenders) > 40 else ""
        raise RuntimeError(
            f"Searchable OCR-layer PDF is missing expected invisible text on pages: {listed}{suffix}"
        )


def validate_searchable_pdf_visual_identity(
    source_pdf_path,
    searchable_pdf_path,
    sample_page_indexes=None,
    dpi=SEARCHABLE_VISUAL_VALIDATION_DPI,
):
    with fitz.open(source_pdf_path) as source, fitz.open(searchable_pdf_path) as searchable:
        if source.page_count != searchable.page_count:
            raise RuntimeError(
                "Searchable OCR-layer PDF page count "
                f"{searchable.page_count} does not match source page count {source.page_count}"
            )
        indexes = (
            list(sample_page_indexes)
            if sample_page_indexes is not None
            else searchable_visual_sample_indexes(source.page_count)
        )
        matrix = fitz.Matrix(dpi / 72, dpi / 72)
        offenders = []
        for page_index in indexes:
            if not (0 <= page_index < source.page_count):
                continue
            source_digest = hashlib.sha256(
                source[page_index].get_pixmap(matrix=matrix, alpha=False).samples
            ).hexdigest()
            searchable_digest = hashlib.sha256(
                searchable[page_index].get_pixmap(matrix=matrix, alpha=False).samples
            ).hexdigest()
            if source_digest != searchable_digest:
                offenders.append(page_index + 1)
        if offenders:
            listed = ", ".join(str(page) for page in offenders[:40])
            suffix = f" (+{len(offenders) - 40} more)" if len(offenders) > 40 else ""
            raise RuntimeError(
                f"Searchable OCR-layer PDF changed rendered pixels on pages: {listed}{suffix}"
            )


_COMPONENT_EXPORTS = (
    "SEARCHABLE_SKIP_CATEGORIES",
    "SEARCHABLE_MIN_RECT_POINTS",
    "SEARCHABLE_MIN_FONT_SIZE",
    "SEARCHABLE_VISUAL_SAMPLE_LIMIT",
    "SEARCHABLE_VISUAL_VALIDATION_DPI",
    "SEARCHABLE_INVISIBLE_RENDER_MODE",
    "build_searchable_pdf",
    "searchable_visual_sample_indexes",
    "validate_searchable_pdf_text_presence",
    "validate_searchable_pdf_visual_identity",
)
_COMPONENT_RUNTIME = ComponentRuntime(globals(), _COMPONENT_EXPORTS)


def component_exports():
    return _COMPONENT_RUNTIME.exports()


def invoke_component(name, namespace, *args, **kwargs):
    return _COMPONENT_RUNTIME.invoke(name, namespace, *args, **kwargs)
