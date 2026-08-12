# Benchmark and comparison policy

Repository tests protect deterministic control-flow and output invariants; they
do not establish OCR model quality.

Before promoting a behavior-changing revision, compare old and new versions on
the same source set and record:

- exit status and per-file batch status;
- source/output page-count equality;
- fallback and suspect page sets;
- text-only raster-image count;
- formula rendering failures and LaTeX residue;
- nested-table and footnote preservation;
- elapsed time, peak VRAM, retry count and timeout count;
- SHA-256 of unchanged artifacts where byte identity is expected;
- rendered-page review for representative normal, formula, table, multilingual,
  low-quality scan, handwriting and trailing-blank pages.

For the initial repository import, two gates apply:

- `scripts/compare-baseline.sh` requires repository and production source to be
  byte-identical;
- `scripts/compare-synthetic-artifact.py` renders the same multilingual fixture
  through both copies and compares page count, extracted text, raster-image
  count and rendered pixels, including a trailing blank page.

Neither gate invokes MinerU or reads production books.
