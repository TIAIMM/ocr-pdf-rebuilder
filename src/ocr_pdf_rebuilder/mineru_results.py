"""MinerU JSON selection, normalization, coverage and page-result loading."""

from pathlib import Path
import json
import re

import fitz
from PIL import Image

from .component_runtime import ComponentRuntime
from .pipeline_config import *

def repair_single_incomplete_layout_json(text):
    stripped = str(text or "").strip()
    if not stripped.startswith("[{"):
        return None

    bbox_match = re.search(r'"bbox"\s*:\s*\[([^\]]+)\]', stripped)
    category_match = re.search(r'"category"\s*:\s*"([^"]+)"', stripped)
    text_match = re.search(r'"text"\s*:\s*"', stripped)
    if not bbox_match or not category_match or not text_match:
        return None

    bbox = []
    for item in bbox_match.group(1).split(","):
        item = item.strip()
        try:
            bbox.append(int(float(item)))
        except ValueError:
            return None
    if len(bbox) != 4:
        return None

    content = stripped[text_match.end():]
    content = content.rstrip()
    while content.endswith("\\"):
        content = content[:-1]
    content = content.replace('\\"', '"').replace("\\\\", "\\")
    content = normalize_markdown_text(content)
    if not content:
        return None

    return [
        {
            "bbox": bbox,
            "category": category_match.group(1),
            "text": content,
            "__repaired_incomplete": True,
        }
    ]


