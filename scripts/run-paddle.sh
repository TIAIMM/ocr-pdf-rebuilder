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
paddle_python="${PADDLEOCR_PYTHON:-$HOME/miniconda3/envs/paddleocr/bin/python}"
backend="${PADDLEOCR_VL_BACKEND:-vllm-server}"
vllm_python="${PADDLEOCR_VLLM_PYTHON:-$HOME/miniconda3/envs/mineru/bin/python}"

[[ -n "$python_bin" && -x "$python_bin" ]] || {
    printf 'Shared pipeline Python was not found; set OCR_PYTHON.\n' >&2
    exit 1
}
[[ -x "$paddle_python" ]] || {
    printf 'PaddleOCR Python was not found; set PADDLEOCR_PYTHON.\n' >&2
    exit 1
}
if [[ "$backend" == "vllm-server" && ! -x "$vllm_python" ]]; then
    printf 'vLLM Python was not found; set PADDLEOCR_VLLM_PYTHON or use an explicitly tested backend.\n' >&2
    exit 1
fi

export PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"
export OCR_RUNTIME_ROOT="${OCR_RUNTIME_ROOT:-$repo_root}"
export PADDLEOCR_PYTHON="$paddle_python"
export PADDLEOCR_VL_BACKEND="$backend"
export PADDLEOCR_VLLM_PYTHON="$vllm_python"
exec "$python_bin" -m ocr_pdf_rebuilder.paddle_textonly_pdf "$@"
