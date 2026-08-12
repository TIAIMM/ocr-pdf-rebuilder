#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
python_bin="${OCR_PYTHON:-/home/ocr/miniconda3/envs/mineru/bin/python}"
runtime_root="${OCR_RUNTIME_ROOT:-/home/ocr/ocr_jobs}"
font_root="$runtime_root/fonts"
failures=0

pass() { printf 'PASS: %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; failures=$((failures + 1)); }

if [[ -x "$python_bin" ]]; then
    python_version="$($python_bin -c 'import platform; print(platform.python_version())')"
    [[ "$python_version" == 3.12.* ]] && pass "Python $python_version" || fail "expected Python 3.12.x, got $python_version"
    export PATH="$(dirname -- "$python_bin"):$PATH"
else
    fail "missing Python executable $python_bin"
fi

for command in git mineru pdftoppm pdfinfo pdffonts dvisvgm latex sha256sum; do
    command -v "$command" >/dev/null 2>&1 && pass "command $command" || fail "missing command $command"
done

if [[ -x "$python_bin" ]]; then
    "$python_bin" - "$repo_root" <<'PY' || failures=$((failures + 1))
import importlib.metadata as metadata
import pathlib
import sys

expected = {
    "mineru": "3.3.1",
    "PyMuPDF": "1.27.2.3",
    "Pillow": "12.2.0",
    "reportlab": "4.5.1",
    "svglib": "2.0.2",
}
bad = False
for package, wanted in expected.items():
    try:
        actual = metadata.version(package)
    except metadata.PackageNotFoundError:
        print(f"FAIL: package {package} is missing", file=sys.stderr)
        bad = True
        continue
    if actual != wanted:
        print(f"FAIL: package {package} expected {wanted}, got {actual}", file=sys.stderr)
        bad = True
    else:
        print(f"PASS: package {package}=={actual}")

source = pathlib.Path(sys.argv[1]) / "src/document_ocr_pipeline/mineru_textonly_pdf.py"
compile(source.read_text(encoding="utf-8"), str(source), "exec")
print("PASS: repository production source compiles")
raise SystemExit(1 if bad else 0)
PY
fi

if [[ -d "$font_root" ]]; then
    if (cd "$font_root" && sha256sum -c "$repo_root/provenance/fonts.sha256"); then
        pass "runtime font hashes"
    else
        fail "runtime font hashes"
    fi
else
    fail "missing runtime font directory $font_root"
fi

if command -v nvidia-smi >/dev/null 2>&1; then
    gpu_line="$(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader | head -1)"
    [[ -n "$gpu_line" ]] && pass "GPU $gpu_line" || fail "nvidia-smi returned no GPU"
else
    fail "nvidia-smi is unavailable"
fi

if "$repo_root/scripts/compare-baseline.sh"; then
    pass "repository/production baseline comparison"
else
    fail "repository/production baseline comparison"
fi

if (( failures > 0 )); then
    printf 'Environment verification failed with %d issue(s).\n' "$failures" >&2
    exit 1
fi
printf 'Environment verification completed successfully.\n'
