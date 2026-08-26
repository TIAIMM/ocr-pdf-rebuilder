"""ReportLab font selection, measurement, fitting and PDF rendering."""

from functools import lru_cache
from pathlib import Path
import re

import fitz

from .component_runtime import ComponentRuntime
from .pipeline_config import *

def reportlab_register_fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    global REPORTLAB_FONTS_REGISTERED
    if REPORTLAB_FONTS_REGISTERED:
        return

    require_file(CJK_REGULAR_FONT, "CJK regular TTF font not found")
    require_file(CJK_MEDIUM_FONT, "CJK medium TTF font not found")
    require_file(CJK_BOLD_FONT, "CJK bold TTF font not found")
    require_file(LATIN_REGULAR_FONT, "Latin regular font not found")
    require_file(LATIN_BOLD_FONT, "Latin bold font not found")
    require_file(LATIN_ITALIC_FONT, "Latin italic font not found")
    require_file(GREEK_REGULAR_FONT, "Greek regular font not found")
    require_file(GREEK_BOLD_FONT, "Greek bold font not found")
    require_file(GREEK_ITALIC_FONT, "Greek italic font not found")
    require_file(MONO_FONT, "Mono font not found")

    pdfmetrics.registerFont(TTFont("SourceHanSerifCN-Regular", str(CJK_REGULAR_FONT)))
    pdfmetrics.registerFont(TTFont("SourceHanSerifCN-Medium", str(CJK_MEDIUM_FONT)))
    pdfmetrics.registerFont(TTFont("SourceHanSerifCN-Bold", str(CJK_BOLD_FONT)))

    pdfmetrics.registerFont(TTFont("Latin-Regular", str(LATIN_REGULAR_FONT)))
    pdfmetrics.registerFont(TTFont("Latin-Bold", str(LATIN_BOLD_FONT)))
    pdfmetrics.registerFont(TTFont("Latin-Italic", str(LATIN_ITALIC_FONT)))

    pdfmetrics.registerFont(TTFont("Greek-Regular", str(GREEK_REGULAR_FONT)))
    pdfmetrics.registerFont(TTFont("Greek-Bold", str(GREEK_BOLD_FONT)))
    pdfmetrics.registerFont(TTFont("Greek-Italic", str(GREEK_ITALIC_FONT)))

    pdfmetrics.registerFont(TTFont("Mono", str(MONO_FONT)))

    latin_chars = set(pdfmetrics.getFont("Latin-Regular").face.charToGlyph.keys())
    greek_chars = set(pdfmetrics.getFont("Greek-Regular").face.charToGlyph.keys())
    missing_fractions = [
        char for char in VULGAR_FRACTION_CHARS
        if ord(char) not in latin_chars and ord(char) not in greek_chars
    ]
    if missing_fractions:
        raise RuntimeError(
            "Registered Latin/Greek fallback fonts do not cover vulgar fraction characters: "
            + " ".join(f"{char}(U+{ord(char):04X})" for char in missing_fractions)
        )

    REPORTLAB_FONTS_REGISTERED = True

LATIN_LIKE_RE = re.compile(
    r"[A-Za-z0-9"
    r"\u00C0-\u024F"   # 拉丁扩展，含 ä é ö ü 等
    r"\u1E00-\u1EFF"   # 更多拉丁扩展
    r"\u0250-\u02AF"   # IPA extensions
    r"\u1D00-\u1DBF"   # Phonetic extensions
    r"\u2C60-\u2C7F"   # Latin Extended-C
    r"\uA720-\uA7FF"   # Latin Extended-D
    r"\uAB30-\uAB6F"   # Latin Extended-E
    r"]"
)

GREEK_LIKE_RE = re.compile(
    r"[\u0370-\u03FF"   # 希腊字母
    r"\u1F00-\u1FFF"   # 希腊扩展，含 polytonic Greek
    r"]"
)

CYRILLIC_LIKE_RE = re.compile(
    r"[\u0400-\u052F"   # Cyrillic + Supplement
    r"\u2DE0-\u2DFF"   # Cyrillic Extended-A
    r"\uA640-\uA69F"   # Cyrillic Extended-B
    r"\u1C80-\u1C8F"   # Cyrillic Extended-C
    r"\u1E030-\u1E08F" # Cyrillic Extended-D
    r"]"
)

COMBINING_MARK_RE = re.compile(
    r"[\u0300-\u036F"   # Combining Diacritical Marks
    r"\u1AB0-\u1AFF"   # Combining Diacritical Marks Extended
    r"\u1DC0-\u1DFF"   # Combining Diacritical Marks Supplement
    r"\u20D0-\u20FF"   # Combining Diacritical Marks for Symbols
    r"\uFE20-\uFE2F"   # Combining Half Marks
    r"]"
)

CJK_LIKE_RE = re.compile(
    r"[\u3400-\u9fff"
    r"\u3000-\u303f"
    r"\uff00-\uffef"
    r"]"
)

FONT_COVERAGE_CACHE = {}
REPORTLAB_REGISTERED_FONT_NAMES = (
    "SourceHanSerifCN-Regular",
    "SourceHanSerifCN-Medium",
    "SourceHanSerifCN-Bold",
    "Latin-Regular",
    "Latin-Bold",
    "Latin-Italic",
    "Greek-Regular",
    "Greek-Bold",
    "Greek-Italic",
    "Mono",
)
REPORTLAB_MISSING_GLYPH_REPLACEMENT = "□"


def font_supports_char(fontname, ch):
    from reportlab.pdfbase import pdfmetrics

    if not ch:
        return True
    if fontname not in FONT_COVERAGE_CACHE:
        reportlab_register_fonts()
        font = pdfmetrics.getFont(fontname)
        FONT_COVERAGE_CACHE[fontname] = set(font.face.charToGlyph.keys())
    return ord(ch) in FONT_COVERAGE_CACHE[fontname]


def font_supports_text(fontname, text):
    return all(ch.isspace() or font_supports_char(fontname, ch) for ch in str(text or ""))


@lru_cache(maxsize=65536)
def reportlab_char_has_registered_glyph(ch):
    if not ch or ch.isspace():
        return True
    return any(font_supports_char(fontname, ch) for fontname in REPORTLAB_REGISTERED_FONT_NAMES)


def reportlab_unsupported_chars(text):
    return sorted(
        {ch for ch in str(text or "") if not reportlab_char_has_registered_glyph(ch)},
        key=ord,
    )


def reportlab_safe_text(text):
    text = str(text or "")
    if not text:
        return ""
    return "".join(
        ch if reportlab_char_has_registered_glyph(ch) else REPORTLAB_MISSING_GLYPH_REPLACEMENT
        for ch in text
    )


