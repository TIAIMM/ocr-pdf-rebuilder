"""Page-level retry, forward-leak repair and safe fallback policy."""

from pathlib import Path
import re

import fitz

from .component_runtime import ComponentRuntime
from .pipeline_config import *

def apply_forward_page_leak_metadata(result, original, validation=None):
    result["forward_page_content_leak_detected"] = True
    result["forward_page_content_leak_targets"] = list(
        original.get("forward_page_content_leak_targets") or []
    )
    result["forward_page_content_leak_metrics"] = list(
        original.get("forward_page_content_leak_metrics") or []
    )
    if validation is not None:
        result["forward_page_content_leak_retry_validation"] = validation
    append_retry_reason(result, FORWARD_PAGE_LEAK_RETRY_REASON)


def repair_forward_page_content_leaks(pdf_path, page_results, work_dir, log_path):
    source_texts = source_page_overlap_texts(pdf_path)
    detected = detect_forward_page_content_leaks(
        pdf_path,
        page_results,
        source_texts=source_texts,
        mark=True,
    )
    candidates = sorted(detected)
    if not candidates:
        return page_results

    log(
        f"    Forward page-content leak detected on {len(candidates)} page(s): "
        + ", ".join(str(page_index + 1) for page_index in candidates)
    )
    retry_results = {}
    retry_errors = {}
    retry_root = work_dir / "forward_page_leak_isolated_retry"
    for batch_number, start in enumerate(
        range(0, len(candidates), FORWARD_PAGE_LEAK_RETRY_BATCH_SIZE),
        1,
    ):
        batch = candidates[start : start + FORWARD_PAGE_LEAK_RETRY_BATCH_SIZE]
        batch_root = retry_root / f"batch_{batch_number:04d}"
        isolated_pdf = batch_root / f"{pdf_path.stem}_isolated_pages.pdf"
        retry_output_dir = batch_root / "output"
        retry_log = log_path.with_name(
            f"{log_path.stem}_forward_page_leak_batch_{batch_number:04d}{log_path.suffix}"
        )
        retry_checkpoint = batch_root / "isolated_batch_complete.json"
        try:
            isolated_pages = run_or_reuse_mineru_isolated_pages(
                pdf_path,
                batch,
                isolated_pdf,
                retry_output_dir,
                retry_log,
                retry_checkpoint,
            )
        except Exception as exc:
            log(f"    Isolated MinerU repair batch {batch_number} failed: {exc}")
            for page_index in batch:
                retry_errors[page_index] = str(exc)
            continue
        for local_candidate_index, page_index in enumerate(batch):
            retry_results[page_index] = isolated_pages.get(local_candidate_index * 2)

    repaired = []
    source_fallback = []
    image_fallback = []
    image_dir = work_dir / "forward_page_leak_image_fallback_pages"
    with fitz.open(pdf_path) as source:
        for page_index in candidates:
            original = page_results[page_index]
            retry_result = retry_results.get(page_index)
            if retry_result is not None:
                valid, validation = retry_result_is_page_local(
                    retry_result,
                    page_index,
                    source_texts,
                )
            else:
                validation = {
                    "reason": retry_errors.get(
                        page_index,
                        "isolated MinerU repair returned no candidate page result",
                    )
                }
                valid = False

            if valid:
                accept_repaired_retry_result(retry_result)
                apply_forward_page_leak_metadata(retry_result, original, validation)
                retry_result["source_page_retry"] = True
                retry_result["retry_page"] = page_index + 1
                retry_result["forward_page_content_leak_repaired"] = True
                retry_result["forward_page_content_leak_repair_mode"] = "isolated_mineru"
                page_results[page_index] = retry_result
                repaired.append(page_index + 1)
                continue

            fallback_result = source_pdf_text_page_result(source, page_index)
            if fallback_result is not None:
                apply_forward_page_leak_metadata(fallback_result, original, validation)
                fallback_result["forward_page_content_leak_repaired"] = True
                fallback_result["forward_page_content_leak_source_text_fallback"] = True
                fallback_result["forward_page_content_leak_repair_mode"] = "source_pdf_page_text"
                append_retry_reason(
                    fallback_result,
                    FORWARD_PAGE_LEAK_SOURCE_TEXT_FALLBACK_REASON,
                )
                page_results[page_index] = fallback_result
                source_fallback.append(page_index + 1)
                continue

            image_path = image_dir / f"page_{page_index + 1:04d}.png"
            render_pdf_page_to_png(pdf_path, page_index, image_path)
            fallback_result = {
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
                "image_fallback_path": str(image_path),
                "image_fallback_page": True,
                "image_fallback_kind": "forward_page_content_leak",
                "forward_page_content_leak_image_fallback": True,
                "forward_page_content_leak_repair_mode": "source_page_image",
            }
            apply_forward_page_leak_metadata(fallback_result, original, validation)
            append_retry_reason(
                fallback_result,
                FORWARD_PAGE_LEAK_IMAGE_FALLBACK_REASON,
            )
            page_results[page_index] = fallback_result
            image_fallback.append(page_index + 1)

    if repaired:
        log(
            "    Forward page-content leak repaired by isolated MinerU: "
            + ", ".join(str(page_no) for page_no in repaired)
        )
    if source_fallback:
        log(
            "    Forward page-content leak repaired with page-local source PDF text: "
            + ", ".join(str(page_no) for page_no in source_fallback)
        )
    if image_fallback:
        log(
            "    Forward page-content leak pages moved to the image variant: "
            + ", ".join(str(page_no) for page_no in image_fallback)
        )
    return page_results


