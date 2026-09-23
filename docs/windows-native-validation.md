# Windows native PaddleOCR-VL GPU validation

Historical validation of a separate Windows checkout. The active project is
the WSL checkout; Windows native inference is a dormant, manually restorable
fallback. The Windows virtual environments, model copies, outputs, OCR page
results, logs and benchmark artifacts were **not** migrated to WSL. This
document retains the earlier measurements and validation boundaries.
The WSL production model cache is independent and remains required for the
active WSL route.

## Runtime and model files

The two inference engines use separate Python 3.12 environments:

| Engine | Environment | Main packages |
| --- | --- | --- |
| PaddlePaddle | `.venv-paddle` | `paddlepaddle-gpu 3.3.1` (CUDA 12.9), `paddleocr 3.7.0`, `paddlex 3.7.2` |
| Transformers | `.venv-transformers` | `torch 2.11.0+cu128`, `torchvision 0.26.0+cu128`, `transformers 5.11.0`, `paddleocr 3.7.0`, `paddlex 3.7.2` |

The inactive Windows checkout's `models/` directory held PaddleOCR-VL-1.6 and
both formats of PP-DocLayoutV3. The Paddle-format models were copied from the
WSL cache. The Transformers-format layout model came from the official
`PaddlePaddle/PP-DocLayoutV3_safetensors` repository on Hugging Face. The
PaddleOCR-VL `model.safetensors` copy has the same SHA-256 as the WSL file:
`85a479d506a11e724e7285d395c551be69f41dbc16b6342d3cacfb189aed71db`.
Those Windows model copies, environments and benchmark artifacts were not
retained in WSL.

To restore this optional route later, first make a separate Windows-local
checkout of the current project. With `uv` installed on Windows, recreate its
environment explicitly (nothing downloads during ordinary WSL operation):

```powershell
uv venv .venv-paddle --python 3.12 --managed-python
uv pip install --python .\.venv-paddle\Scripts\python.exe 'https://paddle-whl.cdn.bcebos.com/stable/cu129/paddlepaddle-gpu/paddlepaddle_gpu-3.3.1-cp312-cp312-win_amd64.whl'
uv pip install --python .\.venv-paddle\Scripts\python.exe 'paddleocr[doc-parser]==3.7.0' 'PyMuPDF==1.27.2.3'

uv venv .venv-transformers --python 3.12 --managed-python
uv pip install --python .\.venv-transformers\Scripts\python.exe 'torch==2.11.0+cu128' 'torchvision==0.26.0+cu128' --index-url 'https://download.pytorch.org/whl/cu128'
uv pip install --python .\.venv-transformers\Scripts\python.exe 'paddleocr[doc-parser]==3.7.0' 'transformers==5.11.0' 'PyMuPDF==1.27.2.3'
```

Obtain `PP-DocLayoutV3`, `PaddleOCR-VL-1.6` and
`PaddlePaddle/PP-DocLayoutV3_safetensors` again when needed, placing them in
the new Windows checkout's `models/` directory. Provision the required fonts
there too. `scripts\setup-windows.ps1` and `scripts\run-windows.ps1` refuse to
run without the local models; they do not silently download them. The
Transformers environment needs `torchvision` to load the layout image
processor. If the CLI uses a SOCKS proxy, install `httpx[socks]` in that
environment first.

## Reproduce the page benchmarks

Use PowerShell from a newly restored Windows checkout, if the optional route
is enabled again:

```powershell
& .\.venv-paddle\Scripts\python.exe .\scripts\benchmark_windows_native.py --engine paddle --pdf .\input\Le_Gout_du_secret.pdf --pages 10,30,75 --timeout-seconds 600
& .\.venv-transformers\Scripts\python.exe .\scripts\benchmark_windows_native.py --engine transformers --pdf .\input\Le_Gout_du_secret.pdf --pages 10,30,75 --timeout-seconds 600
```

Both commands use a 200 DPI page render, the same PaddleOCR-VL-1.6 weights,
`max_new_tokens=2048`, and `gpu:0`. Each starts a new process and loads both
models once for all selected pages. When run, the scripts write normalized per-page JSON,
raw model output, rendered page PNGs, logs and `summary.json` under
`artifacts/windows-native-benchmark/`. `gpu_memory_peak_mib` is the sampled
memory used by the entire GPU, including other processes. It is not isolated
process VRAM.
The timeout stops the complete Windows worker process tree and preserves any
pages that finished before the limit.

To run the existing WSL vLLM route on the same copied PDF, use the runtime's
Python environment from the WSL repository root:

```bash
python scripts/benchmark_wsl_vllm.py --pdf input/Le_Gout_du_secret.pdf --output-root artifacts/wsl-vllm-benchmark --pages 10,30,75
```

This wrapper starts and terminates the repository's vLLM process group. It
sets GPU memory utilization to `0.65` for this process because the 12 GB GPU
has concurrent baseline usage. It reports server startup and page-worker time
separately. The source PDF and copied test PDF have identical SHA-256:
`522d83560be92584f5bcfa66200d80a1c5335458908181edd592d7b464f43278`.

## Validation status

PaddlePaddle and Transformers have both completed real Windows GPU inference
for page 21 of `II.13.pdf`, each producing 15 normalized blocks. The existing
WSL PaddlePaddle route produced 15 blocks with all text fields matching the
Windows PaddlePaddle output. The Windows Transformers route matched 14 of 15
blocks exactly; the remaining block differed only in two apostrophe glyphs.

The Windows page-21 result files were not migrated and remain only in that
checkout. A
single-page cold-start measurement includes environment import and model
loading, so it is insufficient for a throughput ranking. The three-page
`Le_Gout_du_secret.pdf` measurement is the main comparison for this checkout.