def serif_script_for_char(ch):
    # SourceSerif is the primary unified serif family for Latin, Greek, and Cyrillic.
    # DejaVu Serif is used only as a glyph-safety fallback to avoid U+0000 extraction.
    if font_supports_char("Latin-Regular", ch):
        return "latin"
    if font_supports_char("Greek-Regular", ch):
        return "serif-fallback"
    if font_supports_char("Mono", ch):
        return "mono-fallback"
    if font_supports_char("SourceHanSerifCN-Regular", ch):
        return "cjk"
    return "missing"


def script_supports_char(script, ch):
    fontname = {
        "latin": "Latin-Regular",
        "serif-fallback": "Greek-Regular",
        "greek": "Greek-Regular",
        "mono-fallback": "Mono",
        "cjk": "SourceHanSerifCN-Regular",
    }.get(script)
    return bool(fontname and font_supports_char(fontname, ch))


def char_script(ch):
    if COMBINING_MARK_RE.match(ch):
        return "combining"
    if ch in GREEK_SPACING_MARKS:
        return "serif-fallback"
    if LATIN_LIKE_RE.match(ch) or GREEK_LIKE_RE.match(ch) or CYRILLIC_LIKE_RE.match(ch):
        return serif_script_for_char(ch)
    if CJK_LIKE_RE.match(ch) and font_supports_char("SourceHanSerifCN-Regular", ch):
        return "cjk"
    if ch.isspace():
        return "neutral"
    if ord(ch) < 128:
        return "neutral"
    if font_supports_char("Latin-Regular", ch):
        return "latin"
    if font_supports_char("Greek-Regular", ch):
        return "serif-fallback"
    if font_supports_char("Mono", ch):
        return "mono-fallback"
    if font_supports_char("SourceHanSerifCN-Regular", ch):
        return "cjk"
    return "missing"


def split_reportlab_font_runs(segments):
    result = []

    for seg in segments:
        style = seg.get("style", "normal")
        # OCR/LaTeX normalization can introduce a different Unicode character
        # from the source spelling (for example F^-1 -> F⁻¹). Normalize before
        # assigning font runs so every final glyph is checked against the font
        # that will actually draw it.
        text = normalize_draw_segment_text(
            text=seg.get("text", ""),
            strip_markdown=style != "code",
        )
        text = reportlab_safe_text(text)
        if not text:
            continue

        current = ""
        current_script = None

        for ch in text:
            script = char_script(ch)

            if script == "combining":
                if current_script and script_supports_char(current_script, ch):
                    script = current_script
                else:
                    script = serif_script_for_char(ch)

            if script == "neutral":
                script = current_script or "cjk"

            if current and script != current_script:
                new_seg = dict(seg)
                new_seg["text"] = current
                new_seg["script"] = current_script
                result.append(new_seg)

                current = ch
                current_script = script
            else:
                current += ch
                current_script = script

        if current:
            new_seg = dict(seg)
            new_seg["text"] = current
            new_seg["script"] = current_script or "cjk"
            result.append(new_seg)

    return result

def reportlab_font_for_segment(seg, line_style):
    style = seg.get("style", "normal")
    script = seg.get("script", "cjk")

    if script == "mono-fallback":
        fontname = "Mono"
    elif script == "latin":
        if style == "code":
            fontname = "Mono"
        elif style == "math":
            fontname = "Latin-Italic"
        elif style in {"strong", "strongitalic"} or line_is_bold(line_style):
            fontname = "Latin-Bold"
        elif style == "italic" or line_is_italic(line_style):
            fontname = "Latin-Italic"
        else:
            fontname = "Latin-Regular"
    elif script in {"greek", "serif-fallback"}:
        if style == "code":
            fontname = "Greek-Regular"
        elif style == "math":
            fontname = "Greek-Italic"
        elif style in {"strong", "strongitalic"} or line_is_bold(line_style):
            fontname = "Greek-Bold"
        elif style == "italic" or line_is_italic(line_style):
            fontname = "Greek-Italic"
        else:
            fontname = "Greek-Regular"
    elif style in {"strong", "strongitalic"} or line_style in {"strong", "heading1", "heading2"}:
        fontname = "SourceHanSerifCN-Bold"
    elif line_style in {"heading3", "heading4", "heading5", "heading6"}:
        fontname = "SourceHanSerifCN-Medium"
    else:
        fontname = "SourceHanSerifCN-Regular"

    text = seg.get("text", "")
    if font_supports_text(fontname, text):
        return fontname, False

    italic = style in {"math", "italic", "strongitalic"} or line_is_italic(line_style)
    bold = style in {"strong", "strongitalic"} or line_is_bold(line_style)
    candidates = (
        "Greek-Italic" if italic else "Greek-Bold" if bold else "Greek-Regular",
        "Mono",
        "SourceHanSerifCN-Bold" if bold else "SourceHanSerifCN-Regular",
        "Latin-Italic" if italic else "Latin-Bold" if bold else "Latin-Regular",
    )
    for candidate in candidates:
        if font_supports_text(candidate, text):
            return candidate, False
    unsupported = reportlab_unsupported_chars(text)
    details = " ".join(f"{ch}(U+{ord(ch):04X})" for ch in unsupported)
    raise RuntimeError(f"No registered ReportLab font covers segment characters: {details}")


@lru_cache(maxsize=32768)
def reportlab_text_width(text, fontname, fontsize):
    from reportlab.pdfbase import pdfmetrics

    return pdfmetrics.stringWidth(text, fontname, fontsize)


def reportlab_plain_segments(text):
    text = normalize_draw_segment_text(text)
    if not text:
        return []
    return split_reportlab_font_runs([{"text": text, "style": "normal"}])


@lru_cache(maxsize=8192)
def reportlab_plain_text_width(text, fontsize):
    total = 0.0
    for seg in reportlab_plain_segments(text):
        fontname, _fake_bold = reportlab_font_for_segment(seg, "normal")
        total += reportlab_text_width(seg["text"], fontname, fontsize)
    return total


def reportlab_draw_plain_text(c, x, baseline, text, fontsize):
    cursor = x
    for seg in reportlab_plain_segments(text):
        seg_text = seg["text"]
        if not seg_text:
            continue
        fontname, fake_bold = reportlab_font_for_segment(seg, "normal")
        reportlab_draw_text(c, cursor, baseline, seg_text, fontname, fontsize, fake_bold)
        cursor += reportlab_text_width(seg_text, fontname, fontsize)


def reportlab_split_to_fit(text, fontname, fontsize, available_width):
    if reportlab_text_width(text, fontname, fontsize) <= available_width:
        return text, ""
    if available_width <= fontsize:
        return "", text
    current = ""
    for ch in text:
        trial = current + ch
        if current and reportlab_text_width(trial, fontname, fontsize) > available_width:
            return current, text[len(current):]
        current = trial
    return current, ""


