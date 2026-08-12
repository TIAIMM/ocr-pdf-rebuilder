"""MinerU production CLI built on the shared OCR/PDF pipeline runtime."""

from . import pipeline_runtime as runtime
from .batch_runner import PdfBatchRunner
from .signal_cleanup import termination_raises_keyboard_interrupt


def pdf_batch_runner() -> PdfBatchRunner:
    return PdfBatchRunner(
        input_dir=runtime.INPUT_DIR,
        output_dir=runtime.OUTPUT_DIR,
        mineru_output_dir=runtime.MINERU_OUTPUT_DIR,
        log_dir=runtime.LOG_DIR,
        process_pdf=runtime.process_pdf,
        write_checkpoint=runtime.write_checkpoint,
        logger=runtime.log,
    )


def write_batch_summary(started_at, pdfs, results, status, finished_at=None):
    return pdf_batch_runner().write_summary(
        started_at,
        pdfs,
        results,
        status,
        finished_at=finished_at,
    )


def main() -> None:
    try:
        with termination_raises_keyboard_interrupt():
            pdf_batch_runner().run()
    except KeyboardInterrupt:
        runtime.log("MinerU task interrupted; child process cleanup completed")
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
