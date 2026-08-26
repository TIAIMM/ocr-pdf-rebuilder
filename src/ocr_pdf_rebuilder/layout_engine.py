"""Page block conversion, layout fitting and Markdown reconstruction."""

from difflib import SequenceMatcher
import math
import re

import fitz

from .component_runtime import ComponentRuntime
from .pipeline_config import *

def cell_to_block(cell, order, page_width, page_height, image_size):
    bbox = cell.get("bbox")
    text = get_text_from_cell(cell)
    category = normalize_category(cell.get("category"))

    if category == "Picture" and not text:
        return None
    if not text and category not in {"Formula", "Table"}:
        return None
    if not isinstance(bbox, list) or len(bbox) != 4:
        return None

    x0, y0, x1, y1 = [float(v) for v in bbox]

    bbox_units = cell.get("__bbox_units")
    if bbox_units == "relative":
        image_width = 1.0
        image_height = 1.0
    elif bbox_units == "mineru_1000":
        image_width = 1000.0
        image_height = 1000.0
    elif bbox_units == "pdf":
        image_width = page_width
        image_height = page_height
    elif bbox_units == "image" and image_size:
        image_width, image_height = image_size
    elif bbox_units is None and (
        x1 <= page_width * 1.35 and y1 <= page_height * 1.35
    ):
        # Coordinate guessing is only safe for legacy cells that do not state
        # their units. PaddleOCR image-space boxes near the upper-left can be
        # numerically smaller than a large PDF page and must still be scaled.
        image_width = page_width
        image_height = page_height
    elif image_size:
        image_width, image_height = image_size
    else:
        image_width = page_width * DPI / 72
        image_height = page_height * DPI / 72

    scale_x = page_width / max(float(image_width), 1.0)
    scale_y = page_height / max(float(image_height), 1.0)

    left = max(0.0, min(x0 * scale_x, page_width))
    top = max(0.0, min(y0 * scale_y, page_height))
    right = max(left + 1.0, min(x1 * scale_x, page_width))
    bottom = max(top + 1.0, min(y1 * scale_y, page_height))

    return {
        "order": order,
        "category": category,
        "text": text,
        "source_text": source_text_from_cell(cell),
        "source_cell": cell,
        "table_rows": extract_table_rows(source_text_from_cell(cell)) if category == "Table" else [],
        "line_info": cell.get("__line_info") or [],
        "bbox_scale_x": scale_x,
        "bbox_scale_y": scale_y,
        "left": left,
        "top": top,
        "width": right - left,
        "height": bottom - top,
    }


def fallback_text_block(text, order, page_width, page_height):
    text = normalize_markdown_text(text)
    if not text:
        return None
    margin_x = page_width * 0.09
    margin_top = page_height * 0.08
    margin_bottom = page_height * 0.08
    return {
        "order": order,
        "category": "Text",
        "text": text,
        "left": margin_x,
        "top": margin_top,
        "width": page_width - margin_x * 2,
        "height": page_height - margin_top - margin_bottom,
    }


def image_fallback_block(image_path, page_width, page_height):
    return {
        "order": 0,
        "category": "ImageFallback",
        "text": "",
        "image_path": str(image_path),
        "left": 0.0,
        "top": 0.0,
        "width": float(page_width),
        "height": float(page_height),
    }


def blocks_from_page_result(result, page_width, page_height):
    blocks = []
    for order, cell in enumerate(result.get("cells", []) or []):
        block = cell_to_block(cell, order, page_width, page_height, result.get("image_size"))
        if block:
            blocks.append(block)
    return blocks


SOURCE_TEXT_ROTATION_MIN_HORIZONTAL_LINES = 3
SOURCE_TEXT_ROTATION_MIN_WEIGHT = 24
SOURCE_TEXT_ROTATION_DOMINANCE_RATIO = 0.75


def source_page_text_rotation_degrees(page):
    """Detect a physically inverted source page from its OCR text direction.

    A scanned page can be upside down while carrying a hidden text layer whose
    line direction is ``(-1, 0)``.  Paddle recognizes the glyphs upright but
    reports boxes in the inverted raster coordinates, so the page must be
    geometrically rotated before layout reconstruction.
    """

    forward_weight = 0
    reverse_weight = 0
    horizontal_lines = 0
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            direction = line.get("dir") or (1.0, 0.0)
            if len(direction) < 2:
                continue
            direction_x = float(direction[0])
            direction_y = float(direction[1])
            if abs(direction_y) > 0.20 or abs(direction_x) < 0.90:
                continue
            text = "".join(
                str(span.get("text", "")) for span in line.get("spans", [])
            ).strip()
            if not text:
                continue
            weight = max(1, len(re.sub(r"\s+", "", text)))
            horizontal_lines += 1
            if direction_x < 0.0:
                reverse_weight += weight
            else:
                forward_weight += weight

    total_weight = forward_weight + reverse_weight
    if (
        horizontal_lines >= SOURCE_TEXT_ROTATION_MIN_HORIZONTAL_LINES
        and reverse_weight >= SOURCE_TEXT_ROTATION_MIN_WEIGHT
        and reverse_weight / max(1, total_weight)
        >= SOURCE_TEXT_ROTATION_DOMINANCE_RATIO
    ):
        return 180
    return 0


def rotate_blocks_for_source_orientation(
    blocks, page_width, page_height, rotation_degrees
):
    """Move OCR boxes into the readable orientation of an inverted page."""

    if int(rotation_degrees or 0) % 360 != 180:
        return blocks
    for block in blocks:
        old_left = float(block["left"])
        old_top = float(block["top"])
        width = float(block["width"])
        height = float(block["height"])
        block["left"] = max(0.0, page_width - (old_left + width))
        block["top"] = max(0.0, page_height - (old_top + height))
        # Structured line boxes are expressed in the old absolute coordinate
        # system.  The block text remains safe and preserves recognition.
        block["line_info"] = []
        block["source_orientation_correction_degrees"] = 180
    return blocks


def has_cjk(text):
    return bool(re.search(r"[\u3400-\u9fff]", text or ""))


def inline_markdown_segments(text):
    text = normalize_inline_markup_aliases(text)
    segments = []
    i = 0
    patterns = [
        ("math", "$$", "$$"),
        ("math", "\\(", "\\)"),
        ("math", "\\[", "\\]"),
        ("strongitalic", "***", "***"),
        ("strongitalic", "___", "___"),
        ("strong", "**", "**"),
        ("strong", "__", "__"),
        ("strike", "~~", "~~"),
        ("mark", "==", "=="),
        ("code", "`", "`"),
        ("math", "$", "$"),
        ("sub", "~", "~"),
        ("sup", "^", "^"),
        ("italic", "*", "*"),
        ("italic", "_", "_"),
    ]

    while i < len(text):
        matched = False
        for style, start, end in patterns:
            if not text.startswith(start, i):
                continue
            if style in {"italic"}:
                if text.startswith(start * 2, i):
                    continue
            j = text.find(end, i + len(start))
            if j <= i + len(start):
                continue
            inner = text[i + len(start):j]
            if "\n" in inner:
                continue
            if inner.strip():
                if style == "math" and start == "$" and not is_likely_math_text(inner):
                    continue
                segments.append({"text": inner, "style": style})
                i = j + len(end)
                matched = True
                break
        if matched:
            continue

        next_positions = [
            pos for marker in ("$$", "\\(", "\\[", "***", "___", "**", "__", "~~", "==", "`", "$", "~", "^", "*", "_")
            for pos in [text.find(marker, i + 1)]
            if pos != -1
        ]
        next_i = min(next_positions) if next_positions else len(text)
        segments.append({"text": text[i:next_i], "style": "normal"})
        i = next_i

    return [seg for seg in segments if seg["text"]]


def markdown_line_to_segments(line):
    raw = line.rstrip()
    stripped = raw.strip()
    if not stripped:
        return [], 0, "normal"

    if re.fullmatch(r"(?:[-*_]\s*){3,}", stripped):
        return [], 0, "rule"

    if stripped.startswith("```"):
        return [], 0, "code"

    heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
    if heading:
        level = len(heading.group(1))
        return inline_markdown_segments(heading.group(2)), 0, f"heading{level}"

    footnote_def = re.match(r"^\[\^([^\]]{1,24})\]:\s*(.+)$", stripped)
    if footnote_def:
        prefix = f"{footnote_def.group(1)}. "
        return [{"text": prefix, "style": "normal"}] + inline_markdown_segments(footnote_def.group(2)), 14, "footnote"

    quote = re.match(r"^>\s*(.+)$", stripped)
    if quote:
        return inline_markdown_segments(quote.group(1)), 18, "quote"

    unordered = re.match(r"^[-+*]\s+(?:\[[ xX]\]\s*)?(.+)$", stripped)
    if unordered:
        return [{"text": "• ", "style": "normal"}] + inline_markdown_segments(unordered.group(1)), 14, "list"

    ordered = re.match(r"^(\d+)[.)]\s+(.+)$", stripped)
    if ordered:
        prefix = f"{ordered.group(1)}. "
        return [{"text": prefix, "style": "normal"}] + inline_markdown_segments(ordered.group(2)), 14, "list"

    table_cells = markdown_table_cells(stripped)
    if table_cells is not None:
        if not table_cells:
            return [], 0, "table-rule"
        segments = []
        for index, cell in enumerate(table_cells):
            if index:
                segments.append({"text": "    ", "style": "normal"})
            segments.extend(inline_markdown_segments(cell))
        return segments, 0, "table"

    return inline_markdown_segments(stripped), 0, "normal"


def line_font_scale(line_style):
    if line_style == "table":
        return 0.92
    return 1.0