def reportlab_split_plain_lines(text, fontname, fontsize, available_width):
    text = normalize_draw_segment_text(text)
    if not text:
        return []

    lines = []
    pending = text
    while pending:
        if reportlab_plain_text_width(pending, fontsize) <= available_width:
            fit_text, rest = pending, ""
        else:
            fit_text = ""
            for index, ch in enumerate(pending):
                trial = fit_text + ch
                if fit_text and reportlab_plain_text_width(trial, fontsize) > available_width:
                    rest = pending[index:]
                    break
                fit_text = trial
            else:
                rest = ""
            if not fit_text:
                fit_text, rest = pending[:1], pending[1:]
        lines.append(fit_text)
        pending = rest
    return lines


def reportlab_toc_table_layout(rect, rows, fontsize, line_height):
    toc_rows = []
    for row in rows:
        left_text, right_text = table_row_parts(row)
        if left_text or right_text:
            toc_rows.append(
                {
                    "left_text": left_text,
                    "right_text": right_text,
                    "level": table_row_level(row, left_text),
                }
            )
    if not toc_rows:
        return []

    min_line_gap = fontsize * max(1.0, line_height)
    natural_line_gap = rect.height / max(len(toc_rows), 1)
    line_gap = clamp(natural_line_gap, min_line_gap, fontsize * 2.15)
    effective_fontsize = min(fontsize, line_gap * 0.72)

    right_width = 0.0
    for row in toc_rows:
        right_text = row["right_text"]
        if right_text:
            right_width = max(right_width, reportlab_plain_text_width(right_text, effective_fontsize))
    if right_width:
        right_width = min(right_width + effective_fontsize * 0.6, rect.width * 0.22)
    gap = effective_fontsize * 0.8 if right_width else 0.0
    left_width = rect.width - right_width - gap
    if left_width <= effective_fontsize * 3:
        return None

    y_top = rect.y0
    layout = []
    for row in toc_rows:
        indent = min(left_width * 0.28, row["level"] * effective_fontsize * 1.6)
        available_left_width = max(effective_fontsize * 3, left_width - indent)
        left_lines = reportlab_split_plain_lines(
            row["left_text"],
            None,
            effective_fontsize,
            available_left_width,
        ) or [""]
        row_height = len(left_lines) * line_gap
        if y_top + row_height > rect.y1 + 0.01:
            return None
        layout.append(
            {
                "y_top": y_top,
                "indent": indent,
                "left_lines": left_lines,
                "right_text": row["right_text"],
                "right_width": right_width,
                "line_gap": line_gap,
                "fontsize": effective_fontsize,
            }
        )
        y_top += row_height
    return {"mode": "toc", "rows": layout}


def table_column_count(rows):
    return max((row.get("col_count", len(row.get("cells", []))) for row in rows), default=1)


def reportlab_plain_table_layout(rect, rows, fontsize, line_height):
    if not rows:
        return []

    col_count = table_column_count(rows)
    col_gap = fontsize * 0.8
    usable_width = rect.width - col_gap * max(0, col_count - 1)
    if usable_width <= fontsize * col_count:
        return None

    col_width = usable_width / col_count
    line_gap = fontsize * line_height
    y_top = rect.y0
    layout = []
    for row in rows:
        cells = row.get("cells") or row.get("nonempty_cells") or []
        cell_lines = []
        max_lines = 1
        for col_index in range(col_count):
            cell = cells[col_index] if col_index < len(cells) else ""
            lines = reportlab_split_plain_lines(cell, None, fontsize, col_width) or [""]
            cell_lines.append(lines)
            max_lines = max(max_lines, len(lines))
        row_height = max_lines * line_gap
        if y_top + row_height > rect.y1 + 0.01:
            return None
        layout.append(
            {
                "y_top": y_top,
                "cell_lines": cell_lines,
                "col_width": col_width,
                "col_gap": col_gap,
                "line_gap": line_gap,
                "fontsize": fontsize,
            }
        )
        y_top += row_height
    return {"mode": "plain", "rows": layout}


def reportlab_grid_table_layout(rect, rows, fontsize, line_height):
    if not rows:
        return []

    line_gap = fontsize * max(0.92, min(line_height, 1.10))
    y_top = rect.y0
    layout = []
    for row in rows:
        cells = row.get("cells") or row.get("nonempty_cells") or []
        nonempty = [normalize_text(cell) for cell in cells if normalize_text(cell)]
        if not nonempty:
            continue
        indent = min(rect.width * 0.22, float(row.get("first_col", 0)) * fontsize * 1.3)
        row_text = "    ".join(nonempty)
        lines = reportlab_split_plain_lines(row_text, None, fontsize, max(fontsize * 3, rect.width - indent)) or [""]
        row_height = len(lines) * line_gap
        if y_top + row_height > rect.y1 + 0.01:
            return None
        layout.append(
            {
                "y_top": y_top,
                "indent": indent,
                "lines": lines,
                "line_gap": line_gap,
                "fontsize": fontsize,
            }
        )
        y_top += row_height
    return {"mode": "grid", "rows": layout}


def reportlab_table_layout(rect, block_or_text, fontsize, line_height):
    rows = table_rows_for_block(block_or_text)
    if not rows:
        return []
    if is_toc_table(rows):
        layout = reportlab_toc_table_layout(rect, rows, fontsize, line_height)
        if layout is not None:
            return layout
    layout = reportlab_plain_table_layout(rect, rows, fontsize, line_height)
    if layout is not None:
        return layout
    return reportlab_grid_table_layout(rect, rows, fontsize, line_height)


def reportlab_draw_table_block(c, page_height, rect, block_or_text, fontsize, line_height, dry_run=False):
    text = block_or_text.get("text", "") if isinstance(block_or_text, dict) else str(block_or_text or "")
    text = normalize_markdown_text(text)
    assert_no_radicals_in_text(text, "reportlab table block")
    layout = reportlab_table_layout(rect, block_or_text, fontsize, line_height)
    if layout is None:
        return False
    if dry_run:
        return True

    if layout == []:
        return True

    if layout["mode"] == "plain":
        for row in layout["rows"]:
            y_top = row["y_top"]
            line_gap = row["line_gap"]
            fontsize = row["fontsize"]
            for col_index, lines in enumerate(row["cell_lines"]):
                x = rect.x0 + col_index * (row["col_width"] + row["col_gap"])
                for line_index, line in enumerate(lines):
                    if not line:
                        continue
                    baseline = page_height - (y_top + line_index * line_gap) - fontsize
                    reportlab_draw_plain_text(c, x, baseline, line, fontsize)
        return True

    if layout["mode"] == "grid":
        if isinstance(block_or_text, dict):
            block_or_text["table_structural_fallback"] = True
        for row in layout["rows"]:
            y_top = row["y_top"]
            line_gap = row["line_gap"]
            fontsize = row["fontsize"]
            x = rect.x0 + row.get("indent", 0.0)
            for line_index, line in enumerate(row["lines"]):
                if not line:
                    continue
                baseline = page_height - (y_top + line_index * line_gap) - fontsize
                reportlab_draw_plain_text(c, x, baseline, line, fontsize)
        return True

    for row in layout["rows"]:
        y_top = row["y_top"]
        line_gap = row["line_gap"]
        fontsize = row["fontsize"]
        for line_index, left_text in enumerate(row["left_lines"]):
            baseline = page_height - y_top - fontsize
            x_left = rect.x0 + row.get("indent", 0.0)
            if left_text:
                reportlab_draw_plain_text(c, x_left, baseline, left_text, fontsize)
            if line_index == 0 and row["right_text"]:
                right_width = reportlab_plain_text_width(row["right_text"], fontsize)
                right_x = rect.x1 - right_width
                left_width = reportlab_plain_text_width(left_text, fontsize) if left_text else 0.0
                dot_start = x_left + left_width + fontsize * 0.7
                dot_end = right_x - fontsize * 0.7
                dot_width = reportlab_text_width(".", "Latin-Regular", fontsize)
                if dot_end > dot_start + dot_width:
                    dot_count = int((dot_end - dot_start) / dot_width)
                    reportlab_draw_text(c, dot_start, baseline, "." * dot_count, "Latin-Regular", fontsize)
                reportlab_draw_plain_text(c, right_x, baseline, row["right_text"], fontsize)
            y_top += line_gap
    return True


