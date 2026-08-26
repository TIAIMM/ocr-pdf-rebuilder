"""Page-level retry, forward-leak repair and safe fallback policy."""

from pathlib import Path
import re
import statistics

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


def repair_forward_page_content_leaks(
    pdf_path,
    page_results,
    work_dir,
    log_path,
    api_session=None,
):
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
                api_session=api_session,
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


def retry_bad_pages(
    pdf_path,
    page_results,
    work_dir,
    log_path,
    api_session=None,
):
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
            api_session=api_session,
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


def retry_unfitting_table_pages_as_text_batch(
    pdf_path,
    page_results,
    work_dir,
    log_path,
    api_session=None,
):
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
            api_session=api_session,
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


def source_page_large_raster_count(page):
    """Count distinct large rasters used by a source PDF page.

    A normal scanned page has one page-sized raster.  A facsimile reproduced
    inside that scanned page has a second independent large raster; preserving
    the source page image is safer than pretending a few OCR fragments can
    reproduce the manuscript, title page, or newspaper layout.
    """

    large = set()
    for image in page.get_images(full=True):
        xref = int(image[0])
        width = int(image[2])
        height = int(image[3])
        short_edge = min(width, height)
        long_edge = max(width, height)
        if (
            short_edge >= SOURCE_FACSIMILE_MIN_SHORT_EDGE_PX
            and long_edge >= SOURCE_FACSIMILE_MIN_LONG_EDGE_PX
        ):
            large.add((xref, width, height))
    return len(large)


