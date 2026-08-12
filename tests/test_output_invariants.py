from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest

import fitz
from PIL import Image

from support import load_pipeline


def make_three_page_source(path: Path):
    with fitz.open() as doc:
        page = doc.new_page(width=300, height=400)
        page.insert_text((40, 60), "Page one")
        doc.new_page(width=300, height=400)
        doc.new_page(width=300, height=400)
        doc.save(path)


def make_text_only_output(path: Path, pages: int):
    with fitz.open() as doc:
        for index in range(pages):
            page = doc.new_page(width=300, height=400)
            if index == 0:
                page.insert_text((40, 60), "Rebuilt page one")
        doc.save(path)


class OutputInvariantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pipeline = load_pipeline(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_trailing_blank_pages_are_counted_and_remain_blank(self):
        source = self.root / "source.pdf"
        output = self.root / "output.pdf"
        short_output = self.root / "short.pdf"
        make_three_page_source(source)
        make_text_only_output(output, 3)
        make_text_only_output(short_output, 2)

        scan = self.pipeline.scan_pdf_validation(output)
        self.pipeline.validate_pdf_page_count(output, 3, scan)
        self.pipeline.validate_pdf_pages_are_blank(output, [1, 2], scan)
        with self.assertRaisesRegex(RuntimeError, "page count"):
            self.pipeline.validate_pdf_page_count(short_output, 3)

    def test_text_and_image_variants_enforce_opposite_fallback_rules(self):
        text_pdf = self.root / "text.pdf"
        image_pdf = self.root / "with_images.pdf"
        make_text_only_output(text_pdf, 3)

        image = Image.new("RGB", (32, 32), "black")
        payload = io.BytesIO()
        image.save(payload, format="PNG")
        with fitz.open() as doc:
            for index in range(3):
                page = doc.new_page(width=300, height=400)
                if index == 0:
                    page.insert_text((40, 60), "Rebuilt page one")
                if index == 1:
                    page.insert_image(fitz.Rect(0, 0, 300, 400), stream=payload.getvalue())
            doc.save(image_pdf)

        text_scan = self.pipeline.scan_pdf_validation(text_pdf)
        image_scan = self.pipeline.scan_pdf_validation(image_pdf)
        self.pipeline.validate_pdf_has_no_raster_images(text_pdf, text_scan)
        self.pipeline.validate_pdf_pages_are_blank(text_pdf, [1], text_scan)
        self.pipeline.validate_pdf_has_images_on_pages(image_pdf, [1], image_scan)


if __name__ == "__main__":
    unittest.main()
