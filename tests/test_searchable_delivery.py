from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import fitz

from ocr_pdf_rebuilder import paddle_pipeline as paddle
from ocr_pdf_rebuilder.searchable_backfill import backfill
from ocr_pdf_rebuilder.task_lock import CrossProcessTaskLock, TaskLockBusyError


class SearchableDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "book.pdf"
        with fitz.open() as doc:
            doc.new_page(width=300, height=400)
            doc.save(self.source)
        self.result_dir = self.root / "paddle_output/book/page_results"
        self.result_dir.mkdir(parents=True)
        self.page = self.result_dir / "page_0001.json"
        self.result = {"engine": "PaddleOCR-VL", "page_index": 0, "cells": [{
            "bbox": [30, 40, 240, 80], "category": "Text", "text": "Cached OCR",
            "__bbox_units": "pdf",
        }]}
        self.page.write_text(json.dumps(self.result), encoding="utf-8")
        self.job_state = self.root / "tmp/paddle_textonly_pdf/book/job_state.json"
        paddle.shared.write_checkpoint(self.job_state, {
            "source": paddle.shared.source_file_signature(self.source),
            "implementation_identity_hash": "old-ocr-code",
        })

    def test_completion_requires_searchable_file_and_signature(self):
        text = self.root / "book_paddle.pdf"
        text.write_bytes(self.source.read_bytes())
        overlay = self.root / "book_paddle_searchable.pdf"
        overlay.write_bytes(self.source.read_bytes())
        md = self.root / "book_paddle.md"
        md.write_text("OCR", encoding="utf-8")
        state = self.root / "book_paddle.version"
        image = self.root / "book_paddle_with_images.pdf"
        with mock.patch.object(paddle, "paddle_runtime_identity", return_value={}):
            paddle.write_completion_state(state, self.source, text, md, image, [])
            self.assertTrue(paddle.completion_matches(state, self.source, text, md, image))
            original = overlay.read_bytes()
            overlay.unlink()
            self.assertFalse(paddle.completion_matches(state, self.source, text, md, image))
            overlay.write_bytes(b"damaged PDF")
            self.assertFalse(paddle.completion_matches(state, self.source, text, md, image))
            overlay.write_bytes(original)
            payload = paddle.shared.read_checkpoint(state)
            payload.pop("output_searchable_pdf")
            paddle.shared.write_checkpoint(state, payload)
            self.assertFalse(paddle.completion_matches(state, self.source, text, md, image))

    def test_backfill_does_not_start_models_or_upgrade_old_completion(self):
        marker = self.root / "pdf_paddle/book_paddle.version"
        marker.parent.mkdir()
        marker.write_text("old completion", encoding="utf-8")
        with (
            mock.patch.object(paddle, "paddle_inference_session", side_effect=AssertionError("model started")),
            mock.patch.object(paddle, "prepare_workspace", side_effect=AssertionError("cache cleanup")),
            mock.patch.object(paddle.shared, "suppress_blank_page_outputs"),
        ):
            output = backfill(self.source, self.root)
        with fitz.open(output) as doc:
            self.assertIn("Cached OCR", doc[0].get_text())
            self.assertTrue(all(span["type"] == 0 for span in doc[0].get_texttrace()))
        self.assertEqual(marker.read_text(), "old completion")
        self.assertTrue(self.page.is_file())
        record = paddle.shared.read_checkpoint(output.with_suffix(".version"))
        self.assertEqual(record["ocr_implementation_identity_hash"], "old-ocr-code")

    def test_backfill_rejects_missing_pages_wrong_source_and_busy_lock(self):
        self.page.unlink()
        with self.assertRaisesRegex(RuntimeError, "Incomplete OCR cache"):
            backfill(self.source, self.root)
        self.page.write_text(json.dumps(self.result), encoding="utf-8")
        with CrossProcessTaskLock(
            self.root / "tmp/ocr_pdf_rebuilder/task.lock", engine_name="test",
            input_dir=self.root, output_dir=self.root,
        ):
            with self.assertRaises(TaskLockBusyError):
                backfill(self.source, self.root)
        self.source.write_bytes(b"different source")
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            backfill(self.source, self.root)
        self.assertTrue(self.page.is_file())

    def test_failed_validation_preserves_existing_destination(self):
        output = self.root / "existing.pdf"
        output.write_bytes(b"existing artifact")
        with mock.patch.object(
            paddle.shared, "validate_searchable_pdf_visual_identity",
            side_effect=RuntimeError("pixel mismatch"),
        ):
            with self.assertRaisesRegex(RuntimeError, "pixel mismatch"):
                paddle.shared.build_validated_searchable_pdf(self.source, {0: self.result}, output)
        self.assertEqual(output.read_bytes(), b"existing artifact")
        self.assertEqual(list(self.root.glob(".existing*")), [])


if __name__ == "__main__":
    unittest.main()