def source_raster_has_chromatic_pixels(page, xref):
    """Return true only when a raster contains visibly non-gray pixels."""

    try:
        pixmap = fitz.Pixmap(page.parent, int(xref))
        if pixmap.colorspace is None:
            return False
        if pixmap.colorspace.n != 3 or pixmap.alpha:
            pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
        samples = pixmap.samples
        components = int(pixmap.n)
        if components < 3:
            return False
        pixel_count = max(1, pixmap.width * pixmap.height)
        sample_stride = max(1, pixel_count // 4096)
        chromatic = 0
        for pixel_index in range(0, pixel_count, sample_stride):
            offset = pixel_index * components
            if offset + 2 >= len(samples):
                break
            red, green, blue = samples[offset : offset + 3]
            if max(red, green, blue) - min(red, green, blue) >= 18:
                chromatic += 1
                if chromatic >= 3:
                    return True
    except (RuntimeError, ValueError):
        return False
    return False


def source_page_auxiliary_color_raster_count(page):
    """Count small, genuinely colored rasters embedded beside the page scan.

    A publisher mark or other colored illustration is commonly a second image
    layered over a bi-level page raster.  OCR text cannot reproduce it.  The
    area and chroma checks avoid treating a normal RGB page scan or a small
    black decoration as an auxiliary color artifact.
    """

    images = list(page.get_images(full=True))
    if len(images) < 2:
        return 0
    largest_area = max(int(image[2]) * int(image[3]) for image in images)
    count = 0
    for image in images:
        xref = int(image[0])
        width = int(image[2])
        height = int(image[3])
        area = width * height
        if min(width, height) < SOURCE_AUXILIARY_COLOR_MIN_SHORT_EDGE_PX:
            continue
        if area > largest_area * SOURCE_AUXILIARY_COLOR_MAX_AREA_RATIO:
            continue
        if str(image[5] or "") in {"", "DeviceGray"}:
            continue
        if source_raster_has_chromatic_pixels(page, xref):
            count += 1
    return count


def degrade_source_facsimile_pages_to_images(pdf_path, page_results, work_dir):
    image_dir = Path(work_dir) / "facsimile_image_fallback_pages"
    degraded = []
    with fitz.open(pdf_path) as doc:
        for page_index, page in enumerate(doc):
            result = page_results.get(page_index)
            if not result:
                continue
            if result.get("image_fallback_path") or result.get("blank_page"):
                continue
            large_raster_count = source_page_large_raster_count(page)
            auxiliary_color_count = source_page_auxiliary_color_raster_count(page)
            if (
                large_raster_count < SOURCE_FACSIMILE_MIN_LARGE_RASTERS
                and auxiliary_color_count == 0
            ):
                continue

            image_path = image_dir / f"page_{page_index + 1:04d}.png"
            render_pdf_page_to_png(pdf_path, page_index, image_path)
            result["cells"] = []
            result["fallback_text"] = ""
            result["md_nohf_text"] = ""
            result["needs_retry"] = False
            result["image_fallback_path"] = str(image_path)
            result["image_fallback_page"] = True
            result["image_fallback_kind"] = "source_facsimile"
            result["source_facsimile_detected"] = True
            result["source_facsimile_large_raster_count"] = large_raster_count
            result["source_auxiliary_color_raster_count"] = auxiliary_color_count
            append_retry_reason(result, SOURCE_FACSIMILE_IMAGE_FALLBACK_REASON)
            page_results[page_index] = result
            degraded.append(page_index + 1)

    if degraded:
        log(
            "    Embedded facsimile/color-artifact pages moved to the image variant: "
            + ", ".join(str(page_no) for page_no in degraded)
        )
    return page_results


def normalized_layout_lines(text):
    normalized = []
    for line in str(text or "").splitlines():
        value = normalize_text(line).strip()
        if value:
            normalized.append(value)
    return normalized


def normalized_layout_key(text):
    return re.sub(
        r"[^0-9A-Za-zÀ-ÖØ-öø-ÿΑ-ω]+",
        "",
        normalize_text(text),
    ).casefold()


def longest_monotonic_number_run(text):
    """Return the longest separator-linked run n, n+1, n+2, ... .

    Ordinary prose years and page references are deliberately ignored unless
    at least two adjacent values are joined only by punctuation/whitespace.
    The production threshold is much higher, so real numbered lists do not
    become image fallbacks merely for being sequential.
    """

    normalized = normalize_text(text)
    matches = list(re.finditer(r"(?<![\d.])\d{2,4}(?![\d.])", normalized))
    if not matches:
        return 0
    best = current = 1
    for previous, following in zip(matches, matches[1:]):
        separator = normalized[previous.end() : following.start()]
        if (
            int(following.group()) == int(previous.group()) + 1
            and re.fullmatch(r"[\s,;:/\-–—]+", separator or "")
        ):
            current += 1
        else:
            current = 1
        best = max(best, current)
    return best


def block_overlap_ratio(a, b):
    a_right = a["left"] + a["width"]
    a_bottom = a["top"] + a["height"]
    b_right = b["left"] + b["width"]
    b_bottom = b["top"] + b["height"]
    width = max(0.0, min(a_right, b_right) - max(a["left"], b["left"]))
    height = max(0.0, min(a_bottom, b_bottom) - max(a["top"], b["top"]))
    intersection = width * height
    smaller_area = min(
        max(1.0, a["width"] * a["height"]),
        max(1.0, b["width"] * b["height"]),
    )
    return intersection / smaller_area


def complex_layout_fallback_reasons(result, page_width, page_height):
    """Identify layouts for which plain OCR boxes cannot preserve topology.

    These rules intentionally target explicit evidence of flattened columns,
    tables, or hallucinated numeric expansion.  Normal prose, poems, and
    detached line numbers remain renderable text.
    """

    blocks = blocks_from_page_result(result, page_width, page_height)
    reasons = []
    repeated_blocks = []

    contents_keys = {
        normalized_layout_key(block.get("text"))
        for block in blocks
        if block.get("text")
    }
    if contents_keys & COMPLEX_LAYOUT_CONTENTS_KEYS:
        reasons.append({"kind": "contents_page_topology"})

    for block in blocks:
        if block.get("category") not in {"Text", "List-item", "Table"}:
            continue
        block_lines = normalized_layout_lines(block.get("text"))
        line_keys = [normalized_layout_key(line) for line in block_lines]
        line_lengths = [len(value) for value in line_keys]
        width_ratio = block["width"] / max(1.0, page_width)
        height_ratio = block["height"] / max(1.0, page_height)

        if (
            block.get("category") in {"Text", "List-item"}
            and len(block_lines) >= COMPLEX_LAYOUT_DENSE_MIN_LINES
            and statistics.median(line_lengths or [999])
            <= COMPLEX_LAYOUT_DENSE_MAX_MEDIAN_KEY_CHARS
            and height_ratio >= COMPLEX_LAYOUT_DENSE_MIN_HEIGHT_RATIO
        ):
            reasons.append(
                {
                    "kind": "dense_short_line_block",
                    "order": block.get("order"),
                    "line_count": len(block_lines),
                    "median_key_chars": statistics.median(line_lengths),
                }
            )

        numeric_line_ratio = (
            sum(bool(re.search(r"\d", line)) for line in block_lines)
            / max(1, len(block_lines))
        )
        if (
            block.get("category") in {"Text", "List-item"}
            and len(block_lines) >= COMPLEX_LAYOUT_REFERENCE_MIN_LINES
            and numeric_line_ratio
            >= COMPLEX_LAYOUT_REFERENCE_MIN_NUMERIC_LINE_RATIO
            and width_ratio >= COMPLEX_LAYOUT_REFERENCE_MIN_WIDTH_RATIO
            and height_ratio >= COMPLEX_LAYOUT_REFERENCE_MIN_HEIGHT_RATIO
        ):
            reasons.append(
                {
                    "kind": "flattened_reference_list",
                    "order": block.get("order"),
                    "line_count": len(block_lines),
                    "numeric_line_ratio": round(numeric_line_ratio, 3),
                }
            )

        counts = {}
        for value in line_keys:
            if len(value) < COMPLEX_LAYOUT_REPEATED_MIN_KEY_CHARS:
                continue
            counts[value] = counts.get(value, 0) + 1
        repeated = sorted(value for value, count in counts.items() if count >= 2)
        if repeated:
            repeated_blocks.append(
                (block, block_lines, width_ratio, height_ratio, repeated)
            )

        monotonic_run = longest_monotonic_number_run(block.get("text"))
        if monotonic_run >= COMPLEX_LAYOUT_MONOTONIC_MIN_RUN:
            reasons.append(
                {
                    "kind": "monotonic_number_hallucination",
                    "order": block.get("order"),
                    "run_length": monotonic_run,
                }
            )

    for block, block_lines, width_ratio, height_ratio, repeated in repeated_blocks:
        if width_ratio >= COMPLEX_LAYOUT_REPEATED_MIN_WIDTH_RATIO and (
            height_ratio >= 0.15 or len(block_lines) <= 12
        ):
            reasons.append(
                {
                    "kind": "flattened_repeated_columns",
                    "order": block.get("order"),
                    "line_count": len(block_lines),
                    "repeated_line_count": len(repeated),
                }
            )
        elif (
            2 <= len(block_lines) <= 8
            and width_ratio <= COMPLEX_LAYOUT_SHORT_REPEATED_MAX_WIDTH_RATIO
            and height_ratio <= COMPLEX_LAYOUT_SHORT_REPEATED_MAX_HEIGHT_RATIO
        ):
            reasons.append(
                {
                    "kind": "collapsed_parallel_signature",
                    "order": block.get("order"),
                    "line_count": len(block_lines),
                }
            )

    tables = [block for block in blocks if block.get("category") == "Table"]
    text_blocks = [block for block in blocks if block.get("category") == "Text"]
    for table in tables:
        table_lines = normalized_layout_lines(table.get("text"))
        if not table_lines:
            continue
        table_tail = re.sub(r"\d+$", "", normalized_layout_key(table_lines[-1]))
        if len(table_tail) < 8:
            continue
        for text_block in text_blocks:
            if block_overlap_ratio(table, text_block) < 0.15:
                continue
            text_lines = normalized_layout_lines(text_block.get("text"))
            if not text_lines:
                continue
            text_head = normalized_layout_key(text_lines[0])
            if text_head.startswith(table_tail) or table_tail.startswith(text_head):
                reasons.append(
                    {
                        "kind": "overlapping_table_text_duplicate",
                        "table_order": table.get("order"),
                        "text_order": text_block.get("order"),
                    }
                )

    unique = []
    seen = set()
    for reason in reasons:
        marker = tuple(sorted(reason.items()))
        if marker not in seen:
            seen.add(marker)
            unique.append(reason)
    return unique


def compact_layout_text(text):
    # Character coverage must reflect the source text layer literally.  The
    # normalizer deliberately collapses repeated OCR phrases, which is useful
    # for rendering but would under-count legitimate repeated source entries.
    return re.sub(r"\s+", "", str(text or ""))


def source_gap_content_evidence(page, blocks, page_width, page_height):
    """Find source raster content inside a large OCR-empty internal gap."""

    intervals = []
    for block in blocks:
        if block.get("category") in {"Page-header", "Page-footer"}:
            continue
        if float(block.get("width", 0.0)) < (
            page_width * COMPLEX_LAYOUT_GAP_MIN_BLOCK_WIDTH_RATIO
        ):
            continue
        top = max(0.0, float(block.get("top", 0.0)))
        bottom = min(page_height, top + max(0.0, float(block.get("height", 0.0))))
        if bottom > top:
            intervals.append((top, bottom))
    if len(intervals) < 2:
        return []

    merged = []
    for top, bottom in sorted(intervals):
        if merged and top <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], bottom))
        else:
            merged.append((top, bottom))

    evidence = []
    minimum_gap = page_height * COMPLEX_LAYOUT_GAP_MIN_HEIGHT_RATIO
    for previous, following in zip(merged, merged[1:]):
        gap_top = previous[1]
        gap_bottom = following[0]
        if gap_bottom - gap_top < minimum_gap:
            continue
        clip = fitz.Rect(
            page_width * 0.08,
            gap_top + 2.0,
            page_width * 0.92,
            gap_bottom - 2.0,
        )
        if clip.height <= 2.0 or clip.width <= 2.0:
            continue
        pixmap = page.get_pixmap(
            matrix=fitz.Matrix(1.0, 1.0),
            colorspace=fitz.csGRAY,
            clip=clip,
            alpha=False,
        )
        width = int(pixmap.width)
        height = int(pixmap.height)
        if width <= 0 or height <= 0:
            continue
        samples = pixmap.samples
        row_counts = [0] * height
        column_counts = [0] * width
        dark_count = 0
        for row in range(height):
            row_offset = row * width
            for column in range(width):
                if samples[row_offset + column] < COMPLEX_LAYOUT_GAP_DARK_PIXEL_THRESHOLD:
                    row_counts[row] += 1
                    column_counts[column] += 1
                    dark_count += 1
        dark_ratio = dark_count / max(1, width * height)
        active_row_ratio = sum(value >= 3 for value in row_counts) / height
        active_column_ratio = sum(value >= 3 for value in column_counts) / width
        if (
            dark_ratio >= COMPLEX_LAYOUT_GAP_MIN_DARK_RATIO
            and active_row_ratio >= COMPLEX_LAYOUT_GAP_MIN_ACTIVE_ROW_RATIO
            and active_column_ratio >= COMPLEX_LAYOUT_GAP_MIN_ACTIVE_COLUMN_RATIO
        ):
            evidence.append(
                {
                    "kind": "unrepresented_source_graphic",
                    "gap_top": round(gap_top, 1),
                    "gap_bottom": round(gap_bottom, 1),
                    "dark_ratio": round(dark_ratio, 3),
                    "active_row_ratio": round(active_row_ratio, 3),
                    "active_column_ratio": round(active_column_ratio, 3),
                }
            )
    return evidence