def collapse_repeated_text_in_split_band_result(result):
    if not result:
        return result
    for cell in result.get("cells", []) or []:
        if not isinstance(cell, dict):
            continue
        for key in ("text", "content", "html", "latex"):
            value = cell.get(key)
            if value:
                cell[key] = collapse_repeated_ocr_text(str(value))
    for key in ("fallback_text", "md_nohf_text"):
        value = result.get(key)
        if value:
            result[key] = collapse_repeated_ocr_text(str(value))
    return result


def retry_bad_pages(pdf_path, page_results, work_dir, log_path):
    bad_pages = sorted(
        page_index
        for page_index, result in page_results.items()
        if result.get("needs_retry")
    )
    if not bad_pages:
        return page_results

    log(
        "    Bad layout JSON pages detected: "
        + ", ".join(str(page_index + 1) for page_index in bad_pages)
    )
    for page_index in bad_pages:
        reason = page_results.get(page_index, {}).get("retry_reason") or "unknown reason"
        log(f"        Page {page_index + 1}: {reason}")
    log(f"    Retrying all {len(bad_pages)} bad page(s) in one MinerU batch")

    retry_root = work_dir / "page_retry_mineru_batch"
    retry_root.mkdir(parents=True, exist_ok=True)

    selected_pdf = retry_root / f"{pdf_path.stem}_bad_pages.pdf"
    retry_output_dir = retry_root / "output"
    retry_log = log_path.with_name(f"{log_path.stem}_bad_pages_batch_retry{log_path.suffix}")
    retry_checkpoint = retry_root / "bad_pages_batch_complete.json"
    try:
        retry_pages = run_or_reuse_mineru_selected_pages(
            pdf_path,
            bad_pages,
            selected_pdf,
            retry_output_dir,
            retry_log,
            retry_checkpoint,
        )
    except Exception as exc:
        log(f"    Batched MinerU quality retry failed; keeping sanitized originals: {exc}")
        for page_index in bad_pages:
            result = page_results.get(page_index) or {"cells": [], "fallback_text": "", "needs_retry": True}
            result["needs_retry"] = True
            append_retry_reason(result, f"batched quality retry failed: {exc}")
            page_results[page_index] = result
        return page_results

    for local_page_index, page_index in enumerate(bad_pages):
        original = page_results.get(page_index) or {"cells": [], "fallback_text": "", "needs_retry": True}
        retry_result = retry_pages.get(local_page_index)
        if retry_result is None:
            append_retry_reason(original, "batched quality retry returned no page result")
            page_results[page_index] = original
            log(f"        Page {page_index + 1}: no result in batched retry; kept original")
            continue

        retry_had_pseudotext = page_result_has_high_repeated_pseudotext(retry_result)
        retry_result = collapse_repeated_text_in_split_band_result(retry_result)
        if retry_had_pseudotext:
            retry_result["needs_retry"] = True
            retry_result["pseudotext_detected"] = True
            append_retry_reason(retry_result, PSEUDOTEXT_RETRY_REASON)
        elif retry_result_has_usable_cells(retry_result):
            accept_repaired_retry_result(retry_result)

        if retry_result.get("needs_retry"):
            append_retry_reason(original, "batched quality retry remained invalid")
            page_results[page_index] = original
            log(f"        Page {page_index + 1}: batched retry remained invalid; kept original")
            continue

        retry_result["source_page_retry"] = True
        retry_result["retry_page"] = page_index + 1
        retry_result["batch_retry_local_page"] = local_page_index + 1
        page_results[page_index] = retry_result
        log(f"        Page {page_index + 1}: accepted result from batched quality retry")

    return page_results


