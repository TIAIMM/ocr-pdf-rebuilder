"""PaddleOCR-VL-first OCR pipeline using the shared PDF reconstruction stack."""

from __future__ import annotations

from functools import lru_cache
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

import fitz

from .batch_runner import PdfBatchRunner
from . import pipeline_runtime as shared


RUNTIME_ROOT = shared.RUNTIME_ROOT
INPUT_DIR = RUNTIME_ROOT / "input"
OUTPUT_DIR = RUNTIME_ROOT / "pdf_paddle"
PADDLE_OUTPUT_DIR = RUNTIME_ROOT / "paddle_output"
TMP_DIR = RUNTIME_ROOT / "tmp/paddle_textonly_pdf"
LOG_DIR = RUNTIME_ROOT / "logs_paddle"
QC_DIR = RUNTIME_ROOT / "qc_paddle"

PADDLE_PYTHON = Path(
    os.environ.get(
        "PADDLEOCR_PYTHON",
        "/home/ocr/miniconda3/envs/paddleocr/bin/python",
    )
).expanduser()
PADDLE_DEVICE = os.environ.get("PADDLEOCR_DEVICE", "gpu:0")
PADDLE_PIPELINE_VERSION = "v1.6"
PADDLE_LAYOUT_MODEL = "PP-DocLayoutV3"
PADDLE_RECOGNITION_MODEL = "PaddleOCR-VL-1.6-0.9B"
PADDLE_BACKEND = os.environ.get("PADDLEOCR_VL_BACKEND", "vllm-server")
PADDLE_MAX_NEW_TOKENS = 2048
PADDLE_MODEL_SOURCE = os.environ.get("PADDLE_PDX_MODEL_SOURCE", "ModelScope")
PADDLE_MODEL_ROOT = Path.home() / ".paddlex/official_models"
VLLM_PYTHON = Path(
    os.environ.get(
        "PADDLEOCR_VLLM_PYTHON",
        "/home/ocr/miniconda3/envs/mineru/bin/python",
    )
).expanduser()
VLLM_MODEL_DIR = Path(
    os.environ.get(
        "PADDLEOCR_VLLM_MODEL_DIR",
        str(PADDLE_MODEL_ROOT / "PaddleOCR-VL-1.6"),
    )
).expanduser()
VLLM_HOST = "127.0.0.1"
VLLM_PORT = int(os.environ.get("PADDLEOCR_VLLM_PORT", "0"))
VLLM_MAX_MODEL_LEN = int(os.environ.get("PADDLEOCR_VLLM_MAX_MODEL_LEN", "16384"))
VLLM_GPU_MEMORY_UTILIZATION = float(
    os.environ.get("PADDLEOCR_VLLM_GPU_MEMORY_UTILIZATION", "0.80")
)
VLLM_START_TIMEOUT_SECONDS = float(
    os.environ.get("PADDLEOCR_VLLM_START_TIMEOUT_SECONDS", "600")
)
PADDLE_DPI = shared.DPI
PADDLE_PROCESS_TIMEOUT_SECONDS = 24 * 60 * 60
PADDLE_PROCESS_IDLE_TIMEOUT_SECONDS = 30 * 60
PADDLE_PROCESS_TERMINATE_GRACE_SECONDS = 20
OUTPUT_VERSION = "paddleocr-vl-1.6-reportlab-shared-render-v1"
CHECKPOINT_SCHEMA = 1
SKIP_EXISTING = True


