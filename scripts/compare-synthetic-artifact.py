#!/usr/bin/env python3
"""Compare repository and deployed production renderers on one safe fixture."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

import fitz


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPOSITORY_SOURCE = (
    REPOSITORY_ROOT / "src/ocr_pdf_rebuilder/mineru_textonly_pdf.py"
)
DEFAULT_PRODUCTION_SOURCE = DEFAULT_REPOSITORY_SOURCE
DEFAULT_FONT_ROOT = REPOSITORY_ROOT / "fonts"


def load_module(name: str, source: Path, home: Path):
    previous_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        spec = importlib.util.spec_from_file_location(name, source)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"could not load {source}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if previous_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = previous_home


def configure_fonts(module, font_root: Path):
    module.CJK_REGULAR_FONT = font_root / "SourceHanSerifCN-Regular.ttf"
    module.CJK_MEDIUM_FONT = font_root / "SourceHanSerifCN-Medium.ttf"
    module.CJK_BOLD_FONT = font_root / "SourceHanSerifCN-Bold.ttf"
    module.LATIN_REGULAR_FONT = font_root / "SourceSerif4-Regular.ttf"
    module.LATIN_BOLD_FONT = font_root / "SourceSerif4-Bold.ttf"
    module.LATIN_ITALIC_FONT = font_root / "SourceSerif4-It.ttf"
    module.REPORTLAB_FONTS_REGISTERED = False


def fixture_page_specs():
    text_block = {
        "category": "Text",
        "order": 0,
        "left": 36.0,
        "top": 36.0,
        "width": 228.0,
        "height": 90.0,
        "text": "Repository baseline 中文 English Ελληνικά 123.",
        "font_size": 11.0,
        "min_font_size": 8.6,
        "line_height": 1.2,
        "line_info": [],
    }
    return [
        (300.0, 400.0, [text_block]),
        # A trailing blank page is intentional and guards the historical loss.
        (300.0, 400.0, []),
    ]


def render(module, output: Path, work: Path):
    module.render_blocks_to_pdf_reportlab(
        fixture_page_specs(),
        output,
        formula_work_dir=work / "formula-cache",
        include_full_page_images=False,
        allow_formula_image_fallback=False,
    )


def artifact_signature(path: Path):
    page_signatures = []
    with fitz.open(path) as document:
        for page in document:
            pixmap = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
            page_signatures.append(
                {
                    "width": round(page.rect.width, 3),
                    "height": round(page.rect.height, 3),
                    "text": page.get_text().strip(),
                    "image_count": len(page.get_images(full=True)),
                    "render_sha256": hashlib.sha256(pixmap.samples).hexdigest(),
                }
            )
    return {
        "page_count": len(page_signatures),
        "pages": page_signatures,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-source", type=Path, default=DEFAULT_REPOSITORY_SOURCE)
    parser.add_argument("--production-source", type=Path, default=DEFAULT_PRODUCTION_SOURCE)
    parser.add_argument("--font-root", type=Path, default=DEFAULT_FONT_ROOT)
    args = parser.parse_args()

    for path in (args.repository_source, args.production_source):
        if not path.is_file():
            parser.error(f"missing source file: {path}")
    if not args.font_root.is_dir():
        parser.error(f"missing font directory: {args.font_root}")

    with tempfile.TemporaryDirectory(prefix="document-ocr-artifact-compare-") as temp:
        root = Path(temp)
        repository_module = load_module(
            "repository_artifact_renderer", args.repository_source, root / "repo-home"
        )
        production_module = load_module(
            "production_artifact_renderer", args.production_source, root / "prod-home"
        )
        configure_fonts(repository_module, args.font_root)
        configure_fonts(production_module, args.font_root)

        repository_pdf = root / "repository.pdf"
        production_pdf = root / "production.pdf"
        render(repository_module, repository_pdf, root / "repository-work")
        render(production_module, production_pdf, root / "production-work")

        repository_signature = artifact_signature(repository_pdf)
        production_signature = artifact_signature(production_pdf)
        result = {
            "fixture": "multilingual text plus trailing blank page",
            "repository_source_sha256": hashlib.sha256(
                args.repository_source.read_bytes()
            ).hexdigest(),
            "production_source_sha256": hashlib.sha256(
                args.production_source.read_bytes()
            ).hexdigest(),
            "repository_artifact": repository_signature,
            "production_artifact": production_signature,
            "equivalent": repository_signature == production_signature,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["equivalent"]:
            raise SystemExit("FAIL: repository and production artifacts differ")
        if repository_signature["page_count"] != 2:
            raise SystemExit("FAIL: trailing blank page was lost")
        if repository_signature["pages"][1]["text"]:
            raise SystemExit("FAIL: trailing page is not blank")
        print("PASS: repository and production renderers are artifact-equivalent")


if __name__ == "__main__":
    main()
