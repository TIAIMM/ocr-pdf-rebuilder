from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from support import load_pipeline


class ContentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pipeline = load_pipeline(self.root)

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


if __name__ == "__main__":
    unittest.main()
