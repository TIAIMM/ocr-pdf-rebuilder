"""Run the repository's PaddleOCR-VL page worker in a Windows GPU environment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def gpu_memory_mib() -> int | None:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return int(result.stdout.splitlines()[0].strip())
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True, choices=("paddle", "transformers"))
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--pages", required=True, help="One-based comma-separated page numbers")
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--max-new-tokens", type=int, default=2048)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--python", type=Path)
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")

    pdf = args.pdf.resolve(strict=True)
    python = args.python or ROOT / f".venv-{args.engine}" / "Scripts" / "python.exe"
    layout_name = (
        "PP-DocLayoutV3_safetensors"
        if args.engine == "transformers"
        else "PP-DocLayoutV3"
    )
    layout = ROOT / "models" / layout_name
    recognition = ROOT / "models" / "PaddleOCR-VL-1.6"
    for required in (python, layout, recognition):
        if not required.exists():
            parser.error(f"Missing Windows runtime dependency: {required}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    result_dir = ROOT / "artifacts" / "windows-native-benchmark" / args.engine / stamp
    result_dir.mkdir(parents=True, exist_ok=False)
    command = [
        str(python),
        str(ROOT / "src" / "ocr_pdf_rebuilder" / "paddle_worker.py"),
        "--pdf", str(pdf),
        "--output-dir", str(result_dir),
        "--pages", args.pages,
        "--dpi", str(args.dpi),
        "--max-new-tokens", str(args.max_new_tokens),
        "--device", "gpu:0",
        "--backend", "native",
        "--engine", args.engine,
        "--layout-model-dir", str(layout),
        "--recognition-model-dir", str(recognition),
    ]

    before_mib = gpu_memory_mib()
    readings: list[int] = []
    page_finish_seconds: list[dict[str, object]] = []
    stop = threading.Event()

    def sample_gpu() -> None:
        while not stop.is_set():
            used = gpu_memory_mib()
            if used is not None:
                readings.append(used)
            stop.wait(1)

    monitor = threading.Thread(target=sample_gpu, daemon=True)
    monitor.start()
    started = time.monotonic()
    timed_out = threading.Event()
    with (result_dir / "worker.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        def stop_after_timeout() -> None:
            if process.poll() is None:
                timed_out.set()
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                    capture_output=True,
                    check=False,
                    timeout=20,
                )

        timer = threading.Timer(args.timeout_seconds, stop_after_timeout)
        timer.start()
        assert process.stdout is not None
        try:
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
                log.flush()
                if line.startswith("PaddleOCR page ") and "blocks=" in line:
                    page_finish_seconds.append(
                        {"message": line.strip(), "elapsed_seconds": round(time.monotonic() - started, 2)}
                    )
            returncode = process.wait()
        finally:
            timer.cancel()
    elapsed = round(time.monotonic() - started, 2)
    stop.set()
    monitor.join(timeout=12)

    page_results = sorted((result_dir / "page_results").glob("page_*.json"))
    summary = {
        "engine": args.engine,
        "python": str(python),
        "pdf": str(pdf),
        "source_sha256": file_sha256(pdf),
        "pages_requested": args.pages,
        "dpi": args.dpi,
        "max_new_tokens": args.max_new_tokens,
        "exit_code": returncode,
        "timed_out": timed_out.is_set(),
        "timeout_seconds": args.timeout_seconds,
        "elapsed_seconds": elapsed,
        "gpu_memory_before_mib": before_mib,
        "gpu_memory_peak_mib": max(readings) if readings else None,
        "page_finish_seconds": page_finish_seconds,
        "page_results": [
            {"page": path.stem, "cells": len(json.loads(path.read_text(encoding="utf-8")).get("cells", []))}
            for path in page_results
        ],
    }
    (result_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"summary": str(result_dir / "summary.json"), **summary}, ensure_ascii=False))
    return 124 if timed_out.is_set() else returncode


if __name__ == "__main__":
    raise SystemExit(main())