def line_is_bold(line_style):
    return line_style in {"strong", "heading1", "heading2", "heading3", "heading4", "heading5", "heading6"}


def line_is_italic(line_style):
    return line_style in {"italic", "quote"}


def is_header_footer(block):
    return block["category"] in {"Page-header", "Page-footer"}


def is_title(block):
    return block["category"] in {"Title", "Section-header"}


def clean_block_text(block):
    return normalize_text(block.get("text", ""))


def is_numeric_page_marker(text):
    text = text.strip()
    return bool(re.fullmatch(r"\d{1,4}", text) or re.fullmatch(r"\[\s*\d{1,4}\s*\]", text))


def is_short_running_text(block):
    text = clean_block_text(block)
    return 1 <= len(text) <= 12


def is_body_text_block(block):
    if block.get("category") != "Text":
        return False
    text = clean_block_text(block)
    if len(text) < 30:
        return False
    if is_numeric_page_marker(text):
        return False
    return True


NUMBERED_LAYOUT_TEXT_RE = re.compile(r"^\s*(\d{1,4})[.)]\s+\S")


def numbered_layout_text_number(block):
    """Return the leading item number for an OCR-positioned numbered block."""

    if block.get("category") not in {"Text", "List-item"}:
        return None
    match = NUMBERED_LAYOUT_TEXT_RE.match(clean_block_text(block))
    return int(match.group(1)) if match else None


def dense_numbered_note_page_font_size(blocks, page_height):
    """Detect compact endnote/reference pages and recover their natural text size.

    MinerU sometimes emits one logical cell per numbered note but only one
    ``line_info`` entry for the entire multi-line cell. Treating that bbox as a
    single source line inflates the estimated font size and then forces unsafe
    line-height compression. A dense, mostly sequential numbered page provides
    enough neighbouring one-line boxes to estimate the original note size.
    """

    text_blocks = [
        block
        for block in blocks
        if block.get("category") in {"Text", "List-item"} and clean_block_text(block)
    ]
    numbered = [
        (block, numbered_layout_text_number(block))
        for block in text_blocks
        if numbered_layout_text_number(block) is not None
    ]
    if len(numbered) < 8 or len(numbered) / max(1, len(text_blocks)) < 0.75:
        return None

    numbers = [number for _block, number in numbered]
    increasing_pairs = sum(1 for first, second in zip(numbers, numbers[1:]) if second > first)
    if increasing_pairs / max(1, len(numbers) - 1) < 0.75:
        return None

    heights = sorted(
        float(block.get("height", 0.0))
        for block, _number in numbered
        if float(block.get("height", 0.0)) > 0.0
    )
    median_height = median(heights)
    if median_height is None or median_height > float(page_height) * 0.035:
        return None

    compact_heights = [height for height in heights if height <= median_height + 0.01]
    compact_line_height = median(compact_heights) or median_height
    return clamp(
        compact_line_height / 1.20,
        2.6,
        10.5 * page_font_scale(page_height),
    )


def is_misclassified_long_header_footer(block, page_width):
    if block.get("category") not in {"Page-header", "Page-footer"}:
        return False
    text = clean_block_text(block)
    if len(text) < 36 or is_numeric_page_marker(text):
        return False
    if block.get("width", 0) >= page_width * 0.45 and block.get("height", 0) >= 14:
        return False
    comma_list = len(re.findall(r"\d+[.,]?\d*", text)) >= 6
    prose_like = len(re.findall(r"[\u3400-\u9fffA-Za-zÀ-ÖØ-öø-ÿΑ-ω]", text)) >= 16
    too_wide = estimated_inline_width(text, max(6.0, min(9.0, block.get("height", 9.0)))) > block.get("width", 1) * 1.8
    return too_wide and (comma_list or prose_like)


def repair_misclassified_header_footer_block(block, page_width, page_height, body_size):
    if not is_misclassified_long_header_footer(block, page_width):
        return
    original_category = block["category"]
    block["category"] = "Text"
    block["misclassified_header_footer_repaired"] = original_category

    margin_x = page_width * 0.075
    current_right = block["left"] + block["width"]
    left = min(block["left"], margin_x)
    right = max(current_right, page_width - margin_x)
    block["left"] = max(0.0, left)
    block["width"] = min(page_width, right) - block["left"]

    font_size = clamp(estimate_bbox_font_size(block), body_size * 0.72, body_size * 0.92)
    line_height = 1.05
    needed = estimate_required_height(block, font_size, line_height)
    max_bottom = page_height * 0.92 if original_category == "Page-header" else page_height - 8
    block["height"] = min(max(block["height"], needed * 1.05, 14.0), max(14.0, max_bottom - block["top"]))


def line_count_for_text(text):
    return max(1, len([line for line in re.split(r"\n+", text) if line.strip()]))


def block_structural_line_count(block):
    line_info = block.get("line_info") or []
    count = len([line for line in line_info if str(line.get("text", "")).strip()])
    return count if count > 1 else None


def line_info_font_size_estimate(block):
    line_info = block.get("line_info") or []
    if len(line_info) < 2:
        return None

    scale_y = float(block.get("bbox_scale_y", 1.0))
    heights = []
    for line in line_info:
        bbox = line.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        height = max(0.0, float(bbox[3]) - float(bbox[1])) * scale_y
        if 2.5 <= height <= max(3.0, block["height"] * 0.45):
            heights.append(height)

    if len(heights) < 2:
        return None

    med = median(heights)
    if not med:
        return None
    if max(heights) / max(1.0, min(heights)) > 2.4:
        return None
    return med * 0.82


def estimate_bbox_font_size(block):
    height = max(block["height"], 1.0)
    text = block["text"]
    lines = block_structural_line_count(block) or line_count_for_text(text)
    by_height = height / (lines * 1.20)
    by_line_info = line_info_font_size_estimate(block)
    if by_line_info:
        return clamp(by_line_info, by_height * 0.75, by_height * 1.25)
    return by_height


def median(values):
    values = sorted(values)
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def page_font_scale(page_height):
    return clamp(float(page_height) / 842.0, 0.75, MAX_PAGE_FONT_SCALE)


def scaled_body_font_min(page_height):
    return BODY_FONT_MIN * page_font_scale(page_height)


def scaled_body_font_max(page_height):
    return BODY_FONT_MAX * page_font_scale(page_height)


def scaled_body_text_font_floor(page_height):
    return BODY_TEXT_FONT_FLOOR * page_font_scale(page_height)


def estimate_page_body_size(blocks, page_height):
    candidates = []
    min_size = scaled_body_font_min(page_height)
    max_size = scaled_body_font_max(page_height)
    for block in blocks:
        if block["category"] != "Text":
            continue
        text = clean_block_text(block)
        if len(text) < 80 or is_numeric_page_marker(text):
            continue
        candidates.append(clamp(estimate_bbox_font_size(block), min_size, max_size))
    return median(candidates) or (10.8 * page_font_scale(page_height))


def estimate_font_size(block, body_size):
    category = block["category"]
    clean_text = clean_block_text(block)
    by_height = estimate_bbox_font_size(block)

    if category == "Title":
        return clamp(by_height, body_size * 1.02, body_size * 1.35)
    if category == "Section-header":
        return clamp(by_height, body_size * 0.88, body_size * 1.16)
    if is_numeric_page_marker(clean_text):
        return clamp(by_height, body_size * 0.62, body_size * 0.82)
    if category == "Page-header":
        return clamp(by_height, body_size * 0.72, body_size * 0.92)
    if category == "Page-footer":
        return clamp(by_height, body_size * 0.55, body_size * 0.82)
    if category == "Caption":
        return clamp(by_height, body_size * 0.62, body_size * 0.86)
    if category == "Footnote":
        return clamp(by_height, body_size * 0.62, body_size * 0.78)
    if category == "List-item":
        return clamp(by_height, body_size * 0.82, body_size * 0.98)
    if category == "Text" and is_short_running_text(block):
        return clamp(by_height, body_size * 0.82, body_size * 1.05)
    if category == "Table":
        row_count = max(1, len(table_rows_for_block(block)))
        by_rows = block["height"] / max(row_count * 1.35, 1)
        return clamp(by_rows, body_size * 0.72, body_size * 1.08)
    if category == "Formula":
        return clamp(by_height, body_size * 0.65, body_size * 0.92)
    return clamp(by_height, body_size * 0.88, body_size * 1.00)


def clamp(value, low, high):
    return max(low, min(high, value))


def estimate_required_height(block, font_size, line_height):
    text = block["text"]
    if not text:
        return 0
    width = max(block["width"], 20)
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿΑ-ω]", text))
    total = max(len(text), 1)
    unit = 1.05 if cjk >= latin else 0.58
    chars_per_line = max(1, int(width / max(font_size * unit, 1)))
    explicit_lines = [line for line in re.split(r"\n+", text) if line.strip()]
    lines = 0
    for line in explicit_lines or [text]:
        lines += max(1, math.ceil(len(line) / chars_per_line))
    return lines * font_size * line_height


def estimate_visual_line_count(block, font_size):
    structural_lines = block_structural_line_count(block)
    if structural_lines:
        return structural_lines

    text = block["text"]
    clean_text = clean_block_text(block)
    if not clean_text:
        return 1

    width = max(block["width"], 20)
    cjk = len(re.findall(r"[\u3400-\u9fff]", clean_text))
    latin = len(re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿΑ-ω]", clean_text))
    unit = 1.05 if cjk >= latin else 0.58
    chars_per_line = max(1, int(width / max(font_size * unit, 1)))

    raw_lines = [line for line in re.split(r"\n+", text) if line.strip()]
    lines = 0
    for line in raw_lines or [clean_text]:
        line_text = strip_markdown_inline(line).strip()
        lines += max(1, math.ceil(len(line_text) / chars_per_line))
    return max(1, lines)


