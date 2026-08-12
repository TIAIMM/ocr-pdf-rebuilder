import shutil
import subprocess
import sys
import threading
import fitz

if __package__:
    from .pipeline_config import *
    from . import layout_engine as _layout_engine
    from . import mineru_results as _mineru_results
    from . import mineru_runner as _mineru_runner
    from . import page_recovery as _page_recovery
    from . import pipeline_orchestrator as _pipeline_orchestrator
    from . import qc_reporting as _qc_reporting
    from . import reportlab_renderer as _reportlab_renderer
    from . import runtime_state as _runtime_state
    from . import text_processing as _text_processing
    from .component_runtime import install_component
    from .forward_page_leak import ForwardPageLeakAnalyzer, ForwardPageLeakConfig
    from .pdf_validation import PdfArtifactValidator
    from .process_control import LiveProcessController
else:  # Support direct file loading used by diagnostics and the test harness.
    import importlib as _importlib
    import ocr_pdf_rebuilder.pipeline_config as _pipeline_config

    # A direct loader can execute this entry module more than once with a
    # different OCR_RUNTIME_ROOT. Refresh configuration before components bind.
    _importlib.reload(_pipeline_config)
    from ocr_pdf_rebuilder.pipeline_config import *

    import ocr_pdf_rebuilder.layout_engine as _layout_engine
    import ocr_pdf_rebuilder.mineru_results as _mineru_results
    import ocr_pdf_rebuilder.mineru_runner as _mineru_runner
    import ocr_pdf_rebuilder.page_recovery as _page_recovery
    import ocr_pdf_rebuilder.pipeline_orchestrator as _pipeline_orchestrator
    import ocr_pdf_rebuilder.qc_reporting as _qc_reporting
    import ocr_pdf_rebuilder.reportlab_renderer as _reportlab_renderer
    import ocr_pdf_rebuilder.runtime_state as _runtime_state
    import ocr_pdf_rebuilder.text_processing as _text_processing
    from ocr_pdf_rebuilder.component_runtime import install_component
    from ocr_pdf_rebuilder.forward_page_leak import (
        ForwardPageLeakAnalyzer,
        ForwardPageLeakConfig,
    )
    from ocr_pdf_rebuilder.pdf_validation import PdfArtifactValidator
    from ocr_pdf_rebuilder.process_control import LiveProcessController

ensure_runtime_directories()
LOG_LOCK = threading.Lock()


def log(message):
    with LOG_LOCK:
        print(message, flush=True)