def reportlab_draw_text(c, x, y, text, fontname, fontsize, fake_bold=False):
    text = text_for_single_pdf_draw(text)
    if not text:
        return
    if not font_supports_text(fontname, text):
        unsupported = sorted(
            {
                ch
                for ch in text
                if not ch.isspace() and not font_supports_char(fontname, ch)
            },
            key=ord,
        )
        details = " ".join(f"{ch}(U+{ord(ch):04X})" for ch in unsupported)
        raise RuntimeError(
            f"Unsafe ReportLab font run reached drawing: font={fontname}, chars={details}"
        )
    if fake_bold:
        c.saveState()
        c.setLineWidth(max(0.25, fontsize * 0.035))
        c.setStrokeColorRGB(0, 0, 0)
        c.setFillColorRGB(0, 0, 0)
        text_obj = c.beginText(x, y)
        text_obj.setFont(fontname, fontsize)
        text_obj.setTextRenderMode(2)
        text_obj.textOut(text)
        c.drawText(text_obj)
        c.restoreState()
        return
    c.setFont(fontname, fontsize)
    c.drawString(x, y, text)


def block_page_rect(block):
    return fitz.Rect(
        block["left"],
        block["top"],
        block["left"] + block["width"],
        block["top"] + block["height"],
    )


def line_info_rect(block, line):
    bbox = line.get("bbox") if isinstance(line, dict) else None
    if not isinstance(bbox, list) or len(bbox) != 4:
        return None

    scale_x = float(block.get("bbox_scale_x", 1.0))
    scale_y = float(block.get("bbox_scale_y", 1.0))
    x0, y0, x1, y1 = [float(value) for value in bbox]
    rect = fitz.Rect(x0 * scale_x, y0 * scale_y, x1 * scale_x, y1 * scale_y)
    parent = block_page_rect(block)
    rect = rect & parent
    if rect.is_empty or rect.width <= 0 or rect.height <= 0:
        return None
    return rect


def structured_line_info_has_latex_residue(block):
    """Detect raw TeX retained in structured line/span drawing caches."""

    def value_has_residue(value):
        return isinstance(value, str) and bool(LATEX_RESIDUE_RE.search(value))

    for line in block.get("line_info") or []:
        if not isinstance(line, dict):
            continue
        if any(
            value_has_residue(line.get(key))
            for key in ("text", "content", "source_text", "latex", "html")
        ):
            return True
        for span in line.get("spans") or []:
            if not isinstance(span, dict):
                continue
            if any(
                value_has_residue(span.get(key))
                for key in ("text", "content", "source_text", "latex", "html")
            ):
                return True
    return False


def usable_line_info(block):
    if not USE_LINE_INFO_RENDERING:
        return []
    if block.get("category") == "Table":
        return []
    lines = []
    for line in block.get("line_info") or []:
        text = normalize_markdown_text(line.get("text", ""))
        if not text.strip():
            continue
        rect = line_info_rect(block, line)
        if rect is None:
            continue
        text_parts = [part.strip() for part in re.split(r"\n+", text) if part.strip()]
        if len(text_parts) > 1:
            part_height = rect.height / len(text_parts)
            for part_index, part in enumerate(text_parts):
                part_rect = fitz.Rect(
                    rect.x0,
                    rect.y0 + part_height * part_index,
                    rect.x1,
                    rect.y0 + part_height * (part_index + 1),
                )
                lines.append({"text": part, "rect": part_rect, "spans": []})
            continue
        lines.append({"text": text, "rect": rect, "spans": line.get("spans") or []})

    if not lines:
        return []

    clean_text = clean_block_text(block)
    if len(lines) == 1:
        # MinerU preproc sometimes stores a whole paragraph as one giant line.
        # Treat that as paragraph text, not as a real line coordinate.
        if len(clean_text) > 80 and lines[0]["rect"].height > block["height"] * 0.45:
            return []
        return lines

    lines.sort(key=lambda item: (item["rect"].y0, item["rect"].x0))
    return lines


def reportlab_segments_for_line(text):
    segments, indent, line_style = markdown_line_to_segments(text)
    return split_reportlab_font_runs(segments), indent, line_style


def reportlab_measure_segments(segments, line_style, fontsize):
    total = 0.0
    line_fontsize = fontsize * line_font_scale(line_style)
    for seg in segments:
        seg_text = normalize_draw_segment_text(seg["text"], strip_markdown=seg["style"] != "code")
        if not seg_text:
            continue
        fontname, _fake_bold = reportlab_font_for_segment(seg, line_style)
        total += reportlab_text_width(seg_text, fontname, line_fontsize)
    return total


def reportlab_fit_line_fontsize(rect, text, start_fontsize, min_fontsize):
    segments, indent, line_style = reportlab_segments_for_line(text)
    if not segments:
        return start_fontsize, segments, indent, line_style

    fontsize = min(float(start_fontsize), max(3.2, rect.height * 0.86))
    min_fontsize = max(3.2, float(min_fontsize))
    while fontsize >= min_fontsize - 0.001:
        line_fontsize = fontsize * line_font_scale(line_style)
        width = reportlab_measure_segments(segments, line_style, fontsize)
        if line_fontsize <= rect.height + 0.01 and width + indent <= rect.width + 0.01:
            return fontsize, segments, indent, line_style
        fontsize -= 0.2

    emergency = min(min_fontsize, max(2.6, rect.height * 0.82))
    while emergency >= 2.6:
        line_fontsize = emergency * line_font_scale(line_style)
        width = reportlab_measure_segments(segments, line_style, emergency)
        if line_fontsize <= rect.height + 0.01 and width + indent <= rect.width + 0.01:
            return emergency, segments, indent, line_style
        emergency -= 0.15
    return None, segments, indent, line_style