def estimate_line_height(block, font_size):
    visual_lines = estimate_visual_line_count(block, font_size)
    raw = block["height"] / max(font_size * visual_lines, 1)
    category = block["category"]
    clean_text = clean_block_text(block)

    if block.get("dense_numbered_note"):
        return clamp(raw, 1.08, 1.35)

    if category in {"Title", "Section-header"}:
        return clamp(raw, 0.95, 1.20)
    if category in {"Page-header", "Page-footer"} or is_numeric_page_marker(clean_text):
        return clamp(raw, 0.90, 1.15)
    if category == "Caption":
        return clamp(raw, 0.95, 1.20)
    if category == "Footnote":
        return clamp(raw, 0.95, 1.55)
    if category == "List-item":
        return clamp(raw, 0.98, 1.75)
    if category in {"Formula", "Table"}:
        return clamp(raw, 0.95, 1.20)
    if is_short_running_text(block):
        return clamp(raw, 0.95, 1.20)
    return clamp(raw, 1.08, 2.35)


def estimated_inline_width(text, font_size):
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z0-9À-ÖØ-öø-ÿΑ-ω]", text))
    other = max(len(text) - cjk - latin, 0)
    return cjk * font_size + latin * font_size * 0.55 + other * font_size * 0.45


def horizontal_overlap_ratio(a, b):
    left = max(a["left"], b["left"])
    right = min(a["left"] + a["width"], b["left"] + b["width"])
    overlap = max(0.0, right - left)
    return overlap / max(1.0, min(a["width"], b["width"]))


def block_bottom(block):
    return block["top"] + block["height"]


TEXT_MERGE_CATEGORIES = {"Text", "List-item"}
MIN_EXACT_TEXT_OVERLAP_CHARS = 24


