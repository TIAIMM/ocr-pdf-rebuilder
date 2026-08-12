# document-ocr-pipeline

Resumable, failure-isolated OCR and PDF reconstruction for long multilingual
documents. The production path uses MinerU layout/OCR results and rebuilds two
page-aligned PDFs when image fallback is required:

- a text-only PDF in which handwriting, unusable scans and full-page-image
  fallback pages remain blank;
- an image variant in which those pages contain their source-page images.

The repository intentionally contains source, configuration contracts, tests,
deployment tools and documentation only. Books, generated PDFs, model weights,
fonts, logs, checkpoints and QC renders stay in `/home/ocr/ocr_jobs`.

## Baseline status

The first repository snapshot preserves the production script byte-for-byte.
Its recorded SHA-256 is:

```text
23f2bc81ffa3233e742fa47eb060918b3f9310d78adecaf52656c78f07fdd715
```

See `provenance/production-baseline.json`. Repository organization must not be
treated as proof of OCR accuracy; production promotion still requires tests,
environment verification and a same-input artifact comparison.

## Runtime

The captured production environment is:

- WSL distribution name: `Ubuntu-24.04-OCR`
- actual guest OS: Ubuntu 22.04.5 LTS
- Python 3.12.13
- MinerU 3.3.1
- MinerU2.5-Pro-2605-1.2B
- NVIDIA RTX 5070 Ti Laptop GPU, 12 GB VRAM

Python dependencies are pinned in `requirements.lock`; system packages and font
hashes are recorded under `provenance/`.

## Tests

Run the CPU regression suite inside the existing MinerU environment:

```bash
cd /home/ocr/document-ocr-pipeline
/home/ocr/miniconda3/envs/mineru/bin/python -m unittest discover -s tests -v
```

The suite uses generated temporary files and does not read production books.

Before committing, audit the exact staged set:

```bash
git add -A
./scripts/audit-staged.sh
```

## Environment verification

```bash
./scripts/verify-environment.sh
```

This is read-only. It verifies commands, package versions, fonts, GPU visibility
and the repository source hash.

## Deployment

Deployment is dry-run by default:

```bash
./scripts/deploy-to-wsl.sh
```

Apply only after tests pass:

```bash
./scripts/deploy-to-wsl.sh --apply
```

The script compiles the source, compares hashes, creates a timestamped backup
when content changes, installs through an atomic rename, and verifies the final
hash. It never copies runtime data into the repository.

Compare repository and production renderers on the same generated fixture:

```bash
/home/ocr/miniconda3/envs/mineru/bin/python \
  ./scripts/compare-synthetic-artifact.py
```

This verifies final page count, extracted text, raster-image count and rendered
pixels, including a trailing blank page. It does not invoke MinerU or read books.

## Production execution

```bash
/home/ocr/miniconda3/envs/mineru/bin/python \
  /home/ocr/ocr_jobs/mineru_textonly_pdf.py
```

Operational details and recovery rules are in `docs/production-runbook.md` and
`docs/failure-recovery.md`.

## License status

No public license has been selected. Until code provenance and third-party
notices are reviewed, this repository should remain private and no permission to
redistribute its contents is implied. MinerU, model weights and fonts retain
their own licenses and are not vendored here.
