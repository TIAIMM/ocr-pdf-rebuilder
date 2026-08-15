"""Top-level multi-PDF batch orchestration for the OCR production CLI."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import time
import traceback

from .task_lock import CrossProcessTaskLock, TaskLockBusyError


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
        engine_name: str = "MinerU",
        raw_label: str = "Raw",
        log_suffix: str = "mineru",
        lock_path: Path | None = None,
    ) -> None:
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.mineru_output_dir = mineru_output_dir
        self.log_dir = log_dir
        self.process_pdf = process_pdf
        self.write_checkpoint = write_checkpoint
        self.logger = logger
        self.engine_name = engine_name
        self.raw_label = raw_label
        self.log_suffix = log_suffix
        self.lock_path = lock_path or (
            self.input_dir.parent / "tmp/ocr_pdf_rebuilder/task.lock"
        )

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
        interrupted = sum(
            1 for result in results if result.get("status") == "interrupted"
        )
        if interrupted:
            counts["interrupted"] = interrupted
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
        lock = CrossProcessTaskLock(
            self.lock_path,
            engine_name=self.engine_name,
            input_dir=self.input_dir,
            output_dir=self.output_dir,
        )
        try:
            with lock:
                self.logger(f"Task lock acquired: {self.lock_path}")
                self._run_locked()
        except TaskLockBusyError as exc:
            self.logger(f"ERROR: {exc}")
            raise SystemExit(2) from None

    def _run_locked(self) -> None:
        pdfs = sorted(self.input_dir.glob("*.pdf"))
        if not pdfs:
            self.logger(f"No PDF files found in: {self.input_dir}")
            return

        self.logger(f"{self.engine_name} text-only PDF production")
        self.logger(f"Input:  {self.input_dir}")
        self.logger(f"Output: {self.output_dir}")
        self.logger(f"{self.raw_label}:    {self.mineru_output_dir}")
        self.logger(f"PDFs:   {len(pdfs)}")

        started_at = time.time()
        results = []
        self.write_summary(started_at, pdfs, results, "running")
        active_pdf: Path | None = None
        active_start: float | None = None
        try:
            for index, pdf_path in enumerate(pdfs, 1):
                active_pdf = pdf_path
                active_start = time.monotonic()
                try:
                    result = self.process_pdf(pdf_path, index, len(pdfs)) or {
                        "status": "completed"
                    }
                    result = dict(result)
                    result["input_pdf"] = str(pdf_path)
                    result["elapsed_seconds"] = round(
                        time.monotonic() - active_start, 3
                    )
                except Exception as exc:
                    error_text = f"{type(exc).__name__}: {exc}"
                    traceback_text = traceback.format_exc()
                    self.logger(
                        f"[{index}/{len(pdfs)}] FAILED: {pdf_path.name}: {error_text}"
                    )
                    self.logger(traceback_text.rstrip())
                    result = {
                        "status": "failed",
                        "input_pdf": str(pdf_path),
                        "error": error_text,
                        "traceback": traceback_text,
                        "log_path": str(
                            self.log_dir / f"{pdf_path.stem}_{self.log_suffix}.log"
                        ),
                        "elapsed_seconds": round(
                            time.monotonic() - active_start, 3
                        ),
                    }
                results.append(result)
                active_pdf = None
                active_start = None
                self.write_summary(started_at, pdfs, results, "running")
        except BaseException as exc:
            if active_pdf is not None and active_start is not None:
                error_text = type(exc).__name__
                if str(exc):
                    error_text += f": {exc}"
                results.append(
                    {
                        "status": "interrupted",
                        "input_pdf": str(active_pdf),
                        "error": error_text,
                        "log_path": str(
                            self.log_dir
                            / f"{active_pdf.stem}_{self.log_suffix}.log"
                        ),
                        "elapsed_seconds": round(
                            time.monotonic() - active_start, 3
                        ),
                    }
                )
            summary_path, _summary = self.write_summary(
                started_at,
                pdfs,
                results,
                "interrupted",
                finished_at=time.time(),
            )
            self.logger(
                "Batch interrupted; completed chunk checkpoints remain reusable"
            )
            self.logger(f"Batch summary: {summary_path}")
            raise

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