def degrade_complex_layout_pages_to_images(pdf_path, page_results, work_dir):
    image_dir = Path(work_dir) / "complex_layout_image_fallback_pages"
    degraded = []
    with fitz.open(pdf_path) as doc:
        for page_index, page in enumerate(doc):
            result = page_results.get(page_index)
            if not result:
                continue
            if result.get("image_fallback_path") or result.get("blank_page"):
                continue
            page_width = float(page.rect.width)
            page_height = float(page.rect.height)
            blocks = blocks_from_page_result(result, page_width, page_height)
            reasons = complex_layout_fallback_reasons(
                result,
                page_width,
                page_height,
            )
            if not reasons:
                source_chars = len(compact_layout_text(page.get_text("text")))
                result_chars = len(
                    compact_layout_text("\n".join(block.get("text", "") for block in blocks))
                )
                source_ratio = result_chars / max(1, source_chars)
                if (
                    source_chars >= COMPLEX_LAYOUT_SOURCE_TEXT_MIN_CHARS
                    and source_ratio < COMPLEX_LAYOUT_MAX_OUTPUT_SOURCE_TEXT_RATIO
                ):
                    reasons.append(
                        {
                            "kind": "severe_source_text_loss",
                            "source_chars": source_chars,
                            "result_chars": result_chars,
                            "result_source_ratio": round(source_ratio, 3),
                        }
                    )
            if not reasons:
                reasons.extend(
                    source_gap_content_evidence(page, blocks, page_width, page_height)
                )
            if not reasons:
                continue

            image_path = image_dir / f"page_{page_index + 1:04d}.png"
            render_pdf_page_to_png(pdf_path, page_index, image_path)
            result["cells"] = []
            result["fallback_text"] = ""
            result["md_nohf_text"] = ""
            result["needs_retry"] = False
            result["image_fallback_path"] = str(image_path)
            result["image_fallback_page"] = True
            result["image_fallback_kind"] = "complex_layout"
            result["complex_layout_detected"] = True
            result["complex_layout_fallback_reasons"] = reasons
            append_retry_reason(result, COMPLEX_LAYOUT_IMAGE_FALLBACK_REASON)
            page_results[page_index] = result
            degraded.append(page_index + 1)

    if degraded:
        log(
            "    Complex OCR layouts moved to the image variant: "
            + ", ".join(str(page_no) for page_no in degraded)
        )
    return page_results