def dedupe_text_key(text):
    text = normalize_text(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def join_deduplicated_text(base, suffix):
    suffix = suffix.lstrip()
    if not suffix:
        return base.strip()
    if suffix[0] in ".,;:!?":
        if base.rstrip().endswith(tuple(".,;:!?")):
            base = base.rstrip()[:-1].rstrip()
        return (base.rstrip() + suffix).strip()
    if suffix[0] in "”»\"')]}":
        return (base.rstrip() + suffix).strip()
    return (base.rstrip() + " " + suffix).strip()


def append_text_with_overlap(base, addition):
    """Append OCR text while removing duplicated split retry overlap.

    Historical split retries cropped pages with vertical overlap. OCR engines can re-OCR the
    same paragraph in adjacent bands with slightly different bboxes and line
    breaks. This keeps the earlier text and appends only the non-overlap suffix
    from the later band.
    """
    base_norm = dedupe_text_key(base)
    add_norm = dedupe_text_key(addition)

    if not base_norm:
        return add_norm
    if not add_norm:
        return base_norm

    if add_norm in base_norm:
        return base_norm
    if base_norm in add_norm:
        return add_norm

    max_k = min(len(base_norm), len(add_norm), 1400)

    # Exact suffix-prefix overlap.
    for k in range(max_k, MIN_EXACT_TEXT_OVERLAP_CHARS - 1, -1):
        if base_norm[-k:] == add_norm[:k]:
            return join_deduplicated_text(base_norm, add_norm[k:])

    # Fuzzy overlap for OCR differences, hyphenation differences and line breaks.
    tail = base_norm[-1400:]
    head = add_norm[:1400]
    matcher = SequenceMatcher(None, tail, head, autojunk=False)

    best = None
    for match in matcher.get_matching_blocks():
        if match.size < 45:
            continue
        touches_tail_end = match.a + match.size >= len(tail) - 8
        near_head_start = match.b <= 80
        if touches_tail_end and near_head_start:
            if best is None or match.size > best.size:
                best = match

    if best is not None:
        cut = best.b + best.size
        return join_deduplicated_text(base_norm, add_norm[cut:])

    return (base_norm + " " + add_norm).strip()


def text_overlap_like(a, b):
    a_norm = dedupe_text_key(a)
    b_norm = dedupe_text_key(b)
    if not a_norm or not b_norm:
        return False
    if a_norm in b_norm or b_norm in a_norm:
        return True
    merged = append_text_with_overlap(a_norm, b_norm)
    naive = (a_norm + " " + b_norm).strip()
    return len(merged) <= len(naive) - MIN_EXACT_TEXT_OVERLAP_CHARS


def retry_blocks_should_merge(a, b):
    if a.get("category") not in TEXT_MERGE_CATEGORIES:
        return False
    if b.get("category") not in TEXT_MERGE_CATEGORIES:
        return False

    h_overlap = horizontal_overlap_ratio(a, b)
    if h_overlap < 0.20:
        return False

    a_bottom = block_bottom(a)
    b_bottom = block_bottom(b)
    vertical_overlap = min(a_bottom, b_bottom) - max(a["top"], b["top"])
    gap = b["top"] - a_bottom

    if text_overlap_like(a.get("text", ""), b.get("text", "")):
        return True

    # Adjacent split retry crops can produce overlapping bboxes for the same paragraph
    # even when OCR text starts at a different point.
    if vertical_overlap > min(a["height"], b["height"]) * 0.10:
        return True

    # Boundary paragraphs can be nearly touching after coordinate restoration.
    if gap <= 4.0 and h_overlap >= 0.55:
        return True

    return False


def merge_retry_block_pair(a, b):
    a_right = a["left"] + a["width"]
    b_right = b["left"] + b["width"]
    left = min(a["left"], b["left"])
    top = min(a["top"], b["top"])
    right = max(a_right, b_right)
    bottom = max(block_bottom(a), block_bottom(b))

    merged_text = append_text_with_overlap(a.get("text", ""), b.get("text", ""))
    a["text"] = merged_text
    a["source_text"] = append_text_with_overlap(
        a.get("source_text", a.get("text", "")),
        b.get("source_text", b.get("text", "")),
    )
    a["left"] = left
    a["top"] = top
    a["width"] = right - left
    a["height"] = bottom - top
    a["category"] = "Text"
    a["band_merged"] = True
    return a


def merge_overlapping_retry_blocks(blocks):
    ordered = sorted(
        blocks,
        key=lambda b: (is_header_footer(b), b["top"], b["left"], b.get("order", 0)),
    )

    changed = True
    while changed:
        changed = False
        merged = []
        for block in ordered:
            if merged and retry_blocks_should_merge(merged[-1], block):
                merged[-1] = merge_retry_block_pair(merged[-1], block)
                changed = True
            else:
                merged.append(block)
        ordered = merged

    for index, block in enumerate(ordered):
        block["order"] = index
    return ordered


def duplicate_ocr_blocks_should_merge(a, b):
    """Return true only for geometrically overlapping blocks sharing real text.

    PaddleOCR can emit a long paragraph and a second block that repeats its
    final line with a short continuation.  Unlike split-band retry merging,
    this deliberately requires textual overlap and ignores short margin
    markers so line numbers are never folded into body text.
    """

    if a.get("category") not in TEXT_MERGE_CATEGORIES:
        return False
    if b.get("category") not in TEXT_MERGE_CATEGORIES:
        return False
    if min(
        len(dedupe_text_key(a.get("text", ""))),
        len(dedupe_text_key(b.get("text", ""))),
    ) < MIN_EXACT_TEXT_OVERLAP_CHARS:
        return False
    if horizontal_overlap_ratio(a, b) < 0.20:
        return False
    vertical_overlap = min(block_bottom(a), block_bottom(b)) - max(a["top"], b["top"])
    if vertical_overlap <= 0.0:
        return False
    return text_overlap_like(a.get("text", ""), b.get("text", ""))


def merge_overlapping_duplicate_ocr_blocks(blocks):
    """Merge duplicate OCR fragments even when margin markers sit between them."""

    ordered = sorted(
        blocks,
        key=lambda block: (
            is_header_footer(block),
            block["top"],
            block["left"],
            block.get("order", 0),
        ),
    )
    changed = True
    while changed:
        changed = False
        for left_index, left in enumerate(ordered):
            for right_index in range(left_index + 1, len(ordered)):
                right = ordered[right_index]
                if duplicate_ocr_blocks_should_merge(left, right):
                    merged = merge_retry_block_pair(left, right)
                    merged["duplicate_ocr_blocks_merged"] = True
                    ordered[left_index] = merged
                    del ordered[right_index]
                    changed = True
                    break
            if changed:
                break

    for index, block in enumerate(ordered):
        block["order"] = index
    return ordered


FUZZY_DUPLICATE_MIN_KEY_CHARS = 12
FUZZY_DUPLICATE_MAX_KEY_CHARS = 180
FUZZY_DUPLICATE_MIN_LINE_SIMILARITY = 0.56
FUZZY_DUPLICATE_MIN_ANCHORED_SIMILARITY = 0.78
FUZZY_DUPLICATE_CATEGORIES = TEXT_MERGE_CATEGORIES | {"Section-header"}
SUPERSCRIPT_DEDUPE_TRANSLATION = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
LEADING_OCR_REFERENCE_RE = re.compile(
    r"^\s*(?:[|1Il][bBdD])?\.?\s*(\d{2,4})(?:\.\d+)?(?=\D|$)"
)


def fuzzy_dedupe_text_key(text):
    text = normalize_text(text).translate(SUPERSCRIPT_DEDUPE_TRANSLATION)
    return re.sub(
        r"[^0-9A-Za-zÀ-ÖØ-öø-ÿΑ-ω]+",
        "",
        text,
    ).casefold()


def leading_ocr_reference(text):
    match = LEADING_OCR_REFERENCE_RE.match(normalize_text(text))
    return match.group(1) if match else None


def anchored_duplicate_short_block(short, long):
    """Recognize a short alternate OCR reading contained in a long block.

    Paddle can emit both a full Greek reference paragraph and a second,
    heavily transliterated reading of its first line.  A shared leading
    bibliographic anchor plus strong geometric containment is safer evidence
    than script-dependent fuzzy matching.
    """

    short_spans = nonempty_line_spans(short.get("text", ""))
    if not short_spans or len(short_spans) > 2:
        return False
    short_key = fuzzy_dedupe_text_key(short.get("text", ""))
    long_key = fuzzy_dedupe_text_key(long.get("text", ""))
    if len(short_key) < 12 or len(long_key) < len(short_key) * 2:
        return False
    reference = leading_ocr_reference(short.get("text", ""))
    if not reference or reference != leading_ocr_reference(long.get("text", "")):
        return False
    if short["width"] < long["width"] * 0.55:
        return False
    if long["height"] < short["height"] * 1.75:
        return False
    if horizontal_overlap_ratio(short, long) < 0.65:
        return False
    vertical_overlap = min(block_bottom(short), block_bottom(long)) - max(
        short["top"], long["top"]
    )
    return vertical_overlap / max(1.0, short["height"]) >= 0.50


def nonempty_line_spans(text):
    spans = []
    offset = 0
    for raw_line in str(text or "").splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        stripped = line.strip()
        if stripped:
            start = offset + line.find(stripped)
            spans.append((start, start + len(stripped), stripped))
        offset += len(raw_line)
    if not spans and str(text or "").strip():
        stripped = str(text).strip()
        start = str(text).find(stripped)
        spans.append((start, start + len(stripped), stripped))
    return spans


def fuzzy_edge_candidates(long_text, short_line, edge):
    long_text = str(long_text or "")
    spans = nonempty_line_spans(long_text)
    candidates = []
    if edge == "start":
        candidates.extend((start, end, value, "line") for start, end, value in spans[:2])
    else:
        candidates.extend((start, end, value, "line") for start, end, value in spans[-2:])

    first_word = re.search(r"[A-Za-zÀ-ÖØ-öø-ÿΑ-ω]{4,}", short_line)
    if first_word:
        word = first_word.group(0)
        matches = list(re.finditer(re.escape(word), long_text, flags=re.IGNORECASE))
        if edge == "start":
            for match in matches:
                if match.start() <= max(120, len(long_text) * 0.25):
                    candidates.append((0, min(len(long_text), match.start() + max(80, len(short_line) * 2)), long_text[: match.start() + max(80, len(short_line) * 2)], "anchor"))
                    break
        else:
            for match in reversed(matches):
                if match.start() >= len(long_text) * 0.35:
                    candidates.append((match.start(), len(long_text), long_text[match.start() :], "anchor"))
                    break
    return candidates


def remove_fuzzy_edge_duplicate_from_long_block(short, long):
    short_spans = nonempty_line_spans(short.get("text", ""))
    if not short_spans or len(short_spans) > 3:
        return False

    short_center = short["top"] + short["height"] * 0.5
    long_center = long["top"] + long["height"] * 0.5
    edge = "start" if short_center <= long_center else "end"
    best = None
    for _short_start, _short_end, short_line in short_spans:
        short_key = fuzzy_dedupe_text_key(short_line)
        if not (
            FUZZY_DUPLICATE_MIN_KEY_CHARS
            <= len(short_key)
            <= FUZZY_DUPLICATE_MAX_KEY_CHARS
        ):
            continue
        for start, end, candidate, candidate_kind in fuzzy_edge_candidates(
            long.get("text", ""), short_line, edge
        ):
            candidate_key = fuzzy_dedupe_text_key(candidate)
            if not candidate_key:
                continue
            similarity = SequenceMatcher(
                None,
                short_key,
                candidate_key,
                autojunk=False,
            ).ratio()
            threshold = (
                FUZZY_DUPLICATE_MIN_ANCHORED_SIMILARITY
                if candidate_kind == "anchor"
                else FUZZY_DUPLICATE_MIN_LINE_SIMILARITY
            )
            if similarity < threshold:
                continue
            if best is None or similarity > best[0]:
                best = (similarity, start, end, candidate_kind)

    if best is None:
        return False

    _similarity, start, end, _candidate_kind = best
    long_text = str(long.get("text", ""))
    before = long_text[:start].rstrip()
    after = long_text[end:].lstrip()
    repaired = "\n".join(part for part in (before, after) if part).strip()
    if not repaired or repaired == long_text.strip():
        return False

    old_top = float(long["top"])
    old_bottom = block_bottom(long)
    if edge == "start":
        new_top = min(old_bottom - 1.0, max(old_top, block_bottom(short)))
        long["top"] = new_top
        long["height"] = old_bottom - new_top
    else:
        new_bottom = max(old_top + 1.0, min(old_bottom, float(short["top"])))
        long["height"] = new_bottom - old_top
    long["text"] = repaired
    long["source_text"] = repaired
    long["line_info"] = []
    long["fuzzy_duplicate_ocr_lines_removed"] = int(
        long.get("fuzzy_duplicate_ocr_lines_removed", 0)
    ) + 1
    return True


def repair_overlapping_fuzzy_duplicate_ocr_lines(blocks):
    """Remove a conflicting OCR edge line while retaining its better twin.

    The short block is preserved because Paddle usually emits the corrected
    line separately; only a geometrically overlapping first/last line of the
    larger block is removed.  This also handles punctuation and superscript
    variants that defeat exact substring merging.
    """

    ordered = list(blocks)
    changed = True
    while changed:
        changed = False
        for left_index, left in enumerate(ordered):
            for right_index in range(left_index + 1, len(ordered)):
                right = ordered[right_index]
                if left.get("category") not in FUZZY_DUPLICATE_CATEGORIES:
                    continue
                if right.get("category") not in FUZZY_DUPLICATE_CATEGORIES:
                    continue
                if horizontal_overlap_ratio(left, right) < 0.20:
                    continue
                vertical_overlap = min(block_bottom(left), block_bottom(right)) - max(
                    left["top"], right["top"]
                )
                if vertical_overlap <= 0.0:
                    continue
                if vertical_overlap / max(1.0, min(left["height"], right["height"])) < 0.20:
                    continue

                left_key = fuzzy_dedupe_text_key(left.get("text", ""))
                right_key = fuzzy_dedupe_text_key(right.get("text", ""))
                short, long = (left, right) if len(left_key) <= len(right_key) else (right, left)
                if long.get("category") not in TEXT_MERGE_CATEGORIES:
                    continue
                if anchored_duplicate_short_block(short, long):
                    long["anchored_duplicate_ocr_blocks_removed"] = int(
                        long.get("anchored_duplicate_ocr_blocks_removed", 0)
                    ) + 1
                    del ordered[left_index if short is left else right_index]
                    changed = True
                    break
                if remove_fuzzy_edge_duplicate_from_long_block(short, long):
                    changed = True
                    break
            if changed:
                break

    for index, block in enumerate(
        sorted(
            ordered,
            key=lambda item: (
                is_header_footer(item),
                item["top"],
                item["left"],
                item.get("order", 0),
            ),
        )
    ):
        block["order"] = index
    return ordered


def trim_small_adjacent_text_block_overlaps(blocks):
    """Trim small same-column bbox intersections without deleting either text.

    OCR line boxes often overlap by a few points even when the two blocks are
    consecutive rather than duplicates. Keeping both bboxes unchanged makes
    their final baselines collide. Only the earlier block's trailing bbox area
    is trimmed; fit preflight still rejects text that genuinely cannot fit.
    """

    candidates = sorted(
        blocks,
        key=lambda item: (item["top"], item["left"], item.get("order", 0)),
    )
    for index, earlier in enumerate(candidates):
        if earlier.get("category") not in TEXT_MERGE_CATEGORIES:
            continue
        if is_numeric_page_marker(clean_block_text(earlier)):
            continue
        earlier_lines = nonempty_line_spans(earlier.get("text", ""))
        if len(earlier_lines) < 2:
            continue
        for later in candidates[index + 1 :]:
            if later["top"] <= earlier["top"]:
                continue
            if later.get("category") not in TEXT_MERGE_CATEGORIES:
                continue
            if is_numeric_page_marker(clean_block_text(later)):
                continue
            later_lines = nonempty_line_spans(later.get("text", ""))
            if len(later_lines) != 1:
                continue
            if len(fuzzy_dedupe_text_key(later_lines[0][2])) < 12:
                continue
            overlap = block_bottom(earlier) - later["top"]
            if overlap < 2.5 or overlap > 6.0:
                continue
            if horizontal_overlap_ratio(earlier, later) < 0.50:
                continue
            if later["width"] < earlier["width"] * 0.45:
                continue
            if abs(later["left"] - earlier["left"]) > max(
                earlier["width"], later["width"]
            ) * 0.12:
                continue
            overlap_ratio = overlap / max(
                1.0, min(earlier["height"], later["height"])
            )
            if not (0.20 <= overlap_ratio <= 0.55):
                continue
            earlier["height"] = max(1.0, later["top"] - earlier["top"])
            earlier["adjacent_text_overlap_trimmed"] = True
            break
    return blocks


STRETCHED_MARGIN_NUMBER_RE = re.compile(r"^\d{1,3}$")


def stretched_margin_number_tokens(text):
    """Return numeric tokens when a tall margin block contains only numbers.

    Paddle may flatten a vertical run such as ``15`` / ``20`` into either
    newline-separated text or the single string ``"15 20"``.  Treat both
    encodings identically, while rejecting punctuation or surrounding prose.
    """

    raw = str(text or "").strip()
    if not raw or re.sub(r"\d{1,3}|\s+", "", raw):
        return []
    tokens = re.findall(r"\d{1,3}", raw)
    # Paddle occasionally duplicates every member of a vertical run before
    # flattening it (for example ``20 20 / 25 25 / 30 30``).  Keep a bounded
    # upper limit so prose-like numeric tables are still rejected, but allow
    # enough tokens for these duplicated margin markers to reach the
    # source-geometry restoration pass.
    return tokens if 1 <= len(tokens) <= 12 else []


def stretched_margin_numeric_block_side(block, page_width, page_height):
    tokens = stretched_margin_number_tokens(block.get("text", ""))
    if not (
        block.get("category") in TEXT_MERGE_CATEGORIES
        and tokens
        and block["width"] <= page_width * 0.06
        and block["height"] >= page_height * 0.25
    ):
        return None
    if block["left"] + block["width"] <= page_width * 0.16:
        return "left"
    if block["left"] >= page_width * 0.84:
        return "right"
    return None


def intended_stretched_margin_numbers(text):
    numbers = set()
    for token in stretched_margin_number_tokens(text):
        value = int(token)
        # Some scans append a detected rule/glyph to the real five-line
        # marker: 151, 201, 251 are 15, 20, 25 respectively.
        if value > 100 and token.endswith("1"):
            shortened = int(token[:-1])
            if 0 < shortened <= 100 and shortened % 5 == 0:
                value = shortened
        numbers.add(value)
    return numbers


def repair_stretched_margin_numeric_ocr_blocks(
    blocks,
    page_width,
    page_height,
    source_page=None,
):
    """Restore merged line-number runs from the source page's exact geometry."""

    stretched = [
        (block, stretched_margin_numeric_block_side(block, page_width, page_height))
        for block in blocks
    ]
    stretched = [(block, side) for block, side in stretched if side]
    if not stretched:
        return blocks

    ordinary_numbers = set()
    for block in blocks:
        if any(block is candidate for candidate, _side in stretched):
            continue
        text = clean_block_text(block)
        if not STRETCHED_MARGIN_NUMBER_RE.fullmatch(text):
            continue
        side = None
        if block["left"] + block["width"] <= page_width * 0.16:
            side = "left"
        elif block["left"] >= page_width * 0.84:
            side = "right"
        if side and block["height"] <= page_height * 0.05:
            ordinary_numbers.add((side, int(text)))

    source_words = []
    if source_page is not None:
        for word in source_page.get_text("words"):
            token = str(word[4]).strip()
            if not STRETCHED_MARGIN_NUMBER_RE.fullmatch(token):
                continue
            left, top, right, bottom = map(float, word[:4])
            side = None
            if right <= page_width * 0.18:
                side = "left"
            elif left >= page_width * 0.82:
                side = "right"
            if side:
                source_words.append((side, int(token), left, top, right, bottom))

    retained = [
        block
        for block in blocks
        if not any(block is candidate for candidate, _side in stretched)
    ]
    for candidate, side in stretched:
        intended = intended_stretched_margin_numbers(candidate.get("text", ""))
        restored = []
        candidate_top = float(candidate["top"])
        candidate_bottom = block_bottom(candidate)
        for word_side, value, left, top, right, bottom in source_words:
            if word_side != side or value not in intended:
                continue
            if (side, value) in ordinary_numbers:
                continue
            center = (top + bottom) / 2.0
            if not (candidate_top - 12.0 <= center <= candidate_bottom + 12.0):
                continue
            restored.append((value, left, top, right, bottom))
            ordinary_numbers.add((side, value))
        for restored_index, (value, left, top, right, bottom) in enumerate(
            sorted(restored, key=lambda item: item[2])
        ):
            text = str(value)
            retained.append(
                {
                    "order": float(candidate.get("order", 0)) + (restored_index + 1) / 1000,
                    "category": candidate.get("category", "Text"),
                    "text": text,
                    "source_text": text,
                    "line_info": [],
                    "left": left,
                    "top": top,
                    "width": max(1.0, right - left),
                    "height": max(1.0, bottom - top),
                    "stretched_margin_numeric_block_repaired": True,
                }
            )
    return retained


def transformed_source_word_bbox(word, page_width, page_height, rotation_degrees=0):
    left, top, right, bottom = map(float, word[:4])
    if int(rotation_degrees or 0) % 360 == 180:
        return (
            page_width - right,
            page_height - bottom,
            page_width - left,
            page_height - top,
        )
    return left, top, right, bottom


def source_margin_number_sequences(
    source_page,
    page_width,
    page_height,
    rotation_degrees=0,
):
    """Find high-confidence five-line number sequences in the source text layer."""

    candidates_by_side = {"left": [], "right": []}
    if source_page is None:
        return candidates_by_side
    for word in source_page.get_text("words"):
        token = str(word[4]).strip()
        if not STRETCHED_MARGIN_NUMBER_RE.fullmatch(token):
            continue
        value = int(token)
        if value <= 0 or value > 100 or value % 5:
            continue
        left, top, right, bottom = transformed_source_word_bbox(
            word, page_width, page_height, rotation_degrees
        )
        if not (page_height * 0.08 <= top <= page_height * 0.90):
            continue
        side = None
        if right <= page_width * 0.16:
            side = "left"
        elif left >= page_width * 0.84:
            side = "right"
        if side:
            candidates_by_side[side].append(
                (value, left, top, right, bottom)
            )

    sequences = {"left": [], "right": []}
    for side, candidates in candidates_by_side.items():
        candidates.sort(key=lambda item: (item[2], item[1]))
        paths = [[index] for index in range(len(candidates))]
        for index, candidate in enumerate(candidates):
            value, left, top, right, _bottom = candidate
            center_x = (left + right) / 2.0
            for previous_index in range(index):
                previous = candidates[previous_index]
                previous_value, previous_left, previous_top, previous_right, _ = previous
                previous_center_x = (previous_left + previous_right) / 2.0
                if (
                    value == previous_value + 5
                    and top > previous_top + page_height * 0.015
                    and abs(center_x - previous_center_x) <= page_width * 0.045
                    and len(paths[previous_index]) + 1 > len(paths[index])
                ):
                    paths[index] = paths[previous_index] + [index]
        if paths:
            best = max(
                paths,
                key=lambda path: (
                    len(path),
                    -candidates[path[0]][0],
                    candidates[path[-1]][2] - candidates[path[0]][2],
                ),
            )
            if len(best) >= 5:
                sequences[side] = [candidates[index] for index in best]
    return sequences


def margin_sequence_block_side(block, page_width, page_height):
    text = clean_block_text(block)
    if (
        block.get("category") not in TEXT_MERGE_CATEGORIES
        or not STRETCHED_MARGIN_NUMBER_RE.fullmatch(text)
        or block["height"] > page_height * 0.035
        or not (page_height * 0.08 <= block["top"] <= page_height * 0.90)
    ):
        return None
    if block["left"] + block["width"] <= page_width * 0.16:
        return "left"
    if block["left"] >= page_width * 0.84:
        return "right"
    return None


def synchronize_margin_number_sequence_from_source(
    blocks,
    page_width,
    page_height,
    source_page,
    source_rotation_degrees=0,
):
    """Restore complete line-number runs from a validated source sequence."""

    sequences = source_margin_number_sequences(
        source_page,
        page_width,
        page_height,
        source_rotation_degrees,
    )
    if not any(sequences.values()):
        return blocks, False

    retained = list(blocks)
    changed = False
    for side, source_sequence in sequences.items():
        if not source_sequence:
            continue
        source_center_x = median(
            [(item[1] + item[3]) / 2.0 for item in source_sequence]
        )
        source_top = source_sequence[0][2]
        source_bottom = source_sequence[-1][4]
        ordinary = []
        stretched = []
        for block in retained:
            block_side = margin_sequence_block_side(
                block, page_width, page_height
            )
            if block_side == side:
                center_x = block["left"] + block["width"] / 2.0
                if (
                    abs(center_x - source_center_x) <= page_width * 0.055
                    and source_top - page_height * 0.035
                    <= block["top"]
                    <= source_bottom + page_height * 0.035
                ):
                    ordinary.append(block)
                continue
            if (
                stretched_margin_numeric_block_side(
                    block, page_width, page_height
                )
                == side
                and block_bottom(block) >= source_top
                and block["top"] <= source_bottom
            ):
                stretched.append(block)

        unmatched = list(ordinary)
        replacements = []
        for target_index, (value, left, top, right, bottom) in enumerate(
            source_sequence
        ):
            target_center_y = (top + bottom) / 2.0
            nearby = [
                block
                for block in unmatched
                if abs(
                    block["top"] + block["height"] / 2.0 - target_center_y
                )
                <= max(12.0, page_height * 0.035)
            ]
            if nearby:
                block = min(
                    nearby,
                    key=lambda item: abs(
                        item["top"] + item["height"] / 2.0 - target_center_y
                    ),
                )
                unmatched.remove(block)
                if clean_block_text(block) != str(value):
                    block["text"] = str(value)
                    block["source_text"] = str(value)
                    block["line_info"] = []
                    block["margin_number_sequence_value_repaired"] = True
                    changed = True
                continue
            replacements.append(
                {
                    "order": float(target_index) / 1000.0,
                    "category": "Text",
                    "text": str(value),
                    "source_text": str(value),
                    "line_info": [],
                    "left": left,
                    "top": top,
                    "width": max(1.0, right - left),
                    "height": max(1.0, bottom - top),
                    "margin_number_sequence_value_repaired": True,
                    "margin_number_sequence_missing_value_restored": True,
                }
            )
            changed = True

        extras = unmatched + stretched
        if extras:
            retained = [
                block
                for block in retained
                if not any(block is extra for extra in extras)
            ]
            changed = True
        retained.extend(replacements)
    return retained, changed


def repair_margin_number_sequence_values(
    blocks,
    page_width,
    page_height,
    source_page=None,
    source_rotation_degrees=0,
):
    """Repair isolated OCR digit errors without shifting over missing markers."""

    blocks, source_synchronized = synchronize_margin_number_sequence_from_source(
        blocks,
        page_width,
        page_height,
        source_page,
        source_rotation_degrees,
    )
    if source_synchronized:
        return blocks

    candidates_by_side = {"left": [], "right": []}
    for block in blocks:
        side = margin_sequence_block_side(block, page_width, page_height)
        if not side:
            continue
        candidates_by_side[side].append((block, int(clean_block_text(block))))

    for candidates in candidates_by_side.values():
        candidates.sort(key=lambda item: item[0]["top"])
        if len(candidates) < 5:
            continue
        observed = [value for _block, value in candidates]
        stable_steps = sum(
            following - previous == 5
            for previous, following in zip(observed, observed[1:])
        )
        if stable_steps < 3:
            continue

        expected_by_index = {}
        for index in range(1, len(candidates) - 1):
            previous = observed[index - 1]
            following = observed[index + 1]
            if following - previous == 10:
                expected_by_index[index] = previous + 5
        if observed[2] - observed[1] == 5:
            expected_by_index[0] = observed[1] - 5
        if observed[-2] - observed[-3] == 5:
            expected_by_index[len(candidates) - 1] = observed[-2] + 5

        for index, target in expected_by_index.items():
            block, actual = candidates[index]
            if actual == target or target <= 0 or target > 100 or target % 5:
                continue
            block["text"] = str(target)
            block["source_text"] = str(target)
            block["line_info"] = []
            block["margin_number_sequence_value_repaired"] = True
    return blocks


def strip_captured_margin_number_prefix(text, margin_number):
    match = re.match(r"^\s*(\d{1,3})(?=\s|[A-ZÀ-ÖØ-Þ])\s*", str(text or ""))
    if not match:
        return str(text or "")
    captured = match.group(1)
    if not str(margin_number).endswith(captured):
        return str(text or "")
    return str(text or "")[match.end() :].lstrip()


def strip_captured_margin_number_suffix(text, margin_number):
    raw = str(text or "")
    match = re.search(r"\s+(\d{1,3})\s*$", raw)
    if not match:
        return raw
    captured = match.group(1)
    if not str(margin_number).endswith(captured):
        return raw
    return raw[: match.start()].rstrip()


def repair_overlapping_margin_line_number_blocks(blocks, page_width, page_height):
    """Reserve a narrow column for detached five-line verse/prose markers."""

    line_numbers = []
    for block in blocks:
        text = clean_block_text(block)
        if not STRETCHED_MARGIN_NUMBER_RE.fullmatch(text):
            continue
        if block.get("category") not in TEXT_MERGE_CATEGORIES:
            continue
        if block["height"] > page_height * 0.035:
            continue
        if not (page_height * 0.08 <= block["top"] <= page_height * 0.90):
            continue
        side = None
        if block["left"] + block["width"] <= page_width * 0.16:
            side = "left"
        elif block["left"] >= page_width * 0.84:
            side = "right"
        if side:
            line_numbers.append((block, text, side))

    for marker, marker_text, side in line_numbers:
        marker_right = marker["left"] + marker["width"]
        marker_bottom = block_bottom(marker)
        for body in blocks:
            if body is marker or body.get("category") not in (
                TEXT_MERGE_CATEGORIES | {"Footnote"}
            ):
                continue
            vertical_overlap = min(marker_bottom, block_bottom(body)) - max(
                marker["top"], body["top"]
            )
            if vertical_overlap <= 0.0:
                continue
            if vertical_overlap / max(1.0, min(marker["height"], body["height"])) < 0.20:
                continue
            if horizontal_overlap_ratio(marker, body) < 0.15:
                continue

            gap = page_width * 0.012
            body_left = float(body["left"])
            body_right = body_left + float(body["width"])
            original_body_text = str(body.get("text", ""))
            if side == "left":
                body_text = strip_captured_margin_number_prefix(
                    original_body_text, marker_text
                )
            else:
                body_text = strip_captured_margin_number_suffix(
                    original_body_text, marker_text
                )
            captured_marker_removed = body_text != original_body_text
            # Wide body blocks are separated geometrically as before. A short
            # caption/signature is touched only when it contains the same
            # marker token at the intersecting margin edge, which is strong
            # evidence that Paddle captured the detached line number twice.
            if body["width"] < page_width * 0.20 and not captured_marker_removed:
                continue
            if side == "left":
                target_left = max(body_left, marker_right + gap)
                if target_left >= body_right - 20.0:
                    continue
                body["left"] = target_left
                body["width"] = body_right - target_left
            else:
                target_right = min(body_right, marker["left"] - gap)
                if target_right <= body_left + 20.0:
                    continue
                body["width"] = target_right - body_left
            if body_text:
                body["text"] = body_text
                body["source_text"] = body_text
            body["line_info"] = []
            body["margin_line_number_block_repaired"] = True
    return blocks


def separate_overlapping_consecutive_text_blocks(blocks, page_width, page_height):
    """Separate distinct consecutive OCR blocks whose bboxes collide."""

    candidates = sorted(
        blocks,
        key=lambda item: (item["top"], item["left"], item.get("order", 0)),
    )
    minimum_overlap = page_height * 0.0035
    maximum_overlap = page_height * 0.016
    for index, earlier in enumerate(candidates):
        if earlier.get("category") not in TEXT_MERGE_CATEGORIES:
            continue
        if earlier["height"] > page_height * 0.04:
            continue
        earlier_key = fuzzy_dedupe_text_key(earlier.get("text", ""))
        if len(earlier_key) < 8:
            continue
        for later in candidates[index + 1 :]:
            if later["top"] <= earlier["top"]:
                continue
            if later.get("category") not in TEXT_MERGE_CATEGORIES:
                continue
            if later["height"] < page_height * 0.035:
                continue
            later_key = fuzzy_dedupe_text_key(later.get("text", ""))
            if len(later_key) < 30 or earlier_key in later_key or later_key in earlier_key:
                continue
            overlap = block_bottom(earlier) - later["top"]
            if overlap < minimum_overlap or overlap > maximum_overlap:
                continue
            if horizontal_overlap_ratio(earlier, later) < 0.50:
                continue
            maximum_width = max(earlier["width"], later["width"])
            left_tolerance = 0.22 if earlier["width"] <= later["width"] * 0.85 else 0.12
            if abs(later["left"] - earlier["left"]) > maximum_width * left_tolerance:
                continue
            old_bottom = block_bottom(later)
            target_top = block_bottom(earlier) + max(0.5, page_height * 0.0007)
            if old_bottom - target_top < page_height * 0.02:
                continue
            later["top"] = target_top
            later["height"] = old_bottom - target_top
            later["consecutive_text_overlap_separated"] = True
            break
    return blocks


REFERENCE_TOKEN_PATTERN = r"\d{1,4}(?:\.\d+)(?:[-–—]\d+)?"
REFERENCE_MARGIN_LINE_RE = re.compile(
    rf"^\s*\(?{REFERENCE_TOKEN_PATTERN}\)?"
    rf"(?:\s+\(?{REFERENCE_TOKEN_PATTERN}\)?)?\s*$"
)
REFERENCE_PREFIX_RE = re.compile(rf"^\s*({REFERENCE_TOKEN_PATTERN})\s*")
BODY_REFERENCE_PREFIX_RE = re.compile(
    r"^\s*((?:\d{1,4})?\.\d+(?:[-–—]\d+)?)\s*"
)


def normalized_reference_token(text):
    return re.sub(r"[–—]", "-", str(text or "").strip("() "))


def strip_captured_reference_prefix(text, reference_tokens):
    prefix = BODY_REFERENCE_PREFIX_RE.match(str(text or ""))
    if not prefix:
        return str(text or "")
    token = normalized_reference_token(prefix.group(1))
    if not any(
        token == reference or (len(token) >= 3 and reference.endswith(token))
        for reference in reference_tokens
    ):
        return str(text or "")
    return str(text or "")[prefix.end() :].lstrip()


def repair_overlapping_margin_reference_blocks(blocks, page_width):
    """Restore a narrow reference column that Paddle placed over body text."""

    reference_blocks = []
    for block in blocks:
        if block.get("category") not in TEXT_MERGE_CATEGORIES:
            continue
        lines = [value for _start, _end, value in nonempty_line_spans(block.get("text", ""))]
        if not (1 <= len(lines) <= 4):
            continue
        if not all(REFERENCE_MARGIN_LINE_RE.fullmatch(value) for value in lines):
            continue
        if block["width"] > page_width * 0.28:
            continue
        reference_blocks.append((block, lines))

    for reference, reference_lines in reference_blocks:
        reference_right = reference["left"] + reference["width"]
        reference_bottom = block_bottom(reference)
        reference_tokens = {
            normalized_reference_token(match.group(1))
            for line in reference_lines
            for match in [REFERENCE_PREFIX_RE.match(line.strip("() "))]
            if match
        }
        for body in blocks:
            if body is reference or body.get("category") not in TEXT_MERGE_CATEGORIES:
                continue
            if body["width"] < page_width * 0.25:
                continue
            if abs(body["left"] - reference["left"]) > page_width * 0.04:
                continue
            if horizontal_overlap_ratio(reference, body) < 0.75:
                continue
            vertical_overlap = min(reference_bottom, block_bottom(body)) - max(
                reference["top"], body["top"]
            )
            if vertical_overlap <= 0.0:
                continue
            if vertical_overlap / max(1.0, min(reference["height"], body["height"])) < 0.20:
                continue

            body_text = strip_captured_reference_prefix(
                body.get("text", ""), reference_tokens
            )
            target_left = max(
                float(body["left"]),
                reference_right + page_width * 0.035,
            )
            body_right = body["left"] + body["width"]
            if target_left >= body_right - 20.0 or not body_text:
                continue
            body["left"] = target_left
            body["width"] = body_right - target_left
            body["text"] = body_text
            body["source_text"] = body_text
            body["line_info"] = []
            body["margin_reference_block_repaired"] = True

    for index, block in enumerate(
        sorted(
            blocks,
            key=lambda item: (
                is_header_footer(item),
                item["top"],
                item["left"],
                item.get("order", 0),
            ),
        )
    ):
        block["order"] = index
    return blocks


MARGIN_CLAMP_BLOCK_CATEGORIES = {
    "Text",
    "List-item",
    "Footnote",
    "Caption",
    "Formula",
    "Table",
}


def clamp_split_retry_block_to_page_margins(block, page_width):
    """Clamp abnormal split retry text boxes back into a safe body text area.

    This is intentionally applied only to pages whose OCR result came from
    split retry. It prevents a single over-wide band bbox from making the
    restored text run all the way to the physical page edge.
    """
    if block.get("category") not in MARGIN_CLAMP_BLOCK_CATEGORIES:
        return block

    safe_left = page_width * 0.065
    safe_right = page_width * 0.935

    left = float(block["left"])
    right = float(block["left"] + block["width"])
    original_left = left
    original_right = right

    if left < safe_left:
        left = safe_left
    if right > safe_right:
        right = safe_right

    if right <= left + 20:
        right = min(page_width - safe_left, left + max(20.0, float(block["width"])))

    if left != original_left or right != original_right:
        block["left"] = left
        block["width"] = right - left
        block["split_margin_clamped"] = True

    return block


def clamp_split_retry_blocks_to_page_margins(blocks, page_width):
    return [clamp_split_retry_block_to_page_margins(block, page_width) for block in blocks]


def min_line_height_for_block(block):
    if block.get("preserve_source_line_spacing"):
        return 1.00
    category = block["category"]
    if category in {"Page-header", "Page-footer"} or is_numeric_page_marker(clean_block_text(block)):
        return 0.82
    if category in {"Title", "Section-header", "Caption"}:
        return 0.86
    if category in {"Footnote", "Formula", "Table"}:
        return 0.82
    if category == "List-item":
        return 0.88
    return 0.88


def preferred_line_height_for_block(block):
    category = block["category"]
    clean_text = clean_block_text(block)

    if block.get("preserve_source_line_spacing"):
        return 1.15

    if category in {"Page-header", "Page-footer"} or is_numeric_page_marker(clean_text):
        return 1.00
    if category in {"Title", "Section-header"}:
        return 1.10
    if category == "Caption":
        return 1.08
    if category == "Footnote":
        return 1.08
    if category == "List-item":
        return 1.16
    if category in {"Formula", "Table"}:
        return 1.05
    if is_short_running_text(block):
        return 1.05
    return 1.20


def body_text_target_font_size(body_size):
    return float(body_size)


def block_has_meaningful_horizontal_overlap(a, b):
    return horizontal_overlap_ratio(a, b) >= 0.12


def next_flow_block_top(block, blocks):
    bottom_limit = None
    block_top = float(block["top"])
    for other in blocks:
        if other is block:
            continue
        if is_header_footer(other):
            continue
        if is_numeric_page_marker(clean_block_text(other)):
            continue
        if float(other.get("top", 0.0)) <= block_top + 2.0:
            continue
        if not block_has_meaningful_horizontal_overlap(block, other):
            continue
        other_top = float(other["top"])
        bottom_limit = other_top if bottom_limit is None else min(bottom_limit, other_top)
    return bottom_limit


def available_body_bottom(block, blocks, page_height):
    next_top = next_flow_block_top(block, blocks)
    if next_top is not None:
        return max(float(block["top"]) + 1.0, next_top - FLOW_BLOCK_GAP)
    return page_height * 0.92


def should_expand_compressed_body_block(block, page_width, body_size):
    if block.get("category") != "Text":
        return False
    clean_text = clean_block_text(block)
    if len(clean_text) < COMPRESSED_BODY_MIN_CHARS:
        return False
    if is_numeric_page_marker(clean_text):
        return False
    if float(block.get("width", 0.0)) < page_width * COMPRESSED_BODY_MIN_WIDTH_RATIO:
        return False

    target_font = body_text_target_font_size(body_size)
    target_line_height = preferred_line_height_for_block(block)
    needed = estimate_required_height(block, target_font, target_line_height)
    if needed <= float(block.get("height", 0.0)) * COMPRESSED_BODY_HEIGHT_EXPAND_RATIO:
        return False

    bbox_font = estimate_bbox_font_size(block)
    return bbox_font < target_font * 0.72


def expand_compressed_body_block(block, blocks, page_width, page_height, body_size):
    if not should_expand_compressed_body_block(block, page_width, body_size):
        return

    target_font = body_text_target_font_size(body_size)
    target_line_height = preferred_line_height_for_block(block)
    needed = estimate_required_height(block, target_font, target_line_height) * 1.04
    available_bottom = available_body_bottom(block, blocks, page_height)
    available_height = max(1.0, available_bottom - float(block["top"]))
    current_height = float(block["height"])

    if available_height <= current_height * 1.08:
        return

    expanded_height = min(needed, available_height)
    if expanded_height <= current_height * 1.08:
        return

    block["height"] = expanded_height
    block["compressed_body_bbox_expanded"] = True
    block["compressed_body_original_height"] = current_height


def textbox_rect(block):
    return fitz.Rect(
        block["left"],
        block["top"],
        block["left"] + block["width"],
        block["top"] + block["height"],
    )


def block_align(block):
    return fitz.TEXT_ALIGN_CENTER if block["category"] in {"Title", "Section-header", "Caption"} else fitz.TEXT_ALIGN_LEFT


def fit_block_to_bbox(block, page_height):
    text = normalize_markdown_text(block["text"])
    if not text:
        block["strict_bbox_fit"] = True
        return

    rect = textbox_rect(block)
    if rect.is_empty or rect.width <= 0 or rect.height <= 0:
        block["strict_bbox_fit"] = False
        return

    font_size, line_height, ok = reportlab_fit_box_params(
        page_height,
        rect,
        block,
        float(block["font_size"]),
        float(block["min_font_size"]),
        float(block["line_height"]),
        block_align(block),
    )

    block["font_size"] = round(font_size, 2)
    block["line_height"] = round(line_height, 3)
    block["strict_bbox_fit"] = ok


def reflow_overflow_pair(first, second, page_height):
    union_top = min(first["top"], second["top"])
    union_bottom = max(block_bottom(first), block_bottom(second))
    union_height = max(1.0, union_bottom - union_top)
    gap = min(FLOW_BLOCK_GAP, union_height * 0.03)
    available = max(1.0, union_height - gap)

    first_need = max(
        1.0,
        estimate_required_height(first, first["min_font_size"], min_line_height_for_block(first)),
    )
    second_need = max(
        1.0,
        estimate_required_height(second, second["min_font_size"], min_line_height_for_block(second)),
    )
    first_height = available * first_need / (first_need + second_need)
    second_height = available - first_height

    min_first = max(10.0, first["min_font_size"] * min_line_height_for_block(first))
    min_second = max(10.0, second["min_font_size"] * min_line_height_for_block(second))
    if first_height < min_first:
        first_height = min_first
        second_height = max(min_second, available - first_height)
    if second_height < min_second:
        second_height = min_second
        first_height = max(min_first, available - second_height)

    first["top"] = union_top
    first["height"] = min(first_height, page_height - first["top"])
    second["top"] = first["top"] + first["height"] + gap
    second["height"] = min(second_height, page_height - second["top"])

    fit_block_to_bbox(first, page_height)
    fit_block_to_bbox(second, page_height)
    first["pair_reflowed"] = True
    second["pair_reflowed"] = True


def resolve_strict_bbox_overflows(blocks, page_height):
    ordered = sorted(blocks, key=lambda b: (is_header_footer(b), b["top"], b["left"], b["order"]))
    flow_blocks = [block for block in ordered if not is_header_footer(block)]

    for index, block in enumerate(flow_blocks):
        if block.get("strict_bbox_fit") or block.get("preserve_source_line_spacing"):
            continue
        partner = None
        for candidate in flow_blocks[index + 1:]:
            if horizontal_overlap_ratio(block, candidate) >= 0.20:
                partner = candidate
                break
        if partner is None:
            continue
        reflow_overflow_pair(block, partner, page_height)

    return sorted(ordered, key=lambda b: (is_header_footer(b), b["top"], b["left"], b["order"]))



def prepare_blocks(blocks, page_width, page_height):
    reportlab_register_fonts()
    body_size = estimate_page_body_size(blocks, page_height)
    body_font_floor = scaled_body_text_font_floor(page_height)
    dense_note_font_size = dense_numbered_note_page_font_size(blocks, page_height)
    prepared = []
    for block in blocks:
        numbered_layout_text = numbered_layout_text_number(block) is not None
        if numbered_layout_text:
            # OCR bboxes already encode the item's horizontal placement. The
            # Markdown renderer must not add another list indent on top of it.
            block["preserve_numbered_bbox_indent"] = True
        dense_numbered_note = dense_note_font_size is not None and numbered_layout_text
        if dense_numbered_note:
            block["dense_numbered_note"] = True
            block["preserve_source_line_spacing"] = True
        repair_misclassified_header_footer_block(block, page_width, page_height, body_size)
        if not dense_numbered_note:
            expand_compressed_body_block(block, blocks, page_width, page_height, body_size)
        block["original_left"] = block["left"]
        block["original_top"] = block["top"]
        block["original_width"] = block["width"]
        block["original_height"] = block["height"]
        font_size = dense_note_font_size if dense_numbered_note else estimate_font_size(block, body_size)
        if is_body_text_block(block) and not dense_numbered_note:
            font_size = max(font_size, min(body_font_floor, body_size))
            block["enforce_body_font_floor"] = True
            block["body_text_font_floor"] = body_font_floor
        clean_text = clean_block_text(block)
        line_height = estimate_line_height(block, font_size)
        need_height = estimate_required_height(block, font_size, line_height)
        if dense_numbered_note:
            min_font = max(2.6, font_size * 0.64)
        elif block["category"] == "Footnote":
            min_font = max(4.8, font_size * 0.64)
        elif block["category"] == "Text":
            if block.get("enforce_body_font_floor"):
                min_font = max(5.2, font_size * 0.64)
            else:
                min_font = max(5.2, font_size * 0.64)
        else:
            min_font = max(5.0, font_size * 0.72)

        if block.get("enforce_body_font_floor") and need_height > block["height"] * 0.96:
            visual_lines = estimate_visual_line_count(block, font_size)
            compact_line_height = block["height"] * 0.96 / max(font_size * visual_lines, 1)
            compact_line_height = clamp(compact_line_height, BODY_TEXT_FLOOR_LINE_HEIGHT_MIN, line_height)
            line_height = compact_line_height
            need_height = estimate_required_height(block, font_size, line_height)

        while need_height > block["height"] * 0.96 and font_size > min_font:
            font_size -= 0.25
            need_height = estimate_required_height(block, font_size, line_height)

        block["font_size"] = round(font_size, 2)
        block["min_font_size"] = round(min_font, 2)
        block["line_height"] = line_height
        fit_block_to_bbox(block, page_height)
        if block["font_size"] < block["min_font_size"]:
            block["min_font_size"] = block["font_size"]
        prepared.append(block)
    return resolve_strict_bbox_overflows(prepared, page_height)


def table_block_to_markdown(block):
    rows = table_rows_for_block(block)
    if not rows:
        return normalize_text(block["text"])

    if is_toc_table(rows):
        lines = []
        for row in rows:
            left_text, right_text = table_row_parts(row)
            if not left_text and not right_text:
                continue
            indent = "  " * table_row_level(row, left_text)
            if right_text:
                lines.append(f"{indent}{left_text} ...... {right_text}".rstrip())
            else:
                lines.append(f"{indent}{left_text}".rstrip())
        return "\n".join(lines).strip()

    normalized_rows = []
    max_cols = 0
    for row in rows:
        cells = row.get("cells") or row.get("nonempty_cells") or []
        cells = [normalize_text(cell) for cell in cells]
        if any(cells):
            normalized_rows.append(cells)
            max_cols = max(max_cols, len(cells))
    if not normalized_rows:
        return ""

    padded = [row + [""] * (max_cols - len(row)) for row in normalized_rows]
    header = padded[0]
    separator = ["---"] * max_cols
    body = padded[1:]
    markdown_rows = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(separator) + " |",
    ]
    markdown_rows.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(markdown_rows).strip()


