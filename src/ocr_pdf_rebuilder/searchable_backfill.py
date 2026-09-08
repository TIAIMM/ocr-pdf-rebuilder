"""Build a Paddle searchable PDF from source-bound cached OCR, without inference."""

from __future__ import annotations

import argparse
from pathlib import Path

import fitz

from . import paddle_pipeline as paddle
from .signal_cleanup import termination_raises_keyboard_interrupt
from .task_lock import CrossProcessTaskLock


def backfill(source: Path, runtime_root: Path, output: Path | None = None) -> Path:
    source = source.expanduser().resolve()
    runtime_root = runtime_root.expanduser().resolve()
    output = (output or runtime_root / "pdf_paddle" / f"{source.stem}_paddle_searchable.pdf").resolve()
    if output == source:
        raise RuntimeError("Searchable output must not replace the source PDF")
    state_path = runtime_root / "tmp/paddle_textonly_pdf" / source.stem / "job_state.json"
    raw_dir = runtime_root / "paddle_output" / source.stem
    with CrossProcessTaskLock(
        runtime_root / "tmp/ocr_pdf_rebuilder/task.lock",
        engine_name="Paddle searchable backfill", input_dir=source.parent,
        output_dir=output.parent,
    ):
        state = paddle.shared.read_checkpoint(state_path)
        if not state or not isinstance(state.get("source"), dict):
            raise RuntimeError(f"Missing or damaged OCR cache provenance: {state_path}")
        # A relocated source is permitted only when its content is identical.
        signature = paddle.shared.source_file_signature(source)
        if any(signature.get(key) != state["source"].get(key) for key in ("size", "sha256")):
            raise RuntimeError("Source PDF does not match the cached OCR source")
        with fitz.open(source) as doc:
            count = doc.page_count
        results = paddle.load_page_results(raw_dir, count)
        missing = [index + 1 for index in range(count) if index not in results]
        if count == 0 or missing:
            raise RuntimeError(f"Incomplete OCR cache; missing pages: {missing[:30]}")
        if any(result.get("engine") != "PaddleOCR-VL" for result in results.values()):
            raise RuntimeError("OCR cache contains unexpected engine results")
        paddle.log(f"Backfill searchable PDF from {count} cached OCR pages; no model startup")
        paddle.shared.suppress_blank_page_outputs(source, results)
        stats = paddle.shared.build_validated_searchable_pdf(source, results, output)
        # This certifies only the new overlay, never upgrades older reconstructed PDFs.
        paddle.shared.write_checkpoint(output.with_suffix(".version"), {
            "schema": 1,
            "operation": "searchable_backfill",
            "source": signature,
            "ocr_job_state": str(state_path),
            "ocr_implementation_identity_hash": state.get("implementation_identity_hash"),
            "ocr_config_hash": state.get("config_hash"),
            "overlay_implementation_identity_hash": paddle.shared.current_package_source_identity_hash(),
            "output_searchable_pdf": paddle.shared.pdf_artifact_signature(output),
            "stats": stats,
        })
        return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--runtime-root", type=Path, default=paddle.RUNTIME_ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with termination_raises_keyboard_interrupt():
        backfill(args.source, args.runtime_root, args.output)


if __name__ == "__main__":
    main()
