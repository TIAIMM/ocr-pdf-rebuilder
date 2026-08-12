#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${OCR_PYTHON:-}"

if [[ -z "$python_bin" ]]; then
  if [[ -x "$HOME/miniconda3/envs/mineru/bin/python" ]]; then
    python_bin="$HOME/miniconda3/envs/mineru/bin/python"
  else
    python_bin="$(command -v python3 || true)"
  fi
fi

if [[ -z "$python_bin" || ! -x "$python_bin" ]]; then
  echo "MinerU Python not found. Set OCR_PYTHON to its executable path." >&2
  exit 1
fi

export PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"
export OCR_RUNTIME_ROOT="${OCR_RUNTIME_ROOT:-$repo_root}"
export PATH="$(dirname -- "$python_bin")${PATH:+:$PATH}"
exec "$python_bin" -m ocr_pdf_rebuilder.gui "$@"