def blocks_to_markdown_page(blocks, page_index):
    lines = []
    for block in blocks:
        if block["category"] in {"Page-header", "Page-footer"}:
            continue
        category = block["category"]
        if category == "Table":
            text = table_block_to_markdown(block)
        else:
            text = normalize_text(block["text"])
        if not text:
            continue
        if category == "Title":
            lines.append(f"# {text}")
        elif category == "Section-header":
            lines.append(f"## {text}")
        elif category == "Caption":
            lines.append(f"*{text}*")
        elif category == "List-item":
            if re.match(r"^\s*(?:[-*+]|\d+[.)])\s+", text):
                lines.append(text)
            else:
                lines.append(f"- {text}")
        elif category == "Formula":
            lines.append("```")
            lines.append(text)
            lines.append("```")
        else:
            lines.append(text)
        lines.append("")
    return "\n".join(lines).strip()


def markdown_page_from_result(result, blocks, page_index):
    md_nohf_text = normalize_markdown_text(result.get("md_nohf_text", ""))
    if md_nohf_text:
        return md_nohf_text
    return blocks_to_markdown_page(blocks, page_index)


def write_book_markdown(md_path, markdown_pages):
    content = "\n\n".join(page for page in markdown_pages if page.strip())
    md_path.write_text(content + "\n", encoding="utf-8")


