from __future__ import annotations

import hashlib
import io
from pathlib import Path
import tempfile
import unittest

import fitz
from PIL import Image

from support import load_pipeline


def make_scan_image(width=160, height=200, shade=200):
    image = Image.new("RGB", (width, height), (shade, shade, shade))
    for x in range(0, width, 8):
        for y in range(0, height, 8):
            image.putpixel((x, y), (60, 60, 60))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_scanned_source(path: Path, pages=3, width=300, height=400, rotate_pages=()):
    png = make_scan_image()
    with fitz.open() as doc:
        for index in range(pages):
            page = doc.new_page(width=width, height=height)
            if index in rotate_pages:
                page.set_rotation(90)
            page.insert_image(page.rect, stream=png)
        doc.save(path)


def span_cell(bbox, lines, category="Text", units="pdf"):
    line_info = []
    texts = []
    for line_bbox, spans in lines:
        normalized_spans = [
            {"text": text, "bbox": span_bbox, "type": "text", "style": "normal"}
            for text, span_bbox in spans
        ]
        line_text = "".join(span["text"] for span in normalized_spans)
        texts.append(line_text)
        line_info.append(
            {"bbox": line_bbox, "text": line_text, "spans": normalized_spans}
        )
    return {
        "bbox": bbox,
        "category": category,
        "text": "\n".join(texts),
        "content": "\n".join(texts),
        "source": "mineru",
        "__bbox_units": units,
        "__line_info": line_info,
    }


def page_stream_bytes(doc, page_index):
    data = b""
    for xref in doc[page_index].get_contents():
        data += doc.xref_stream(xref)
    return data


def pixmap_digest(path: Path, page_index, dpi=72):
    with fitz.open(path) as doc:
        matrix = fitz.Matrix(dpi / 72, dpi / 72)
        return hashlib.sha256(
            doc[page_index].get_pixmap(matrix=matrix, alpha=False).samples
        ).hexdigest()


class SearchablePdfTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pipeline = load_pipeline(self.root)
        self.source = self.root / "source.pdf"
        self.output = self.root / "searchable.pdf"

    def tearDown(self):
        self.temp.cleanup()

    def test_build_places_invisible_text_and_preserves_pixels(self):
        make_scanned_source(self.source, pages=3)
        cell = span_cell(
            [40, 50, 260, 110],
            [
                ([40, 50, 260, 78], [("圖書", [40, 50, 120, 78]), ("分類", [130, 50, 210, 78])]),
                ([40, 82, 260, 110], [("1983", [40, 82, 100, 110])]),
            ],
        )
        page_results = {
            0: {"cells": [cell], "image_size": None},
            2: {"cells": [], "blank_page": True, "image_size": None},
        }
        stats = self.pipeline.build_searchable_pdf(self.source, page_results, self.output)

        self.assertEqual(stats["pages"], 3)
        self.assertEqual(stats["spans_placed"], 3)
        self.assertEqual(stats["text_page_indexes"], [0])
        self.assertEqual(stats["pages_without_text"], [2, 3])
        self.assertEqual(stats["skipped_category_cells"], 0)

        with fitz.open(self.output) as doc:
            self.assertEqual(doc.page_count, 3)
            text = doc[0].get_text()
            self.assertIn("圖書", text)
            self.assertIn("分類", text)
            self.assertIn("1983", text)
            self.assertEqual(doc[1].get_text().strip(), "")
            self.assertEqual(doc[2].get_text().strip(), "")
            self.assertIn(b"3 Tr", page_stream_bytes(doc, 0))

        for index in range(3):
            self.assertEqual(
                pixmap_digest(self.source, index),
                pixmap_digest(self.output, index),
                f"page {index + 1} pixels changed",
            )

    def test_word_boxes_stay_inside_source_boxes(self):
        make_scanned_source(self.source, pages=1)
        boxes = {
            "圖書": fitz.Rect(40, 50, 120, 78),
            "1983": fitz.Rect(40, 82, 100, 110),
        }
        cell = span_cell(
            [40, 50, 120, 110],
            [
                ([40, 50, 120, 78], [("圖書", list(boxes["圖書"]))]),
                ([40, 82, 100, 110], [("1983", list(boxes["1983"]))]),
            ],
        )
        self.pipeline.build_searchable_pdf(
            self.source, {0: {"cells": [cell], "image_size": None}}, self.output
        )
        with fitz.open(self.output) as doc:
            words = doc[0].get_text("words")
        self.assertEqual(len(words), 2)
        tolerance = 0.5
        for x0, y0, x1, y1, word, *_ in words:
            expected = boxes[word]
            self.assertGreaterEqual(x0, expected.x0 - tolerance, word)
            self.assertGreaterEqual(y0, expected.y0 - tolerance, word)
            self.assertLessEqual(x1, expected.x1 + tolerance, word)
            self.assertLessEqual(y1, expected.y1 + tolerance, word)

    def test_mineru_1000_block_fallback(self):
        make_scanned_source(self.source, pages=1)
        cell = {
            "bbox": [100, 200, 500, 300],
            "category": "Text",
            "text": "alpha\nbeta gamma",
            "content": "alpha\nbeta gamma",
            "source": "mineru",
            "__bbox_units": "mineru_1000",
            "__line_info": [],
        }
        stats = self.pipeline.build_searchable_pdf(
            self.source, {0: {"cells": [cell], "image_size": None}}, self.output
        )
        self.assertEqual(stats["spans_placed"], 2)
        scaled = fitz.Rect(30, 80, 150, 120)
        with fitz.open(self.output) as doc:
            text = doc[0].get_text()
            words = doc[0].get_text("words")
        self.assertIn("alpha", text)
        self.assertIn("beta gamma", text)
        tolerance = 0.5
        for x0, y0, x1, y1, word, *_ in words:
            self.assertGreaterEqual(x0, scaled.x0 - tolerance, word)
            self.assertGreaterEqual(y0, scaled.y0 - tolerance, word)
            self.assertLessEqual(x1, scaled.x1 + tolerance, word)
            self.assertLessEqual(y1, scaled.y1 + tolerance, word)

    def test_skipped_categories_produce_no_text(self):
        make_scanned_source(self.source, pages=1)
        cells = [
            {
                "bbox": [40, 40, 260, 100],
                "category": "Table",
                "text": "<table><tr><td>cell</td></tr></table>",
                "content": "<table><tr><td>cell</td></tr></table>",
                "__bbox_units": "pdf",
                "__line_info": [],
            },
            {
                "bbox": [40, 120, 260, 160],
                "category": "Formula",
                "text": "\\frac{a}{b}",
                "content": "\\frac{a}{b}",
                "__bbox_units": "pdf",
                "__line_info": [],
            },
            {
                "bbox": [40, 180, 260, 240],
                "category": "Picture",
                "text": "picture caption",
                "content": "picture caption",
                "__bbox_units": "pdf",
                "__line_info": [],
            },
        ]
        stats = self.pipeline.build_searchable_pdf(
            self.source, {0: {"cells": cells, "image_size": None}}, self.output
        )
        self.assertEqual(stats["skipped_category_cells"], 3)
        self.assertEqual(stats["text_page_indexes"], [])
        with fitz.open(self.output) as doc:
            self.assertEqual(doc[0].get_text().strip(), "")
            self.assertNotIn(b"3 Tr", page_stream_bytes(doc, 0))

    def test_blank_and_missing_pages_get_no_overlay(self):
        make_scanned_source(self.source, pages=3)
        page_results = {
            0: {"cells": [], "blank_page": True, "image_size": None},
            2: {"cells": [], "image_size": None},
        }
        stats = self.pipeline.build_searchable_pdf(self.source, page_results, self.output)
        self.assertEqual(stats["text_page_indexes"], [])
        self.assertEqual(stats["pages_without_text"], [1, 2, 3])
        with fitz.open(self.output) as doc:
            self.assertEqual(doc.page_count, 3)
            for index in range(3):
                self.assertEqual(doc[index].get_text().strip(), "")
        for index in range(3):
            self.assertEqual(
                pixmap_digest(self.source, index), pixmap_digest(self.output, index)
            )

    def test_rotated_page_overlay(self):
        make_scanned_source(self.source, pages=1, rotate_pages=(0,))
        with fitz.open(self.source) as doc:
            visual_rect = doc[0].rect
        self.assertEqual((visual_rect.width, visual_rect.height), (400, 300))
        span_box = [50, 60, 200, 88]
        cell = span_cell(
            span_box,
            [([50, 60, 200, 88], [("旋轉頁", span_box)])],
        )
        self.pipeline.build_searchable_pdf(
            self.source, {0: {"cells": [cell], "image_size": None}}, self.output
        )
        expected = fitz.Rect(span_box)
        with fitz.open(self.output) as doc:
            words = doc[0].get_text("words")
        self.assertTrue(words)
        tolerance = 0.5
        for x0, y0, x1, y1, word, *_ in words:
            self.assertGreaterEqual(x0, expected.x0 - tolerance, word)
            self.assertGreaterEqual(y0, expected.y0 - tolerance, word)
            self.assertLessEqual(x1, expected.x1 + tolerance, word)
            self.assertLessEqual(y1, expected.y1 + tolerance, word)
        self.assertEqual(pixmap_digest(self.source, 0), pixmap_digest(self.output, 0))

    def test_visual_sample_indexes(self):
        sample = self.pipeline.searchable_visual_sample_indexes
        self.assertEqual(sample(0), [])
        self.assertEqual(sample(1), [0])
        self.assertEqual(sample(5), [0, 1, 2, 3, 4])
        large = sample(400)
        self.assertEqual(len(large), 12)
        self.assertEqual(large[0], 0)
        self.assertEqual(large[-1], 399)
        self.assertEqual(large, sorted(set(large)))

    def test_text_presence_validator_raises(self):
        make_scanned_source(self.source, pages=2)
        cell = span_cell(
            [40, 50, 260, 78],
            [([40, 50, 260, 78], [("only page one", [40, 50, 260, 78])])],
        )
        self.pipeline.build_searchable_pdf(
            self.source, {0: {"cells": [cell], "image_size": None}}, self.output
        )
        with self.assertRaisesRegex(RuntimeError, "missing expected invisible text"):
            self.pipeline.validate_searchable_pdf_text_presence(self.output, [0, 1])
        self.pipeline.validate_searchable_pdf_text_presence(self.output, [0])

    def test_visual_identity_validator_detects_pixel_change(self):
        make_scanned_source(self.source, pages=2)
        cell = span_cell(
            [40, 50, 260, 78],
            [([40, 50, 260, 78], [("text", [40, 50, 260, 78])])],
        )
        self.pipeline.build_searchable_pdf(
            self.source, {0: {"cells": [cell], "image_size": None}}, self.output
        )
        self.pipeline.validate_searchable_pdf_visual_identity(
            self.source, self.output, sample_page_indexes=[0, 1]
        )
        with fitz.open(self.output) as doc:
            doc[0].insert_text((40, 40), "visible tamper", render_mode=0)
            doc.save(self.root / "tampered.pdf")
        with self.assertRaisesRegex(RuntimeError, "changed rendered pixels"):
            self.pipeline.validate_searchable_pdf_visual_identity(
                self.source,
                self.root / "tampered.pdf",
                sample_page_indexes=[0, 1],
            )


if __name__ == "__main__":
    unittest.main()
