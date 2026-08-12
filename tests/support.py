from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = REPOSITORY_ROOT / "src/ocr_pdf_rebuilder/mineru_textonly_pdf.py"


def load_pipeline(home: Path):
    os.environ["HOME"] = str(home)
    module_name = f"ocr_pdf_rebuilder_test_{os.getpid()}_{id(home)}"
    spec = importlib.util.spec_from_file_location(module_name, SOURCE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {SOURCE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