def category_render_policy(category):
    policies = {
        "Caption": "caption text block; centered in PDF; italicized in generated Markdown fallback",
        "Footnote": "small footnote block with stricter font bounds; included in PDF and nohf Markdown",
        "Formula": "formula text block; strict bbox rendering; fenced code block in generated Markdown fallback",
        "List-item": "list Markdown line; bullet/number formatting preserved",
        "Page-footer": "page footer rendered in PDF; excluded from generated Markdown fallback",
        "Page-header": "page header rendered in PDF; excluded from generated Markdown fallback",
        "Picture": "ignored unless OCR supplies textual content",
        "Section-header": "section heading; centered/bold PDF; ## in generated Markdown fallback",
        "Table": "cell-aware table rendering; TOC vs plain table auto-detected from original cell data; structured Markdown fallback",
        "Text": "body text block; strict bbox rendering",
        "Title": "title heading; centered/bold PDF; # in generated Markdown fallback",
    }
    return policies.get(category, "generic text block; strict bbox rendering")


def log_category_audit(category_counts, md_nohf_pages, total_pages):
    log("    Layout category audit:")
    for category in LAYOUT_CATEGORIES:
        count = category_counts.get(category, 0)
        log(f"        {category}: {count} - {category_render_policy(category)}")
    extra = sorted(set(category_counts) - set(LAYOUT_CATEGORIES))
    for category in extra:
        log(f"        {category}: {category_counts[category]} - {category_render_policy(category)}")
    log(f"    Markdown source: nohf pages={md_nohf_pages}/{total_pages}; fallback pages={total_pages - md_nohf_pages}")


