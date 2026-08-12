from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import fitz

from support import load_pipeline


class ForwardPageLeakRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pipeline = load_pipeline(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def make_source_pdf(self):
        pdf_path = self.root / "source.pdf"
        page_texts = [
            "CURRENT PAGE "
            + " ".join(f"alpha{i:03d} beta{i:03d}" for i in range(40)),
            "NEXT PAGE "
            + " ".join(f"gamma{i:03d} delta{i:03d}" for i in range(40)),
            "THIRD PAGE "
            + " ".join(f"orange{i:03d} violet{i:03d}" for i in range(40)),
        ]
        doc = fitz.open()
        for text in page_texts:
            page = doc.new_page(width=612, height=792)
            remaining = page.insert_textbox(
                fitz.Rect(36, 36, 576, 756),
                text,
                fontsize=8,
            )
            self.assertGreaterEqual(remaining, 0)
        doc.save(pdf_path)
        doc.close()
        return pdf_path, page_texts

    @staticmethod
    def page_result(text):
        return {
            "cells": [
                {
                    "bbox": [36, 36, 576, 756],
                    "category": "Text",
                    "text": text,
                    "__bbox_units": "pdf",
                }
            ],
            "fallback_text": "",
            "md_nohf_text": "",
            "needs_retry": False,
        }

    def test_detector_finds_forward_page_text_but_not_page_local_text(self):
        pdf_path, page_texts = self.make_source_pdf()
        leaking = {0: self.page_result(page_texts[0] + "\n" + page_texts[1])}
        detected = self.pipeline.detect_forward_page_content_leaks(pdf_path, leaking)
        self.assertEqual(list(detected), [0])
        self.assertEqual(leaking[0]["forward_page_content_leak_targets"], [2])
        self.assertTrue(leaking[0]["needs_retry"])

        local = {0: self.page_result(page_texts[0])}
        self.assertEqual(
            self.pipeline.detect_forward_page_content_leaks(pdf_path, local),
            {},
        )
        self.assertFalse(local[0]["needs_retry"])

    def test_isolated_retry_pdf_places_blank_page_after_each_candidate(self):
        pdf_path, _page_texts = self.make_source_pdf()
        isolated_path = self.root / "isolated.pdf"
        self.pipeline.write_pdf_isolated_selected_pages(
            pdf_path,
            isolated_path,
            [0, 2],
        )
        with fitz.open(isolated_path) as isolated:
            self.assertEqual(isolated.page_count, 4)
            self.assertIn("CURRENT PAGE", isolated[0].get_text())
            self.assertEqual(isolated[1].get_text().strip(), "")
            self.assertIn("THIRD PAGE", isolated[2].get_text())
            self.assertEqual(isolated[3].get_text().strip(), "")

    def test_source_text_fallback_contains_only_requested_source_page(self):
        pdf_path, _page_texts = self.make_source_pdf()
        with fitz.open(pdf_path) as source:
            result = self.pipeline.source_pdf_text_page_result(source, 0)
        self.assertIsNotNone(result)
        text = self.pipeline.page_result_primary_text(result)
        self.assertIn("CURRENT PAGE", text)
        self.assertNotIn("NEXT PAGE", text)
        self.assertTrue(result["source_pdf_text_fallback"])


if __name__ == "__main__":
    unittest.main()
