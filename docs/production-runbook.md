# Production runbook

## Preflight

```bash
${OCR_PYTHON:-python3} -m unittest discover -s tests -v
${OCR_PYTHON:-python3} ./scripts/update-production-baseline.py
./scripts/deploy-to-wsl.sh
./scripts/deploy-to-wsl.sh --apply
./scripts/verify-environment.sh
```

Record the baseline only after reviewing a passing source tree. Review the
dry-run package hashes, then deploy with `--apply`. Deployment refuses any
repository source that differs from the recorded per-file manifest.

Confirm input PDFs are under `input/` and that the output,
temporary, log and QC volumes have enough free space. These repository-local
runtime directories are intentionally ignored and must remain untracked.

## Run

```bash
PYTHONPATH=./.production/src OCR_RUNTIME_ROOT="$PWD" \
  ${OCR_PYTHON:-python3} -m ocr_pdf_rebuilder.mineru_pipeline
```

PaddleOCR-VL-first production uses the same input and reconstruction runtime,
but keeps raw results, logs and outputs separate:

```bash
./scripts/run-paddle.sh
```

The Paddle page worker and layout model run in the dedicated `paddleocr` Conda
environment. On the current NVIDIA Blackwell host, VL recognition defaults to a
local vLLM server from the `mineru` environment. The parent owns separate POSIX
process groups for the page worker and vLLM server. Normal completion calls
`PaddleOCRVL.close()` and then terminates the server group; timeout, SIGINT,
SIGTERM and SIGHUP paths terminate both groups and escalate to SIGKILL when the
configured grace period expires. `PADDLEOCR_VL_BACKEND=native` remains an
explicit override, not the current-machine default.

The batch summary is `logs_mineru/batch_summary.json`.
Individual failures are reported there and do not prevent later files from
being attempted. The process exits nonzero if the completed batch contains any
failed file.

MinerU and Paddle share one non-blocking runtime lock at
`tmp/ocr_pdf_rebuilder/task.lock`. A second GUI or CLI process reports the
current owner PID, engine and start time, then exits with code 2 without
rewriting the active task's summary. A stale `running` label in this diagnostic
file after a hard crash is harmless: the kernel lock is authoritative and the
next successful owner overwrites the metadata.

No renderer path installs packages. Missing LaTeX, dvisvgm or svglib support is
reported and falls back according to the existing formula policy. Provision
dependencies outside a production task and confirm them with
`scripts/verify-environment.sh`.

For the local browser GUI, run:

```bash
./scripts/run-gui.sh
```

The GUI preserves the same all-PDF batch and checkpoint behavior. “安全停止”
sends an interrupt to the task process group so the selected engine's bounded
worker/server cleanup can run. Closing the GUI terminal also requests cleanup;
after 30 seconds it escalates termination. Wait for the status to leave
“正在安全停止” before closing the GUI terminal when practical.

## Acceptance

- batch summary reaches `completed` or the failed files are explicitly handled;
- source and output page counts agree;
- text-only PDFs contain no raster images;
- every fallback page is blank in text-only output and imaged in the image
  variant;
- formula, table, footnote and reading-order suspect pages are reviewed from QC;
- extracted output contains no control characters. Before ReportLab draws a
  run, its selected font must cover every character; known OCR symbol
  confusions are normalized, and an otherwise unsupported code point becomes
  a visible `□` review marker instead of a hidden `.notdef` glyph that extracts
  as U+0000. Formulas whose scripts cannot be represented losslessly as Unicode
  use the vector formula path;
- `paddle_bbox_content_repaired` pages are reviewed to confirm the reference
  marker and reassigned body occupy their distinct source-layout boxes;
- any unresolved non-formula OCR block that cannot fit its bbox produces a
  blank text-only page plus a source-image page in the image variant instead of
  clipping text or aborting the batch;
- no MinerU, Paddle worker or Paddle vLLM process group remains;
- completed-state reuse succeeds on an unchanged rerun.
- a source-code change rejects old intermediate checkpoints and completion
  markers until the affected document is regenerated.

Page-count equality is necessary but does not prove visual or textual quality.
Representative rendered-page inspection remains part of production acceptance.
During step 4, the log and GUI must advance through `Render text PDF page`,
`Validate text PDF page`, and, when a companion artifact exists, `Render image
PDF page` plus `Validate image PDF page`, each with current/total page counts.
