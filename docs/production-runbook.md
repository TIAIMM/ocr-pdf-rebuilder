# Production runbook

## Preflight

```bash
./scripts/verify-environment.sh
${OCR_PYTHON:-python3} -m unittest discover -s tests -v
./scripts/deploy-to-wsl.sh
```

Review the dry-run hashes, then deploy with `--apply` only when the proposed
source is the tested source.

Confirm input PDFs are under `input/` and that the output,
temporary, log and QC volumes have enough free space. Do not place inputs or
outputs inside the Git checkout.

## Run

```bash
PYTHONPATH=./src ${OCR_PYTHON:-python3} -m ocr_pdf_rebuilder.mineru_textonly_pdf
```

The batch summary is `logs_mineru/batch_summary.json`.
Individual failures are reported there and do not prevent later files from
being attempted. The process exits nonzero if the completed batch contains any
failed file.

For the local browser GUI, run:

```bash
./scripts/run-gui.sh
```

The GUI preserves the same all-PDF batch and checkpoint behavior. “安全停止”
sends an interrupt to the controller so its existing bounded MinerU process-group
cleanup can run; wait for the status to leave “正在安全停止” before closing the
GUI terminal.

## Acceptance

- batch summary reaches `completed` or the failed files are explicitly handled;
- source and output page counts agree;
- text-only PDFs contain no raster images;
- every fallback page is blank in text-only output and imaged in the image
  variant;
- formula, table, footnote and reading-order suspect pages are reviewed from QC;
- no timed-out MinerU process group remains;
- completed-state reuse succeeds on an unchanged rerun.

Page-count equality is necessary but does not prove visual or textual quality.
Representative rendered-page inspection remains part of production acceptance.