def reportlab_draw_line_in_rect(c, page_height, rect, text, start_fontsize, min_fontsize, align, dry_run=False):
    fontsize, segments, indent, line_style = reportlab_fit_line_fontsize(rect, text, start_fontsize, min_fontsize)
    if fontsize is None:
        return False
    if not segments:
        return True

    line_fontsize = fontsize * line_font_scale(line_style)
    line_width = reportlab_measure_segments(segments, line_style, fontsize)
    available_width = max(1.0, rect.width - indent)
    if align == fitz.TEXT_ALIGN_CENTER and line_width < available_width:
        x = rect.x0 + indent + (available_width - line_width) / 2
    else:
        x = rect.x0 + indent
    y_top = rect.y0 + max(0.0, (rect.height - line_fontsize) / 2)
    baseline = page_height - y_top - line_fontsize

    if dry_run:
        return True

    for seg in segments:
        seg_text = normalize_draw_segment_text(seg["text"], strip_markdown=seg["style"] != "code")
        if not seg_text:
            continue
        fontname, fake_bold = reportlab_font_for_segment(seg, line_style)
        width = reportlab_text_width(seg_text, fontname, line_fontsize)
        if x + width > rect.x1 + 0.01:
            return False
        reportlab_draw_text(c, x, baseline, seg_text, fontname, line_fontsize, fake_bold)
        x += width
    return True


def reportlab_draw_line_info_block(c, page_height, block, dry_run=False, fontsize=None, min_fontsize=None):
    lines = usable_line_info(block)
    if not lines:
        return None

    align = block_align(block)
    start_fontsize = float(fontsize if fontsize is not None else block["font_size"])
    min_fontsize = float(
        min_fontsize
        if min_fontsize is not None
        else block.get("min_font_size", max(3.8, start_fontsize * 0.72))
    )

    for line in lines:
        if not reportlab_draw_line_in_rect(
            c,
            page_height,
            line["rect"],
            line["text"],
            start_fontsize,
            min_fontsize,
            align,
            dry_run=dry_run,
        ):
            return False
    return True


def strict_tiny_source_line(block):
    """Return one trustworthy OCR source line whose bbox already carries indentation."""

    if not isinstance(block, dict):
        return None
    if block.get("category") in {"Formula", "ImageFallback", "Table"}:
        return None
    lines = block.get("line_info") or []
    if len(lines) != 1:
        return None
    line = lines[0]
    text = normalize_markdown_text(line.get("text", ""))
    if not text or "\n" in text or len(text) > 80:
        return None
    if normalize_text(text) != normalize_text(block.get("text", "")):
        return None
    rect = line_info_rect(block, line)
    if rect is None or rect.height > 8.0:
        return None
    return {"text": text, "rect": rect}


def reportlab_draw_strict_tiny_source_line(c, page_height, block, fontsize, dry_run=False):
    """Draw a tiny source line literally, without adding Markdown list indentation."""

    line = strict_tiny_source_line(block)
    if line is None:
        return None
    segments = split_reportlab_font_runs(inline_markdown_segments(line["text"]))
    if not segments:
        return True
    rect = line["rect"]
    line_fontsize = float(fontsize)
    line_width = reportlab_measure_segments(segments, "normal", line_fontsize)
    if line_fontsize > rect.height + 0.01 or line_width > rect.width + 0.01:
        return False
    if dry_run:
        return True

    align = block_align(block)
    if align == fitz.TEXT_ALIGN_CENTER and line_width < rect.width:
        x = rect.x0 + (rect.width - line_width) / 2
    else:
        x = rect.x0
    y_top = rect.y0 + max(0.0, (rect.height - line_fontsize) / 2)
    baseline = page_height - y_top - line_fontsize
    for seg in segments:
        seg_text = normalize_draw_segment_text(
            seg["text"],
            strip_markdown=seg["style"] != "code",
        )
        if not seg_text:
            continue
        fontname, fake_bold = reportlab_font_for_segment(seg, "normal")
        width = reportlab_text_width(seg_text, fontname, line_fontsize)
        reportlab_draw_text(c, x, baseline, seg_text, fontname, line_fontsize, fake_bold)
        x += width
    return True


def normalize_narrow_vertical_text(text):
    text = normalize_markdown_text(text)
    return re.sub(r"\s+", " ", text).strip()


def is_narrow_vertical_text_block(block_or_text, rect, text):
    if isinstance(block_or_text, dict) and block_or_text.get("category") == "Table":
        return False
    if rect.width > 24.0:
        return False
    if rect.height < rect.width * 4.0:
        return False
    text = normalize_narrow_vertical_text(text)
    if not text:
        return False
    if len(text) > 160:
        return False
    if is_numeric_page_marker(text):
        return False
    return True


def reportlab_rotated_narrow_fontsize(rect, text, start_fontsize, min_fontsize):
    text = normalize_narrow_vertical_text(text)
    segments, indent, line_style = reportlab_segments_for_line(text)
    if not segments:
        return None, [], 0.0, "normal", 0.0

    scale = max(0.01, line_font_scale(line_style))
    available_length = max(1.0, rect.height - 1.5)
    available_thickness = max(1.0, rect.width - 1.0)
    max_by_thickness = available_thickness * 0.78 / scale

    current = min(float(start_fontsize), max_by_thickness)
    emergency_floor = 2.2
    min_fontsize = min(float(min_fontsize), current)
    min_fontsize = max(emergency_floor, min_fontsize)

    while current >= emergency_floor - 0.001:
        line_fontsize = current * scale
        measured = indent + reportlab_measure_segments(segments, line_style, current)
        if line_fontsize <= available_thickness + 0.01 and measured <= available_length + 0.01:
            return current, segments, indent, line_style, measured
        current -= 0.15 if current < min_fontsize else 0.25

    return None, segments, indent, line_style, 0.0


def reportlab_draw_rotated_narrow_textbox(
    c,
    page_height,
    rect,
    block_or_text,
    fontsize,
    min_fontsize,
    dry_run=False,
):
    text = block_or_text.get("text", "") if isinstance(block_or_text, dict) else str(block_or_text or "")
    text = normalize_narrow_vertical_text(text)
    if not is_narrow_vertical_text_block(block_or_text, rect, text):
        return False

    fontsize, segments, indent, line_style, measured = reportlab_rotated_narrow_fontsize(
        rect,
        text,
        fontsize,
        min_fontsize,
    )
    if fontsize is None:
        return False
    if dry_run:
        return True

    line_fontsize = fontsize * line_font_scale(line_style)
    baseline_x = rect.x0 + rect.width * 0.78
    start_y = page_height - rect.y1 + max(0.0, (rect.height - measured) / 2.0)
    cursor = indent

    c.saveState()
    c.translate(baseline_x, start_y)
    c.rotate(90)
    for seg in segments:
        seg_text = normalize_draw_segment_text(seg["text"], strip_markdown=seg["style"] != "code")
        if not seg_text:
            continue
        fontname, fake_bold = reportlab_font_for_segment(seg, line_style)
        width = reportlab_text_width(seg_text, fontname, line_fontsize)
        reportlab_draw_text(c, cursor, 0, seg_text, fontname, line_fontsize, fake_bold)
        cursor += width
    c.restoreState()
    return True


