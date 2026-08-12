# Production runbook

## Preflight

```bash
cd /home/ocr/document-ocr-pipeline
./scripts/verify-environment.sh
/home/ocr/miniconda3/envs/mineru/bin/python -m unittest discover -s tests -v
./scripts/deploy-to-wsl.sh
```

Review the dry-run hashes, then deploy with `--apply` only when the proposed
source is the tested source.

Confirm input PDFs are under `/home/ocr/ocr_jobs/input` and that the output,
temporary, log and QC volumes have enough free space. Do not place inputs or
outputs inside the Git checkout.

## Run

```bash
/home/ocr/miniconda3/envs/mineru/bin/python \
  /home/ocr/ocr_jobs/mineru_textonly_pdf.py
```

The batch summary is `/home/ocr/ocr_jobs/logs_mineru/batch_summary.json`.
Individual failures are reported there and do not prevent later files from
being attempted. The process exits nonzero if the completed batch contains any
failed file.

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
