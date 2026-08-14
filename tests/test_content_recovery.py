from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

import fitz

from support import load_pipeline


class ContentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pipeline = load_pipeline(self.root)
        font_root = Path(__file__).resolve().parents[1] / "fonts"
        for attribute, filename in {
            "CJK_REGULAR_FONT": "SourceHanSerifCN-Regular.ttf",
            "CJK_MEDIUM_FONT": "SourceHanSerifCN-Medium.ttf",
            "CJK_BOLD_FONT": "SourceHanSerifCN-Bold.ttf",
            "LATIN_REGULAR_FONT": "SourceSerif4-Regular.ttf",
            "LATIN_BOLD_FONT": "SourceSerif4-Bold.ttf",
            "LATIN_ITALIC_FONT": "SourceSerif4-It.ttf",
            "GREEK_REGULAR_FONT": "DejaVuSerif.ttf",
            "GREEK_BOLD_FONT": "DejaVuSerif-Bold.ttf",
            "GREEK_ITALIC_FONT": "DejaVuSerif-Italic.ttf",
            "MONO_FONT": "NotoSansMono-Regular.ttf",
        }.items():
            setattr(self.pipeline, attribute, font_root / filename)

    def tearDown(self):
        self.temp.cleanup()

    def test_nested_table_html_is_extracted_from_nested_spans(self):
        html = (
            "<table><tr><td>Outer</td><td>"
            "<table><tr><td>Inner</td></tr></table>"
            "</td></tr></table>"
        )
        block = {
            "type": "table",
            "blocks": [{"lines": [{"spans": [{"html": html}]}]}],
        }
        recovered = self.pipeline.mineru_nested_table_html(block)
        self.assertEqual(recovered, html)
        rows = self.pipeline.extract_html_table_rows(recovered)
        self.assertEqual(rows[0]["nonempty_cells"], ["Outer", "Inner"])

    def test_footnote_block_survives_middle_json_conversion(self):
        block = {
            "type": "footnote",
            "bbox": [10, 10, 300, 40],
            "lines": [
                {
                    "bbox": [10, 10, 300, 40],
                    "spans": [
                        {"content": "A retained footnote", "bbox": [10, 10, 300, 40]}
                    ],
                }
            ],
        }
        page, cell = self.pipeline.mineru_block_to_cell(
            block,
            0,
            Path("sample_middle.json"),
            page_size=(1000, 1000),
        )
        self.assertEqual(page, 0)
        self.assertEqual(cell["category"], "Footnote")
        self.assertEqual(cell["text"], "A retained footnote")

    def test_formula_renderer_reaches_image_crop_fallback(self):
        class Rect:
            height = 30.0

        block = {"text": r"\\unsupported{formula}", "order": 1}
        with (
            mock.patch.object(self.pipeline, "formula_to_unicode_if_simple", return_value=None),
            mock.patch.object(
                self.pipeline,
                "render_latex_formula_to_svg",
                side_effect=RuntimeError("latex failed"),
            ),
            mock.patch.object(self.pipeline, "linearize_latex_formula", return_value=""),
            mock.patch.object(self.pipeline, "draw_formula_crop_fallback", return_value=True),
        ):
            rendered = self.pipeline.render_formula_block(
                object(), 800, Rect(), block, self.root, allow_image_fallback=True
            )
        self.assertTrue(rendered)
        self.assertEqual(block["formula_render_mode"], "image_crop")
        self.assertIn("latex failed", block["formula_render_error"])

    def test_formula_dependency_probe_never_runs_an_installer(self):
        with (
            mock.patch.object(self.pipeline, "FORMULA_RENDER_DEPS", None),
            mock.patch.object(self.pipeline.shutil, "which", return_value=None),
            mock.patch.object(self.pipeline, "python_module_exists", return_value=False),
            mock.patch.object(
                self.pipeline.subprocess,
                "run",
                side_effect=AssertionError("dependency probe attempted to run a command"),
            ) as run_command,
        ):
            dependencies = self.pipeline.formula_render_dependencies()
        self.assertEqual(
            dependencies,
            {"latex": None, "dvisvgm": None, "svglib": False},
        )
        run_command.assert_not_called()
        self.assertFalse(hasattr(self.pipeline, "install_formula_render_dependencies"))

    def test_tiny_reference_line_search_does_not_skip_emergency_candidates(self):
        text = "14. Chew and Frautschi(1961a, b)."
        block = {
            "order": 5,
            "category": "Text",
            "text": text,
            "source_text": text,
            "line_info": [],
            "left": 34.0,
            "top": 68.0,
            "width": 61.0,
            "height": 5.0,
            "font_size": 3.29,
            "min_font_size": 3.29,
            "line_height": 0.74,
            "enforce_body_font_floor": True,
            "body_text_font_floor": 6.45,
            "strict_bbox_fit": True,
        }
        rect = fitz.Rect(34.0, 68.0, 95.0, 73.0)

        fontsize, line_height, fits = self.pipeline.reportlab_fit_box_params(
            330.0,
            rect,
            block,
            block["font_size"],
            block["min_font_size"],
            block["line_height"],
            self.pipeline.block_align(block),
        )

        self.assertTrue(fits)
        self.assertGreaterEqual(fontsize, 2.6)
        self.assertLess(fontsize, 3.2)
        self.assertGreaterEqual(line_height, 0.68)
        output = self.root / "tiny-reference.pdf"
        self.pipeline.render_blocks_to_pdf_reportlab(
            [(202.5, 330.0, [block])],
            output,
            include_full_page_images=False,
            allow_formula_image_fallback=False,
        )
        with fitz.open(output) as document:
            page = document[0]
            self.assertIn(text, page.get_text().replace("\n", " "))
            self.assertFalse(page.get_images(full=True))

    def test_tiny_numbered_source_line_keeps_its_bbox_indent_and_one_line(self):
        text = "14. Chew and Frautschi(1961a, b)."
        block = {
            "order": 5,
            "category": "Text",
            "text": text,
            "source_text": text,
            "line_info": [
                {
                    "bbox": [34.0, 68.0, 95.0, 73.0],
                    "text": text,
                    "spans": [],
                }
            ],
            "bbox_scale_x": 1.0,
            "bbox_scale_y": 1.0,
            "left": 34.0,
            "top": 68.0,
            "width": 61.0,
            "height": 5.0,
            "font_size": 3.29,
            "min_font_size": 3.29,
            "line_height": 0.74,
            "strict_bbox_fit": True,
        }
        output = self.root / "numbered-reference.pdf"

        self.assertTrue(
            self.pipeline.reportlab_draw_strict_tiny_source_line(
                None,
                330.0,
                block,
                block["font_size"],
                dry_run=True,
            )
        )
        self.pipeline.render_blocks_to_pdf_reportlab(
            [(202.5, 330.0, [block])],
            output,
            include_full_page_images=False,
            allow_formula_image_fallback=False,
        )
        with fitz.open(output) as document:
            lines = document[0].get_text().splitlines()
            self.assertIn(text, lines)

    def test_dense_numbered_note_page_uses_neighbour_line_size_without_compression(self):
        blocks = []
        top = 30.0
        for number in range(10, 20):
            is_long = number == 19
            text = (
                "19. A longer numbered reference entry with enough text to wrap "
                "across several source lines without borrowing body-text sizing."
                if is_long
                else f"{number}. Reference item {number}."
            )
            height = 15.0 if is_long else 5.0
            blocks.append(
                {
                    "order": number - 10,
                    "category": "Text",
                    "text": text,
                    "source_text": text,
                    "line_info": [],
                    "left": 25.0,
                    "top": top,
                    "width": 152.0,
                    "height": height,
                }
            )
            top += height

        estimated = self.pipeline.dense_numbered_note_page_font_size(blocks, 330.0)
        self.assertAlmostEqual(estimated, 5.0 / 1.2, places=2)

        prepared = self.pipeline.prepare_blocks(blocks, 202.0, 330.0)
        numbered = [block for block in prepared if block.get("dense_numbered_note")]
        self.assertEqual(len(numbered), 10)
        self.assertTrue(all(block.get("preserve_numbered_bbox_indent") for block in numbered))
        self.assertTrue(all(block.get("preserve_source_line_spacing") for block in numbered))
        self.assertTrue(all(not block.get("enforce_body_font_floor") for block in numbered))
        self.assertTrue(all(block["font_size"] <= 4.17 for block in numbered))
        self.assertTrue(all(block["line_height"] >= 1.0 for block in numbered))
        self.assertEqual(self.pipeline.reportlab_page_fit_failures(330.0, numbered), [])

    def test_positioned_numbered_text_does_not_receive_markdown_list_indent(self):
        rect = fitz.Rect(34.0, 68.0, 160.0, 88.0)
        text = "14. Chew and Frautschi(1961a, b)."
        ordinary = self.pipeline.reportlab_markdown_textbox_layout(
            330.0,
            rect,
            text,
            4.0,
            1.15,
            fitz.TEXT_ALIGN_LEFT,
        )
        positioned = self.pipeline.reportlab_markdown_textbox_layout(
            330.0,
            rect,
            text,
            4.0,
            1.15,
            fitz.TEXT_ALIGN_LEFT,
            literal_numbered=True,
        )

        self.assertIsNotNone(ordinary)
        self.assertIsNotNone(positioned)
        self.assertAlmostEqual(ordinary[0][0][0], rect.x0 + 14.0, places=2)
        self.assertAlmostEqual(positioned[0][0][0], rect.x0, places=2)

    def test_any_unfitting_nonformula_block_gets_safe_page_fallback(self):
        source = self.root / "source.pdf"
        with fitz.open() as document:
            page = document.new_page(width=300, height=400)
            page.insert_text((40, 60), "source page fallback")
            document.save(source)
        result = {"cells": [], "retry_reason": ""}
        block = {
            "order": 3,
            "category": "Text",
            "text": "Unfitting text " * 20,
            "source_text": "Unfitting text " * 20,
            "line_info": [],
            "left": 10.0,
            "top": 200.0,
            "width": 10.0,
            "height": 1.0,
            "font_size": 6.0,
            "min_font_size": 5.2,
            "line_height": 1.0,
        }

        blocks, failures = self.pipeline.fallback_unfitting_layout_page(
            source,
            0,
            result,
            [block],
            300.0,
            400.0,
            self.root / "work",
        )

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["category"], "Text")
        self.assertEqual([item["category"] for item in blocks], ["ImageFallback"])
        self.assertTrue(Path(result["image_fallback_path"]).is_file())
        self.assertTrue(result["image_fallback_page"])
        self.assertEqual(result["image_fallback_kind"], "layout_fit")
        self.assertIn(
            self.pipeline.UNFITTING_LAYOUT_IMAGE_FALLBACK_REASON,
            result["retry_reason"],
        )


if __name__ == "__main__":
    unittest.main()
