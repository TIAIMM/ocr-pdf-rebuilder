# Architecture

## Boundary

The Git repository is the immutable source-and-test layer. The runtime tree at
`/home/ocr/ocr_jobs` is the mutable data layer. Input PDFs, raw MinerU output,
checkpoints, logs, QC renders, final PDFs, models and fonts belong only to the
runtime layer.

## Production flow

1. Enumerate input PDFs without allowing one file failure to abort later files.
2. Fingerprint the source, parser configuration, MinerU runtime and model cache.
3. Split long documents into tasks of at most 60 pages.
4. Run MinerU in a new POSIX process group with total and idle timeouts.
5. Retry recognized transient failures before considering recursive page splits.
6. Prefer `_middle.json`; recover block, line, span, cell, bbox, formula, table
   HTML, footnote and reading-order information.
7. Retry bad pages or table-fit failures through narrowly scoped routes.
8. Rebuild page-aligned PDFs with ReportLab.
9. Keep unusable fallback pages blank in the text-only PDF and render the source
   page only in the optional image variant.
10. Validate page count, images, blank fallback pages, text residues and artifact
    integrity before writing completed state.

## Stability invariants

- A failed source file produces a failed batch entry but later files still run.
- MinerU descendants must not survive timeout, interruption or parent exit.
- A transient infrastructure failure does not immediately trigger recursive
  splitting.
- Checkpoints are checksummed and tied to source, configuration and runtime.
- Completed output reuse requires all artifact signatures and page counts.
- Text-only output never silently embeds source-page raster images.
- Both output variants preserve source page count, including trailing blanks.

The baseline remains a single module for the initial repository snapshot. Split
it only behind regression tests so version-control work is not conflated with
OCR behavior changes.