def ensure_directories() -> None:
    for directory in (OUTPUT_DIR, PADDLE_OUTPUT_DIR, TMP_DIR, LOG_DIR, QC_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def log(message: object) -> None:
    shared.log(message)


def worker_path() -> Path:
    return Path(__file__).with_name("paddle_worker.py")


def paddle_config() -> dict[str, object]:
    return {
        "schema": CHECKPOINT_SCHEMA,
        "output_version": OUTPUT_VERSION,
        "dpi": PADDLE_DPI,
        "device": PADDLE_DEVICE,
        "pipeline_version": PADDLE_PIPELINE_VERSION,
        "layout_model": PADDLE_LAYOUT_MODEL,
        "recognition_model": PADDLE_RECOGNITION_MODEL,
        "backend": PADDLE_BACKEND,
        "max_new_tokens": PADDLE_MAX_NEW_TOKENS,
        "model_source": PADDLE_MODEL_SOURCE,
    }


def paddle_config_hash() -> str:
    return shared.stable_json_hash(paddle_config())


def expected_model_directories() -> list[Path]:
    recognition_candidates = (
        PADDLE_MODEL_ROOT / "PaddleOCR-VL-1.6",
        PADDLE_MODEL_ROOT / PADDLE_RECOGNITION_MODEL,
    )
    directories = [PADDLE_MODEL_ROOT / PADDLE_LAYOUT_MODEL]
    directories.extend(path for path in recognition_candidates if path.exists())
    unique = {}
    for path in directories:
        if path.exists():
            unique[str(path.resolve())] = path.resolve()
    return [unique[key] for key in sorted(unique)]


def model_snapshot() -> list[dict[str, object]]:
    snapshot = []
    for model_dir in expected_model_directories():
        for path in sorted(model_dir.rglob("*")):
            if not path.is_file():
                continue
            stat = path.stat()
            snapshot.append(
                {
                    "model": model_dir.name,
                    "path": path.relative_to(model_dir).as_posix(),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
    return snapshot


def hash_model_files(snapshot: list[dict[str, object]]) -> list[dict[str, object]]:
    directories = {path.name: path for path in expected_model_directories()}
    signatures = []
    for record in snapshot:
        path = directories[str(record["model"])] / str(record["path"])
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        signatures.append(
            {
                "model": record["model"],
                "path": record["path"],
                "size": record["size"],
                "sha256": digest.hexdigest(),
            }
        )
    return signatures


def paddle_worker_identity() -> dict[str, object]:
    if not PADDLE_PYTHON.is_file():
        raise RuntimeError(f"PaddleOCR Python environment was not found: {PADDLE_PYTHON}")
    result = subprocess.run(
        [str(PADDLE_PYTHON), str(worker_path()), "--identity"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Could not inspect PaddleOCR environment: "
            + (result.stderr.strip() or result.stdout.strip())
        )
    lines = [line for line in result.stdout.splitlines() if line.strip().startswith("{")]
    if not lines:
        raise RuntimeError("PaddleOCR worker returned no identity JSON")
    return json.loads(lines[-1])


def vllm_server_identity() -> dict[str, object] | None:
    if PADDLE_BACKEND != "vllm-server":
        return None
    if not VLLM_PYTHON.is_file():
        raise RuntimeError(f"vLLM Python environment was not found: {VLLM_PYTHON}")
    code = (
        "import importlib.metadata,json,sys;"
        "print(json.dumps({'python':sys.version.split()[0],"
        "'python_executable':sys.executable,"
        "'vllm':importlib.metadata.version('vllm')},sort_keys=True))"
    )
    result = subprocess.run(
        [str(VLLM_PYTHON), "-c", code],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Could not inspect vLLM environment: "
            + (result.stderr.strip() or result.stdout.strip())
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


@lru_cache(maxsize=1)
def paddle_runtime_identity() -> dict[str, object]:
    ensure_directories()
    snapshot = model_snapshot()
    cache_path = LOG_DIR / "paddle_runtime_identity_cache.json"
    cached = shared.read_checkpoint(cache_path)
    if cached and cached.get("snapshot") == snapshot:
        model_files = cached.get("model_files") or []
    else:
        model_files = hash_model_files(snapshot)
        shared.write_checkpoint(
            cache_path,
            {
                "schema": 1,
                "snapshot": snapshot,
                "model_files": model_files,
            },
        )
    return {
        "schema": 1,
        "worker": paddle_worker_identity(),
        "inference_server": vllm_server_identity(),
        "models": model_files,
        "config": paddle_config(),
    }


def paddle_runtime_identity_hash() -> str:
    return shared.stable_json_hash(paddle_runtime_identity())


def completion_matches(
    state_path: Path,
    source_pdf: Path,
    output_pdf: Path,
    output_md: Path,
    output_image_pdf: Path,
) -> bool:
    state = shared.read_checkpoint(state_path)
    if not state:
        return False
    expected = {
        "schema": CHECKPOINT_SCHEMA,
        "output_version": OUTPUT_VERSION,
        "source": shared.source_file_signature(source_pdf),
        "config_hash": paddle_config_hash(),
        "implementation_identity_hash": shared.current_package_source_identity_hash(),
        "runtime_identity_hash": paddle_runtime_identity_hash(),
    }
    if any(state.get(key) != value for key, value in expected.items()):
        return False
    paths = {
        "output_pdf": output_pdf,
        "output_md": output_md,
    }
    if state.get("has_image_variant"):
        paths["output_image_pdf"] = output_image_pdf
    for key, path in paths.items():
        if not path.is_file():
            return False
        signature = (
            shared.pdf_artifact_signature(path)
            if path.suffix.lower() == ".pdf"
            else shared.file_integrity_signature(path)
        )
        if state.get(key) != signature:
            return False
    return True


def write_completion_state(
    state_path: Path,
    source_pdf: Path,
    output_pdf: Path,
    output_md: Path,
    output_image_pdf: Path,
    image_fallback_pages: list[int],
) -> None:
    paddle_runtime_identity.cache_clear()
    identity = paddle_runtime_identity()
    payload = {
        "schema": CHECKPOINT_SCHEMA,
        "output_version": OUTPUT_VERSION,
        "source": shared.source_file_signature(source_pdf),
        "config": paddle_config(),
        "config_hash": paddle_config_hash(),
        "implementation_identity": shared.current_package_source_identity(),
        "implementation_identity_hash": shared.current_package_source_identity_hash(),
        "runtime_identity": identity,
        "runtime_identity_hash": shared.stable_json_hash(identity),
        "output_pdf": shared.pdf_artifact_signature(output_pdf),
        "output_md": shared.file_integrity_signature(output_md),
        "output_image_pdf": (
            shared.pdf_artifact_signature(output_image_pdf)
            if image_fallback_pages
            else None
        ),
        "has_image_variant": bool(image_fallback_pages),
        "image_fallback_pages": [page_index + 1 for page_index in image_fallback_pages],
        "completed_at": time.time(),
    }
    shared.write_checkpoint(state_path, payload)


def prepare_workspace(pdf_path: Path, work_dir: Path, raw_dir: Path) -> bool:
    state_path = work_dir / "job_state.json"
    expected = {
        "schema": CHECKPOINT_SCHEMA,
        "source": shared.source_file_signature(pdf_path),
        "config_hash": paddle_config_hash(),
        "implementation_identity_hash": shared.current_package_source_identity_hash(),
        "runtime_identity_hash": paddle_runtime_identity_hash(),
    }
    state = shared.read_checkpoint(state_path)
    resumable = bool(
        state
        and all(state.get(key) == value for key, value in expected.items())
        and work_dir.exists()
        and raw_dir.exists()
    )
    if resumable:
        log(f"    Resume PaddleOCR workspace found: {work_dir}")
        return True
    if work_dir.exists():
        shutil.rmtree(work_dir)
    if raw_dir.exists():
        shutil.rmtree(raw_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    shared.write_checkpoint(
        state_path,
        {
            **expected,
            "status": "active",
            "implementation_identity": shared.current_package_source_identity(),
            "runtime_identity": paddle_runtime_identity(),
            "updated_at": time.time(),
        },
    )
    return False


def worker_command(
    pdf_path: Path,
    raw_dir: Path,
    *,
    pages: list[int] | None = None,
    force: bool = False,
    backend: str | None = None,
    server_url: str | None = None,
) -> list[str]:
    backend = backend or PADDLE_BACKEND
    command = [
        str(PADDLE_PYTHON),
        "-u",
        str(worker_path()),
        "--pdf",
        str(pdf_path),
        "--output-dir",
        str(raw_dir),
        "--dpi",
        str(PADDLE_DPI),
        "--device",
        PADDLE_DEVICE,
        "--pipeline-version",
        PADDLE_PIPELINE_VERSION,
        "--layout-model",
        PADDLE_LAYOUT_MODEL,
        "--recognition-model",
        PADDLE_RECOGNITION_MODEL,
        "--backend",
        backend,
        "--max-new-tokens",
        str(PADDLE_MAX_NEW_TOKENS),
        "--model-source",
        PADDLE_MODEL_SOURCE,
    ]
    if server_url:
        command.extend(["--server-url", server_url])
    if pages:
        command.extend(["--pages", ",".join(str(page_index + 1) for page_index in pages)])
    if force:
        command.append("--force")
    return command


def run_worker(
    pdf_path: Path,
    raw_dir: Path,
    log_path: Path,
    *,
    pages: list[int] | None = None,
    force: bool = False,
    backend: str | None = None,
    server_url: str | None = None,
) -> None:
    command = worker_command(
        pdf_path,
        raw_dir,
        pages=pages,
        force=force,
        backend=backend,
        server_url=server_url,
    )
    log(f"    PaddleOCR command: {' '.join(command)}")
    log(f"    PaddleOCR log: {log_path}")
    returncode = shared.run_live_process(
        command,
        RUNTIME_ROOT,
        log_path,
        stream_to_console=True,
        timeout_seconds=PADDLE_PROCESS_TIMEOUT_SECONDS,
        idle_timeout_seconds=PADDLE_PROCESS_IDLE_TIMEOUT_SECONDS,
        termination_grace_seconds=PADDLE_PROCESS_TERMINATE_GRACE_SECONDS,
        process_label="PaddleOCR-VL",
    )
    if returncode != 0:
        raise RuntimeError(f"PaddleOCR-VL worker exited with code {returncode}")


def available_local_port() -> int:
    if VLLM_PORT:
        return VLLM_PORT
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((VLLM_HOST, 0))
        return int(listener.getsockname()[1])


def vllm_server_command(port: int) -> list[str]:
    return [
        str(VLLM_PYTHON),
        "-u",
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        str(VLLM_MODEL_DIR),
        "--served-model-name",
        PADDLE_RECOGNITION_MODEL,
        "--host",
        VLLM_HOST,
        "--port",
        str(port),
        "--trust-remote-code",
        "--max-model-len",
        str(VLLM_MAX_MODEL_LEN),
        "--gpu-memory-utilization",
        str(VLLM_GPU_MEMORY_UTILIZATION),
    ]


def server_log_tail(path: Path, line_count: int = 40) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-line_count:])
    except OSError:
        return ""


def wait_for_vllm_server(process: subprocess.Popen[bytes], health_url: str, log_path: Path) -> None:
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + VLLM_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        returncode = process.poll()
        if returncode is not None:
            tail = server_log_tail(log_path)
            raise RuntimeError(
                f"PaddleOCR vLLM server exited during startup with code {returncode}"
                + (f":\n{tail}" if tail else "")
            )
        try:
            with opener.open(health_url, timeout=1.5) as response:
                if 200 <= response.status < 300:
                    return
        except (OSError, URLError):
            pass
        time.sleep(0.5)
    raise TimeoutError(
        f"PaddleOCR vLLM server did not become ready within {VLLM_START_TIMEOUT_SECONDS:.0f}s"
        + (f":\n{server_log_tail(log_path)}" if server_log_tail(log_path) else "")
    )


@contextmanager
def paddle_inference_session(log_path: Path):
    """Own the optional vLLM server and always reclaim its complete process group."""

    if PADDLE_BACKEND == "native":
        log("    PaddleOCR recognition backend: native")
        yield {"backend": "native", "server_url": None}
        return
    if PADDLE_BACKEND != "vllm-server":
        raise RuntimeError(f"Unsupported PaddleOCR-VL backend: {PADDLE_BACKEND}")
    if not VLLM_PYTHON.is_file():
        raise RuntimeError(f"vLLM Python environment was not found: {VLLM_PYTHON}")
    if not VLLM_MODEL_DIR.is_dir():
        raise RuntimeError(f"PaddleOCR-VL model directory was not found: {VLLM_MODEL_DIR}")

    port = available_local_port()
    server_url = f"http://{VLLM_HOST}:{port}/v1"
    health_url = f"http://{VLLM_HOST}:{port}/health"
    server_log_path = log_path.with_name(f"{log_path.stem}_vllm_server{log_path.suffix}")
    command = vllm_server_command(port)
    server_log_path.parent.mkdir(parents=True, exist_ok=True)
    log(f"    Starting local PaddleOCR vLLM server: {server_url}")
    log(f"    vLLM server log: {server_log_path}")
    with server_log_path.open("wb") as server_log:
        process = subprocess.Popen(
            command,
            cwd=str(RUNTIME_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=server_log,
            stderr=subprocess.STDOUT,
            start_new_session=(os.name == "posix"),
        )
        process_group_id = os.getpgid(process.pid) if os.name == "posix" else None
        try:
            wait_for_vllm_server(process, health_url, server_log_path)
            log(f"    PaddleOCR vLLM server ready: PID={process.pid}")
            yield {"backend": "vllm-server", "server_url": server_url}
        finally:
            shared.live_process_controller(
                process_label="PaddleOCR vLLM server"
            ).terminate_process_group(
                process,
                process_group_id,
                "PaddleOCR inference session finished",
                PADDLE_PROCESS_TERMINATE_GRACE_SECONDS,
            )


def load_page_results(raw_dir: Path, page_count: int) -> dict[int, dict[str, object]]:
    results = {}
    result_dir = raw_dir / "page_results"
    for page_index in range(page_count):
        path = result_dir / f"page_{page_index + 1:04d}.json"
        if not path.is_file():
            continue
        result = json.loads(path.read_text(encoding="utf-8"))
        if int(result.get("page_index", -1)) != page_index:
            raise RuntimeError(f"Paddle page checkpoint has wrong page index: {path}")
        result["json_path"] = str(path)
        results[page_index] = result
    return results


def retry_suspicious_pages(
    pdf_path: Path,
    raw_dir: Path,
    page_results: dict[int, dict[str, object]],
    log_path: Path,
    session: dict[str, str | None],
) -> dict[int, dict[str, object]]:
    pages = sorted(
        page_index
        for page_index, result in page_results.items()
        if shared.page_result_has_high_repeated_pseudotext(result)
    )
    if not pages:
        return page_results
    log(
        "    Re-running PaddleOCR pages with suspicious repeated text: "
        + ", ".join(str(page + 1) for page in pages)
    )
    run_worker(
        pdf_path,
        raw_dir,
        log_path.with_name(f"{log_path.stem}_quality_retry{log_path.suffix}"),
        pages=pages,
        force=True,
        **session,
    )
    refreshed = load_page_results(raw_dir, max(page_results) + 1)
    for page_index in pages:
        if page_index in refreshed:
            refreshed[page_index]["source_page_retry"] = True
            refreshed[page_index]["retry_page"] = page_index + 1
            page_results[page_index] = refreshed[page_index]
    return page_results


def repair_forward_leaks(
    pdf_path: Path,
    raw_dir: Path,
    page_results: dict[int, dict[str, object]],
    log_path: Path,
    session: dict[str, str | None],
) -> dict[int, dict[str, object]]:
    source_texts = shared.source_page_overlap_texts(pdf_path)
    detected = shared.detect_forward_page_content_leaks(
        pdf_path, page_results, source_texts=source_texts, mark=True
    )
    pages = sorted(detected)
    if not pages:
        return page_results
    log(
        "    PaddleOCR forward-page leak candidates: "
        + ", ".join(str(page + 1) for page in pages)
    )
    run_worker(
        pdf_path,
        raw_dir,
        log_path.with_name(f"{log_path.stem}_forward_page_retry{log_path.suffix}"),
        pages=pages,
        force=True,
        **session,
    )
    refreshed = load_page_results(raw_dir, max(page_results) + 1)
    image_dir = raw_dir / "forward_page_leak_image_fallback_pages"
    with fitz.open(pdf_path) as source:
        for page_index in pages:
            original = page_results[page_index]
            candidate = refreshed.get(page_index)
            valid, validation = shared.retry_result_is_page_local(
                candidate, page_index, source_texts
            ) if candidate else (False, {"reason": "Paddle retry returned no result"})
            if valid:
                shared.apply_forward_page_leak_metadata(candidate, original, validation)
                candidate["source_page_retry"] = True
                candidate["forward_page_content_leak_repaired"] = True
                candidate["forward_page_content_leak_repair_mode"] = "isolated_paddleocr"
                page_results[page_index] = candidate
                continue
            fallback = shared.source_pdf_text_page_result(source, page_index)
            if fallback is not None:
                shared.apply_forward_page_leak_metadata(fallback, original, validation)
                fallback["forward_page_content_leak_repaired"] = True
                fallback["forward_page_content_leak_source_text_fallback"] = True
                fallback["forward_page_content_leak_repair_mode"] = "source_pdf_page_text"
                shared.append_retry_reason(
                    fallback, shared.FORWARD_PAGE_LEAK_SOURCE_TEXT_FALLBACK_REASON
                )
                page_results[page_index] = fallback
                continue
            image_path = image_dir / f"page_{page_index + 1:04d}.png"
            shared.render_pdf_page_to_png(pdf_path, page_index, image_path)
            fallback = {
                "cells": [],
                "fallback_text": "",
                "filtered": False,
                "needs_retry": False,
                "retry_reason": "",
                "image_size": None,
                "md_nohf_text": "",
                "image_fallback_path": str(image_path),
                "image_fallback_page": True,
                "image_fallback_kind": "forward_page_content_leak",
                "forward_page_content_leak_image_fallback": True,
                "forward_page_content_leak_repair_mode": "source_page_image",
            }
            shared.apply_forward_page_leak_metadata(fallback, original, validation)
            shared.append_retry_reason(
                fallback, shared.FORWARD_PAGE_LEAK_IMAGE_FALLBACK_REASON
            )
            page_results[page_index] = fallback
    return page_results


def write_paddle_qc_report(
    pdf_path: Path,
    raw_dir: Path,
    page_results: dict[int, dict[str, object]],
    page_specs: list[tuple[float, float, list[dict[str, object]]]],
    output_pdf: Path,
    output_image_pdf: Path | None,
    image_fallback_pages: list[int],
    validation_scan: dict[str, object],
) -> dict[str, object]:
    suspects = shared.collect_qc_suspect_pages(
        pdf_path, page_results, page_specs, len(page_specs)
    )
    report = {
        "engine": "PaddleOCR-VL",
        "pipeline_config": paddle_config(),
        "input_pdf": str(pdf_path),
        "output_text_pdf": str(output_pdf),
        "output_image_pdf": str(output_image_pdf) if output_image_pdf else None,
        "paddle_output_dir": str(raw_dir),
        "source_page_count": len(page_specs),
        "output_page_count": validation_scan.get("page_count"),
        "image_fallback_pages": [page + 1 for page in image_fallback_pages],
        "suspect_pages": suspects,
        "page_results": {
            str(page + 1): {
                "json_path": result.get("json_path"),
                "retry_reason": result.get("retry_reason"),
                "cell_count": len(result.get("cells") or []),
                "bbox_content_repairs": result.get("paddle_bbox_content_repairs") or [],
            }
            for page, result in sorted(page_results.items())
        },
    }
    report_path = LOG_DIR / f"{pdf_path.stem}_qc.json"
    shared.write_checkpoint(report_path, report)
    log(f"    QC report: {report_path}")
    if suspects:
        log("    QC suspect pages: " + ", ".join(str(page) for page in sorted(suspects)))
    return report


def build_outputs(
    pdf_path: Path,
    page_results: dict[int, dict[str, object]],
    work_dir: Path,
    raw_dir: Path,
    output_pdf: Path,
    output_image_pdf: Path,
    output_md: Path,
) -> list[int]:
    with fitz.open(pdf_path) as source:
        page_count = source.page_count
        log(f"    [3/5] Building layout pages: 0/{page_count}")
        page_specs = []
        markdown_pages = []
        category_counts = {}
        md_nohf_pages = 0
        image_fallback_pages = []
        total_blocks = 0
        for page_index in range(page_count):
            page = source[page_index]
            page_width = float(page.rect.width)
            page_height = float(page.rect.height)
            result = page_results.get(
                page_index,
                {"cells": [], "fallback_text": "", "filtered": False, "image_size": None},
            )
            if result.get("image_fallback_path"):
                blocks = [
                    shared.image_fallback_block(
                        result["image_fallback_path"], page_width, page_height
                    )
                ]
                log(f"        Page {page_index + 1}: used source page image fallback")
            else:
                blocks = shared.blocks_from_page_result(result, page_width, page_height)
                if not blocks and result.get("fallback_text"):
                    block = shared.fallback_text_block(
                        result["fallback_text"], 0, page_width, page_height
                    )
                    blocks = [block] if block else []
                blocks.sort(
                    key=lambda block: (
                        shared.is_header_footer(block),
                        block["top"],
                        block["left"],
                        block["order"],
                    )
                )
                blocks = shared.prepare_blocks(blocks, page_width, page_height)
                failures = shared.reportlab_page_fit_failures(
                    page_height, blocks
                )
                if failures:
                    image_path = work_dir / "layout_image_fallback_pages" / f"page_{page_index + 1:04d}.png"
                    shared.render_pdf_page_to_png(pdf_path, page_index, image_path)
                    result["image_fallback_path"] = str(image_path)
                    result["image_fallback_page"] = True
                    result["layout_fit_failures"] = failures
                    shared.append_retry_reason(
                        result, shared.UNFITTING_LAYOUT_IMAGE_FALLBACK_REASON
                    )
                    blocks = [shared.image_fallback_block(image_path, page_width, page_height)]
                    log(
                        f"        Page {page_index + 1}: {len(failures)} OCR block(s) "
                        "could not fit safely; used source page image fallback"
                    )
            for block in blocks:
                block["page_index"] = page_index
                block["source_pdf_path"] = str(pdf_path)
                block["formula_cache_dir"] = str(work_dir / "formula_render_cache")
                block["formula_crop_dir"] = str(work_dir / "formula_image_crops")
                category = block["category"]
                category_counts[category] = category_counts.get(category, 0) + 1
            if any(block.get("category") == "ImageFallback" for block in blocks):
                image_fallback_pages.append(page_index)
            total_blocks += len(blocks)
            page_specs.append((page_width, page_height, blocks))
            if shared.normalize_markdown_text(result.get("md_nohf_text", "")):
                md_nohf_pages += 1
            markdown_pages.append(shared.markdown_page_from_result(result, blocks, page_index))
            log(f"        Layout page {page_index + 1}/{page_count}: blocks={len(blocks)}")

    shared.log_category_audit(category_counts, md_nohf_pages, page_count)
    shared.write_book_markdown(output_md, markdown_pages)
    log("    [4/5] Rendering text-only PDF with ReportLab")
    shared.render_blocks_to_pdf_reportlab(
        page_specs,
        output_pdf,
        formula_work_dir=work_dir / "formula_render_cache",
        include_full_page_images=False,
        allow_formula_image_fallback=False,
        progress_callback=lambda current, total: log(
            f"        Render text PDF page {current}/{total}"
        ),
    )
    validation = shared.scan_pdf_validation(
        output_pdf,
        progress_callback=lambda current, total: log(
            f"        Validate text PDF page {current}/{total}"
        ),
    )
    shared.validate_pdf_page_count(output_pdf, page_count, validation)
    shared.validate_pdf_has_no_raster_images(output_pdf, validation)
    shared.validate_pdf_pages_are_blank(output_pdf, image_fallback_pages, validation)

    if image_fallback_pages:
        log(
            f"    Rendering image-variant PDF for {len(image_fallback_pages)} fallback page(s): "
            f"{output_image_pdf}"
        )
        image_specs = [
            (width, height, [dict(block) for block in blocks])
            for width, height, blocks in page_specs
        ]
        shared.render_blocks_to_pdf_reportlab(
            image_specs,
            output_image_pdf,
            formula_work_dir=work_dir / "formula_render_cache",
            include_full_page_images=True,
            allow_formula_image_fallback=True,
            progress_callback=lambda current, total: log(
                f"        Render image PDF page {current}/{total}"
            ),
        )
        image_validation = shared.scan_pdf_validation(
            output_image_pdf,
            progress_callback=lambda current, total: log(
                f"        Validate image PDF page {current}/{total}"
            ),
        )
        shared.validate_pdf_page_count(output_image_pdf, page_count, image_validation)
        shared.validate_pdf_has_images_on_pages(
            output_image_pdf, image_fallback_pages, image_validation
        )
        shared.validate_pdf_has_no_radicals(output_image_pdf, image_validation)
        shared.validate_pdf_has_no_control_chars(output_image_pdf, image_validation)
        shared.validate_pdf_has_no_latex_residue(output_image_pdf, image_validation)
    elif output_image_pdf.exists():
        output_image_pdf.unlink()

    write_paddle_qc_report(
        pdf_path,
        raw_dir,
        page_results,
        page_specs,
        output_pdf,
        output_image_pdf if image_fallback_pages else None,
        image_fallback_pages,
        validation,
    )
    shared.validate_pdf_has_no_radicals(output_pdf, validation)
    shared.validate_pdf_has_no_control_chars(output_pdf, validation)
    shared.validate_pdf_has_no_latex_residue(output_pdf, validation)
    shared.warn_pdf_suspected_markdown(output_pdf, validation)
    log(f"    Rendered blocks: {total_blocks}")
    return image_fallback_pages


def process_pdf(pdf_path: Path, index: int, total: int) -> dict[str, object]:
    name = pdf_path.stem
    output_pdf = OUTPUT_DIR / f"{name}_paddle.pdf"
    output_image_pdf = OUTPUT_DIR / f"{name}_paddle_with_images.pdf"
    output_md = OUTPUT_DIR / f"{name}_paddle.md"
    output_version = OUTPUT_DIR / f"{name}_paddle.version"
    if SKIP_EXISTING and completion_matches(
        output_version, pdf_path, output_pdf, output_md, output_image_pdf
    ):
        state = shared.read_checkpoint(output_version) or {}
        variant = " plus image variant" if state.get("has_image_variant") else ""
        log(
            f"[{index}/{total}] Skip existing current-version PaddleOCR text output{variant} "
            f"before model startup: {output_pdf}"
        )
        return {
            "status": "skipped",
            "output_text_pdf": str(output_pdf),
            "output_image_pdf": str(output_image_pdf) if state.get("has_image_variant") else None,
        }

    work_dir = TMP_DIR / name
    raw_dir = PADDLE_OUTPUT_DIR / name
    log_path = LOG_DIR / f"{name}_paddle.log"
    start = time.monotonic()
    log(f"[{index}/{total}] Start: {pdf_path.name}")
    log(f"    Input:  {pdf_path}")
    log(f"    Output: {output_pdf}")
    prepare_workspace(pdf_path, work_dir, raw_dir)
    with fitz.open(pdf_path) as source:
        page_count = source.page_count
    if page_count == 0:
        raise RuntimeError(f"Empty PDF: {pdf_path}")
    log(f"    Pages:  {page_count}")
    log("    [1/5] Running PaddleOCR-VL parser")
    with paddle_inference_session(log_path) as session:
        run_worker(pdf_path, raw_dir, log_path, **session)

        log("    [2/5] Loading and validating PaddleOCR page results")
        page_results = load_page_results(raw_dir, page_count)
        page_results = retry_suspicious_pages(
            pdf_path, raw_dir, page_results, log_path, session
        )
        page_results = repair_forward_leaks(
            pdf_path, raw_dir, page_results, log_path, session
        )
    shared.suppress_blank_page_outputs(pdf_path, page_results)
    page_results = shared.degrade_repeated_pseudotext_pages_to_images(
        pdf_path, page_results, work_dir
    )
    page_results = shared.degrade_unusable_nonblank_pages_to_images(
        pdf_path, page_results, work_dir
    )
    image_fallback_pages = build_outputs(
        pdf_path,
        page_results,
        work_dir,
        raw_dir,
        output_pdf,
        output_image_pdf,
        output_md,
    )
    write_completion_state(
        output_version,
        pdf_path,
        output_pdf,
        output_md,
        output_image_pdf,
        image_fallback_pages,
    )
    log("    [5/5] Done")
    log(
        f"    Saved: {output_pdf} ({output_pdf.stat().st_size / 1024 / 1024:.2f} MB), "
        f"time={shared.format_seconds(time.monotonic() - start)}"
    )
    log(f"    Saved Markdown: {output_md}")
    return {
        "status": "completed",
        "output_text_pdf": str(output_pdf),
        "output_image_pdf": str(output_image_pdf) if image_fallback_pages else None,
        "image_fallback_pages": [page + 1 for page in image_fallback_pages],
    }


def batch_runner() -> PdfBatchRunner:
    return PdfBatchRunner(
        input_dir=INPUT_DIR,
        output_dir=OUTPUT_DIR,
        mineru_output_dir=PADDLE_OUTPUT_DIR,
        log_dir=LOG_DIR,
        process_pdf=process_pdf,
        write_checkpoint=shared.write_checkpoint,
        logger=log,
        engine_name="PaddleOCR-VL",
        raw_label="Raw",
        log_suffix="paddle",
    )


def main() -> None:
    ensure_directories()
    batch_runner().run()
