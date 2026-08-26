from __future__ import annotations

from pathlib import Path
import re
import sys
import tempfile
import unittest

import fitz


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from ocr_pdf_rebuilder import pipeline_runtime as pipeline


class LatexHardeningTests(unittest.TestCase):
    def test_nested_scripts_and_editorial_star_marker_are_normalized(self):
        source = r"$ ^{{**}} $ Zum] $ D^{{3}} $ Zum"
        normalized = pipeline.normalize_text(source)

        self.assertEqual(normalized, "*** Zum] D³ Zum")
        self.assertIsNone(pipeline.LATEX_RESIDUE_RE.search(normalized))

    def test_real_paddle_command_variants_are_linearized_losslessly(self):
        source = (
            r"\parallel \triangle \langle sei\rangle \cdots \div "
            r"\cos.\, a J\textsuperscript¹"
        )
        normalized = pipeline.normalize_text(source)

        self.assertEqual(normalized, "∥ △ ⟨sei⟩ ⋯ ÷ cos. a J¹")
        self.assertIsNone(pipeline.LATEX_RESIDUE_RE.search(normalized))

    def test_adjacent_latex_commands_cannot_merge_after_style_unwrap(self):
        source = (
            r"2\,dh\cdot\operatorname{Cos.}\,\mathbf{a}; "
            r"\mathrm{I I}\operatorname{t a n g.}\frac{1}{2}"
            r"(\mathbf{C}+\mathbf{B})\colon\operatorname{t a n g.}"
        )
        normalized = pipeline.normalize_text(source)

        self.assertEqual(
            normalized,
            "2 dh·Cos. a; I It a n g.½(C+B):t a n g.",
        )
        self.assertNotIn("cdot", normalized)
        self.assertNotIn("operatorname", normalized)
        self.assertIsNone(pipeline.LATEX_RESIDUE_RE.search(normalized))

    def test_contextual_greek_command_hallucinations_are_recovered(self):
        source = (
            r"19.20  $ \mathring{A} $τομοι ἀρχαί; "
            r"19.20  $ \checkmark $τομα στοιχεία"
        )
        self.assertEqual(
            pipeline.normalize_text(source),
            "19.20 Ἄτομοι ἀρχαί; 19.20 ἄτομα στοιχεία",
        )

        self.assertEqual(
            pipeline.normalize_text(r"\mathring{A} τομοι; \checkmark τομα"),
            "Ἄτομοι; ἄτομα",
        )

    def test_editorial_angle_markers_do_not_delete_unknown_words(self):
        source = r"\gleichsam\ die; Staatsganzen \gegenübe[r]"
        self.assertEqual(
            pipeline.normalize_text(source),
            "⟨gleichsam⟩ die; Staatsganzen ⟨gegenübe[r]⟩",
        )
        self.assertEqual(pipeline.normalize_text(r"\unbekannt{Inhalt}"), "Inhalt")

    def test_repeated_and_unbalanced_ocr_braces_are_removed(self):
        source = "Herwegsh}}} und •S}{{Stattstattung}"
        normalized = pipeline.normalize_text(source)

        self.assertEqual(normalized, "Herwegsh und •SStattstattung")
        self.assertNotRegex(normalized, r"[{}]")

    def test_residue_detector_covers_generic_commands_and_nested_braces(self):
        for residue in (
            r"\parallel",
            r"\unrecognized{payload}",
            r"D^{{3}}",
            r"D{{3}}",
            r"{{**}}",
            r"\,",
            "dhcdotCos.",
            "operatornametang",
            "⟨colon⟩",
        ):
            with self.subTest(residue=residue):
                self.assertIsNotNone(pipeline.LATEX_RESIDUE_RE.search(residue))

        self.assertIsNone(pipeline.LATEX_RESIDUE_RE.search("ordinary set {x, y}"))

    def test_cached_table_rows_are_normalized_at_the_draw_boundary(self):
        raw_cell = r"$ ^{{**}} $ Zum] $ D^{{3}} $ Zum"
        table_html = (
            "<table><tr><td>121.7</td><td>"
            + raw_cell
            + "</td></tr></table>"
        )
        block = {
            "order": 1,
            "category": "Table",
            "text": table_html,
            "source_text": table_html,
            # This cached structure deliberately retains the pre-normalized
            # OCR payload, which was the path that escaped the old block gate.
            "table_rows": [
                {
                    "cells": ["121.7", raw_cell],
                    "nonempty_cells": ["121.7", raw_cell],
                    "first_col": 0,
                    "col_count": 2,
                    "source": "html",
                }
            ],
            "line_info": [],
            "left": 25.0,
            "top": 30.0,
            "width": 250.0,
            "height": 70.0,
            "font_size": 10.0,
            "min_font_size": 8.0,
            "line_height": 1.2,
            "strict_bbox_fit": True,
        }

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "table.pdf"
            pipeline.render_blocks_to_pdf_reportlab(
                [(300.0, 180.0, [block])],
                output,
                include_full_page_images=False,
                allow_formula_image_fallback=False,
            )
            with fitz.open(output) as document:
                extracted = re.sub(r"\s+", " ", document[0].get_text()).strip()

        self.assertIn("*** Zum] D³ Zum", extracted)
        self.assertIsNone(pipeline.LATEX_RESIDUE_RE.search(extracted))

    def test_cached_line_info_cannot_bypass_contextual_normalization(self):
        block = {
            "order": 1,
            "category": "Text",
            "text": "19.20 Ἄτομοι ἀρχαί; 19.20 ἄτομα στοιχεία",
            "source_text": "19.20 Ἄτομοι ἀρχαί; 19.20 ἄτομα στοιχεία",
            # The main block is clean, but a MinerU-style structured cache can
            # still retain raw command fragments from before normalization.
            "line_info": [
                {
                    "bbox": [20.0, 25.0, 280.0, 45.0],
                    "text": r"19.20 $ \mathring{A} $τομοι ἀρχαί",
                    "spans": [
                        {"text": "19.20 "},
                        {"text": r"$ \mathring{A} $"},
                        {"text": "τομοι ἀρχαί"},
                    ],
                },
                {
                    "bbox": [20.0, 48.0, 280.0, 68.0],
                    "text": r"19.20 $ \checkmark $τομα στοιχεία",
                    "spans": [
                        {"text": "19.20 "},
                        {"text": r"$ \checkmark $"},
                        {"text": "τομα στοιχεία"},
                    ],
                },
            ],
            "bbox_scale_x": 1.0,
            "bbox_scale_y": 1.0,
            "left": 20.0,
            "top": 20.0,
            "width": 260.0,
            "height": 65.0,
            "font_size": 10.0,
            "min_font_size": 7.0,
            "line_height": 1.2,
            "strict_bbox_fit": True,
        }

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "line-info.pdf"
            pipeline.render_blocks_to_pdf_reportlab(
                [(300.0, 120.0, [block])],
                output,
                include_full_page_images=False,
                allow_formula_image_fallback=False,
            )
            with fitz.open(output) as document:
                extracted = re.sub(r"\s+", " ", document[0].get_text()).strip()

        self.assertIn("Ἄτομοι ἀρχαί", extracted)
        self.assertIn("ἄτομα στοιχεία", extracted)
        self.assertNotIn("Å τομοι", extracted)
        self.assertNotIn("✓ τομα", extracted)
        self.assertEqual(block["line_info"], [])

    def test_combining_vector_mark_never_renders_as_a_visible_tofu_square(self):
        block = {
            "order": 1,
            "category": "Formula",
            "text": r"$$ \vec{x}+\vec{d}=\vec{d}^{2}-\vec{x}^{2}; $$",
            "source_text": r"$$ \vec{x}+\vec{d}=\vec{d}^{2}-\vec{x}^{2}; $$",
            "left": 20.0,
            "top": 20.0,
            "width": 260.0,
            "height": 35.0,
            "font_size": 10.0,
            "min_font_size": 6.0,
            "line_height": 1.0,
            "strict_bbox_fit": True,
        }

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "vector.pdf"
            pipeline.render_blocks_to_pdf_reportlab(
                [(300.0, 100.0, [block])],
                output,
                include_full_page_images=False,
                allow_formula_image_fallback=False,
            )
            with fitz.open(output) as document:
                extracted = document[0].get_text()

        self.assertIn("vec(x)", extracted)
        self.assertNotIn("□", extracted)
        self.assertNotIn("\u20d7", extracted)


if __name__ == "__main__":
    unittest.main()