def reportlab_markdown_textbox_layout(
    page_height,
    rect,
    text,
    fontsize,
    line_height,
    align,
    literal_numbered=False,
):
    text = normalize_markdown_text(text)
    assert_no_radicals_in_text(text, "reportlab markdown textbox")
    lines = re.split(r"\n+", text) if text else []
    y_top = rect.y0
    layout = []

    for line in lines:
        if literal_numbered and NUMBERED_LAYOUT_TEXT_RE.match(line.strip()):
            segments, indent, line_style = inline_markdown_segments(line.strip()), 0, "normal"
        else:
            segments, indent, line_style = markdown_line_to_segments(line)
        segments = split_reportlab_font_runs(segments)
        line_fontsize = fontsize * line_font_scale(line_style)
        line_gap = line_fontsize * line_height
        if not segments:
            y_top += line_gap
            continue
        if y_top + line_gap > rect.y1:
            return None

        pending = segments[:]
        visual_line_index = 0
        while pending:
            if y_top + line_gap > rect.y1:
                return None
            effective_indent = indent
            x = rect.x0 + effective_indent
            max_x = rect.x1
            draw_items = []
            line_pending = []
            while pending:
                seg = pending.pop(0)
                seg_text = normalize_draw_segment_text(seg["text"], strip_markdown=seg["style"] != "code")
                if not seg_text:
                    continue
                fontname, fake_bold = reportlab_font_for_segment(seg, line_style)
                fit_text, rest = reportlab_split_to_fit(seg_text, fontname, line_fontsize, max_x - x)
                if not fit_text:
                    line_pending.insert(0, seg)
                    break
                width = reportlab_text_width(fit_text, fontname, line_fontsize)
                draw_items.append((fit_text, fontname, fake_bold, width, line_fontsize))
                x += width
                if rest:
                    line_pending.insert(0, {
                        "text": rest,
                        "style": seg["style"],
                        "script": seg.get("script", "cjk"),
                    })
                    break

            line_width = sum(item[3] for item in draw_items)
            if align == fitz.TEXT_ALIGN_CENTER and line_width < (rect.width - effective_indent):
                x = rect.x0 + effective_indent + ((rect.width - effective_indent) - line_width) / 2
            else:
                x = rect.x0 + effective_indent

            baseline = page_height - y_top - line_fontsize
            row_items = []
            for fit_text, fontname, fake_bold, width, item_fontsize in draw_items:
                row_items.append((x, baseline, fit_text, fontname, item_fontsize, fake_bold))
                x += width
            layout.append(row_items)
            pending = line_pending + pending
            y_top += line_gap
            visual_line_index += 1
    return layout


def reportlab_draw_markdown_textbox(
    c,
    page_height,
    rect,
    text,
    fontsize,
    line_height,
    align,
    dry_run=False,
    literal_numbered=False,
):
    layout = reportlab_markdown_textbox_layout(
        page_height,
        rect,
        text,
        fontsize,
        line_height,
        align,
        literal_numbered=literal_numbered,
    )
    if layout is None:
        return False
    if dry_run:
        return True

    for row_items in layout:
        for x, baseline, fit_text, fontname, item_fontsize, fake_bold in row_items:
            reportlab_draw_text(c, x, baseline, fit_text, fontname, item_fontsize, fake_bold)
    return True


def reportlab_fit_fontsize(page_height, rect, text, fontsize, min_size, line_height, align):
    fontsize, _line_height, _ok = reportlab_fit_box_params(
        page_height,
        rect,
        text,
        fontsize,
        min_size,
        line_height,
        align,
    )
    return fontsize


def reportlab_box_fits(page_height, rect, block_or_text, fontsize, line_height, align):
    if isinstance(block_or_text, dict) and block_or_text.get("category") == "Table":
        return reportlab_table_fits(page_height, rect, block_or_text, fontsize, line_height)
    text = block_or_text.get("text", "") if isinstance(block_or_text, dict) else str(block_or_text or "")
    min_fontsize = (
        block_or_text.get("min_font_size", max(3.2, float(fontsize) * 0.72))
        if isinstance(block_or_text, dict)
        else max(3.2, float(fontsize) * 0.72)
    )
    if reportlab_draw_rotated_narrow_textbox(
        None,
        page_height,
        rect,
        block_or_text,
        fontsize,
        min_fontsize,
        dry_run=True,
    ):
        return True
    if isinstance(block_or_text, dict):
        strict_line_ok = reportlab_draw_strict_tiny_source_line(
            None,
            page_height,
            block_or_text,
            fontsize,
            dry_run=True,
        )
        if strict_line_ok is not None:
            return strict_line_ok
        line_ok = reportlab_draw_line_info_block(
            None,
            page_height,
            block_or_text,
            dry_run=True,
            fontsize=fontsize,
            min_fontsize=block_or_text.get("min_font_size"),
        )
        if line_ok is True:
            return True
    return reportlab_textbox_fits(
        page_height,
        rect,
        text,
        fontsize,
        line_height,
        align,
        literal_numbered=bool(
            isinstance(block_or_text, dict)
            and block_or_text.get("preserve_numbered_bbox_indent")
        ),
    )


def line_height_candidates(start_line_height, preferred_line_height, min_line_height, line_step):
    candidates = []

    current = max(start_line_height, preferred_line_height)
    while current >= preferred_line_height - 0.001:
        candidates.append(current)
        current -= line_step

    current = min(preferred_line_height - line_step, start_line_height - line_step)
    while current >= min_line_height - 0.001:
        candidates.append(current)
        current -= line_step

    candidates.append(min_line_height)
    result = []
    seen = set()
    for value in candidates:
        value = round(max(min_line_height, value), 3)
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def descending_candidates(start, minimum, step):
    """Return a descending numeric search that always tests its lower bound."""

    start = float(start)
    minimum = float(minimum)
    step = max(0.001, float(step))
    if start < minimum - 0.001:
        return []

    values = []
    current = start
    while current > minimum + 0.001:
        values.append(round(current, 3))
        current -= step
    values.append(round(minimum, 3))
    return list(dict.fromkeys(values))


