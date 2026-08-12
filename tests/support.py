from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = REPOSITORY_ROOT / "src/ocr_pdf_rebuilder/mineru_textonly_pdf.py"


def load_pipeline(home: Path):
    previous_home = os.environ.get("HOME")
    previous_runtime_root = os.environ.get("OCR_RUNTIME_ROOT")
    os.environ["HOME"] = str(home)
    os.environ["OCR_RUNTIME_ROOT"] = str(home / "runtime")
    module_name = f"ocr_pdf_rebuilder_test_{os.getpid()}_{id(home)}"
    spec = importlib.util.spec_from_file_location(module_name, SOURCE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {SOURCE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        if previous_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = previous_home
        if previous_runtime_root is None:
            os.environ.pop("OCR_RUNTIME_ROOT", None)
        else:
            os.environ["OCR_RUNTIME_ROOT"] = previous_runtime_root
