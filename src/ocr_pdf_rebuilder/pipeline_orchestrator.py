"""Single-document production orchestration and workspace lifecycle."""

import shutil
import time

import fitz

from .component_runtime import ComponentRuntime
from .pipeline_config import *

def same_job_intermediate_files(name):
    mineru_log_prefix = f"{name}_mineru"
    for path in LOG_DIR.iterdir():
        if path.name == f"{name}_qc.json":
            yield path
            continue
        if path.name.startswith(mineru_log_prefix) and path.suffix == ".log":
            yield path


def cleanup_success_artifacts(name, work_dir, mineru_dir, log_path):
    cleanup_paths = [
        work_dir,
        mineru_dir,
        QC_DIR / name,
        *same_job_intermediate_files(name),
    ]
    if log_path not in cleanup_paths:
        cleanup_paths.append(log_path)

    removed = 0
    for cleanup_path in cleanup_paths:
        try:
            if cleanup_path.is_dir():
                shutil.rmtree(cleanup_path)
                removed += 1
            elif cleanup_path.exists():
                cleanup_path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    log(f"    Cleaned intermediate files for this PDF: {removed} path(s)")


def prepare_resumable_job_workspace(pdf_path, work_dir, mineru_dir):
    state_path = work_dir / "job_state.json"
    expected = {
        "schema": MINERU_CHECKPOINT_SCHEMA,
        "source": source_file_signature(pdf_path),
        "parser_config_hash": mineru_parser_config_hash(),
        "implementation_identity_hash": current_package_source_identity_hash(),
        "runtime_identity_hash": mineru_runtime_identity_hash(),
    }
    existing = read_checkpoint(state_path)
    can_resume = (
        existing is not None
        and all(existing.get(key) == value for key, value in expected.items())
        and work_dir.exists()
        and mineru_dir.exists()
    )

    if can_resume:
        log(f"    Resume workspace found: {work_dir}")
        return True

    if work_dir.exists():
        shutil.rmtree(work_dir)
    if mineru_dir.exists():
        shutil.rmtree(mineru_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    mineru_dir.mkdir(parents=True, exist_ok=True)
    write_checkpoint(
        state_path,
        {
            **expected,
            "status": "active",
            "implementation_identity": current_package_source_identity(),
            "runtime_identity": mineru_runtime_identity(),
            "updated_at": time.time(),
        },
    )
    return False


def process_pdf(pdf_path, index, total):
    name = pdf_path.stem
    output_pdf = OUTPUT_DIR / f"{name}_mineru.pdf"
    output_image_pdf = OUTPUT_DIR / f"{name}_mineru_with_images.pdf"
    output_md = OUTPUT_DIR / f"{name}_mineru.md"
    output_version = OUTPUT_DIR / f"{name}_mineru.version"
    output_searchable_pdf = OUTPUT_DIR / f"{name}_mineru_searchable.pdf"
    if (
        SKIP_EXISTING
        and completed_output_state_matches(
            output_version,
            pdf_path,
            output_pdf,
            output_md,
            output_image_pdf,
            output_searchable_pdf,
        )
    ):
        if upgrade_completed_output_runtime_identity(output_version):
            log(f"    Upgraded legacy completion runtime identity: {output_version}")
        state = read_checkpoint(output_version) or {}
        variant_note = " plus image variant" if state.get("has_image_variant") else ""
        log(
            f"[{index}/{total}] Skip existing current-version text output{variant_note} "
            f"before MinerU/VLM startup: {output_pdf}"
        )
        return {
            "status": "skipped",
            "output_text_pdf": str(output_pdf),
            "output_image_pdf": str(output_image_pdf) if state.get("has_image_variant") else None,
            "output_searchable_pdf": str(output_searchable_pdf) if state.get("searchable_pdf") else None,
        }
    elif SKIP_EXISTING and output_pdf.exists():
        log(f"[{index}/{total}] Existing output is missing current version marker; regenerating: {output_pdf}")

    work_dir = TMP_DIR / name
    mineru_dir = MINERU_OUTPUT_DIR / name
    log_path = LOG_DIR / f"{name}_mineru.log"

    start = time.monotonic()
    log(f"[{index}/{total}] Start: {pdf_path.name}")
    log(f"    Input:  {pdf_path}")
    log(f"    Output: {output_pdf}")
    prepare_resumable_job_workspace(pdf_path, work_dir, mineru_dir)

    src = fitz.open(pdf_path)
    page_count = src.page_count
    if page_count == 0:
        src.close()
        raise RuntimeError(f"Empty PDF: {pdf_path}")
    page_width = float(src[0].rect.width)
    page_height = float(src[0].rect.height)
    src.close()

    log(f"    Pages:  {page_count}")
    log("    [1/5] Running MinerU parser")
    with create_mineru_api_session(work_dir, log_path) as api_session:
        parser_runs = build_mineru_parser_runs(
            pdf_path,
            page_count,
            work_dir,
            mineru_dir,
            log_path,
            api_session=api_session,
        )

        log("    [2/5] Loading MinerU JSON")
        page_results = collect_parser_run_results(pdf_path, parser_runs)
        page_results = retry_bad_pages(
            pdf_path,
            page_results,
            work_dir,
            log_path,
            api_session=api_session,
        )
        page_results = retry_unfitting_table_pages_as_text_batch(
            pdf_path,
            page_results,
            work_dir,
            log_path,
            api_session=api_session,
        )
        page_results = repair_forward_page_content_leaks(
            pdf_path,
            page_results,
            work_dir,
            log_path,
            api_session=api_session,
        )
    suppress_blank_page_outputs(pdf_path, page_results)
    page_results = degrade_repeated_pseudotext_pages_to_images(pdf_path, page_results, work_dir)
    page_results = degrade_unusable_nonblank_pages_to_images(pdf_path, page_results, work_dir)

    log(f"    [3/5] Building layout pages: 0/{page_count}")
    src = fitz.open(pdf_path)

    page_specs = []
    markdown_pages = []
    total_blocks = 0
    category_counts = {}
    md_nohf_pages = 0
    image_fallback_pages = []
    for page_index in range(page_count):
        page = src[page_index]
        page_width = float(page.rect.width)
        page_height = float(page.rect.height)

        result = page_results.get(
            page_index,
            {"cells": [], "fallback_text": "", "filtered": False, "image_size": None},
        )
        blocks = []
        if result.get("image_fallback_path"):
            blocks.append(image_fallback_block(result["image_fallback_path"], page_width, page_height))
            log(f"        Page {page_index + 1}: used source page image fallback")
        else:
            blocks = blocks_from_page_result(result, page_width, page_height)

            retry_reason = result.get("retry_reason", "")
            is_split_retry_page = (
                "split-band" in retry_reason
                and not result.get("source_page_retry")
            )
            if is_split_retry_page and blocks:
                before_count = len(blocks)
                blocks = merge_overlapping_retry_blocks(blocks)
                after_count = len(blocks)
                if after_count < before_count:
                    log(
                        f"        Page {page_index + 1}: merged overlapping split-band blocks "
                        f"{before_count} -> {after_count}"
                    )

                clamped_before = sum(1 for block in blocks if block.get("split_margin_clamped"))
                blocks = clamp_split_retry_blocks_to_page_margins(blocks, page_width)
                clamped_after = sum(1 for block in blocks if block.get("split_margin_clamped"))
                if clamped_after > clamped_before:
                    log(
                        f"        Page {page_index + 1}: clamped split-band block margins "
                        f"for {clamped_after - clamped_before} block(s)"
                    )

            if not blocks and result.get("fallback_text"):
                block = fallback_text_block(result["fallback_text"], 0, page_width, page_height)
                if block:
                    blocks.append(block)
                    log(f"        Page {page_index + 1}: used Markdown fallback because layout JSON was invalid")
            elif result.get("filtered"):
                log(f"        Page {page_index + 1}: MinerU reported filtered/cleaned JSON")

            blocks.sort(key=lambda b: (is_header_footer(b), b["top"], b["left"], b["order"]))
            blocks = prepare_blocks(blocks, page_width, page_height)

            blocks, fit_failures = fallback_unfitting_layout_page(
                pdf_path,
                page_index,
                result,
                blocks,
                page_width,
                page_height,
                work_dir,
            )
            if fit_failures:
                first_failure = fit_failures[0]
                log(
                    f"        Page {page_index + 1}: {len(fit_failures)} OCR block(s) "
                    "could not fit safely; used source page image fallback "
                    f"because {first_failure.get('category')} block order={first_failure.get('order')} "
                    "was still outside its safe bbox limits"
                )
        for block in blocks:
            block["page_index"] = page_index
            block["source_pdf_path"] = str(pdf_path)
            block["formula_cache_dir"] = str(work_dir / "formula_render_cache")
            block["formula_crop_dir"] = str(work_dir / "formula_image_crops")
            category_counts[block["category"]] = category_counts.get(block["category"], 0) + 1
        if any(block.get("category") == "ImageFallback" for block in blocks):
            image_fallback_pages.append(page_index)
        total_blocks += len(blocks)
        log(f"        Layout page {page_index + 1}/{page_count}: blocks={len(blocks)}")
        page_specs.append((page_width, page_height, blocks))
        if normalize_markdown_text(result.get("md_nohf_text", "")):
            md_nohf_pages += 1
        markdown_pages.append(markdown_page_from_result(result, blocks, page_index))

    src.close()

    log_category_audit(category_counts, md_nohf_pages, page_count)
    write_book_markdown(output_md, markdown_pages)

    log("    [4/5] Rendering text-only PDF with ReportLab")
    render_blocks_to_pdf_reportlab(
        page_specs,
        output_pdf,
        formula_work_dir=work_dir / "formula_render_cache",
        include_full_page_images=False,
        allow_formula_image_fallback=False,
        progress_callback=lambda current, total: log(
            f"        Render text PDF page {current}/{total}"
        ),
    )
    output_validation_scan = scan_pdf_validation(
        output_pdf,
        progress_callback=lambda current, total: log(
            f"        Validate text PDF page {current}/{total}"
        ),
    )
    validate_pdf_page_count(output_pdf, page_count, output_validation_scan)
    validate_pdf_has_no_raster_images(output_pdf, output_validation_scan)
    validate_pdf_pages_are_blank(output_pdf, image_fallback_pages, output_validation_scan)

    output_image_validation_scan = None
    if image_fallback_pages:
        log(
            f"    Rendering image-variant PDF for {len(image_fallback_pages)} fallback page(s): "
            f"{output_image_pdf}"
        )
        # Rendering records formula fallback modes on blocks for QC. Keep the
        # pure-text page specifications authoritative and isolate mutations
        # made while producing the companion image variant.
        image_page_specs = [
            (page_width, page_height, [dict(block) for block in blocks])
            for page_width, page_height, blocks in page_specs
        ]
        render_blocks_to_pdf_reportlab(
            image_page_specs,
            output_image_pdf,
            formula_work_dir=work_dir / "formula_render_cache",
            include_full_page_images=True,
            allow_formula_image_fallback=True,
            progress_callback=lambda current, total: log(
                f"        Render image PDF page {current}/{total}"
            ),
        )
        output_image_validation_scan = scan_pdf_validation(
            output_image_pdf,
            progress_callback=lambda current, total: log(
                f"        Validate image PDF page {current}/{total}"
            ),
        )
        validate_pdf_page_count(output_image_pdf, page_count, output_image_validation_scan)
        validate_pdf_has_images_on_pages(
            output_image_pdf,
            image_fallback_pages,
            output_image_validation_scan,
        )
        validate_pdf_has_no_radicals(output_image_pdf, output_image_validation_scan)
        validate_pdf_has_no_control_chars(output_image_pdf, output_image_validation_scan)
        validate_pdf_has_no_latex_residue(output_image_pdf, output_image_validation_scan)
    elif output_image_pdf.exists():
        output_image_pdf.unlink()
        log(f"    Removed stale image variant because this run has no fallback pages: {output_image_pdf}")

    log(f"    Rendering searchable OCR-layer PDF: {output_searchable_pdf}")
    searchable_stats = build_searchable_pdf(
        pdf_path,
        page_results,
        output_searchable_pdf,
        progress_callback=lambda current, total: log(
            f"        Searchable PDF page {current}/{total}"
        ),
    )
    searchable_validation_scan = scan_pdf_validation(
        output_searchable_pdf,
        progress_callback=lambda current, total: log(
            f"        Validate searchable PDF page {current}/{total}"
        ),
    )
    validate_pdf_page_count(output_searchable_pdf, page_count, searchable_validation_scan)
    validate_searchable_pdf_text_presence(
        output_searchable_pdf,
        searchable_stats["text_page_indexes"],
        searchable_validation_scan,
    )
    validate_searchable_pdf_visual_identity(pdf_path, output_searchable_pdf)

    write_mineru_qc_report(
        pdf_path,
        mineru_dir,
        page_results,
        page_specs,
        output_pdf,
        output_image_pdf=output_image_pdf if image_fallback_pages else None,
        output_searchable_pdf=output_searchable_pdf,
        image_fallback_pages=image_fallback_pages,
        parser_runs=parser_runs,
        output_validation_scan=output_validation_scan,
    )
    validate_pdf_has_no_radicals(output_pdf, output_validation_scan)
    validate_pdf_has_no_control_chars(output_pdf, output_validation_scan)
    validate_pdf_has_no_latex_residue(output_pdf, output_validation_scan)
    warn_pdf_suspected_markdown(output_pdf, output_validation_scan)

    log("    [5/5] Done")
    size_mb = output_pdf.stat().st_size / 1024 / 1024
    log(f"    Saved: {output_pdf} ({size_mb:.2f} MB), blocks={total_blocks}, time={format_seconds(time.monotonic() - start)}")
    if image_fallback_pages:
        image_size_mb = output_image_pdf.stat().st_size / 1024 / 1024
        log(
            f"    Saved image variant: {output_image_pdf} ({image_size_mb:.2f} MB), "
            f"fallback_pages={len(image_fallback_pages)}"
        )
    searchable_size_mb = output_searchable_pdf.stat().st_size / 1024 / 1024
    log(
        f"    Saved searchable variant: {output_searchable_pdf} ({searchable_size_mb:.2f} MB), "
        f"text_pages={len(searchable_stats['text_page_indexes'])}, spans={searchable_stats['spans_placed']}"
    )
    log(f"    Saved Markdown: {output_md}")
    write_completed_output_state(
        output_version,
        pdf_path,
        output_pdf,
        output_md,
        output_image_pdf,
        output_searchable_pdf,
        image_fallback_pages,
    )

    if DELETE_INTERMEDIATE_ON_SUCCESS:
        cleanup_success_artifacts(name, work_dir, mineru_dir, log_path)
    elif CLEAN_TMP_ON_SUCCESS and work_dir.exists():
        shutil.rmtree(work_dir)

    return {
        "status": "completed",
        "output_text_pdf": str(output_pdf),
        "output_image_pdf": str(output_image_pdf) if image_fallback_pages else None,
        "output_searchable_pdf": str(output_searchable_pdf),
        "image_fallback_pages": [page_index + 1 for page_index in image_fallback_pages],
    }




_COMPONENT_EXPORTS = (
    "same_job_intermediate_files",
    "cleanup_success_artifacts",
    "prepare_resumable_job_workspace",
    "process_pdf",
)
_COMPONENT_RUNTIME = ComponentRuntime(globals(), _COMPONENT_EXPORTS)


def component_exports():
    return _COMPONENT_RUNTIME.exports()


def invoke_component(name, namespace, *args, **kwargs):
    return _COMPONENT_RUNTIME.invoke(name, namespace, *args, **kwargs)