def force_table_cells_to_text(result):
    if not result:
        return result
    for cell in result.get("cells", []) or []:
        if not isinstance(cell, dict):
            continue
        if normalize_category(cell.get("category")) == "Table":
            cell["category"] = "Text"
            cell["__table_disabled_retry"] = True
    result["table_disabled_retry"] = True
    return result


def retry_unfitting_table_pages_as_text_batch(pdf_path, page_results, work_dir, log_path):
    candidates = []
    with fitz.open(pdf_path) as src:
        for page_index, result in sorted(page_results.items()):
            if page_index < 0 or page_index >= src.page_count:
                continue
            page = src[page_index]
            page_width = float(page.rect.width)
            page_height = float(page.rect.height)
            blocks = blocks_from_page_result(result, page_width, page_height)
            if not any(block.get("category") == "Table" for block in blocks):
                continue
            blocks.sort(key=lambda b: (is_header_footer(b), b["top"], b["left"], b["order"]))
            blocks = prepare_blocks(blocks, page_width, page_height)
            table_blocks = [block for block in blocks if block.get("category") == "Table"]
            if reportlab_page_fit_failures(page_height, table_blocks):
                candidates.append(page_index)

    if not candidates:
        return page_results

    log(
        f"    Table bbox preflight found {len(candidates)} page(s); "
        "retrying all of them in one MinerU batch with -t false"
    )
    retry_root = work_dir / "table_text_retry_mineru_batch"
    selected_pdf = retry_root / f"{pdf_path.stem}_table_fit_pages.pdf"
    retry_output_dir = retry_root / "output"
    retry_log = log_path.with_name(f"{log_path.stem}_table_fit_pages_batch_retry{log_path.suffix}")
    retry_checkpoint = retry_root / "table_fit_pages_batch_complete.json"
    try:
        retry_pages = run_or_reuse_mineru_selected_pages(
            pdf_path,
            candidates,
            selected_pdf,
            retry_output_dir,
            retry_log,
            retry_checkpoint,
            table_enabled=False,
        )
    except Exception as exc:
        log(f"    Batched table-off retry failed; affected pages will use image fallback: {exc}")
        for page_index in candidates:
            append_retry_reason(page_results[page_index], f"batched table-off retry failed: {exc}")
        return page_results

    with fitz.open(pdf_path) as src:
        for local_page_index, page_index in enumerate(candidates):
            retry_result = retry_pages.get(local_page_index)
            if not retry_result:
                append_retry_reason(page_results[page_index], "batched table-off retry returned no page result")
                continue
            retry_result = force_table_cells_to_text(retry_result)
            retry_result = collapse_repeated_text_in_split_band_result(retry_result)
            page = src[page_index]
            page_width = float(page.rect.width)
            page_height = float(page.rect.height)
            retry_blocks = blocks_from_page_result(retry_result, page_width, page_height)
            retry_blocks.sort(key=lambda b: (is_header_footer(b), b["top"], b["left"], b["order"]))
            retry_blocks = prepare_blocks(retry_blocks, page_width, page_height)
            retry_failures = reportlab_page_fit_failures(page_height, retry_blocks)
            if not retry_blocks or retry_failures:
                append_retry_reason(page_results[page_index], "batched table-off retry still could not fit bbox")
                continue

            retry_result["source_page_retry"] = True
            retry_result["retry_page"] = page_index + 1
            retry_result["batch_retry_local_page"] = local_page_index + 1
            append_retry_reason(retry_result, TABLE_TEXT_RETRY_REASON)
            page_results[page_index] = retry_result
            log(f"        Page {page_index + 1}: accepted batched table-off retry")

    return page_results


