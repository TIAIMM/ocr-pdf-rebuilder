# Architecture

## Boundary

Source and mutable runtime data share one repository root. Input PDFs, raw OCR
output, checkpoints, logs, QC renders, final PDFs and fonts live in
explicit top-level runtime directories and are excluded by `.gitignore`. Source,
configuration, tests and documentation remain tracked; runtime artifacts must
never be staged.

## Shared production flow

1. Enumerate input PDFs without allowing one file failure to abort later files.
2. Fingerprint the source, parser configuration, selected engine runtime and
   model cache.
3. Run the selected OCR engine in a new POSIX process group with total and idle
   timeouts.
4. Normalize engine results into page-local cells with category, bbox, text and
   reading order.
5. Detect repeated pseudotext, missing pages and forward-page content leaks;
   retry narrowly before selecting a safe fallback.
6. Rebuild page-aligned PDFs with ReportLab.
7. Keep unusable fallback pages blank in the text-only PDF and render the source
   page only in the optional image variant.
8. Overlay the original scans with an invisible OCR text layer to build the
   always-produced searchable variant.
9. Validate page count, images, blank fallback pages, text residues, searchable
   text presence and pixel identity, and artifact integrity before writing
   completed state.

## Stability invariants

- A failed source file produces a failed batch entry but later files still run.
- OCR descendants must not survive normal completion, timeout, interruption or
  parent exit.
- A transient infrastructure failure does not immediately trigger recursive
  splitting.
- Checkpoints are checksummed and tied to source, configuration and runtime.
- Completed output reuse requires all artifact signatures and page counts.
- Text-only output never silently embeds source-page raster images.
- Both output variants preserve source page count, including trailing blanks.
- The searchable variant preserves source page count and rendered pixels; its
  invisible layer only adds extractable text.

MinerU additionally chunks long PDFs and prefers `_middle.json` block/line/span
data. It also emits the searchable variant, which places invisible text from
those span/line boxes onto the untouched source pages and validates page count,
text presence and pixel identity against the source. All chunks and repair
passes for one document share one lazy preloaded
`mineru-api` process at concurrency one. The API is connected to an official
stdin-EOF shutdown channel and owns a process group; transient CUDA/vLLM engine
death restarts that whole service within a bounded retry budget. PaddleOCR-VL
keeps one page worker per document, uses one managed local vLLM server session
on the current Blackwell host, processes individual pages, and atomically
checkpoints normalized page JSON. Owned workers and inference servers are
reclaimed on every exit path. Both engines converge on the same layout,
rendering, QC and artifact-validation stack.