def fallback_unfitting_layout_page(
    pdf_path,
    page_index,
    result,
    blocks,
    page_width,
    page_height,
    work_dir,
):
    """Replace a page only when a prepared OCR block cannot render safely."""

    failures = reportlab_page_fit_failures(page_height, blocks)
    if not failures:
        return blocks, []

    image_path = (
        Path(work_dir)
        / "layout_image_fallback_pages"
        / f"page_{page_index + 1:04d}.png"
    )
    render_pdf_page_to_png(pdf_path, page_index, image_path)
    result["image_fallback_path"] = str(image_path)
    result["image_fallback_page"] = True
    result["image_fallback_kind"] = "layout_fit"
    result["layout_fit_failures"] = failures
    append_retry_reason(result, UNFITTING_LAYOUT_IMAGE_FALLBACK_REASON)
    return [image_fallback_block(image_path, page_width, page_height)], failures


def fallback_damaged_source_orientation_page(
    pdf_path,
    page_index,
    result,
    blocks,
    page_width,
    page_height,
    work_dir,
    rotation_degrees,
):
    """Use a rotated facsimile when inversion caused alternate OCR layers."""

    duplicate_count = int(result.get("anchored_duplicate_ocr_blocks_removed", 0))
    if duplicate_count <= 0:
        duplicate_count = sum(
            int(block.get("anchored_duplicate_ocr_blocks_removed", 0))
            for block in blocks
        )
    if int(rotation_degrees or 0) % 360 != 180 or duplicate_count <= 0:
        return blocks, False

    image_path = (
        Path(work_dir)
        / "source_orientation_image_fallback_pages"
        / f"page_{page_index + 1:04d}.png"
    )
    render_pdf_page_to_png(
        pdf_path,
        page_index,
        image_path,
        rotation_degrees=rotation_degrees,
    )
    result["cells"] = []
    result["fallback_text"] = ""
    result["md_nohf_text"] = ""
    result["needs_retry"] = False
    result["image_fallback_path"] = str(image_path)
    result["image_fallback_page"] = True
    result["image_fallback_kind"] = "source_orientation_quality"
    result["source_orientation_quality_fallback"] = True
    result["source_orientation_duplicate_count"] = duplicate_count
    append_retry_reason(result, SOURCE_ORIENTATION_IMAGE_FALLBACK_REASON)
    return [image_fallback_block(image_path, page_width, page_height)], True