The historical Windows pipeline used native Transformers GPU inference with
repository-local model files. The full three-page reconstruction test for
source pages 10, 30 and 75 completed, producing Markdown, text-only PDF,
source-image variant and searchable PDF. All three searchable pages rendered
pixel-identically to the source at the checked resolution. The source-image
variant looked aligned in visual inspection, though re-embedding introduces
minor pixel differences. The text-only PDF has visible line-width and wrap
differences on page 10, so it is not certified as a faithful facsimile.
Full-book validation is tracked separately from this bounded preflight.

### Full-book Windows reconstruction

The 138-page `Le_Gout_du_secret.pdf` completed on Windows Transformers in
44m01s (the per-document pipeline timer, including recognition, reconstruction
and artifact validation). Markdown, text-only, image-variant and searchable
outputs exist, and all three PDFs have 138 pages. An independent render
comparison found zero pixel-mismatch pages between the source and searchable
PDF across all 138 pages. The image variant was visually checked on source
pages 7, 75 and 138. Seven nonblank pages lacked usable OCR and were handled
as source-image fallback pages; these are not evidence of recognized text.
The text-only variant is not a typographic facsimile and still needs visual
review for line wrapping and word placement. The WSL whole-book comparison is
run separately so its GPU work cannot overlap the Windows measurement.

### Whole-book Windows versus WSL comparison

Both routes completed the same 138-page PDF, SHA-256
`522d83560be92584f5bcfa66200d80a1c5335458908181edd592d7b464f43278`.
Their PaddleOCR-VL recognition `model.safetensors` files also have matching
SHA-256 `85a479d506a11e724e7285d395c551be69f41dbc16b6342d3cacfb189aed71db`.
Both used 200 DPI, GPU 0, PaddleOCR-VL 1.6 and 2048 maximum new tokens;
Windows used native Transformers with the safetensors layout model, while WSL
used Paddle layout inference and a locally owned vLLM recognition server at
GPU-memory utilization 0.65. Runs did not overlap and did not share caches.

| Route | End-to-end time | Throughput | Detail |
| --- | ---: | ---: | --- |
| Windows native Transformers | 44m01s | 3.14 pages/min | OCR, reconstruction and validation |
| WSL vLLM backup | 10m01.3s | 13.77 pages/min | vLLM readiness wait 114.3s, OCR worker 272.4s, reconstruction 167.0s |

WSL was 4.39 times faster end to end on this book. The earlier three-page
result favored Windows because vLLM startup dominated that short run; it did
not predict whole-book throughput. Both routes produced Markdown and three
138-page PDFs. Independent full-book render checks found no source/searchable
pixel mismatches on either route. Normalized results have the same block count
on all 138 pages (712 blocks total); 667 aligned category/text pairs are
identical and 45 differ, spread across 41 pages. These differences include
punctuation, accented characters and some content; without ground truth this
does not establish which route is more accurate. Both have seven nonblank
pages without usable OCR, represented through source-image fallback.

The WSL timing record is kept in `artifacts/wsl-fullbook-compare.json`; its
vLLM startup used an existing compilation cache. Windows was also run after the
three-page warm-up. Timings reflect these actual local states, not a controlled
fresh-install cold-start benchmark.

### 2026-09-23 document comparison

The test PDF has 138 pages. Pages 10, 30 and 75 cover a footnote, dense text,
and mixed roman/italic text. All routes used the same input bytes, 200 DPI,
PaddleOCR-VL-1.6 weights and 2048 maximum new tokens.

| Route | Completed | Elapsed | Detail | Sampled GPU memory |
| --- | ---: | ---: | --- | ---: |
| Windows PaddlePaddle | 1/3 | 620.67 s, manually stopped | Page 10 finished at 55.47 s; page 30 did not finish within the observation window | 11,885 MiB peak; 2,007 MiB before |
| Windows Transformers | 3/3 | 71.97 s | Page finish times: 44.99, 59.33, 70.80 s; 4, 4, 6 blocks | 3,887 MiB peak; 840 MiB before |
| WSL vLLM server | 3/3 | 115.30 s | Server ready at 74.19 s; page worker took 38.60 s; 4, 4, 6 blocks | Not sampled in this run |

The Windows PaddlePaddle process was terminated after sustained GPU activity
near the 12 GB limit. Its page-10 checkpoint and worker log were retained.
This is a timeout observation, not an out-of-memory error. These three pages
do not establish whole-book throughput. GPU memory figures include other
processes and differ in their pre-run baseline. The vLLM route used an already
populated compilation cache; server initialization varied between runs.
A separate Windows PaddlePaddle attempt on page 75 left only a worker log and
rendered page image; it produced no page result or summary, and no process
remained. Its exit cause is undetermined, so it is excluded from the timing
table.

All 14 output blocks had matching ordered categories between Windows
Transformers and WSL vLLM. Thirteen blocks had identical text. The remaining
block had the same length and differed only in 11 curly-versus-straight
apostrophes. Windows PaddlePaddle and Transformers produced identical text
and categories for all four blocks on page 10.

After copying the repository's provisioned font files into the Windows
checkout and installing `reportlab 4.5.1` and `svglib 2.0.2`, the focused
`test_paddle_pipeline.py` suite completed: 20 passed, one skipped.

Transformers remains the validated native GPU fallback on this machine, but
it is not installed or launched by the active WSL project. The WSL vLLM route
is the default, particularly for long books where its throughput dominated.
PaddlePaddle direct inference needs a separate investigation of the dense-page
stall before relying on it for long books.