def format_seconds(seconds):
    seconds = int(round(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{sec:02d}s"
    if minutes:
        return f"{minutes}m{sec:02d}s"
    return f"{sec}s"
def require_file(path, message):
    if not path.exists():
        raise RuntimeError(f"{message}: {path}")
def command_exists(name):
    return shutil.which(name) is not None
def python_module_exists(name):
    try:
        __import__(name)
        return True
    except Exception:
        return False
def formula_render_dependencies():
    global FORMULA_RENDER_DEPS
    if FORMULA_RENDER_DEPS is not None:
        return FORMULA_RENDER_DEPS

    latex_engine = shutil.which("latex") or shutil.which("pdflatex")
    FORMULA_RENDER_DEPS = {
        "latex": latex_engine,
        "dvisvgm": shutil.which("dvisvgm"),
        "svglib": python_module_exists("svglib"),
    }
    missing = [key for key, value in FORMULA_RENDER_DEPS.items() if not value]
    if missing:
        log(
            "    Formula vector rendering unavailable; environment is read-only and "
            "the renderer will use its configured fallback. Missing: "
            + ", ".join(missing)
        )
    return FORMULA_RENDER_DEPS


def live_process_controller(process_label="MinerU"):
    return LiveProcessController(
        logger=log,
        console_lock=LOG_LOCK,
        exit_cleanup_seconds=MINERU_PROCESS_EXIT_CLEANUP_SECONDS,
        process_label=process_label,
    )


def posix_process_group_exists(process_group_id):
    return LiveProcessController.posix_process_group_exists(process_group_id)


def wait_for_process_group_exit(process_group_id, timeout_seconds, process=None):
    return live_process_controller().wait_for_process_group_exit(
        process_group_id,
        timeout_seconds,
        process=process,
    )


def terminate_process_group(process, process_group_id, reason, grace_seconds=None):
    if grace_seconds is None:
        grace_seconds = MINERU_PROCESS_TERMINATE_GRACE_SECONDS
    live_process_controller().terminate_process_group(
        process,
        process_group_id,
        reason,
        grace_seconds,
    )


def run_live_process(
    cmd,
    cwd,
    log_path,
    stream_to_console=True,
    timeout_seconds=MINERU_PROCESS_TIMEOUT_SECONDS,
    idle_timeout_seconds=MINERU_PROCESS_IDLE_TIMEOUT_SECONDS,
    termination_grace_seconds=MINERU_PROCESS_TERMINATE_GRACE_SECONDS,
    process_label="MinerU",
):
    return live_process_controller(process_label=process_label).run(
        cmd,
        cwd,
        log_path,
        stream_to_console=stream_to_console,
        timeout_seconds=timeout_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
        termination_grace_seconds=termination_grace_seconds,
    )

install_component(_runtime_state, globals())
install_component(_mineru_runner, globals())

def render_pdf_page_to_png(pdf_path, page_index, image_path):
    image_path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open(pdf_path) as doc:
        page = doc[page_index]
        matrix = fitz.Matrix(DPI / 72, DPI / 72)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        pix.save(str(image_path))


install_component(_text_processing, globals())
install_component(_mineru_results, globals())


def forward_page_leak_analyzer():
    return ForwardPageLeakAnalyzer(
        ForwardPageLeakConfig(
            retry_reason=FORWARD_PAGE_LEAK_RETRY_REASON,
            lookahead=FORWARD_PAGE_LEAK_LOOKAHEAD,
            ngram_size=FORWARD_PAGE_LEAK_NGRAM_SIZE,
            min_source_chars=FORWARD_PAGE_LEAK_MIN_SOURCE_CHARS,
            min_neighbor_chars=FORWARD_PAGE_LEAK_MIN_NEIGHBOR_CHARS,
            min_output_source_ratio=FORWARD_PAGE_LEAK_MIN_OUTPUT_SOURCE_RATIO,
            min_neighbor_coverage=FORWARD_PAGE_LEAK_MIN_NEIGHBOR_COVERAGE,
            min_retry_own_coverage=FORWARD_PAGE_LEAK_MIN_RETRY_OWN_COVERAGE,
            max_retry_source_ratio=FORWARD_PAGE_LEAK_MAX_RETRY_SOURCE_RATIO,
        ),
        normalize_text=normalize_text,
        source_text_from_cell=source_text_from_cell,
        retry_result_has_usable_cells=retry_result_has_usable_cells,
        append_retry_reason=append_retry_reason,
    )


def normalize_page_overlap_text(text):
    return ForwardPageLeakAnalyzer.normalize_overlap_text(text)


def text_ngram_hashes(text, size=FORWARD_PAGE_LEAK_NGRAM_SIZE):
    return forward_page_leak_analyzer().ngram_hashes(text, size=size)


def page_result_primary_text(result):
    return forward_page_leak_analyzer().page_result_primary_text(result)


def source_page_overlap_texts(pdf_path):
    return forward_page_leak_analyzer().source_page_overlap_texts(pdf_path)


def forward_page_leak_matches(output_text, page_index, source_texts):
    return forward_page_leak_analyzer().forward_matches(
        output_text,
        page_index,
        source_texts,
    )


def detect_forward_page_content_leaks(pdf_path, page_results, source_texts=None, mark=True):
    return forward_page_leak_analyzer().detect(
        pdf_path,
        page_results,
        source_texts=source_texts,
        mark=mark,
    )


def retry_result_is_page_local(result, page_index, source_texts):
    return forward_page_leak_analyzer().retry_result_is_page_local(
        result,
        page_index,
        source_texts,
    )


def source_pdf_text_page_result(source, page_index):
    return forward_page_leak_analyzer().source_pdf_text_page_result(source, page_index)


install_component(_page_recovery, globals())


install_component(_layout_engine, globals())

install_component(_reportlab_renderer, globals())


def pdf_artifact_validator():
    return PdfArtifactValidator(
        radical_pattern=RADICAL_CHAR_RE,
        control_pattern=CONTROL_CHAR_RE,
        latex_pattern=LATEX_RESIDUE_RE,
        debug_markdown=DEBUG_MARKDOWN_WARNINGS,
        logger=log,
    )


def scan_pdf_validation(pdf_path, include_markdown=None, checks=None):
    return pdf_artifact_validator().scan(
        pdf_path,
        include_markdown=include_markdown,
        checks=checks,
    )


def validate_pdf_has_no_radicals(pdf_path, validation_scan=None):
    pdf_artifact_validator().validate_no_radicals(pdf_path, validation_scan)


def validate_pdf_page_count(pdf_path, expected_page_count, validation_scan=None):
    pdf_artifact_validator().validate_page_count(
        pdf_path,
        expected_page_count,
        validation_scan,
    )


def validate_pdf_has_no_raster_images(pdf_path, validation_scan=None):
    pdf_artifact_validator().validate_no_raster_images(pdf_path, validation_scan)


def validate_pdf_pages_are_blank(pdf_path, page_indices, validation_scan=None):
    pdf_artifact_validator().validate_pages_are_blank(
        pdf_path,
        page_indices,
        validation_scan,
    )


def validate_pdf_has_images_on_pages(pdf_path, page_indices, validation_scan=None):
    pdf_artifact_validator().validate_images_on_pages(
        pdf_path,
        page_indices,
        validation_scan,
    )


def validate_pdf_has_no_control_chars(pdf_path, validation_scan=None):
    pdf_artifact_validator().validate_no_control_chars(pdf_path, validation_scan)


def validate_pdf_has_no_latex_residue(pdf_path, validation_scan=None):
    pdf_artifact_validator().validate_no_latex_residue(pdf_path, validation_scan)


def warn_pdf_suspected_markdown(pdf_path, validation_scan=None):
    pdf_artifact_validator().warn_suspected_markdown(pdf_path, validation_scan)


install_component(_qc_reporting, globals())

install_component(_pipeline_orchestrator, globals())
