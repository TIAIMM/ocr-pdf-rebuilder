"""Searchable OCR-layer PDF: original scans over ordinary selectable text."""

import hashlib
import os
import re
import tempfile
from pathlib import Path

import fitz

from .component_runtime import ComponentRuntime
from .pipeline_config import *

SEARCHABLE_SKIP_CATEGORIES = frozenset({"Table", "Picture"})
SEARCHABLE_MIN_RECT_POINTS = 1.0
SEARCHABLE_MIN_FONT_SIZE = 0.1
SEARCHABLE_VISUAL_SAMPLE_LIMIT = 12
SEARCHABLE_VISUAL_VALIDATION_DPI = 72
SEARCHABLE_TEXT_RENDER_MODE = 0
SEARCHABLE_SIMPLE_FRACTION_RE = re.compile(
    r"\(([A-Za-z0-9Α-ωΑ-Ω]+)\)/\(([A-Za-z0-9Α-ωΑ-Ω]+)\)"
)


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
    text = _searchable_normalize_text(text).strip()
    if not text:
        return False
    font = fonts["cjk"] if has_cjk(text) else fonts["latin"]
    size = _searchable_fit_size(font, text, rect)
    if size is None:
        return False
    baseline_y = rect.y1 + font.descender * size
    writer.append((rect.x0, baseline_y), text, font=font, fontsize=size)
    return True


def _searchable_normalize_text(text):
    """Return searchable text without raw Markdown/LaTeX residue."""
    normalized = normalize_markdown_text(text or "")
    if LATEX_RESIDUE_RE.search(normalized) or contains_latex_fallback_command(normalized):
        normalized = normalize_markdown_text(linearize_latex_formula(normalized))
    return normalized


def _searchable_text_for_block(block):
    """Return the plain, searchable representation of one layout block."""
    category = str(block.get("category") or "Text")
    if category == "Formula":
        # Search indexes should contain a stable textual form rather than raw
        # TeX commands.  The visible formula remains in the source/image PDF;
        # this text is only the selectable/searchable companion layer.
        text = linearize_latex_formula(formula_source_text(block))
        # Keep simple fractions easy to find and copy.  Parentheses remain for
        # compound numerators/denominators where they carry grouping meaning.
        text = SEARCHABLE_SIMPLE_FRACTION_RE.sub(r"\1/\2", text)
    else:
        text = _searchable_normalize_text(block.get("text", ""))
    lines = [" ".join(line.split()) for line in str(text or "").splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _searchable_block_fallback_rects(block):
    rect = fitz.Rect(
        block["left"],
        block["top"],
        block["left"] + block["width"],
        block["top"] + block["height"],
    )
    text = _searchable_text_for_block(block)
    lines = [line for line in text.split("\n") if line.strip()]
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
    if str(block.get("category") or "Text") == "Formula":
        # Formula cells rarely carry trustworthy line/span boxes.  Use their
        # complete source bbox and the linearized search string so every
        # rendered formula remains findable without leaking TeX markup.
        return _searchable_block_fallback_rects(block)

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
                        # WPS indexes render-mode 3 text but does not reliably expose it
                        # to rectangular mouse selection.  Paint ordinary text first and
                        # keep an opaque paper layer and the source scan above it: the page
                        # remains visually identical even when a scan uses transparency,
                        # while viewers can hit-test the OCR glyph geometry.  PyMuPDF
                        # prepends each overlay=False operation, so draw the paper first.
                        page.draw_rect(
                            page.rect,
                            color=None,
                            fill=(1, 1, 1),
                            overlay=False,
                        )
                        writer.write_text(
                            page,
                            render_mode=SEARCHABLE_TEXT_RENDER_MODE,
                            overlay=False,
                        )
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


def build_validated_searchable_pdf(source_pdf_path, page_results, output_pdf_path):
    """Publish the overlay only after all searchable-specific checks pass."""
    output_pdf_path = Path(output_pdf_path)
    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{output_pdf_path.stem}.validate-", suffix=".tmp",
        dir=output_pdf_path.parent,
    )
    os.close(fd)
    temporary = Path(temporary)
    log(f"    Rendering searchable OCR-layer PDF: {output_pdf_path}")
    try:
        stats = build_searchable_pdf(
            source_pdf_path, page_results, temporary,
            progress_callback=lambda current, total: log(
                f"        Searchable PDF page {current}/{total}"
            ),
        )
        scan = scan_pdf_validation(
            temporary,
            progress_callback=lambda current, total: log(
                f"        Validate searchable PDF page {current}/{total}"
            ),
        )
        validate_pdf_page_count(temporary, stats["pages"], scan)
        validate_searchable_pdf_text_presence(temporary, stats["text_page_indexes"], scan)
        validate_searchable_pdf_visual_identity(source_pdf_path, temporary)
        os.replace(temporary, output_pdf_path)
        log(f"    Saved searchable variant: {output_pdf_path}")
        return stats
    finally:
        temporary.unlink(missing_ok=True)


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
    "SEARCHABLE_TEXT_RENDER_MODE",
    "SEARCHABLE_SIMPLE_FRACTION_RE",
    "build_searchable_pdf",
    "build_validated_searchable_pdf",
    "searchable_visual_sample_indexes",
    "validate_searchable_pdf_text_presence",
    "validate_searchable_pdf_visual_identity",
)
_COMPONENT_RUNTIME = ComponentRuntime(globals(), _COMPONENT_EXPORTS)


def component_exports():
    return _COMPONENT_RUNTIME.exports()


def invoke_component(name, namespace, *args, **kwargs):
    return _COMPONENT_RUNTIME.invoke(name, namespace, *args, **kwargs)
