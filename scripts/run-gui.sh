#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="/home/ocr/miniconda3/envs/mineru/bin/python"

if [[ ! -x "$python_bin" ]]; then
  echo "MinerU Python not found: $python_bin" >&2
  exit 1
fi

export PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$python_bin" -m ocr_pdf_rebuilder.gui "$@"