_COMPONENT_EXPORTS = (
    "cell_to_block",
    "fallback_text_block",
    "image_fallback_block",
    "blocks_from_page_result",
    "source_page_text_rotation_degrees",
    "rotate_blocks_for_source_orientation",
    "has_cjk",
    "inline_markdown_segments",
    "markdown_line_to_segments",
    "line_font_scale",
    "line_is_bold",
    "line_is_italic",
    "is_header_footer",
    "is_title",
    "clean_block_text",
    "is_numeric_page_marker",
    "is_short_running_text",
    "is_body_text_block",
    "NUMBERED_LAYOUT_TEXT_RE",
    "numbered_layout_text_number",
    "dense_numbered_note_page_font_size",
    "is_misclassified_long_header_footer",
    "repair_misclassified_header_footer_block",
    "line_count_for_text",
    "block_structural_line_count",
    "line_info_font_size_estimate",
    "estimate_bbox_font_size",
    "median",
    "page_font_scale",
    "scaled_body_font_min",
    "scaled_body_font_max",
    "scaled_body_text_font_floor",
    "estimate_page_body_size",
    "estimate_font_size",
    "clamp",
    "estimate_required_height",
    "estimate_visual_line_count",
    "estimate_line_height",
    "estimated_inline_width",
    "horizontal_overlap_ratio",
    "block_bottom",
    "dedupe_text_key",
    "join_deduplicated_text",
    "append_text_with_overlap",
    "text_overlap_like",
    "retry_blocks_should_merge",
    "merge_retry_block_pair",
    "merge_overlapping_retry_blocks",
    "duplicate_ocr_blocks_should_merge",
    "merge_overlapping_duplicate_ocr_blocks",
    "fuzzy_dedupe_text_key",
    "leading_ocr_reference",
    "anchored_duplicate_short_block",
    "repair_overlapping_fuzzy_duplicate_ocr_lines",
    "stretched_margin_numeric_block_side",
    "intended_stretched_margin_numbers",
    "repair_stretched_margin_numeric_ocr_blocks",
    "repair_margin_number_sequence_values",
    "strip_captured_margin_number_prefix",
    "repair_overlapping_margin_line_number_blocks",
    "separate_overlapping_consecutive_text_blocks",
    "strip_captured_reference_prefix",
    "repair_overlapping_margin_reference_blocks",
    "trim_small_adjacent_text_block_overlaps",
    "clamp_split_retry_block_to_page_margins",
    "clamp_split_retry_blocks_to_page_margins",
    "min_line_height_for_block",
    "preferred_line_height_for_block",
    "body_text_target_font_size",
    "block_has_meaningful_horizontal_overlap",
    "next_flow_block_top",
    "available_body_bottom",
    "should_expand_compressed_body_block",
    "expand_compressed_body_block",
    "textbox_rect",
    "block_align",
    "fit_block_to_bbox",
    "reflow_overflow_pair",
    "resolve_strict_bbox_overflows",
    "prepare_blocks",
    "table_block_to_markdown",
    "blocks_to_markdown_page",
    "markdown_page_from_result",
    "write_book_markdown",
    "category_render_policy",
    "log_category_audit",
    "TEXT_MERGE_CATEGORIES",
    "MARGIN_CLAMP_BLOCK_CATEGORIES",
)
_COMPONENT_RUNTIME = ComponentRuntime(globals(), _COMPONENT_EXPORTS)


def component_exports():
    return _COMPONENT_RUNTIME.exports()


def invoke_component(name, namespace, *args, **kwargs):
    return _COMPONENT_RUNTIME.invoke(name, namespace, *args, **kwargs)