def read_json(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    for _ in range(2):
        if not isinstance(data, str):
            break
        stripped = data.strip()
        if not stripped or stripped[0] not in "[{":
            break
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            repaired = repair_single_incomplete_layout_json(stripped)
            if repaired is None:
                raise
            return repaired
    return data


def load_jsonl(path):
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def markdown_to_text(path):
    if not path or not Path(path).exists():
        return ""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    text = normalize_markdown_text(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def collect_cell_texts(cells):
    texts = []
    for cell in cells:
        if isinstance(cell, dict):
            text = get_text_from_cell(cell).strip()
            if text:
                texts.append(text)
    return texts


def repeated_pseudotext_metrics(text):
    raw = strip_control_chars(str(text or ""))
    raw_len = len(raw)
    collapsed = collapse_repeated_ocr_text(raw)
    tokens = re.findall(r"\S+", raw)
    lowered = [token.lower() for token in tokens]
    coord_tokens = sum(
        1
        for token in lowered
        if re.fullmatch(r"[xy]\s*=?-?\d+(?:\.\d+)?", token)
    )
    hash_markers = sum(1 for token in lowered if token.strip() == "###")
    compression_ratio = len(collapsed) / max(raw_len, 1)
    return {
        "raw_len": raw_len,
        "collapsed_len": len(collapsed),
        "compression_ratio": compression_ratio,
        "coord_tokens": coord_tokens,
        "hash_markers": hash_markers,
        "token_count": len(tokens),
    }


def is_high_repeated_pseudotext(text):
    metrics = repeated_pseudotext_metrics(text)
    if metrics["raw_len"] < PSEUDOTEXT_MIN_RAW_CHARS:
        return False
    if metrics["coord_tokens"] >= PSEUDOTEXT_MIN_COORD_TOKENS:
        return True
    if metrics["hash_markers"] >= PSEUDOTEXT_MIN_HASH_MARKERS:
        return True
    if (
        metrics["compression_ratio"] <= PSEUDOTEXT_MAX_COMPRESSION_RATIO
        and looks_like_repeated_token_artifact(text)
    ):
        return True
    return False


def page_result_pseudotext_report(result):
    offenders = []
    if not result:
        return offenders

    for cell_index, cell in enumerate(result.get("cells") or []):
        if not isinstance(cell, dict):
            continue
        raw = source_text_from_cell(cell)
        if is_high_repeated_pseudotext(raw):
            metrics = repeated_pseudotext_metrics(raw)
            metrics["source"] = f"cell:{cell_index}"
            metrics["category"] = normalize_category(cell.get("category"))
            offenders.append(metrics)

    for key in ("fallback_text", "md_nohf_text"):
        raw = result.get(key)
        if raw and is_high_repeated_pseudotext(raw):
            metrics = repeated_pseudotext_metrics(raw)
            metrics["source"] = key
            metrics["category"] = "fallback"
            offenders.append(metrics)

    return offenders


def page_result_has_high_repeated_pseudotext(result):
    return bool(page_result_pseudotext_report(result))


def layout_retry_reasons(row, cells, fallback_text):
    reasons = []
    if row.get("filtered"):
        reasons.append("OCR parser filtered/cleaned invalid JSON")
    if fallback_text and not cells:
        reasons.append("using Markdown fallback instead of layout JSON")

    joined_text = "\n".join(collect_cell_texts(cells))
    for phrase in SUSPICIOUS_LAYOUT_PHRASES:
        if phrase in joined_text:
            reasons.append(f"suspicious hallucinated placeholder text: {phrase}")

    for cell in cells:
        if not isinstance(cell, dict):
            continue
        if has_dot_leader_explosion(source_text_from_cell(cell)):
            reasons.append(DOT_LEADER_RETRY_REASON)
            break

    if any(is_high_repeated_pseudotext(source_text_from_cell(cell)) for cell in cells if isinstance(cell, dict)):
        reasons.append(PSEUDOTEXT_RETRY_REASON)

    if fallback_text and has_dot_leader_explosion(fallback_text):
        reasons.append(DOT_LEADER_RETRY_REASON)

    if fallback_text and is_high_repeated_pseudotext(fallback_text):
        reasons.append(PSEUDOTEXT_RETRY_REASON)

    return reasons

MINERU_CATEGORY_MAP = {
    "caption": "Caption",
    "figure_caption": "Caption",
    "footnote": "Footnote",
    "page_footnote": "Footnote",
    "page-footnote": "Footnote",
    "formula": "Formula",
    "equation": "Formula",
    "interline_equation": "Formula",
    "isolate_formula": "Formula",
    "list": "List-item",
    "list-item": "List-item",
    "list_item": "List-item",
    "page-footer": "Page-footer",
    "page_footer": "Page-footer",
    "footer": "Page-footer",
    "page-header": "Page-header",
    "page_header": "Page-header",
    "header": "Page-header",
    "image": "Picture",
    "figure": "Picture",
    "picture": "Picture",
    "section-header": "Section-header",
    "section_header": "Section-header",
    "section title": "Section-header",
    "section_title": "Section-header",
    "table": "Table",
    "text": "Text",
    "plain_text": "Text",
    "paragraph": "Text",
    "page_number": "Page-footer",
    "paragraph_title": "Section-header",
    "title": "Title",
}

MINERU_JSON_PREFERENCE = {
    "middle": 0,
    "model": 1,
    "layout": 2,
    "content": 3,
}


def mineru_json_priority(path):
    name = path.name.lower()
    for token, priority in MINERU_JSON_PREFERENCE.items():
        if token in name:
            return priority
    return 10


def select_mineru_json_files(json_files):
    if not json_files:
        return []

    middle = [path for path in json_files if path.name.endswith("_middle.json")]
    if middle:
        return sorted(middle)

    content_list = [
        path for path in json_files
        if path.name.endswith("_content_list.json") and "_content_list_v2" not in path.name
    ]
    if content_list:
        return sorted(content_list)

    model = [path for path in json_files if path.name.endswith("_model.json")]
    if model:
        return sorted(model)

    return [sorted(json_files, key=lambda path: (mineru_json_priority(path), str(path)))[0]]


def mineru_result_branch_dir(primary_files, output_dir):
    if not primary_files:
        return output_dir
    return primary_files[0].parent


def files_in_mineru_result_branch(files, branch_dir):
    return sorted(path for path in files if path.parent == branch_dir)


def find_mineru_debug_pdf(branch_dir, kind):
    suffixes = [f"_{kind}.pdf"]
    if kind == "span":
        suffixes.append("_spans.pdf")
    matches = []
    for suffix in suffixes:
        matches.extend(sorted(path for path in branch_dir.glob(f"*{suffix}")))
    if matches:
        return sorted(matches)[0]
    for suffix in suffixes:
        matches.extend(sorted(path for path in branch_dir.rglob(f"*{suffix}")))
    return matches[0] if matches else None


def current_mineru_result_files(output_dir):
    all_json_files = sorted(output_dir.rglob("*.json"), key=lambda path: (mineru_json_priority(path), str(path)))
    json_files = select_mineru_json_files(all_json_files)
    if not json_files:
        return [], [], output_dir
    branch_dir = mineru_result_branch_dir(json_files, output_dir)
    branch_json_files = files_in_mineru_result_branch(all_json_files, branch_dir)
    branch_primary_files = files_in_mineru_result_branch(json_files, branch_dir)
    return branch_json_files, branch_primary_files, branch_dir


def select_mineru_supplement_json_files(json_files, primary_files):
    # Keep PDF reconstruction layout-driven. Supplemental hole filling only
    # reads MinerU middle.json preproc blocks, never flat content/model JSON.
    return sorted(path for path in json_files if path.name.endswith("_middle.json"))


def normalize_mineru_category(value):
    raw = str(value or "Text").strip()
    key = raw.lower().replace(" ", "_")
    return MINERU_CATEGORY_MAP.get(key, MINERU_CATEGORY_MAP.get(raw.lower(), raw or "Text"))


def mineru_page_index_from_value(key, value, page_hint=None):
    if value is None:
        return page_hint
    try:
        number = int(value)
    except (TypeError, ValueError):
        return page_hint
    if key in {"page_idx", "page_index", "page_id"}:
        return max(0, number)
    if key in {"page", "page_no", "page_number"}:
        return max(0, number - 1) if number > 0 else 0
    return page_hint


def extract_mineru_page_index(obj, page_hint=None):
    if not isinstance(obj, dict):
        return page_hint
    for key in ("page_idx", "page_index", "page_id", "page_no", "page_number", "page"):
        if key in obj:
            return mineru_page_index_from_value(key, obj.get(key), page_hint)
    return page_hint


def normalize_mineru_bbox(value):
    if isinstance(value, dict):
        keys = ("x0", "y0", "x1", "y1")
        if all(key in value for key in keys):
            value = [value[key] for key in keys]
        else:
            keys = ("left", "top", "right", "bottom")
            if all(key in value for key in keys):
                value = [value[key] for key in keys]
    if not isinstance(value, (list, tuple)):
        return None
    if len(value) == 4 and all(isinstance(item, (int, float)) for item in value):
        x0, y0, x1, y1 = [float(item) for item in value]
        if x1 > x0 and y1 > y0:
            return [x0, y0, x1, y1]
    if len(value) >= 4 and all(isinstance(item, (list, tuple)) and len(item) >= 2 for item in value[:4]):
        xs = [float(point[0]) for point in value]
        ys = [float(point[1]) for point in value]
        return [min(xs), min(ys), max(xs), max(ys)]
    return None


def extract_mineru_bbox(obj):
    if not isinstance(obj, dict):
        return None
    for key in ("bbox", "block_bbox", "layout_bbox", "poly", "polygon"):
        bbox = normalize_mineru_bbox(obj.get(key))
        if bbox:
            return bbox
    return None


def extract_mineru_text(obj):
    if isinstance(obj, str):
        return obj
    if isinstance(obj, list):
        parts = [extract_mineru_text(item) for item in obj]
        return "\n".join(part for part in parts if part.strip())
    if not isinstance(obj, dict):
        return ""

    for key in ("text", "content", "markdown", "html", "latex"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value

    parts = []
    for key in ("spans", "lines", "cells", "children", "blocks"):
        value = obj.get(key)
        if isinstance(value, list):
            text = extract_mineru_text(value)
            if text.strip():
                parts.append(text)
    return "\n".join(parts)


def bbox_overlap_ratio(source_bbox, target_bbox):
    source = normalize_mineru_bbox(source_bbox)
    target = normalize_mineru_bbox(target_bbox)
    if not source or not target:
        return 0.0
    area = bbox_area(source)
    if area <= 0:
        return 0.0
    return bbox_intersection_area(source, target) / area


def mineru_span_text(span):
    if not isinstance(span, dict):
        return ""
    for key in ("content", "text", "markdown", "latex"):
        value = span.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def mineru_span_type(span):
    if not isinstance(span, dict):
        return ""
    return str(span.get("type") or span.get("category") or span.get("span_type") or "").lower()


def normalize_mineru_span(span):
    text = mineru_span_text(span)
    if not text:
        return None
    span_type = mineru_span_type(span)
    style = "math" if "formula" in span_type or "equation" in span_type else "normal"
    return {
        "text": text,
        "bbox": extract_mineru_bbox(span),
        "type": span_type,
        "style": style,
    }


def is_cjk_char(ch):
    return bool(re.match(r"[\u3400-\u9fff\u3000-\u303f\uff00-\uffef]", ch))


def is_latin_greek_digit_char(ch):
    return bool(re.match(r"[A-Za-z0-9\u00C0-\u024F\u1E00-\u1EFF\u0370-\u03FF\u1F00-\u1FFF\u0400-\u052F\u2DE0-\u2DFF\uA640-\uA69F]", ch))


def text_needs_gap(left_text, right_text):
    if not left_text or not right_text:
        return False
    left = left_text[-1]
    right = right_text[0]
    if left.isspace() or right.isspace():
        return False
    if is_cjk_char(left) or is_cjk_char(right):
        return False
    return is_latin_greek_digit_char(left) or is_latin_greek_digit_char(right)


def join_mineru_spans(spans):
    if not spans:
        return ""

    parts = []
    previous = None
    for span in spans:
        text = span.get("text", "")
        if not text:
            continue

        if previous and text_needs_gap(previous.get("text", ""), text):
            previous_bbox = previous.get("bbox")
            current_bbox = span.get("bbox")
            if previous_bbox and current_bbox:
                gap = float(current_bbox[0]) - float(previous_bbox[2])
                height = max(1.0, float(previous_bbox[3]) - float(previous_bbox[1]))
                if gap > height * 0.20:
                    parts.append(" ")

        parts.append(text)
        previous = span
    return "".join(parts)


def mineru_line_text(line):
    if not isinstance(line, dict):
        return ""
    raw_spans = line.get("spans")
    if isinstance(raw_spans, list) and raw_spans:
        spans = [normalize_mineru_span(span) for span in raw_spans]
        spans = [span for span in spans if span]
        text = join_mineru_spans(spans)
        if text.strip():
            return text
    return str(line.get("content") or line.get("text") or "")


def mineru_lines_inside_block(obj):
    block_bbox = extract_mineru_bbox(obj)
    lines = obj.get("lines") if isinstance(obj, dict) else None
    if not block_bbox or not isinstance(lines, list):
        return []
    kept = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        line_bbox = extract_mineru_bbox(line)
        if line_bbox and bbox_overlap_ratio(line_bbox, block_bbox) < 0.50:
            continue
        raw_spans = line.get("spans") if isinstance(line.get("spans"), list) else []
        spans = [normalize_mineru_span(span) for span in raw_spans]
        spans = [span for span in spans if span]
        text = join_mineru_spans(spans) or mineru_line_text(line)
        if not text.strip():
            continue
        kept.append(
            {
                "bbox": line_bbox,
                "text": text,
                "spans": spans,
            }
        )
    return kept


def mineru_block_text(obj):
    if not isinstance(obj, dict):
        return extract_mineru_text(obj)
    for key in ("text", "content", "markdown", "html", "latex"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value
    lines = mineru_lines_inside_block(obj)
    if lines:
        return "\n".join(line["text"] for line in lines if line["text"].strip())
    return extract_mineru_text(obj)


def mineru_nested_table_html(obj):
    if not isinstance(obj, dict):
        return ""
    direct_html = obj.get("html")
    if isinstance(direct_html, str) and direct_html.strip():
        return direct_html.strip()

    fragments = []
    for block in obj.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        for line in block.get("lines") or []:
            if not isinstance(line, dict):
                continue
            for span in line.get("spans") or []:
                if not isinstance(span, dict):
                    continue
                html = span.get("html")
                if isinstance(html, str) and html.strip():
                    fragments.append(html.strip())
    return "\n".join(fragments)


def mineru_raw_block_category(obj):
    if not isinstance(obj, dict):
        return None
    return (
        obj.get("category")
        or obj.get("type")
        or obj.get("block_type")
        or obj.get("layout_type")
        or obj.get("label")
    )


def looks_like_mineru_block(obj):
    if not isinstance(obj, dict):
        return False
    text = mineru_block_text(obj).strip()
    if normalize_mineru_category(mineru_raw_block_category(obj)) == "Table":
        text = mineru_nested_table_html(obj) or text
    return extract_mineru_bbox(obj) is not None and bool(text)


def mineru_bbox_units(obj, bbox, source_path):
    name = source_path.name.lower()
    if "content_list" in name:
        return "mineru_1000"
    if "model" in name and bbox and max(abs(float(v)) for v in bbox) <= 1.5:
        return "relative"
    if "middle" in name:
        return "pdf"
    return None


def mineru_block_to_cell(obj, page_index, source_path, page_size=None):
    bbox = extract_mineru_bbox(obj)
    raw_category = mineru_raw_block_category(obj)
    category = normalize_mineru_category(raw_category)
    text = mineru_block_text(obj)
    table_html = mineru_nested_table_html(obj) if category == "Table" else ""
    if table_html:
        text = table_html
    if not bbox or not text.strip():
        return None
    if obj.get("text_level") is not None and category == "Text":
        category = "Section-header"
    cell = {
        "bbox": bbox,
        "category": category,
        "text": text,
        "source": "mineru",
        "source_json": str(source_path),
        "__bbox_units": mineru_bbox_units(obj, bbox, source_path),
        "__line_info": mineru_lines_inside_block(obj),
        "__block_type": raw_category,
    }
    if page_size and len(page_size) == 2:
        cell["__page_size"] = [float(page_size[0]), float(page_size[1])]
    if category == "Table":
        if table_html:
            # Keep the structured source ahead of the flattened fallback text
            # so cell_to_block() can reconstruct rows and columns.
            cell["text"] = table_html
            cell["html"] = table_html
        elif obj.get("latex"):
            cell["latex"] = str(obj.get("latex"))
    return page_index, cell


def merge_mineru_page_level_blocks(obj, source_path, page_index, page_size, include_preproc):
    found = []

    para_blocks = obj.get("para_blocks")
    if isinstance(para_blocks, list):
        found.extend(walk_mineru_json(para_blocks, source_path, page_index, page_size, include_preproc))

    discarded_blocks = obj.get("discarded_blocks")
    if isinstance(discarded_blocks, list):
        for item in discarded_blocks:
            if not isinstance(item, dict):
                continue
            category = normalize_mineru_category(item.get("type") or item.get("category"))
            if category in {"Footnote", "Page-header", "Page-footer"}:
                found.extend(walk_mineru_json(item, source_path, page_index, page_size, include_preproc))

    preproc_blocks = obj.get("preproc_blocks")
    if isinstance(preproc_blocks, list):
        preproc_cells = walk_mineru_json(preproc_blocks, source_path, page_index, page_size, True)
        existing_cells = [cell for _, cell in found]
        added_preproc = 0
        for preproc_page_index, preproc_cell in preproc_cells:
            if cell_region_covered(preproc_cell, existing_cells):
                continue
            preproc_cell["source"] = "mineru-preproc-hole-fill"
            found.append((preproc_page_index, preproc_cell))
            existing_cells.append(preproc_cell)
            added_preproc += 1
        if added_preproc:
            log(f"    MinerU middle preproc hole-fill blocks added on page {page_index + 1}: {added_preproc}")

    return found


def walk_mineru_json(obj, source_path, page_hint=None, page_size_hint=None, include_preproc=False):
    found = []
    if isinstance(obj, list):
        if obj and all(isinstance(item, list) for item in obj):
            for index, item in enumerate(obj):
                found.extend(walk_mineru_json(item, source_path, index, page_size_hint, include_preproc))
            return found
        for index, item in enumerate(obj):
            found.extend(walk_mineru_json(item, source_path, page_hint, page_size_hint, include_preproc))
        return found
    if not isinstance(obj, dict):
        return found

    page_index = extract_mineru_page_index(obj, page_hint)
    page_size = obj.get("page_size") or page_size_hint
    if looks_like_mineru_block(obj):
        item = mineru_block_to_cell(obj, page_index if page_index is not None else 0, source_path, page_size)
        if item:
            found.append(item)
        return found

    if (
        isinstance(obj.get("para_blocks"), list)
        or isinstance(obj.get("preproc_blocks"), list)
        or isinstance(obj.get("discarded_blocks"), list)
    ):
        return merge_mineru_page_level_blocks(
            obj,
            source_path,
            page_index if page_index is not None else 0,
            page_size,
            include_preproc,
        )

    for key, value in obj.items():
        if key == "pdf_info" and isinstance(value, list):
            for page_index, page_obj in enumerate(value):
                found.extend(walk_mineru_json(page_obj, source_path, page_index, page_obj.get("page_size"), include_preproc))
            continue
        if key == "para_blocks" and isinstance(value, list):
            found.extend(walk_mineru_json(value, source_path, page_index, page_size, include_preproc))
            continue
        if key == "discarded_blocks" and isinstance(value, list):
            for item in value:
                if not isinstance(item, dict):
                    continue
                category = normalize_mineru_category(item.get("type") or item.get("category"))
                if category in {"Footnote", "Page-header", "Page-footer"}:
                    found.extend(walk_mineru_json(item, source_path, page_index, page_size, include_preproc))
            continue
        if key == "preproc_blocks" and isinstance(value, list) and (include_preproc or not obj.get("para_blocks")):
            found.extend(walk_mineru_json(value, source_path, page_index, page_size, include_preproc))
            continue
        if key in {
            "layout_dets",
            "blocks",
            "layouts",
            "cells",
            "tables",
            "children",
        }:
            found.extend(walk_mineru_json(value, source_path, page_index, page_size, include_preproc))
    return found


def cell_relative_bbox(cell):
    bbox = cell.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        return None
    x0, y0, x1, y1 = [float(v) for v in bbox]
    units = cell.get("__bbox_units")
    if units == "relative":
        return [x0, y0, x1, y1]
    if units == "mineru_1000":
        return [x0 / 1000.0, y0 / 1000.0, x1 / 1000.0, y1 / 1000.0]
    if units == "pdf":
        page_size = cell.get("__page_size") or []
        if len(page_size) == 2 and page_size[0] > 0 and page_size[1] > 0:
            return [x0 / page_size[0], y0 / page_size[1], x1 / page_size[0], y1 / page_size[1]]
    max_coord = max(abs(x0), abs(y0), abs(x1), abs(y1))
    if max_coord <= 1.5:
        return [x0, y0, x1, y1]
    if max_coord <= 1200:
        return [x0 / 1000.0, y0 / 1000.0, x1 / 1000.0, y1 / 1000.0]
    return None


def bbox_intersection_area(a, b):
    left = max(a[0], b[0])
    top = max(a[1], b[1])
    right = min(a[2], b[2])
    bottom = min(a[3], b[3])
    return max(0.0, right - left) * max(0.0, bottom - top)


def bbox_area(bbox):
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def cell_region_covered(candidate, existing_cells):
    cand_bbox = cell_relative_bbox(candidate)
    if not cand_bbox:
        return False
    cand_area = bbox_area(cand_bbox)
    if cand_area <= 0:
        return False
    cand_text = normalize_text(source_text_from_cell(candidate))
    for cell in existing_cells:
        if not normalize_text(source_text_from_cell(cell)):
            continue
        cell_bbox = cell_relative_bbox(cell)
        if not cell_bbox:
            continue
        intersection = bbox_intersection_area(cand_bbox, cell_bbox)
        if intersection / cand_area >= 0.45:
            return True
        existing_text = normalize_text(source_text_from_cell(cell))
        if (
            cand_text
            and existing_text
            and len(cand_text) >= 30
            and len(existing_text) >= 30
            and not is_numeric_page_marker(cand_text)
            and not is_numeric_page_marker(existing_text)
            and (cand_text in existing_text or existing_text in cand_text)
        ):
            return True
    return False


def supplement_mineru_holes(pages, all_json_files, primary_files):
    added = 0
    for json_path in select_mineru_supplement_json_files(all_json_files, primary_files):
        try:
            data = read_json(json_path)
        except Exception as exc:
            log(f"    Warning: could not read MinerU supplement JSON {json_path}: {exc}")
            continue
        for page_index, cell in walk_mineru_json(data, json_path, include_preproc=True):
            text = normalize_text(source_text_from_cell(cell))
            if not text:
                continue
            page = pages.setdefault(
                page_index,
                {
                    "cells": [],
                    "fallback_text": "",
                    "filtered": False,
                    "needs_retry": False,
                    "retry_reason": "",
                    "image_size": None,
                    "json_path": json_path,
                    "image_path": None,
                    "md_nohf_text": "",
                    "md_nohf_path": None,
                },
            )
            if cell_region_covered(cell, page["cells"]):
                continue
            cell["source"] = "mineru-supplement"
            page["cells"].append(cell)
            added += 1
    if added:
        log(f"    MinerU supplemental hole-fill blocks added: {added}")


def find_mineru_markdown_files(output_dir):
    files = sorted(output_dir.rglob("*.md"))
    nohf = [path for path in files if "nohf" in path.name.lower()]
    return nohf or files


def page_index_from_filename(path):
    name = path.stem.lower()
    match = re.search(r"(?:page|p)[_-]?(\d+)", name)
    if not match:
        return None
    number = int(match.group(1))
    return max(0, number - 1)


def find_mineru_page_image(output_dir, page_index):
    patterns = [
        f"*page*{page_index + 1}*.jpg",
        f"*page*{page_index + 1}*.png",
        f"*_{page_index + 1}.jpg",
        f"*_{page_index + 1}.png",
    ]
    for pattern in patterns:
        matches = sorted(output_dir.rglob(pattern))
        if matches:
            return matches[0]
    return None


def collect_page_results(pdf_path, output_dir):
    all_json_files, json_files, branch_dir = current_mineru_result_files(output_dir)
    if not json_files:
        raise RuntimeError(f"No MinerU JSON result found in: {output_dir}")

    pages = {}
    seen = set()
    for json_path in json_files:
        try:
            data = read_json(json_path)
        except Exception as exc:
            log(f"    Warning: could not read MinerU JSON {json_path}: {exc}")
            continue

        for page_index, cell in walk_mineru_json(data, json_path):
            text_key = normalize_text(source_text_from_cell(cell))
            if not text_key:
                continue
            bbox_key = tuple(round(float(v), 2) for v in cell.get("bbox", []))
            dedupe_key = (page_index, cell.get("category"), bbox_key, text_key[:300])
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            page = pages.setdefault(
                page_index,
                {
                    "cells": [],
                    "fallback_text": "",
                    "filtered": False,
                    "needs_retry": False,
                    "retry_reason": "",
                    "image_size": None,
                    "json_path": json_path,
                    "image_path": None,
                    "md_nohf_text": "",
                    "md_nohf_path": None,
                },
            )
            page["cells"].append(cell)
            page["json_path"] = json_path

    supplement_mineru_holes(pages, all_json_files, json_files)

    for md_path in find_mineru_markdown_files(branch_dir):
        page_index = page_index_from_filename(md_path)
        if page_index is None:
            continue
        page = pages.setdefault(
            page_index,
            {
                "cells": [],
                "fallback_text": "",
                "filtered": False,
                "needs_retry": False,
                "retry_reason": "",
                "image_size": None,
                "json_path": None,
                "image_path": None,
                "md_nohf_text": "",
                "md_nohf_path": None,
            },
        )
        text = markdown_to_text(md_path)
        page["md_nohf_text"] = text
        page["md_nohf_path"] = md_path
        if not page["cells"]:
            page["fallback_text"] = text

    for page_index, page in pages.items():
        page["cells"] = sanitize_layout_cells(page["cells"])
        reasons = layout_retry_reasons(page, page["cells"], page.get("fallback_text", ""))
        if reasons:
            for reason in reasons:
                append_retry_reason(page, reason)
            page["needs_retry"] = True
        image_path = find_mineru_page_image(branch_dir, page_index)
        if image_path:
            page["image_path"] = image_path
            with Image.open(image_path) as img:
                page["image_size"] = img.size

    log(
        "    MinerU JSON source selected: "
        + ", ".join(str(path.relative_to(output_dir)) for path in json_files)
    )
    log(f"    MinerU result branch: {branch_dir.relative_to(output_dir) if branch_dir != output_dir else '.'}")
    log(f"    MinerU JSON files scanned: {len(json_files)} of {len(all_json_files)}")
    log(f"    MinerU pages with layout cells: {sum(1 for page in pages.values() if page['cells'])}")
    return pages


def build_mineru_result_manifest(pdf_path, output_dir):
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    all_json_files, primary_files, branch_dir = current_mineru_result_files(output_dir)
    if not primary_files:
        raise RuntimeError(f"No MinerU JSON result found in: {output_dir}")

    supplement_files = select_mineru_supplement_json_files(all_json_files, primary_files)
    consumed_files = sorted(set(primary_files) | set(supplement_files))
    for json_path in consumed_files:
        try:
            read_json(json_path)
        except Exception as exc:
            raise RuntimeError(f"Unreadable MinerU JSON result {json_path}: {exc}") from exc

    pages = collect_page_results(pdf_path, output_dir)
    with fitz.open(pdf_path) as source:
        expected_page_count = int(source.page_count)
        covered_pages = sorted(int(page_index) for page_index in pages)
        out_of_range_pages = [
            page_index
            for page_index in covered_pages
            if page_index < 0 or page_index >= expected_page_count
        ]
        if out_of_range_pages:
            raise RuntimeError(
                "MinerU result contains page indices outside the task PDF: "
                + ", ".join(str(page_index + 1) for page_index in out_of_range_pages)
            )
        missing_pages = sorted(set(range(expected_page_count)) - set(covered_pages))
        blank_missing_pages = [
            page_index for page_index in missing_pages if is_source_page_blank(source, page_index)
        ]
        nonblank_missing_pages = sorted(set(missing_pages) - set(blank_missing_pages))

    coverage = {
        "covered_pages": [page_index + 1 for page_index in covered_pages],
        "blank_source_pages_without_result": [
            page_index + 1 for page_index in blank_missing_pages
        ],
        "nonblank_source_pages_without_result": [
            page_index + 1 for page_index in nonblank_missing_pages
        ],
    }
    accounted_pages = (
        set(coverage["covered_pages"])
        | set(coverage["blank_source_pages_without_result"])
        | set(coverage["nonblank_source_pages_without_result"])
    )
    if accounted_pages != set(range(1, expected_page_count + 1)):
        raise RuntimeError(
            f"MinerU result coverage is incomplete for {pdf_path}: "
            f"accounted={len(accounted_pages)}, expected={expected_page_count}"
        )

    return {
        "schema": 1,
        "task_pdf": file_integrity_signature(pdf_path),
        "expected_page_count": expected_page_count,
        **coverage,
        "accounted_page_count": len(accounted_pages),
        "coverage_hash": stable_json_hash(coverage),
        "result_branch": str(branch_dir.resolve().relative_to(output_dir.resolve())),
        "primary_json": [
            file_integrity_signature(path, relative_to=output_dir)
            for path in primary_files
        ],
        "consumed_json": [
            file_integrity_signature(path, relative_to=output_dir)
            for path in consumed_files
        ],
    }


def mineru_result_manifest_matches(checkpoint, pdf_path, output_dir):
    expected_manifest = checkpoint.get("result_manifest") if checkpoint else None
    if not isinstance(expected_manifest, dict):
        return False
    try:
        current_manifest = build_mineru_result_manifest(pdf_path, output_dir)
    except Exception as exc:
        log(f"    Resume integrity check failed: {exc}")
        return False
    if current_manifest != expected_manifest:
        log("    Resume integrity check failed: MinerU result manifest changed")
        return False
    return True


def clone_page_result_with_offset(result, offset):
    cloned = dict(result)
    cloned["cells"] = [dict(cell) for cell in result.get("cells", [])]
    cloned["page_offset"] = offset
    for cell in cloned["cells"]:
        cell["__page_offset"] = offset
    return cloned


def collect_parser_run_results(pdf_path, parser_runs):
    if len(parser_runs) == 1 and parser_runs[0]["page_offset"] == 0:
        pages = collect_page_results(pdf_path, parser_runs[0]["output_dir"])
        mark_log_error_retry_pages(pages, parser_runs[0]["log_path"])
        return pages

    merged = {}
    for run in parser_runs:
        log(
            f"    Loading MinerU JSON for {run['label']}: "
            f"full pages {run['start_page'] + 1}-{run['end_page'] + 1}"
        )
        chunk_pages = collect_page_results(run["pdf_path"], run["output_dir"])
        mark_log_error_retry_pages(chunk_pages, run["log_path"])
        offset = int(run["page_offset"])
        for local_page_index, result in chunk_pages.items():
            full_page_index = local_page_index + offset
            if full_page_index in merged:
                append_retry_reason(
                    result,
                    f"duplicate page result while merging parser chunk {run['label']}",
                )
            merged[full_page_index] = clone_page_result_with_offset(result, offset)

    log(f"    Merged chunked MinerU results: {len(merged)} page(s)")
    return merged


def parser_log_cell_post_process_error_count(log_path):
    if not log_path.exists():
        return 0
    text = log_path.read_text(encoding="utf-8", errors="replace")
    return text.count("cells post process error")


def append_retry_reason(result, reason):
    existing = result.get("retry_reason")
    if existing:
        reasons = [item.strip() for item in existing.split(";") if item.strip()]
        if reason not in reasons:
            reasons.append(reason)
        result["retry_reason"] = "; ".join(reasons)
    else:
        result["retry_reason"] = reason


def mark_log_error_retry_pages(page_results, log_path):
    error_count = parser_log_cell_post_process_error_count(log_path)
    if not error_count:
        return

    marked = []
    for page_index, result in sorted(page_results.items()):
        reason = result.get("retry_reason", "")
        log_related = (
            result.get("filtered")
            or "layout JSON" in reason
            or "Markdown fallback" in reason
            or "suspicious hallucinated placeholder text" in reason
        )
        if not log_related:
            continue

        result["needs_retry"] = True
        append_retry_reason(
            result,
            f"main MinerU log contains cells post process error ({error_count} total)",
        )
        marked.append(page_index + 1)

    if marked:
        log(
            "    Main MinerU log reported cells post process errors; "
            "auto-retry candidate pages: " + ", ".join(str(page) for page in marked)
        )
    else:
        log(
            "    Warning: main MinerU log reported cells post process errors, "
            "but no page could be mapped from JSONL filtered/layout-fallback records"
        )


def rendered_page_dark_ratio(page):
    zoom = BLANK_PAGE_CHECK_DPI / 72
    pix = page.get_pixmap(
        matrix=fitz.Matrix(zoom, zoom),
        colorspace=fitz.csGRAY,
        alpha=False,
    )
    dark = sum(1 for value in pix.samples if value < BLANK_PAGE_DARK_THRESHOLD)
    return dark / max(1, pix.width * pix.height)


def is_source_page_blank(doc, page_index):
    page = doc[page_index]
    if page.get_text().strip():
        return False
    return rendered_page_dark_ratio(page) <= BLANK_PAGE_MAX_DARK_RATIO


def page_result_has_output(result):
    return bool(
        result.get("cells")
        or result.get("fallback_text")
        or result.get("needs_retry")
        or result.get("image_fallback_path")
    )


def page_result_only_font_test_text(result):
    parts = []
    for cell in result.get("cells") or []:
        text = normalize_text(source_text_from_cell(cell))
        if text:
            parts.append(text)
    for key in ("fallback_text", "md_nohf_text"):
        text = normalize_text(result.get(key, ""))
        if text:
            parts.append(text)

    if not parts:
        return False
    return all(is_font_test_text(part) for part in parts)


def clear_page_result_as_blank(result, reason):
    result["cells"] = []
    result["fallback_text"] = ""
    result["md_nohf_text"] = ""
    result["needs_retry"] = False
    result["blank_page"] = True
    append_retry_reason(result, reason)


def suppress_blank_page_outputs(pdf_path, page_results):
    candidate_indexes = [
        page_index
        for page_index, result in page_results.items()
        if page_result_has_output(result)
    ]
    if not candidate_indexes:
        return

    cleared = []
    font_test_cleared = []
    with fitz.open(pdf_path) as doc:
        for page_index in candidate_indexes:
            if page_index < 0 or page_index >= doc.page_count:
                continue

            result = page_results[page_index]
            if page_result_only_font_test_text(result):
                clear_page_result_as_blank(
                    result,
                    "OCR page contains only font test pangram; keeping blank page",
                )
                font_test_cleared.append(page_index + 1)
                continue

            if not is_source_page_blank(doc, page_index):
                continue

            clear_page_result_as_blank(
                result,
                "source page appears blank; keeping blank page",
            )
            cleared.append(page_index + 1)

    if cleared:
        log(
            "    Blank source pages detected; cleared OCR output and kept blank: "
            + ", ".join(str(page_no) for page_no in cleared)
        )
    if font_test_cleared:
        log(
            "    Font test only pages detected; cleared OCR output and kept blank: "
            + ", ".join(str(page_no) for page_no in font_test_cleared)
        )


def retry_result_has_usable_cells(result):
    if not result:
        return False
    if page_result_has_high_repeated_pseudotext(result):
        return False
    cells = result.get("cells") or []
    if not cells:
        return False
    joined_text = "\n".join(collect_cell_texts(cells))
    if not joined_text.strip():
        return False
    return not any(phrase in joined_text for phrase in SUSPICIOUS_LAYOUT_PHRASES)


def accept_repaired_retry_result(result):
    result["needs_retry"] = False
    result["filtered"] = False
    append_retry_reason(result, "accepted MinerU batched quality retry")


_COMPONENT_EXPORTS = (
    "repair_single_incomplete_layout_json",
    "read_json",
    "load_jsonl",
    "markdown_to_text",
    "collect_cell_texts",
    "repeated_pseudotext_metrics",
    "is_high_repeated_pseudotext",
    "page_result_pseudotext_report",
    "page_result_has_high_repeated_pseudotext",
    "layout_retry_reasons",
    "mineru_json_priority",
    "select_mineru_json_files",
    "mineru_result_branch_dir",
    "files_in_mineru_result_branch",
    "find_mineru_debug_pdf",
    "current_mineru_result_files",
    "select_mineru_supplement_json_files",
    "normalize_mineru_category",
    "mineru_page_index_from_value",
    "extract_mineru_page_index",
    "normalize_mineru_bbox",
    "extract_mineru_bbox",
    "extract_mineru_text",
    "bbox_overlap_ratio",
    "mineru_span_text",
    "mineru_span_type",
    "normalize_mineru_span",
    "is_cjk_char",
    "is_latin_greek_digit_char",
    "text_needs_gap",
    "join_mineru_spans",
    "mineru_line_text",
    "mineru_lines_inside_block",
    "mineru_block_text",
    "mineru_nested_table_html",
    "mineru_raw_block_category",
    "looks_like_mineru_block",
    "mineru_bbox_units",
    "mineru_block_to_cell",
    "merge_mineru_page_level_blocks",
    "walk_mineru_json",
    "cell_relative_bbox",
    "bbox_intersection_area",
    "bbox_area",
    "cell_region_covered",
    "supplement_mineru_holes",
    "find_mineru_markdown_files",
    "page_index_from_filename",
    "find_mineru_page_image",
    "collect_page_results",
    "build_mineru_result_manifest",
    "mineru_result_manifest_matches",
    "clone_page_result_with_offset",
    "collect_parser_run_results",
    "parser_log_cell_post_process_error_count",
    "append_retry_reason",
    "mark_log_error_retry_pages",
    "rendered_page_dark_ratio",
    "is_source_page_blank",
    "page_result_has_output",
    "page_result_only_font_test_text",
    "clear_page_result_as_blank",
    "suppress_blank_page_outputs",
    "retry_result_has_usable_cells",
    "accept_repaired_retry_result",
    "MINERU_CATEGORY_MAP",
    "MINERU_JSON_PREFERENCE",
)
_COMPONENT_RUNTIME = ComponentRuntime(globals(), _COMPONENT_EXPORTS)


def component_exports():
    return _COMPONENT_RUNTIME.exports()


def invoke_component(name, namespace, *args, **kwargs):
    return _COMPONENT_RUNTIME.invoke(name, namespace, *args, **kwargs)

