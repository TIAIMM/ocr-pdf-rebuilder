"""Quality-control inspection, debug artifacts and report generation."""

from pathlib import Path
import json
import re

import fitz

from .component_runtime import ComponentRuntime
from .pipeline_config import *

def path_for_report(path, base=None):
    if not path:
        return None
    try:
        return str(path.relative_to(base)) if base else str(path)
    except ValueError:
        return str(path)


def page_result_text(result):
    parts = []
    for cell in result.get("cells") or []:
        text = normalize_text(source_text_from_cell(cell))
        if text:
            parts.append(text)
    for key in ("fallback_text", "md_nohf_text"):
        text = normalize_text(result.get(key, ""))
        if text:
            parts.append(text)
    return "\n".join(parts)


def collect_qc_suspect_pages(pdf_path, page_results, page_specs, page_count):
    suspect = {}
    with fitz.open(pdf_path) as doc:
        for page_index in range(page_count):
            issues = []
            result = page_results.get(page_index, {})
            text = page_result_text(result)
            if result.get("source_page_retry"):
                issues.append("page_range_retry")
            if result.get("forward_page_content_leak_detected"):
                issues.append("forward_page_content_leak_detected")
            if result.get("forward_page_content_leak_repaired"):
                issues.append("forward_page_content_leak_repaired")
            if result.get("forward_page_content_leak_source_text_fallback"):
                issues.append("forward_page_content_leak_source_text_fallback")
            if result.get("forward_page_content_leak_image_fallback"):
                issues.append("forward_page_content_leak_image_fallback")
            if result.get("pseudotext_detected") or page_result_has_high_repeated_pseudotext(result):
                issues.append("high_repeated_pseudotext")
            if result.get("image_fallback_page") or result.get("image_fallback_path"):
                issues.append("image_fallback_page")
            if result.get("layout_fit_failures"):
                issues.append("layout_block_could_not_fit_bbox")
            if result.get("paddle_bbox_content_repaired"):
                issues.append("paddle_bbox_content_repaired")
            if result.get("table_disabled_retry"):
                issues.append("table_recognition_disabled_page_retry")
            if result.get("blank_page"):
                issues.append("blank_page_output_cleared")
            if page_result_only_font_test_text(result):
                issues.append("font_test_pangram")
            if result.get("needs_retry"):
                issues.append("retry_still_needed")
            if result.get("fallback_text") and not result.get("cells"):
                issues.append("markdown_fallback_only")
            if text and RADICAL_CHAR_RE.search(text):
                issues.append("radical_chars_in_ocr_text")
            if text and CONTROL_CHAR_RE.search(text):
                issues.append("control_chars_in_ocr_text")
            if any(phrase in text for phrase in SUSPICIOUS_LAYOUT_PHRASES):
                issues.append("suspicious_placeholder_text")
            if text and len(normalize_text(text)) < 12 and not is_source_page_blank(doc, page_index):
                issues.append("very_low_text_nonblank_source")

            if page_index < len(page_specs):
                _page_width, _page_height, blocks = page_specs[page_index]
                if any(block.get("table_structural_fallback") for block in blocks):
                    issues.append("table_structural_fallback")
                if any(block.get("misclassified_header_footer_repaired") for block in blocks):
                    issues.append("misclassified_header_footer_repaired")
                if any(block.get("category") == "Table" and not block.get("table_rows") for block in blocks):
                    issues.append("table_without_cell_rows")
                formula_modes = {
                    block.get("formula_render_mode")
                    for block in blocks
                    if block.get("formula_render_mode")
                }
                if "unicode_text" in formula_modes:
                    issues.append("formula_unicode_text")
                if "latex_svg" in formula_modes:
                    issues.append("formula_svg_rendered")
                if "linear_text" in formula_modes:
                    issues.append("formula_linear_fallback")
                if "image_crop" in formula_modes:
                    issues.append("formula_image_crop_fallback")
                if any(block.get("formula_render_error") for block in blocks):
                    issues.append("formula_render_error")
                if any(
                    "dependencies missing" in str(block.get("formula_render_error", ""))
                    or "dependency" in str(block.get("formula_render_error", "")).lower()
                    for block in blocks
                ):
                    issues.append("formula_render_dependency_missing")

            if issues:
                suspect[page_index + 1] = sorted(set(issues))
    return suspect


def render_debug_pdf_pages(debug_pdf, pages, output_dir, label):
    rendered = {}
    if not debug_pdf or not debug_pdf.exists():
        return rendered
    output_dir.mkdir(parents=True, exist_ok=True)
    with fitz.open(debug_pdf) as doc:
        for page_no in pages:
            page_index = page_no - 1
            if page_index < 0 or page_index >= doc.page_count:
                continue
            output_path = output_dir / f"page_{page_no:04d}_{label}.png"
            pix = doc[page_index].get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
            pix.save(str(output_path))
            rendered[str(page_no)] = str(output_path)
    return rendered


def mineru_debug_sources(mineru_dir, parser_runs=None):
    runs = parser_runs or [
        {
            "output_dir": mineru_dir,
            "page_offset": 0,
            "start_page": 0,
            "end_page": None,
            "label": "full",
        }
    ]
    sources = []
    for run in runs:
        _all_json_files, json_files, branch_dir = current_mineru_result_files(run["output_dir"])
        layout_pdf = find_mineru_debug_pdf(branch_dir, "layout")
        span_pdf = find_mineru_debug_pdf(branch_dir, "span")
        sources.append(
            {
                "label": run.get("label", "full"),
                "output_dir": run["output_dir"],
                "page_offset": int(run.get("page_offset", 0)),
                "start_page": int(run.get("start_page", 0)),
                "end_page": run.get("end_page"),
                "branch_dir": branch_dir,
                "json_files": json_files,
                "layout_pdf": layout_pdf,
                "span_pdf": span_pdf,
            }
        )
    return sources


