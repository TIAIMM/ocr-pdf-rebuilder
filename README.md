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

Only one MinerU or Paddle batch may use a runtime root at a time. Both CLI and
GUI jobs acquire `tmp/ocr_pdf_rebuilder/task.lock`; a competing process exits
without touching the active batch summary or output tree. The operating system
releases the advisory lock automatically after normal exit, interruption or a
crash.

## Repository snapshot

The complete `ocr_pdf_rebuilder` source package has a deterministic, per-file
SHA-256 manifest in `provenance/production-baseline.json`. The original external
runtime tree has been retired. Production code is atomically promoted to the
ignored `.production/src/ocr_pdf_rebuilder` snapshot, while inputs, outputs,
fonts, logs and checkpoints continue to live under this repository root.

Repository organization must not be
treated as proof of OCR accuracy; production promotion still requires tests,
environment verification and a same-input artifact comparison.

## Pipeline modules

`ocr_pdf_rebuilder.pipeline_runtime` wires shared reconstruction dependencies
and retains compatibility helpers. `mineru_pipeline` and
`paddle_textonly_pdf` are thin engine-specific CLI entry points. Focused
components have no dependency back to either CLI entry point:

- `pipeline_config.py`: runtime paths, production settings and shared patterns;
- `runtime_state.py`: source/runtime fingerprints, checkpoints and completed
  output validation;
- `process_control.py`: streamed child-process execution, timeout handling and
  descendant process-group cleanup;
- `mineru_runner.py`: MinerU command construction, retries, page chunking and
  resumable parser runs;
- `mineru_results.py`: MinerU JSON selection, normalization and page-result
  loading;
- `text_processing.py`: OCR text cleanup, Markdown/LaTeX normalization and
  formula preparation;
- `page_recovery.py`: bad-page retries, forward-page leak repair and safe image
  or source-text fallbacks;
- `layout_engine.py`: cell-to-block conversion, fitting, reflow and Markdown
  reconstruction;
- `reportlab_renderer.py`: font selection, measurement and PDF drawing;
- `qc_reporting.py`: suspect-page analysis, debug artifacts and QC reports;
- `pipeline_orchestrator.py`: one-document production flow and workspace
  lifecycle;
- `paddle_worker.py`: dedicated-environment PaddleOCR-VL model process and
  normalized per-page JSON checkpoints;
- `paddle_pipeline.py`: Paddle runtime identity, retries, recovery and output
  orchestration on top of the shared renderer;
- `batch_runner.py`: multi-file iteration, failure isolation and atomic batch
  summary updates;
- `forward_page_leak.py`: source-page overlap detection and isolated-retry
  validation for forward-page content leaks;
- `pdf_validation.py`: read-only page-count, image, blank-page, character and
  markup validation of generated artifacts;
- `gui.py`: local HTTP controller, per-file progress parsing and browser UI.

`component_runtime.py` supplies the small dependency-injection compatibility
bridge used by extracted components. The entry module keeps established helper
names, including test and diagnostic monkeypatch points, while component
implementations remain independently importable and compile-testable.

## Runtime

The captured production environment is:

- actual guest OS: Ubuntu 22.04.5 LTS
- Python 3.12.13
- MinerU 3.3.1
- MinerU2.5-Pro-2605-1.2B
- NVIDIA RTX 5070 Ti Laptop GPU, 12 GB VRAM

Python dependencies are pinned in `requirements.lock`; system packages and font
hashes are recorded under `provenance/`.

Rendering is environment-read-only. It never invokes `apt-get`, `sudo` or
`pip install`. Missing formula-vector dependencies are reported and handled by
the existing text/image fallback; install or repair dependencies explicitly
during environment provisioning, then rerun the read-only preflight.

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
and byte identity between the recorded baseline, repository package and
deployed production package.

## Deployment

After the tests pass and the source change is reviewed, explicitly record the
complete package baseline:

```bash
${OCR_PYTHON:-python3} ./scripts/update-production-baseline.py
```

Deployment is dry-run by default and refuses source that does not match that
baseline:

```bash
./scripts/deploy-to-wsl.sh
```

Apply only after tests pass:

```bash
./scripts/deploy-to-wsl.sh --apply
```

The script compiles every shipped Python file, stages the complete package,
creates a timestamped backup when content changes, installs the package through
an atomic directory rename, and verifies the final package manifest. It never
copies runtime data into the production snapshot.

Compare repository and production renderers on the same generated fixture:

```bash
${OCR_PYTHON:-python3} ./scripts/compare-synthetic-artifact.py
```

This verifies final page count, extracted text, raster-image count and rendered
pixels, including a trailing blank page. It does not invoke MinerU or read books.

## Production execution

From the repository root:

```bash
PYTHONPATH=./.production/src OCR_RUNTIME_ROOT="$PWD" \
  ${OCR_PYTHON:-python3} -m ocr_pdf_rebuilder.mineru_pipeline
```

Run the PaddleOCR-VL-first pipeline with the shared renderer environment; its
worker automatically uses `~/miniconda3/envs/paddleocr/bin/python`:

```bash
./scripts/run-paddle.sh
```

Both engines read `input/`. MinerU writes to `pdf_mineru/`, `mineru_output/`
and `logs_mineru/`; Paddle writes independently to `pdf_paddle/`,
`paddle_output/` and `logs_paddle/`. Paddle uses PP-DocLayoutV3 plus
PaddleOCR-VL-1.6. On the current NVIDIA Blackwell host, recognition defaults to
a local vLLM OpenAI-compatible server from the `mineru` environment; the
`paddleocr` environment owns layout detection and the page worker. Set
`PADDLEOCR_VL_BACKEND=native` only for an explicitly tested native-inference
host. The pipeline checkpoints every page, retries suspicious pages against the
same server session, and then uses the same cross-page checks,
ReportLab renderer, text-only/image-variant contract and PDF validators.

Operational details and recovery rules are in `docs/production-runbook.md` and
`docs/failure-recovery.md`.

## Local GUI

Start the lightweight local web interface inside WSL:

From the repository root:

```bash
./scripts/run-gui.sh
```

Open `http://localhost:18765/` if the browser does not open automatically. The
GUI lists PDFs from `input/`, lets the operator select MinerU or PaddleOCR-VL,
starts the selected repository batch, streams its console output, requests safe
process-group cleanup when stopped, displays `batch_summary.json`, and offers generated PDFs
for download. Per-file progress shows the active stage, weighted total percent,
and current/total page count. It binds to localhost by default and has no upload
or delete operation.

## License status

No public license has been selected. Until code provenance and third-party
notices are reviewed, this repository should remain private and no permission to
redistribute its contents is implied. MinerU, model weights and fonts retain
their own licenses and are not vendored here.
