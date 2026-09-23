"""Run the WSL main pipeline on the same complete book for timing comparison."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import time


WSL_ROOT = Path("/home/ocr/ocr-pdf-rebuilder")
SOURCE = WSL_ROOT / "input/Le_Gout_du_secret.pdf"
SUMMARY = WSL_ROOT / "artifacts/wsl-fullbook-compare.json"
EXPECTED_SHA256 = "522d83560be92584f5bcfa66200d80a1c5335458908181edd592d7b464f43278"

os.environ["OCR_RUNTIME_ROOT"] = str(WSL_ROOT)
os.environ["PADDLEOCR_PYTHON"] = "/home/ocr/miniconda3/envs/paddleocr/bin/python"
os.environ["PADDLEOCR_VL_BACKEND"] = "vllm-server"
os.environ["PADDLEOCR_VLLM_PYTHON"] = "/home/ocr/miniconda3/envs/mineru/bin/python"
os.environ["PADDLEOCR_VLLM_GPU_MEMORY_UTILIZATION"] = "0.65"
sys.path.insert(0, str(WSL_ROOT / ".production/src"))

import fitz  # noqa: E402
from ocr_pdf_rebuilder import paddle_pipeline  # noqa: E402
from ocr_pdf_rebuilder.task_lock import CrossProcessTaskLock  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    actual_sha256 = sha256(SOURCE)
    if actual_sha256 != EXPECTED_SHA256:
        raise RuntimeError(f"WSL source hash mismatch: {actual_sha256}")
    timings: dict[str, float | int] = {"worker_calls": 0, "ocr_worker_seconds": 0.0}
    original_wait = paddle_pipeline.wait_for_vllm_server
    original_worker = paddle_pipeline.run_worker
    original_build = paddle_pipeline.build_outputs

    def timed_wait(*args, **kwargs):
        began = time.monotonic()
        try:
            return original_wait(*args, **kwargs)
        finally:
            timings["vllm_ready_wait_seconds"] = round(time.monotonic() - began, 3)

    def timed_worker(*args, **kwargs):
        began = time.monotonic()
        try:
            return original_worker(*args, **kwargs)
        finally:
            timings["worker_calls"] += 1
            timings["ocr_worker_seconds"] = round(
                timings["ocr_worker_seconds"] + time.monotonic() - began, 3
            )

    def timed_build(*args, **kwargs):
        began = time.monotonic()
        try:
            return original_build(*args, **kwargs)
        finally:
            timings["rebuild_seconds"] = round(time.monotonic() - began, 3)

    paddle_pipeline.wait_for_vllm_server = timed_wait
    paddle_pipeline.run_worker = timed_worker
    paddle_pipeline.build_outputs = timed_build

    started_at = time.time()
    began = time.monotonic()
    lock_path = WSL_ROOT / "tmp/ocr_pdf_rebuilder/task.lock"
    with CrossProcessTaskLock(lock_path, engine_name="PaddleOCR-VL WSL full-book comparison",
                              input_dir=SOURCE.parent, output_dir=paddle_pipeline.OUTPUT_DIR):
        result = paddle_pipeline.process_pdf(SOURCE, 1, 1)
    elapsed = time.monotonic() - began
    if result["status"] != "completed":
        raise RuntimeError(f"WSL comparison did not run fresh OCR: {result['status']}")
    with fitz.open(SOURCE) as original:
        expected_pages = original.page_count
    for key in ("output_text_pdf", "output_searchable_pdf", "output_image_pdf"):
        path = result.get(key)
        if path:
            with fitz.open(path) as artifact:
                if artifact.page_count != expected_pages:
                    raise RuntimeError(f"{key} page count mismatch")
    markdown = paddle_pipeline.OUTPUT_DIR / f"{SOURCE.stem}_paddle.md"
    if not markdown.is_file() or not markdown.read_text(encoding="utf-8").strip():
        raise RuntimeError("WSL Markdown output missing or empty")
    payload = {
        "source": str(SOURCE),
        "source_sha256": actual_sha256,
        "pages": expected_pages,
        "backend": paddle_pipeline.PADDLE_BACKEND,
        "vllm_gpu_memory_utilization": paddle_pipeline.VLLM_GPU_MEMORY_UTILIZATION,
        "started_at": started_at,
        "finished_at": time.time(),
        "total_seconds": round(elapsed, 3),
        "timings": timings,
        "output_markdown": str(markdown),
        **result,
    }
    SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"comparison_summary": str(SUMMARY), **payload}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
