import json
from pathlib import Path
from unittest.mock import patch

import fitz

from ocr_pdf_rebuilder import searchable_backfill


def test_backfill_uses_selected_cache_and_final_fallback_policy(tmp_path):
    paddle = searchable_backfill.paddle
    source = tmp_path / "source.pdf"
    with fitz.open() as doc:
        for _ in range(2):
            doc.new_page(width=300, height=400).insert_text((40, 60), "Original source caption")
        doc.save(source)
    state = tmp_path / "state"
    raw = state / "raw/source/page_results"
    raw.mkdir(parents=True)
    for i, text in enumerate(["Rejected cached OCR", "Preserved cached OCR"]):
        (raw / f"page_{i+1:04d}.json").write_text(json.dumps({
            "page_index": i, "engine": "PaddleOCR-VL", "image_size": None,
            "cells": [{"bbox": [40, 100, 260, 140], "category": "Text",
                       "text": text, "__bbox_units": "pdf"}]}))
    paddle.shared.write_checkpoint(state / "tmp/source/job_state.json", {
        "source": paddle.shared.source_file_signature(source)})
    logs = state / "logs"
    logs.mkdir()
    (logs / "source_qc.json").write_text(json.dumps({"page_results": {
        "1": {"layout_trace": {"full_page_image_fallback": True},
              "visual_fallback_text_preserved": False},
        "2": {"layout_trace": {"full_page_image_fallback": True},
              "visual_fallback_text_preserved": True}}}))
    output_dir = tmp_path / "products"
    with patch.multiple(paddle, RUNTIME_ROOT=tmp_path, TMP_DIR=state / "tmp",
                        PADDLE_OUTPUT_DIR=state / "raw", LOG_DIR=logs, OUTPUT_DIR=output_dir):
        output = searchable_backfill.backfill(source, tmp_path)
    assert output.parent == output_dir
    with fitz.open(output) as doc:
        assert "Rejected cached OCR" not in doc[0].get_text()
        assert "Original source caption" in doc[0].get_text()
        assert "Preserved cached OCR" in doc[1].get_text()
    assert "Rejected cached OCR" in (raw / "page_0001.json").read_text()
