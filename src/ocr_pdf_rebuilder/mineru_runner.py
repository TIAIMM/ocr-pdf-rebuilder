"""MinerU command construction, retries, chunking and resumable parser runs."""

from pathlib import Path
import math
import re
import shutil
import subprocess
import sys
import time

import fitz

from .component_runtime import ComponentRuntime
from .pipeline_config import *

def parser_log_has_json_errors(log_path):
    if not log_path.exists():
        return False
    text = log_path.read_text(encoding="utf-8", errors="replace")
    return any(pattern in text for pattern in PARSER_RETRY_PATTERNS)


def retry_log_path(log_path, attempt):
    if attempt <= 1:
        return log_path
    return log_path.with_name(f"{log_path.stem}_retry{attempt - 1}{log_path.suffix}")


class MinerUParserAttemptsExhausted(RuntimeError):
    def __init__(self, message, transient_only=False, failures=None):
        super().__init__(message)
        self.transient_only = bool(transient_only)
        self.failures = list(failures or [])


def mineru_failure_is_transient(error, log_path=None):
    parts = [str(error)]
    if log_path and Path(log_path).exists():
        try:
            parts.append(Path(log_path).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    text = "\n".join(parts).lower()
    return any(pattern in text for pattern in TRANSIENT_MINERU_FAILURE_PATTERNS)


def optional_cli_args(table_enabled=None):
    args = []
    option_pairs = [
        ("--api-url", MINERU_API_URL),
        ("-b", MINERU_BACKEND),
        ("-m", MINERU_METHOD),
        ("-l", MINERU_LANG),
        ("--effort", MINERU_EFFORT),
        ("--client-side-output-generation", MINERU_CLIENT_SIDE_OUTPUT_GENERATION),
    ]
    for option, value in option_pairs:
        if value is None or value == "":
            continue
        args.extend([option, str(value)])

    bool_pairs = [
        ("-f", MINERU_FORMULA),
        ("-t", MINERU_TABLE if table_enabled is None else table_enabled),
        ("--image-analysis", MINERU_IMAGE_ANALYSIS),
    ]
    for option, value in bool_pairs:
        if value is None:
            continue
        args.extend([option, bool_cli_value(bool(value))])

    return args + list(MINERU_EXTRA_ARGS)


def run_mineru_parser(
    pdf_path,
    output_root,
    log_path,
    result_dir=None,
    table_enabled=None,
):
    if result_dir is None:
        result_dir = output_root / pdf_path.stem

    cmd = [
        MINERU_COMMAND,
        "-p",
        str(pdf_path),
        "-o",
        str(output_root),
        *optional_cli_args(table_enabled=table_enabled),
    ]

    log(f"    MinerU command: {' '.join(cmd)}")
    log(f"    MinerU log: {log_path}")
    log("    MinerU live progress:")

    start = time.monotonic()
    returncode = run_live_process(cmd, RUNTIME_ROOT, log_path, stream_to_console=True)
    with LOG_LOCK:
        sys.stdout.write("\n")
        sys.stdout.flush()
    if returncode != 0:
        raise RuntimeError(f"MinerU parser failed for {pdf_path}. See log: {log_path}")

    if not output_root.exists() or not any(output_root.rglob("*")):
        raise RuntimeError(f"MinerU finished but produced no output in: {output_root}")

    log(f"    MinerU finished in {format_seconds(time.monotonic() - start)}")


def run_mineru_parser_with_retries(
    pdf_path,
    output_root,
    log_path,
    result_dir=None,
    table_enabled=None,
    max_attempts=None,
):
    attempts = PARSER_MAX_ATTEMPTS if max_attempts is None else int(max_attempts)
    attempts = max(1, attempts)
    failures = []

    for attempt in range(1, attempts + 1):
        attempt_log = retry_log_path(log_path, attempt)
        if attempt > 1:
            delay = max(0.0, float(PARSER_RETRY_BACKOFF_SECONDS)) * (attempt - 1)
            log(
                f"    Restarting the same MinerU task, attempt {attempt}/{attempts} "
                f"after {delay:.1f}s backoff"
            )
            if delay:
                time.sleep(delay)
            if output_root.exists():
                shutil.rmtree(output_root)
            output_root.mkdir(parents=True, exist_ok=True)

        try:
            run_mineru_parser(
                pdf_path,
                output_root,
                attempt_log,
                result_dir=result_dir,
                table_enabled=table_enabled,
            )
            if parser_log_has_json_errors(attempt_log):
                raise RuntimeError(
                    f"MinerU log reported malformed/incomplete JSON output. See log: {attempt_log}"
                )
            if not mineru_output_has_results(output_root):
                raise RuntimeError(
                    f"MinerU finished without a usable middle/content JSON result: {output_root}"
                )
            result_manifest = build_mineru_result_manifest(pdf_path, output_root)
            if attempt > 1:
                log(f"    Same-task MinerU retry succeeded on attempt {attempt}/{attempts}")
            return result_manifest
        except RuntimeError as exc:
            transient = mineru_failure_is_transient(exc, attempt_log)
            failures.append(
                {
                    "attempt": attempt,
                    "error": str(exc),
                    "log_path": str(attempt_log),
                    "transient": transient,
                }
            )
            kind = "transient" if transient else "content/unknown"
            log(
                f"    MinerU same-task attempt {attempt}/{attempts} failed "
                f"({kind}): {exc}"
            )
            if attempt >= attempts:
                break

    transient_only = bool(failures) and all(item["transient"] for item in failures)
    details = " | ".join(
        f"attempt {item['attempt']}: {item['error']}" for item in failures
    )
    raise MinerUParserAttemptsExhausted(
        f"MinerU failed after {attempts} attempt(s) for the same task: {details}",
        transient_only=transient_only,
        failures=failures,
    )


def write_pdf_page_chunk(pdf_path, chunk_path, start_page, end_page):
    chunk_path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open(pdf_path) as src:
        out = fitz.open()
        out.insert_pdf(src, from_page=start_page, to_page=end_page)
        out.save(chunk_path, garbage=4, deflate=True)
        out.close()


def write_pdf_selected_pages(pdf_path, output_path, page_indices):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open(pdf_path) as src:
        out = fitz.open()
        for page_index in page_indices:
            if page_index < 0 or page_index >= src.page_count:
                raise IndexError(f"Selected page index {page_index} is outside {pdf_path}")
            out.insert_pdf(src, from_page=page_index, to_page=page_index)
        out.save(output_path, garbage=4, deflate=True)
        out.close()


def write_pdf_isolated_selected_pages(pdf_path, output_path, page_indices):
    """Write selected pages with one blank separator after every source page.

    MinerU occasionally assigns several following source pages to the current
    page.  A blank page on both sides of every candidate (apart from the file
    boundaries) prevents one candidate page from being adjacent to another
    during the repair pass.  Candidate results therefore occupy local page
    indexes 0, 2, 4, ... in the generated PDF.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open(pdf_path) as src:
        out = fitz.open()
        for page_index in page_indices:
            if page_index < 0 or page_index >= src.page_count:
                raise IndexError(f"Selected page index {page_index} is outside {pdf_path}")
            page = src[page_index]
            out.insert_pdf(src, from_page=page_index, to_page=page_index)
            out.new_page(width=float(page.rect.width), height=float(page.rect.height))
        out.save(output_path, garbage=4, deflate=True)
        out.close()


def selected_pages_checkpoint_identity(pdf_path, page_indices, table_enabled=None):
    return {
        "schema": MINERU_CHECKPOINT_SCHEMA,
        "source": source_file_signature(pdf_path),
        "selected_pages": [int(page_index) for page_index in page_indices],
        "parser_config_hash": mineru_parser_config_hash(table_enabled=table_enabled),
        "runtime_identity_hash": mineru_runtime_identity_hash(),
    }


def isolated_pages_checkpoint_identity(pdf_path, page_indices):
    return {
        "schema": MINERU_CHECKPOINT_SCHEMA,
        "source": source_file_signature(pdf_path),
        "selected_pages": [int(page_index) for page_index in page_indices],
        "isolation": "blank-page-after-each-selected-page-v1",
        "parser_config_hash": mineru_parser_config_hash(),
        "runtime_identity_hash": mineru_runtime_identity_hash(),
    }


def run_or_reuse_mineru_selected_pages(
    pdf_path,
    page_indices,
    selected_pdf,
    output_root,
    log_path,
    checkpoint_path,
    table_enabled=None,
):
    page_indices = [int(page_index) for page_index in page_indices]
    identity = selected_pages_checkpoint_identity(
        pdf_path,
        page_indices,
        table_enabled=table_enabled,
    )
    checkpoint = read_checkpoint(checkpoint_path)
    if checkpoint_matches(checkpoint, identity, status="complete"):
        if not selected_pdf.exists():
            write_pdf_selected_pages(pdf_path, selected_pdf, page_indices)
        if mineru_result_manifest_matches(checkpoint, selected_pdf, output_root):
            log(
                f"    Resume: reusing one integrity-verified MinerU review batch containing "
                f"{len(page_indices)} page(s)"
            )
            return collect_page_results(selected_pdf, output_root)

    if selected_pdf.exists():
        selected_pdf.unlink()
    write_pdf_selected_pages(pdf_path, selected_pdf, page_indices)
    if output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    log(
        "    Running one MinerU review batch for original pages: "
        + ", ".join(str(page_index + 1) for page_index in page_indices)
    )
    result_manifest = run_mineru_parser_with_retries(
        selected_pdf,
        output_root,
        log_path,
        table_enabled=table_enabled,
    )
    write_checkpoint(
        checkpoint_path,
        {
            **identity,
            "status": "complete",
            "updated_at": time.time(),
            "output_dir": str(output_root),
            "result_manifest": result_manifest,
        },
    )
    return collect_page_results(selected_pdf, output_root)


def run_or_reuse_mineru_isolated_pages(
    pdf_path,
    page_indices,
    isolated_pdf,
    output_root,
    log_path,
    checkpoint_path,
):
    page_indices = [int(page_index) for page_index in page_indices]
    identity = isolated_pages_checkpoint_identity(pdf_path, page_indices)
    checkpoint = read_checkpoint(checkpoint_path)
    if checkpoint_matches(checkpoint, identity, status="complete"):
        if not isolated_pdf.exists():
            write_pdf_isolated_selected_pages(pdf_path, isolated_pdf, page_indices)
        if mineru_result_manifest_matches(checkpoint, isolated_pdf, output_root):
            log(
                "    Resume: reusing one integrity-verified isolated MinerU repair batch "
                f"containing {len(page_indices)} source page(s)"
            )
            return collect_page_results(isolated_pdf, output_root)

    if isolated_pdf.exists():
        isolated_pdf.unlink()
    write_pdf_isolated_selected_pages(pdf_path, isolated_pdf, page_indices)
    if output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    log(
        "    Running isolated MinerU repair batch for original pages: "
        + ", ".join(str(page_index + 1) for page_index in page_indices)
    )
    result_manifest = run_mineru_parser_with_retries(
        isolated_pdf,
        output_root,
        log_path,
    )
    write_checkpoint(
        checkpoint_path,
        {
            **identity,
            "status": "complete",
            "updated_at": time.time(),
            "output_dir": str(output_root),
            "result_manifest": result_manifest,
        },
    )
    return collect_page_results(isolated_pdf, output_root)


def build_mineru_parser_runs(pdf_path, page_count, work_dir, mineru_dir, log_path):
    chunk_pdf_dir = work_dir / "mineru_pdf_chunks"
    checkpoint_dir = work_dir / "mineru_checkpoints"
    chunk_output_root = mineru_dir / "chunks"
    chunk_pdf_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    chunk_output_root.mkdir(parents=True, exist_ok=True)

    def run_chunk(start_page, end_page, chunk_id, use_full_paths=False):
        label = f"chunk_{chunk_id}_pages_{start_page + 1:04d}_{end_page + 1:04d}"
        checkpoint_path = checkpoint_dir / f"{label}.json"
        identity = checkpoint_identity(pdf_path, start_page, end_page)
        if use_full_paths:
            chunk_pdf = pdf_path
            chunk_output_dir = mineru_dir
            chunk_log = log_path
        else:
            chunk_pdf = chunk_pdf_dir / f"{pdf_path.stem}_{label}.pdf"
            chunk_output_dir = chunk_output_root / label
            chunk_log = log_path.with_name(f"{log_path.stem}_{label}{log_path.suffix}")

        page_total = end_page - start_page + 1
        log(
            f"    MinerU task {label}: original pages {start_page + 1}-{end_page + 1} "
            f"({page_total} page(s))"
        )

        checkpoint = read_checkpoint(checkpoint_path)
        if checkpoint_matches(checkpoint, identity, status="complete"):
            if not use_full_paths and not chunk_pdf.exists():
                write_pdf_page_chunk(pdf_path, chunk_pdf, start_page, end_page)
            if mineru_result_manifest_matches(checkpoint, chunk_pdf, chunk_output_dir):
                log(f"    Resume: reusing integrity-verified MinerU task {label}")
                return [
                    {
                        "pdf_path": chunk_pdf,
                        "output_dir": chunk_output_dir,
                        "log_path": chunk_log,
                        "page_offset": start_page,
                        "start_page": start_page,
                        "end_page": end_page,
                        "label": label,
                    }
                ]

        if checkpoint_matches(checkpoint, identity, status="split") and page_total > MINERU_MIN_PAGES_PER_TASK:
            midpoint = start_page + (page_total // 2) - 1
            log(
                f"    Resume: task {label} was already split; continuing directly with "
                f"pages {start_page + 1}-{midpoint + 1} and {midpoint + 2}-{end_page + 1}"
            )
            return (
                run_chunk(start_page, midpoint, f"{chunk_id}a")
                + run_chunk(midpoint + 1, end_page, f"{chunk_id}b")
            )

        if not use_full_paths:
            if chunk_pdf.exists():
                chunk_pdf.unlink()
            write_pdf_page_chunk(pdf_path, chunk_pdf, start_page, end_page)
        if chunk_output_dir.exists():
            shutil.rmtree(chunk_output_dir)
        chunk_output_dir.mkdir(parents=True, exist_ok=True)

        try:
            result_manifest = run_mineru_parser_with_retries(
                chunk_pdf,
                chunk_output_dir,
                chunk_log,
            )
        except RuntimeError as exc:
            if isinstance(exc, MinerUParserAttemptsExhausted) and exc.transient_only:
                log(
                    f"    MinerU task {label} exhausted same-task retries due only to "
                    "transient failures; preserving the original chunk instead of splitting"
                )
                raise RuntimeError(
                    f"MinerU transient failure persisted for task {label}; task was not split. "
                    "Retry this file later with the original chunk boundaries."
                ) from exc
            if page_total <= MINERU_MIN_PAGES_PER_TASK:
                raise RuntimeError(
                    f"MinerU failed even after adaptive splitting on original page {start_page + 1}. "
                    f"See log: {chunk_log}"
                ) from exc

            if chunk_output_dir.exists():
                shutil.rmtree(chunk_output_dir)
            midpoint = start_page + (page_total // 2) - 1
            log(
                f"    MinerU task {label} failed; splitting into "
                f"pages {start_page + 1}-{midpoint + 1} and {midpoint + 2}-{end_page + 1}"
            )
            write_checkpoint(
                checkpoint_path,
                {
                    **identity,
                    "status": "split",
                    "updated_at": time.time(),
                    "failure": str(exc),
                },
            )
            return (
                run_chunk(start_page, midpoint, f"{chunk_id}a")
                + run_chunk(midpoint + 1, end_page, f"{chunk_id}b")
            )

        write_checkpoint(
            checkpoint_path,
            {
                **identity,
                "status": "complete",
                "updated_at": time.time(),
                "output_dir": str(chunk_output_dir),
                "result_manifest": result_manifest,
            },
        )

        return [
            {
                "pdf_path": chunk_pdf,
                "output_dir": chunk_output_dir,
                "log_path": chunk_log,
                "page_offset": start_page,
                "start_page": start_page,
                "end_page": end_page,
                "label": label,
            }
        ]

    if page_count <= MINERU_MAX_PAGES_PER_TASK:
        return run_chunk(0, page_count - 1, "full", use_full_paths=True)

    total_chunks = math.ceil(page_count / MINERU_MAX_PAGES_PER_TASK)
    log(
        f"    Large PDF detected: {page_count} pages; splitting into {total_chunks} "
        f"initial MinerU task(s), max {MINERU_MAX_PAGES_PER_TASK} pages each"
    )

    runs = []
    for chunk_index, start_page in enumerate(range(0, page_count, MINERU_MAX_PAGES_PER_TASK), 1):
        end_page = min(page_count - 1, start_page + MINERU_MAX_PAGES_PER_TASK - 1)
        runs.extend(run_chunk(start_page, end_page, f"{chunk_index:04d}"))
    return runs



_COMPONENT_EXPORTS = (
    "parser_log_has_json_errors",
    "retry_log_path",
    "MinerUParserAttemptsExhausted",
    "mineru_failure_is_transient",
    "optional_cli_args",
    "run_mineru_parser",
    "run_mineru_parser_with_retries",
    "write_pdf_page_chunk",
    "write_pdf_selected_pages",
    "write_pdf_isolated_selected_pages",
    "selected_pages_checkpoint_identity",
    "isolated_pages_checkpoint_identity",
    "run_or_reuse_mineru_selected_pages",
    "run_or_reuse_mineru_isolated_pages",
    "build_mineru_parser_runs",
)
_COMPONENT_RUNTIME = ComponentRuntime(globals(), _COMPONENT_EXPORTS)


def component_exports():
    return _COMPONENT_RUNTIME.exports()


def invoke_component(name, namespace, *args, **kwargs):
    return _COMPONENT_RUNTIME.invoke(name, namespace, *args, **kwargs)
