"""Measure the existing WSL vLLM route without changing its installation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from datetime import datetime


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wsl-repo", type=Path, default=Path("/home/ocr/ocr-pdf-rebuilder"))
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--pages", default="21", help="One-based comma-separated page numbers")
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.65)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    pages = sorted({int(value) for value in args.pages.split(",")})
    if not pages or pages[0] < 1:
        parser.error("--pages must contain positive page numbers")
    if not 0 < args.gpu_memory_utilization < 1:
        parser.error("--gpu-memory-utilization must be between 0 and 1")

    repo = args.wsl_repo.resolve(strict=True)
    pdf = (args.pdf or repo / "input" / "II.13.pdf").resolve(strict=True)
    sys.path.insert(0, str(repo / "src"))
    os.environ["PADDLEOCR_VLLM_GPU_MEMORY_UTILIZATION"] = str(args.gpu_memory_utilization)
    from ocr_pdf_rebuilder import paddle_pipeline as pipeline

    result_dir = args.output_root / datetime.now().strftime("%Y%m%d-%H%M%S")
    result_dir.mkdir(parents=True, exist_ok=False)
    log_path = result_dir / "worker.log"
    started = time.monotonic()
    with pipeline.paddle_inference_session(log_path) as session:
        ready_seconds = round(time.monotonic() - started, 2)
        worker_started = time.monotonic()
        pipeline.run_worker(
            pdf,
            result_dir,
            log_path,
            pages=[page - 1 for page in pages],
            backend="vllm-server",
            server_url=session["server_url"],
        )
        worker_seconds = round(time.monotonic() - worker_started, 2)
    total_seconds = round(time.monotonic() - started, 2)

    results = []
    for page in pages:
        result_path = result_dir / "page_results" / f"page_{page:04d}.json"
        page_result = json.loads(result_path.read_text(encoding="utf-8"))
        results.append({"page": page, "cells": len(page_result.get("cells", []))})
    summary = {
        "engine": "wsl-vllm-server",
        "pdf": str(pdf),
        "source_sha256": sha256(pdf),
        "pages": pages,
        "dpi": pipeline.PADDLE_DPI,
        "max_new_tokens": pipeline.PADDLE_MAX_NEW_TOKENS,
        "vllm_gpu_memory_utilization": pipeline.VLLM_GPU_MEMORY_UTILIZATION,
        "server_ready_seconds": ready_seconds,
        "worker_seconds": worker_seconds,
        "total_seconds": total_seconds,
        "page_results": results,
    }
    (result_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"summary": str(result_dir / "summary.json"), **summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