def reportlab_fit_box_params(page_height, rect, block_or_text, fontsize, min_size, line_height, align):
    if isinstance(block_or_text, dict):
        min_line_height = min_line_height_for_block(block_or_text)
        preferred_line_height = preferred_line_height_for_block(block_or_text)
        enforce_body_font_floor = bool(block_or_text.get("enforce_body_font_floor"))
        preserve_source_line_spacing = bool(block_or_text.get("preserve_source_line_spacing"))
    else:
        min_line_height = 0.82
        preferred_line_height = 1.16
        enforce_body_font_floor = False
        preserve_source_line_spacing = False

    min_size = max(3.8, float(min_size))
    start_fontsize = float(fontsize)
    start_line_height = max(float(line_height), preferred_line_height)
    font_step = 0.25 if start_fontsize < 9 else 0.35
    line_step = 0.035
    body_font_floor = (
        float(block_or_text.get("body_text_font_floor", BODY_TEXT_FONT_FLOOR))
        if isinstance(block_or_text, dict)
        else BODY_TEXT_FONT_FLOOR
    )
    soft_font_floor = min(start_fontsize, body_font_floor) if enforce_body_font_floor else min_size
    first_pass_min_size = soft_font_floor if enforce_body_font_floor else min_size

    for current_line_height in line_height_candidates(
        start_line_height,
        preferred_line_height,
        min_line_height,
        line_step,
    ):
        current_font = start_fontsize
        while current_font >= first_pass_min_size - 0.001:
            if reportlab_box_fits(page_height, rect, block_or_text, current_font, current_line_height, align):
                return current_font, current_line_height, True
            current_font -= font_step

    if enforce_body_font_floor:
        emergency_min_line_height = BODY_TEXT_FLOOR_LINE_HEIGHT_MIN
        for current_line_height in descending_candidates(
            min_line_height - line_step,
            emergency_min_line_height,
            line_step,
        ):
            for current_font in descending_candidates(
                start_fontsize,
                soft_font_floor,
                font_step,
            ):
                if reportlab_box_fits(page_height, rect, block_or_text, current_font, current_line_height, align):
                    return current_font, current_line_height, True

    # The former decrementing loop could step directly from 3.29 to 3.04 and
    # never enter a search whose fixed lower bound was 3.2 pt. Already-tiny
    # source reference text may legitimately need the same 2.6 pt emergency
    # floor used by the line-coordinate renderer to remain on one source line.
    emergency_floor = (
        2.6
        if start_fontsize < 3.8 or preserve_source_line_spacing
        else 3.2
    )
    emergency_min_size = min(emergency_floor, start_fontsize)
    emergency_min_line_height = min_line_height if preserve_source_line_spacing else 0.68
    emergency_start_font = (
        min(soft_font_floor - font_step, start_fontsize)
        if enforce_body_font_floor
        else min(min_size, start_fontsize)
    )
    emergency_start_font = max(emergency_min_size, emergency_start_font)
    emergency_line_height_start = (
        min_line_height
        if preserve_source_line_spacing
        else min_line_height - line_step
    )
    for current_line_height in descending_candidates(
        emergency_line_height_start,
        emergency_min_line_height,
        line_step,
    ):
        for current_font in descending_candidates(
            emergency_start_font,
            emergency_min_size,
            font_step,
        ):
            if reportlab_box_fits(page_height, rect, block_or_text, current_font, current_line_height, align):
                return current_font, current_line_height, True

    return emergency_min_size, emergency_min_line_height, False


def reportlab_textbox_fits(
    page_height,
    rect,
    text,
    fontsize,
    line_height,
    align,
    literal_numbered=False,
):
    from reportlab.pdfgen import canvas
    from io import BytesIO

    c = canvas.Canvas(BytesIO(), pagesize=(rect.x1 + 1, page_height))
    return reportlab_draw_markdown_textbox(
        c,
        page_height,
        rect,
        text,
        fontsize,
        line_height,
        align,
        dry_run=True,
        literal_numbered=literal_numbered,
    )


def reportlab_table_fits(page_height, rect, block_or_text, fontsize, line_height):
    from reportlab.pdfgen import canvas
    from io import BytesIO

    c = canvas.Canvas(BytesIO(), pagesize=(rect.x1 + 1, page_height))
    return reportlab_draw_table_block(
        c,
        page_height,
        rect,
        block_or_text,
        fontsize,
        line_height,
        dry_run=True,
    )


def reportlab_block_fit_failure(page_height, block):
    if block.get("category") == "ImageFallback":
        return None
    # Formula blocks take a separate vector/unicode/image-crop path before the
    # ordinary textbox fitting branch.  A text-only dry run would reject valid
    # formulas prematurely, so their final renderer remains authoritative.
    if is_formula_render_block(block):
        return None

    text = normalize_markdown_text(block.get("text", ""))
    if not text:
        return None

    rect = fitz.Rect(
        block["left"],
        block["top"],
        block["left"] + block["width"],
        block["top"] + block["height"],
    )
    if rect.is_empty or rect.width <= 0 or rect.height <= 0:
        return None

    fontsize = float(block["font_size"])
    min_size = float(block.get("min_font_size", max(6.0, fontsize * 0.86)))
    line_height = float(block["line_height"])
    align = block_align(block)
    _fontsize, _line_height, ok = reportlab_fit_box_params(
        page_height,
        rect,
        block,
        fontsize,
        min_size,
        line_height,
        align,
    )
    if ok:
        return None

    snippet = re.sub(r"\s+", " ", text).strip()[:120]
    return {
        "category": block.get("category"),
        "order": block.get("order"),
        "bbox": [round(rect.x0, 1), round(rect.y0, 1), round(rect.x1, 1), round(rect.y1, 1)],
        "text": snippet,
    }


def reportlab_page_fit_failures(page_height, blocks):
    reportlab_register_fonts()
    failures = []
    for block in blocks:
        failure = reportlab_block_fit_failure(page_height, block)
        if failure:
            failures.append(failure)
    return failures


