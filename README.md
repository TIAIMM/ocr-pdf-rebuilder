# ocr-pdf-rebuilder

Resumable, failure-isolated OCR and PDF reconstruction for long multilingual
documents. The production path uses MinerU layout/OCR results and rebuilds two
page-aligned PDFs when image fallback is required:

- a text-only PDF in which handwriting, unusable scans and full-page-image
  fallback pages remain blank;
- an image variant in which those pages contain their source-page images.

Runtime directories now live directly under this repository. Books, generated
PDFs, fonts, logs, checkpoints and QC renders are excluded by `.gitignore`; they
remain local and must not be staged or committed.

## Repository snapshot

The current repository renderer has a recorded SHA-256 in
`provenance/production-baseline.json`. The original external runtime tree has
been retired; the renderer and all ignored runtime directories now share this
repository root.

Repository organization must not be
treated as proof of OCR accuracy; production promotion still requires tests,
environment verification and a same-input artifact comparison.

## Runtime

The captured production environment is:

- actual guest OS: Ubuntu 22.04.5 LTS
- Python 3.12.13
- MinerU 3.3.1
- MinerU2.5-Pro-2605-1.2B
- NVIDIA RTX 5070 Ti Laptop GPU, 12 GB VRAM

Python dependencies are pinned in `requirements.lock`; system packages and font
hashes are recorded under `provenance/`.

## Tests

Run the CPU regression suite inside the existing MinerU environment:

From the repository root:

```bash
${OCR_PYTHON:-python3} -m unittest discover -s tests -v
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
${OCR_PYTHON:-python3} ./scripts/compare-synthetic-artifact.py
```

This verifies final page count, extracted text, raster-image count and rendered
pixels, including a trailing blank page. It does not invoke MinerU or read books.

## Production execution

From the repository root:

```bash
PYTHONPATH=./src ${OCR_PYTHON:-python3} -m ocr_pdf_rebuilder.mineru_textonly_pdf
```

Operational details and recovery rules are in `docs/production-runbook.md` and
`docs/failure-recovery.md`.

## Local GUI

Start the lightweight local web interface inside WSL:

From the repository root:

```bash
./scripts/run-gui.sh
```

Open `http://localhost:18765/` if the browser does not open automatically. The
GUI lists PDFs from `input/`, starts the repository
batch, streams its console output, requests the existing safe process-group
cleanup when stopped, displays `batch_summary.json`, and offers generated PDFs
for download. It binds to localhost by default and has no upload or delete
operation.

## License status

No public license has been selected. Until code provenance and third-party
notices are reviewed, this repository should remain private and no permission to
redistribute its contents is implied. MinerU, model weights and fonts retain
their own licenses and are not vendored here.