def degrade_repeated_pseudotext_pages_to_images(pdf_path, page_results, work_dir):
    candidates = []
    for page_index, result in sorted(page_results.items()):
        if not result:
            continue
        reason = result.get("retry_reason", "")
        if page_result_has_high_repeated_pseudotext(result) or PSEUDOTEXT_RETRY_REASON in reason:
            candidates.append(page_index)

    if not candidates:
        return page_results

    image_dir = work_dir / "image_fallback_pages"
    degraded = []
    with fitz.open(pdf_path) as doc:
        for page_index in candidates:
            if page_index < 0 or page_index >= doc.page_count:
                continue

            result = page_results.get(page_index) or {}
            if result.get("blank_page"):
                continue

            # Only degrade pages that still look bad after the MinerU batch retry.
            if result.get("source_page_retry") and not page_result_has_high_repeated_pseudotext(result):
                continue

            image_path = image_dir / f"page_{page_index + 1:04d}.png"
            render_pdf_page_to_png(pdf_path, page_index, image_path)
            result["cells"] = []
            result["fallback_text"] = ""
            result["md_nohf_text"] = ""
            result["needs_retry"] = False
            result["image_fallback_path"] = str(image_path)
            result["image_fallback_page"] = True
            result["pseudotext_detected"] = True
            append_retry_reason(result, PSEUDOTEXT_IMAGE_FALLBACK_REASON)
            page_results[page_index] = result
            degraded.append(page_index + 1)

    if degraded:
        log(
            "    High-repetition pseudo-text pages degraded to source page images: "
            + ", ".join(str(page_no) for page_no in degraded)
        )
    return page_results


def degrade_unusable_nonblank_pages_to_images(pdf_path, page_results, work_dir):
    image_dir = work_dir / "unusable_ocr_image_fallback_pages"
    degraded = []
    blank = []
    with fitz.open(pdf_path) as doc:
        for page_index in range(doc.page_count):
            result = page_results.get(page_index)
            if result and (result.get("image_fallback_path") or result.get("blank_page")):
                continue

            missing_result = result is None
            if result is None:
                result = {
                    "cells": [],
                    "fallback_text": "",
                    "md_nohf_text": "",
                    "filtered": False,
                    "needs_retry": False,
                    "image_size": None,
                }

            usable_text = normalize_text(page_result_text(result))
            unresolved = bool(result.get("needs_retry"))
            if not missing_result and usable_text and not unresolved:
                continue

            if is_source_page_blank(doc, page_index):
                clear_page_result_as_blank(
                    result,
                    "source page appears blank and has no usable OCR result; keeping blank page",
                )
                page_results[page_index] = result
                blank.append(page_index + 1)
                continue

            reason = (
                MISSING_OCR_IMAGE_FALLBACK_REASON
                if missing_result
                else UNUSABLE_OCR_IMAGE_FALLBACK_REASON
            )
            image_path = image_dir / f"page_{page_index + 1:04d}.png"
            render_pdf_page_to_png(pdf_path, page_index, image_path)
            result["cells"] = []
            result["fallback_text"] = ""
            result["md_nohf_text"] = ""
            result["needs_retry"] = False
            result["image_fallback_path"] = str(image_path)
            result["image_fallback_page"] = True
            result["image_fallback_kind"] = "missing_ocr" if missing_result else "handwriting_or_low_quality_scan"
            append_retry_reason(result, reason)
            page_results[page_index] = result
            degraded.append(page_index + 1)

    if blank:
        log(
            "    Pages without usable OCR confirmed blank: "
            + ", ".join(str(page_no) for page_no in blank)
        )
    if degraded:
        log(
            "    Nonblank pages without usable OCR moved to the image variant: "
            + ", ".join(str(page_no) for page_no in degraded)
        )
    return page_results


_COMPONENT_EXPORTS = (
    "apply_forward_page_leak_metadata",
    "repair_forward_page_content_leaks",
    "collapse_repeated_text_in_split_band_result",
    "retry_bad_pages",
    "force_table_cells_to_text",
    "retry_unfitting_table_pages_as_text_batch",
    "degrade_repeated_pseudotext_pages_to_images",
    "degrade_unusable_nonblank_pages_to_images",
)
_COMPONENT_RUNTIME = ComponentRuntime(globals(), _COMPONENT_EXPORTS)


def component_exports():
    return _COMPONENT_RUNTIME.exports()


def invoke_component(name, namespace, *args, **kwargs):
    return _COMPONENT_RUNTIME.invoke(name, namespace, *args, **kwargs)