def render_debug_pdf_pages_from_sources(sources, pages, output_dir, label):
    rendered = {}
    output_dir.mkdir(parents=True, exist_ok=True)
    for source in sources:
        debug_pdf = source.get(f"{label}_pdf")
        if not debug_pdf or not debug_pdf.exists():
            continue
        offset = int(source.get("page_offset", 0))
        start_page = int(source.get("start_page", 0))
        end_page = source.get("end_page")
        with fitz.open(debug_pdf) as doc:
            for page_no in pages:
                full_index = page_no - 1
                if full_index < start_page:
                    continue
                if end_page is not None and full_index > int(end_page):
                    continue
                local_page_index = full_index - offset
                if local_page_index < 0 or local_page_index >= doc.page_count:
                    continue
                output_path = output_dir / f"page_{page_no:04d}_{label}.png"
                pix = doc[local_page_index].get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                pix.save(str(output_path))
                rendered[str(page_no)] = str(output_path)
    return rendered


def write_mineru_qc_report(
    pdf_path,
    mineru_dir,
    page_results,
    page_specs,
    output_pdf,
    output_image_pdf=None,
    image_fallback_pages=None,
    parser_runs=None,
    output_validation_scan=None,
):
    sources = mineru_debug_sources(mineru_dir, parser_runs)
    first_source = sources[0] if sources else {}
    layout_pdf = first_source.get("layout_pdf")
    span_pdf = first_source.get("span_pdf")

    with fitz.open(pdf_path) as src:
        source_page_count = src.page_count
    output_page_count = None
    if output_pdf.exists():
        if output_validation_scan is not None:
            output_page_count = output_validation_scan["page_count"]
        else:
            with fitz.open(output_pdf) as out:
                output_page_count = out.page_count

    suspect_pages = collect_qc_suspect_pages(pdf_path, page_results, page_specs, source_page_count)
    if output_page_count != source_page_count:
        suspect_pages.setdefault(1, []).append("output_page_count_mismatch")

    qc_book_dir = QC_DIR / pdf_path.stem
    page_numbers = sorted(suspect_pages)
    rendered_layout = render_debug_pdf_pages_from_sources(sources, page_numbers, qc_book_dir, "layout")
    rendered_span = render_debug_pdf_pages_from_sources(sources, page_numbers, qc_book_dir, "span")

    source_reports = []
    for source in sources:
        output_dir = source["output_dir"]
        source_reports.append(
            {
                "label": source["label"],
                "page_start": source["start_page"] + 1,
                "page_end": None if source["end_page"] is None else int(source["end_page"]) + 1,
                "output_dir": str(output_dir),
                "mineru_result_branch": path_for_report(source["branch_dir"], output_dir),
                "primary_json": [path_for_report(path, output_dir) for path in source["json_files"]],
                "layout_pdf": path_for_report(source["layout_pdf"], output_dir),
                "span_pdf": path_for_report(source["span_pdf"], output_dir),
            }
        )

    report = {
        "input_pdf": str(pdf_path),
        "output_pdf": str(output_pdf),
        "output_text_pdf": str(output_pdf),
        "output_image_pdf": str(output_image_pdf) if output_image_pdf else None,
        "image_fallback_pages": [int(page_index) + 1 for page_index in (image_fallback_pages or [])],
        "mineru_output_dir": str(mineru_dir),
        "mineru_sources": source_reports,
        "source_page_count": source_page_count,
        "output_page_count": output_page_count,
        "debug_pdf_page_count_match": {
            "layout": None,
            "span": None,
        },
        "suspect_pages": suspect_pages,
        "rendered_debug_pages": {
            "layout": rendered_layout,
            "span": rendered_span,
        },
    }

    for key, debug_pdf in (("layout", layout_pdf), ("span", span_pdf)):
        if debug_pdf and debug_pdf.exists():
            with fitz.open(debug_pdf) as doc:
                if len(sources) == 1:
                    report["debug_pdf_page_count_match"][key] = doc.page_count == source_page_count
                else:
                    report["debug_pdf_page_count_match"][key] = None

    report_path = LOG_DIR / f"{pdf_path.stem}_qc.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"    QC report: {report_path}")
    if page_numbers:
        log("    QC suspect pages: " + ", ".join(str(page) for page in page_numbers))
    if not any(source.get("span_pdf") for source in sources):
        log("    QC note: MinerU span.pdf was not found; using middle.json line/span data only")
    return report




_COMPONENT_EXPORTS = (
    "path_for_report",
    "page_result_text",
    "collect_qc_suspect_pages",
    "render_debug_pdf_pages",
    "mineru_debug_sources",
    "render_debug_pdf_pages_from_sources",
    "write_mineru_qc_report",
)
_COMPONENT_RUNTIME = ComponentRuntime(globals(), _COMPONENT_EXPORTS)


def component_exports():
    return _COMPONENT_RUNTIME.exports()


def invoke_component(name, namespace, *args, **kwargs):
    return _COMPONENT_RUNTIME.invoke(name, namespace, *args, **kwargs)
