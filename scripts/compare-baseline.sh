#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
python_bin="${OCR_PYTHON:-}"
if [[ -z "$python_bin" ]]; then
    if [[ -x "$HOME/miniconda3/envs/mineru/bin/python" ]]; then
        python_bin="$HOME/miniconda3/envs/mineru/bin/python"
    else
        python_bin="$(command -v python3 || true)"
    fi
fi
[[ -n "$python_bin" && -x "$python_bin" ]] || {
    printf 'missing Python; set OCR_PYTHON\n' >&2
    exit 2
}

production_package="${OCR_DEPLOY_ROOT:-$repo_root/.production/src/ocr_pdf_rebuilder}"
exec "$python_bin" "$repo_root/scripts/compare-package-baseline.py" \
    --repository-package "$repo_root/src/ocr_pdf_rebuilder" \
    --production-package "$production_package" \
    "$@"