def render_blocks_to_pdf_reportlab(
    page_specs,
    output_pdf,
    formula_work_dir=None,
    include_full_page_images=True,
    allow_formula_image_fallback=True,
    progress_callback=None,
):
    from reportlab.pdfgen import canvas

    reportlab_register_fonts()
    formula_blocks_present = any(
        is_formula_render_block(block)
        for _page_width, _page_height, blocks in page_specs
        for block in blocks
    )
    if formula_blocks_present:
        formula_render_dependencies()
    if formula_work_dir is None:
        formula_work_dir = TMP_DIR / "formula_render_cache"

    c = canvas.Canvas(str(output_pdf), pagesize=(page_specs[0][0], page_specs[0][1]))
    for page_no, (page_width, page_height, blocks) in enumerate(page_specs):
        c.setPageSize((page_width, page_height))
        for block in blocks:
            if block.get("category") == "ImageFallback":
                if include_full_page_images:
                    image_path = block.get("image_path")
                    if image_path and Path(image_path).exists():
                        c.drawImage(
                            str(image_path),
                            0,
                            0,
                            width=page_width,
                            height=page_height,
                            preserveAspectRatio=True,
                            anchor="c",
                            mask="auto",
                        )
                continue

            rect = fitz.Rect(
                block["left"],
                block["top"],
                block["left"] + block["width"],
                block["top"] + block["height"],
            )
            if rect.is_empty or rect.width <= 0 or rect.height <= 0:
                continue

            if is_formula_render_block(block):
                if render_formula_block(
                    c,
                    page_height,
                    rect,
                    block,
                    formula_work_dir,
                    allow_image_fallback=allow_formula_image_fallback,
                ):
                    continue

            raw_text = str(block.get("text", "") or "")
            raw_source_text = str(block.get("source_text", "") or "")
            cached_line_residue = structured_line_info_has_latex_residue(block)
            raw_block_residue = bool(
                LATEX_RESIDUE_RE.search(raw_text)
                or LATEX_RESIDUE_RE.search(raw_source_text)
            )
            text = normalize_markdown_text(raw_text)
            if LATEX_RESIDUE_RE.search(text) or (not text and is_formula_render_block(block)):
                text = normalize_markdown_text(linearize_latex_formula(text or formula_source_text(block)))
            if raw_block_residue or cached_line_residue:
                block["text"] = text
                # Structured line fragments can retain the pre-normalized OCR
                # payload even when the main block is already clean. Drawing
                # those fragments would bypass contextual normalization.
                block["line_info"] = []
            if LATEX_RESIDUE_RE.search(text):
                raise RuntimeError(
                    "LaTeX residue remains after formula fallback normalization: "
                    f"page={page_no + 1}, order={block.get('order')}, text={text[:160]!r}"
                )
            assert_no_radicals_in_text(text, f"page block order={block.get('order')}")
            if not text:
                continue

            fontsize = float(block["font_size"])
            min_size = float(block.get("min_font_size", max(6.0, fontsize * 0.86)))
            line_height = float(block["line_height"])
            align = block_align(block)
            fontsize, line_height, ok = reportlab_fit_box_params(
                page_height,
                rect,
                block,
                fontsize,
                min_size,
                line_height,
                align,
            )
            if not ok:
                snippet = re.sub(r"\s+", " ", text).strip()[:120]
                raise RuntimeError(
                    "Unable to fit OCR text inside its page bbox without clipping: "
                    f"page={page_no + 1}, category={block.get('category')}, "
                    f"order={block.get('order')}, bbox="
                    f"[{rect.x0:.1f}, {rect.y0:.1f}, {rect.x1:.1f}, {rect.y1:.1f}], "
                    f"text={snippet!r}"
                )

            if reportlab_draw_rotated_narrow_textbox(
                c,
                page_height,
                rect,
                block,
                fontsize,
                min_size,
                dry_run=True,
            ):
                drawn = reportlab_draw_rotated_narrow_textbox(
                    c,
                    page_height,
                    rect,
                    block,
                    fontsize,
                    min_size,
                )
            else:
                strict_line_drawn = reportlab_draw_strict_tiny_source_line(
                    c,
                    page_height,
                    block,
                    fontsize=fontsize,
                )
                if strict_line_drawn is not None:
                    drawn = strict_line_drawn
                elif block["category"] == "Table":
                    drawn = reportlab_draw_table_block(
                        c,
                        page_height,
                        rect,
                        block,
                        fontsize,
                        line_height,
                    )
                else:
                    line_drawn = reportlab_draw_line_info_block(
                        c,
                        page_height,
                        block,
                        fontsize=fontsize,
                        min_fontsize=min_size,
                    )
                    if line_drawn is None:
                        drawn = reportlab_draw_markdown_textbox(
                            c,
                            page_height,
                            rect,
                            text,
                            fontsize,
                            line_height,
                            align,
                            literal_numbered=bool(block.get("preserve_numbered_bbox_indent")),
                        )
                    elif line_drawn is False:
                        drawn = reportlab_draw_markdown_textbox(
                            c,
                            page_height,
                            rect,
                            text,
                            fontsize,
                            line_height,
                            align,
                            literal_numbered=bool(block.get("preserve_numbered_bbox_indent")),
                        )
                    else:
                        drawn = line_drawn
            if not drawn:
                snippet = re.sub(r"\s+", " ", text).strip()[:120]
                raise RuntimeError(
                    "ReportLab draw refused a block that passed fitting: "
                    f"page={page_no + 1}, category={block.get('category')}, "
                    f"order={block.get('order')}, text={snippet!r}"
                )
        # Commit every source page explicitly. ReportLab's save() only commits
        # the current page when it has drawing commands, so a trailing blank
        # source page would otherwise be omitted from the output PDF.
        c.showPage()
        if progress_callback is not None:
            progress_callback(page_no + 1, len(page_specs))
    c.save()


_COMPONENT_EXPORTS = (
    "reportlab_register_fonts",
    "font_supports_char",
    "font_supports_text",
    "reportlab_char_has_registered_glyph",
    "reportlab_unsupported_chars",
    "reportlab_safe_text",
    "serif_script_for_char",
    "char_script",
    "split_reportlab_font_runs",
    "reportlab_font_for_segment",
    "reportlab_text_width",
    "reportlab_plain_segments",
    "reportlab_plain_text_width",
    "reportlab_draw_plain_text",
    "reportlab_split_to_fit",
    "reportlab_split_plain_lines",
    "reportlab_toc_table_layout",
    "table_column_count",
    "reportlab_plain_table_layout",
    "reportlab_grid_table_layout",
    "reportlab_table_layout",
    "reportlab_draw_table_block",
    "reportlab_draw_text",
    "block_page_rect",
    "line_info_rect",
    "usable_line_info",
    "reportlab_segments_for_line",
    "reportlab_measure_segments",
    "reportlab_fit_line_fontsize",
    "reportlab_draw_line_in_rect",
    "reportlab_draw_line_info_block",
    "strict_tiny_source_line",
    "reportlab_draw_strict_tiny_source_line",
    "normalize_narrow_vertical_text",
    "is_narrow_vertical_text_block",
    "reportlab_rotated_narrow_fontsize",
    "reportlab_draw_rotated_narrow_textbox",
    "reportlab_markdown_textbox_layout",
    "reportlab_draw_markdown_textbox",
    "reportlab_fit_fontsize",
    "reportlab_box_fits",
    "line_height_candidates",
    "descending_candidates",
    "reportlab_fit_box_params",
    "reportlab_textbox_fits",
    "reportlab_table_fits",
    "reportlab_block_fit_failure",
    "reportlab_page_fit_failures",
    "render_blocks_to_pdf_reportlab",
    "LATIN_LIKE_RE",
    "GREEK_LIKE_RE",
    "CYRILLIC_LIKE_RE",
    "COMBINING_MARK_RE",
    "CJK_LIKE_RE",
    "FONT_COVERAGE_CACHE",
    "REPORTLAB_REGISTERED_FONT_NAMES",
    "REPORTLAB_MISSING_GLYPH_REPLACEMENT",
)
_COMPONENT_RUNTIME = ComponentRuntime(globals(), _COMPONENT_EXPORTS)


def component_exports():
    return _COMPONENT_RUNTIME.exports()


def invoke_component(name, namespace, *args, **kwargs):
    return _COMPONENT_RUNTIME.invoke(name, namespace, *args, **kwargs)
