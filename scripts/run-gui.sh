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

source_root="${OCR_SOURCE_ROOT:-$repo_root/.production/src}"
if [[ ! -f "$source_root/ocr_pdf_rebuilder/__init__.py" ]]; then
  echo "Deployed OCR package not found: $source_root/ocr_pdf_rebuilder" >&2
  echo "Run ./scripts/deploy-to-wsl.sh --apply after recording the reviewed baseline." >&2
  exit 1
fi

export PYTHONPATH="$source_root${PYTHONPATH:+:$PYTHONPATH}"
export OCR_RUNTIME_ROOT="${OCR_RUNTIME_ROOT:-$repo_root}"
export PATH="$(dirname -- "$python_bin")${PATH:+:$PATH}"
exec "$python_bin" -m ocr_pdf_rebuilder.gui "$@"
