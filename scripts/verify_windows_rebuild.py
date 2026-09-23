"""Run one Windows document through recognition and all reconstruction outputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ocr_pdf_rebuilder import paddle_pipeline  # noqa: E402
from ocr_pdf_rebuilder.task_lock import CrossProcessTaskLock  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, default=ROOT / "input/Le_Gout_du_secret.pdf")
    parser.add_argument("--pages", help="One-based comma-separated source pages for a bounded smoke test")
    args = parser.parse_args()
    source_path = args.pdf.resolve(strict=True)
    target_path = source_path
    if args.pages:
        page_numbers = [int(token) for token in args.pages.split(",")]
        target_dir = ROOT / "artifacts/windows-native-end-to-end/input"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{source_path.stem}_pages_{'_'.join(map(str, page_numbers))}.pdf"
        if not target_path.exists():
            with fitz.open(source_path) as source, fitz.open() as subset:
                for number in page_numbers:
                    if number < 1 or number > source.page_count:
                        parser.error(f"page outside source: {number}")
                    subset.insert_pdf(source, from_page=number - 1, to_page=number - 1)
                subset.save(target_path)
    paddle_pipeline.preflight_windows_native()
    paddle_pipeline.ensure_directories()
    lock_path = ROOT / "tmp/ocr_pdf_rebuilder/task.lock"
    with CrossProcessTaskLock(lock_path, engine_name="PaddleOCR-VL Windows verification",
                              input_dir=target_path.parent, output_dir=paddle_pipeline.OUTPUT_DIR):
        result = paddle_pipeline.process_pdf(target_path, 1, 1)
    with fitz.open(target_path) as source:
        expected_pages = source.page_count
    markdown_path = paddle_pipeline.OUTPUT_DIR / f"{target_path.stem}_paddle.md"
    if not markdown_path.is_file() or not markdown_path.read_text(encoding="utf-8").strip():
        raise RuntimeError(f"Markdown output is missing or empty: {markdown_path}")
    for key in ("output_text_pdf", "output_searchable_pdf"):
        with fitz.open(result[key]) as document:
            if document.page_count != expected_pages:
                raise RuntimeError(f"{key} has {document.page_count} pages; expected {expected_pages}")
    if result.get("output_image_pdf"):
        with fitz.open(result["output_image_pdf"]) as document:
            if document.page_count != expected_pages:
                raise RuntimeError("image PDF page count mismatch")
    print(json.dumps({"source": str(target_path), "pages": expected_pages,
                      "output_markdown": str(markdown_path), **result},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
