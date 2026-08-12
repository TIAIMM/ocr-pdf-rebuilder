"""Top-level multi-PDF batch orchestration for the OCR production CLI."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import time
import traceback


class PdfBatchRunner:
    def __init__(
        self,
        *,
        input_dir: Path,
        output_dir: Path,
        mineru_output_dir: Path,
        log_dir: Path,
        process_pdf: Callable[[Path, int, int], dict[str, object] | None],
        write_checkpoint: Callable[[Path, dict[str, object]], None],
        logger: Callable[[str], None],
    ) -> None:
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.mineru_output_dir = mineru_output_dir
        self.log_dir = log_dir
        self.process_pdf = process_pdf
        self.write_checkpoint = write_checkpoint
        self.logger = logger

    def write_summary(
        self,
        started_at: float,
        pdfs: list[Path],
        results: list[dict[str, object]],
        status: str,
        finished_at: float | None = None,
    ) -> tuple[Path, dict[str, object]]:
        counts = {
            key: sum(1 for result in results if result.get("status") == key)
            for key in ("completed", "skipped", "failed")
        }
        payload = {
            "schema": 1,
            "status": status,
            "started_at": started_at,
            "updated_at": time.time(),
            "finished_at": finished_at,
            "input_dir": str(self.input_dir),
            "output_dir": str(self.output_dir),
            "total_files": len(pdfs),
            "processed_files": len(results),
            "counts": counts,
            "results": results,
        }
        summary_path = self.log_dir / "batch_summary.json"
        self.write_checkpoint(summary_path, payload)
        return summary_path, payload

    def run(self) -> None:
        pdfs = sorted(self.input_dir.glob("*.pdf"))
        if not pdfs:
            self.logger(f"No PDF files found in: {self.input_dir}")
            return

        self.logger("MinerU text-only PDF production")
        self.logger(f"Input:  {self.input_dir}")
        self.logger(f"Output: {self.output_dir}")
        self.logger(f"Raw:    {self.mineru_output_dir}")
        self.logger(f"PDFs:   {len(pdfs)}")

        started_at = time.time()
        results = []
        self.write_summary(started_at, pdfs, results, "running")
        for index, pdf_path in enumerate(pdfs, 1):
            file_start = time.monotonic()
            try:
                result = self.process_pdf(pdf_path, index, len(pdfs)) or {
                    "status": "completed"
                }
                result = dict(result)
                result["input_pdf"] = str(pdf_path)
                result["elapsed_seconds"] = round(time.monotonic() - file_start, 3)
            except Exception as exc:
                error_text = f"{type(exc).__name__}: {exc}"
                traceback_text = traceback.format_exc()
                self.logger(f"[{index}/{len(pdfs)}] FAILED: {pdf_path.name}: {error_text}")
                self.logger(traceback_text.rstrip())
                result = {
                    "status": "failed",
                    "input_pdf": str(pdf_path),
                    "error": error_text,
                    "traceback": traceback_text,
                    "log_path": str(self.log_dir / f"{pdf_path.stem}_mineru.log"),
                    "elapsed_seconds": round(time.monotonic() - file_start, 3),
                }
            results.append(result)
            self.write_summary(started_at, pdfs, results, "running")

        failed = [result for result in results if result.get("status") == "failed"]
        final_status = "completed_with_failures" if failed else "completed"
        summary_path, summary = self.write_summary(
            started_at,
            pdfs,
            results,
            final_status,
            finished_at=time.time(),
        )
        counts = summary["counts"]
        self.logger(
            "Batch done: "
            f"completed={counts['completed']}, skipped={counts['skipped']}, "
            f"failed={counts['failed']}"
        )
        self.logger(f"Batch summary: {summary_path}")
        if failed:
            raise SystemExit(1)