_COMPONENT_EXPORTS = (
    "apply_forward_page_leak_metadata",
    "repair_forward_page_content_leaks",
    "collapse_repeated_text_in_split_band_result",
    "retry_bad_pages",
    "force_table_cells_to_text",
    "retry_unfitting_table_pages_as_text_batch",
    "degrade_repeated_pseudotext_pages_to_images",
    "degrade_unusable_nonblank_pages_to_images",
    "source_page_large_raster_count",
    "source_raster_has_chromatic_pixels",
    "source_page_auxiliary_color_raster_count",
    "degrade_source_facsimile_pages_to_images",
    "normalized_layout_lines",
    "normalized_layout_key",
    "longest_monotonic_number_run",
    "complex_layout_fallback_reasons",
    "compact_layout_text",
    "source_gap_content_evidence",
    "degrade_complex_layout_pages_to_images",
    "fallback_unfitting_layout_page",
    "fallback_damaged_source_orientation_page",
)
_COMPONENT_RUNTIME = ComponentRuntime(globals(), _COMPONENT_EXPORTS)


def component_exports():
    return _COMPONENT_RUNTIME.exports()


def invoke_component(name, namespace, *args, **kwargs):
    return _COMPONENT_RUNTIME.invoke(name, namespace, *args, **kwargs)
