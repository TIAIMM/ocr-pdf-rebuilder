from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import fitz

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from ocr_pdf_rebuilder import paddle_pipeline, paddle_worker


class PaddlePipelineTests(unittest.TestCase):
    def test_completion_state_records_implementation_identity(self):
        captured = {}
        implementation = {"schema": 1, "files_sha256": "c" * 64, "files": []}
        runtime = {"schema": 1, "worker": {}}
        with (
            mock.patch.object(paddle_pipeline, "paddle_runtime_identity", return_value=runtime),
            mock.patch.object(
                paddle_pipeline.shared,
                "current_package_source_identity",
                return_value=implementation,
            ),
            mock.patch.object(
                paddle_pipeline.shared,
                "current_package_source_identity_hash",
                return_value="c" * 64,
            ),
            mock.patch.object(
                paddle_pipeline.shared,
                "source_file_signature",
                return_value={"sha256": "s" * 64},
            ),
            mock.patch.object(
                paddle_pipeline.shared,
                "pdf_artifact_signature",
                return_value={"sha256": "p" * 64},
            ),
            mock.patch.object(
                paddle_pipeline.shared,
                "file_integrity_signature",
                return_value={"sha256": "m" * 64},
            ),
            mock.patch.object(
                paddle_pipeline.shared,
                "write_checkpoint",
                side_effect=lambda _path, payload: captured.update(payload),
            ),
        ):
            paddle_pipeline.write_completion_state(
                Path("state"),
                Path("source.pdf"),
                Path("output.pdf"),
                Path("output.md"),
                Path("output-images.pdf"),
                [],
            )
        self.assertEqual(captured["implementation_identity"], implementation)
        self.assertEqual(captured["implementation_identity_hash"], "c" * 64)

    def test_explicit_image_bbox_is_scaled_even_when_values_resemble_pdf_units(self):
        cell = {
            "bbox": [485.0, 2149.0, 555.0, 2220.0],
            "category": "Text",
            "text": "10",
            "__bbox_units": "image",
        }
        block = paddle_pipeline.shared.cell_to_block(
            cell,
            3,
            1392.0,
            2040.0,
            [3867, 5667],
        )
        self.assertIsNotNone(block)
        self.assertAlmostEqual(block["left"], 174.6, places=1)
        self.assertAlmostEqual(block["top"], 773.6, places=1)
        self.assertAlmostEqual(block["width"], 25.2, places=1)
        self.assertAlmostEqual(block["height"], 25.6, places=1)

    def test_paddle_blocks_are_normalized_for_shared_layout_engine(self):
        raw = {
            "res": {
                "parsing_res_list": [
                    {
                        "block_label": "paragraph_title",
                        "block_content": "Section",
                        "block_bbox": [20, 30, 500, 90],
                        "block_order": 1,
                    },
                    {
                        "block_label": "table",
                        "block_content": "| A | B |\n|---|---|\n| 1 | 2 |",
                        "block_bbox": [20, 120, 700, 420],
                        "block_order": 2,
                    },
                ]
            }
        }
        result = paddle_worker.normalized_page_result(
            raw,
            page_index=0,
            image_width=1000,
            image_height=1400,
            raw_json_path=Path("raw.json"),
        )
        self.assertEqual(
            [cell["category"] for cell in result["cells"]],
            ["Section-header", "Table"],
        )
        self.assertEqual(result["image_size"], [1000, 1400])
        self.assertTrue(all(cell["__bbox_units"] == "image" for cell in result["cells"]))

    def test_marker_body_text_is_reassigned_to_adjacent_empty_layout_bbox(self):
        quotation = (
            "我们不能想象在时间中的传播，除非要么作为物质实体通过空间的漂移，"
            "要么作为运动状态或已存在于空间中的介质中的应力的传播。"
        )
        raw = {
            "parsing_res_list": [
                {
                    "block_id": 3,
                    "block_order": 4,
                    "block_label": "text",
                    "block_content": f"[35]\n{quotation}",
                    "block_bbox": [31, 665, 54, 678],
                },
                {
                    "block_id": 4,
                    "block_order": 5,
                    "block_label": "text",
                    "block_content": "",
                    "block_bbox": [93, 663, 488, 738],
                },
            ]
        }
        result = paddle_worker.normalized_page_result(
            raw,
            page_index=102,
            image_width=562,
            image_height=917,
            raw_json_path=Path("page_0103_res.json"),
        )

        self.assertTrue(result["paddle_bbox_content_repaired"])
        self.assertEqual(len(result["paddle_bbox_content_repairs"]), 1)
        self.assertEqual([cell["text"] for cell in result["cells"]], ["[35]", quotation])
        self.assertEqual(result["cells"][0]["bbox"], [31.0, 665.0, 54.0, 678.0])
        self.assertEqual(result["cells"][1]["bbox"], [93.0, 663.0, 488.0, 738.0])
        self.assertEqual(
            [cell["__paddle_bbox_content_repair_role"] for cell in result["cells"]],
            ["marker", "body"],
        )

    def test_marker_body_text_is_not_guessed_without_an_empty_sibling(self):
        raw = {
            "parsing_res_list": [
                {
                    "block_label": "text",
                    "block_content": "[35]\n正文内容足够长，但没有可验证的空白正文布局框，因此必须保留原结果。",
                    "block_bbox": [31, 665, 54, 678],
                },
                {
                    "block_label": "text",
                    "block_content": "已经占用的相邻文字块",
                    "block_bbox": [93, 663, 488, 738],
                },
            ]
        }
        result = paddle_worker.normalized_page_result(
            raw,
            page_index=0,
            image_width=562,
            image_height=917,
            raw_json_path=Path("raw.json"),
        )
        self.assertFalse(result["paddle_bbox_content_repaired"])
        self.assertEqual(len(result["cells"]), 2)
        self.assertTrue(result["cells"][0]["text"].startswith("[35]"))

    def test_worker_command_uses_dedicated_paddle_environment_and_page_numbers(self):
        command = paddle_pipeline.worker_command(
            Path("book.pdf"),
            Path("raw"),
            pages=[0, 4],
            force=True,
            backend="vllm-server",
            server_url="http://127.0.0.1:9999/v1",
        )
        self.assertEqual(command[0], str(paddle_pipeline.PADDLE_PYTHON))
        self.assertIn("paddle_worker.py", command[2])
        self.assertEqual(command[command.index("--pages") + 1], "1,5")
        self.assertIn("--force", command)
        self.assertEqual(command[command.index("--backend") + 1], "vllm-server")
        self.assertEqual(
            command[command.index("--server-url") + 1],
            "http://127.0.0.1:9999/v1",
        )

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    def test_vllm_session_reclaims_server_and_descendant_processes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_dir = root / "model"
            model_dir.mkdir()
            captured = []
            real_popen = paddle_pipeline.subprocess.Popen

            def command(port):
                script = (
                    "from http.server import BaseHTTPRequestHandler,HTTPServer;"
                    "import subprocess,sys;"
                    "subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                    "H=type('H',(BaseHTTPRequestHandler,),{"
                    "'do_GET':lambda s:(s.send_response(200),s.end_headers()),"
                    "'log_message':lambda *a:None});"
                    f"HTTPServer(('127.0.0.1',{port}),H).serve_forever()"
                )
                return [sys.executable, "-u", "-c", script]

            def recording_popen(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                captured.append(process)
                return process

            with (
                mock.patch.object(paddle_pipeline, "PADDLE_BACKEND", "vllm-server"),
                mock.patch.object(paddle_pipeline, "VLLM_PYTHON", Path(sys.executable)),
                mock.patch.object(paddle_pipeline, "VLLM_MODEL_DIR", model_dir),
                mock.patch.object(paddle_pipeline, "VLLM_START_TIMEOUT_SECONDS", 5),
                mock.patch.object(paddle_pipeline, "vllm_server_command", side_effect=command),
                mock.patch.object(paddle_pipeline.subprocess, "Popen", side_effect=recording_popen),
            ):
                with paddle_pipeline.paddle_inference_session(root / "worker.log") as session:
                    self.assertEqual(session["backend"], "vllm-server")

            self.assertEqual(len(captured), 1)
            self.assertIsNotNone(captured[0].poll())
            self.assertFalse(
                paddle_pipeline.shared.posix_process_group_exists(captured[0].pid)
            )

    def test_page_checkpoints_load_by_original_page_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result_dir = root / "page_results"
            result_dir.mkdir()
            (result_dir / "page_0002.json").write_text(
                json.dumps({"page_index": 1, "cells": []}), encoding="utf-8"
            )
            results = paddle_pipeline.load_page_results(root, 3)
            self.assertEqual(list(results), [1])

    def test_engine_specific_batch_runner_keeps_separate_outputs(self):
        runner = paddle_pipeline.batch_runner()
        self.assertEqual(runner.engine_name, "PaddleOCR-VL")
        self.assertEqual(runner.output_dir.name, "pdf_paddle")
        self.assertEqual(runner.log_suffix, "paddle")

    def test_shared_renderer_builds_paddle_text_pdf_from_normalized_cells(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.pdf"
            with fitz.open() as document:
                document.new_page(width=300, height=400)
                document.new_page(width=300, height=400)
                document.save(source)
            result = {
                "cells": [
                    {
                        "bbox": [40, 40, 260, 100],
                        "category": "Text",
                        "text": "Paddle shared renderer",
                        "__bbox_units": "pdf",
                    }
                ],
                "fallback_text": "",
                "filtered": False,
                "needs_retry": False,
                "image_size": None,
                "md_nohf_text": "Paddle shared renderer",
            }
            output_pdf = root / "output.pdf"
            output_images = root / "output_with_images.pdf"
            output_md = root / "output.md"
            with (
                mock.patch.object(paddle_pipeline, "write_paddle_qc_report"),
                mock.patch.object(paddle_pipeline, "log") as log_mock,
            ):
                fallback_pages = paddle_pipeline.build_outputs(
                    source,
                    {0: result},
                    root / "work",
                    root / "raw",
                    output_pdf,
                    output_images,
                    output_md,
                )
            self.assertEqual(fallback_pages, [])
            with fitz.open(output_pdf) as output:
                self.assertEqual(output.page_count, 2)
                self.assertIn("Paddle shared renderer", output[0].get_text())
                self.assertEqual(output[1].get_text().strip(), "")
                self.assertFalse(output[0].get_images(full=True))
            messages = [str(call.args[0]) for call in log_mock.call_args_list]
            self.assertIn("        Render text PDF page 1/2", messages)
            self.assertIn("        Render text PDF page 2/2", messages)
            self.assertIn("        Validate text PDF page 1/2", messages)
            self.assertIn("        Validate text PDF page 2/2", messages)

    def test_unsupported_glyphs_never_emit_null_and_wave_function_psi_is_repaired(self):
        self.assertEqual(
            paddle_pipeline.shared.normalize_draw_segment_text("⚲函数本身不能直接解释"),
            "Ψ函数本身不能直接解释",
        )
        self.assertEqual(
            paddle_pipeline.shared.reportlab_unsupported_chars("A⚲B"),
            ["⚲"],
        )
        self.assertEqual(
            paddle_pipeline.shared.reportlab_safe_text("A⚲B"),
            "A□B",
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.pdf"
            with fitz.open() as document:
                document.new_page(width=300, height=400)
                document.save(source)
            result = {
                "cells": [
                    {
                        "bbox": [40, 80, 260, 120],
                        "category": "Text",
                        "text": "⚲函数本身不能直接解释",
                        "__bbox_units": "pdf",
                    },
                    {
                        "bbox": [40, 160, 260, 200],
                        "category": "Text",
                        "text": "未知符号⚲保留为可见缺字标记",
                        "__bbox_units": "pdf",
                    },
                ],
                "fallback_text": "",
                "filtered": False,
                "needs_retry": False,
                "image_size": None,
                "md_nohf_text": "⚲函数本身不能直接解释",
            }
            output_pdf = root / "output.pdf"
            with mock.patch.object(paddle_pipeline, "write_paddle_qc_report"):
                fallback_pages = paddle_pipeline.build_outputs(
                    source,
                    {0: result},
                    root / "work",
                    root / "raw",
                    output_pdf,
                    root / "output_with_images.pdf",
                    root / "output.md",
                )

            self.assertEqual(fallback_pages, [])
            scan = paddle_pipeline.shared.scan_pdf_validation(output_pdf)
            self.assertEqual(scan["control_char_offenders"], {})
            with fitz.open(output_pdf) as output:
                page = output[0]
                extracted = page.get_text()
                self.assertNotIn("\x00", extracted)
                self.assertIn("Ψ函数本身不能直接解释", extracted)
                self.assertIn("未知符号□保留为可见缺字标记", extracted)
                self.assertFalse(page.get_images(full=True))

    def test_formula_signed_superscript_does_not_emit_null_character(self):
        formula = (
            r"$$ k_{\mu}\Gamma_{\mu}(q,p)=S_{F}^{-1}(q)"
            r"-S_{F}^{-1}(p),k=q-p, $$"
        )
        self.assertIsNone(paddle_pipeline.shared.formula_to_unicode_if_simple(formula))
        self.assertEqual(
            paddle_pipeline.shared.normalize_draw_segment_text("F^-1"),
            "F⁻¹",
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.pdf"
            with fitz.open() as document:
                document.new_page(width=300, height=400)
                document.save(source)
            result = {
                "cells": [
                    {
                        "bbox": [40, 180, 260, 220],
                        "category": "Formula",
                        "text": formula,
                        "__bbox_units": "pdf",
                    },
                    {
                        "bbox": [40, 300, 260, 330],
                        "category": "Page-footer",
                        "text": "VISIBLE FOOTER 448",
                        "__bbox_units": "pdf",
                    },
                ],
                "fallback_text": "",
                "filtered": False,
                "needs_retry": False,
                "image_size": None,
                "md_nohf_text": formula,
            }
            output_pdf = root / "output.pdf"
            output_images = root / "output_with_images.pdf"
            output_md = root / "output.md"
            with mock.patch.object(paddle_pipeline, "write_paddle_qc_report"):
                fallback_pages = paddle_pipeline.build_outputs(
                    source,
                    {0: result},
                    root / "work",
                    root / "raw",
                    output_pdf,
                    output_images,
                    output_md,
                )

            self.assertEqual(fallback_pages, [])
            with fitz.open(output_pdf) as output:
                page = output[0]
                extracted = page.get_text()
                self.assertNotIn("\x00", extracted)
                self.assertIn("F⁻¹", extracted)
                self.assertIn("VISIBLE FOOTER 448", extracted)
                self.assertFalse(page.get_images(full=True))
                footer = page.get_pixmap(
                    matrix=fitz.Matrix(2, 2),
                    clip=fitz.Rect(40, 300, 260, 330),
                    colorspace=fitz.csGRAY,
                    alpha=False,
                )
                self.assertLess(min(footer.samples), 200)

    def test_unfitting_non_table_text_uses_safe_page_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.pdf"
            with fitz.open() as document:
                document.new_page(width=300, height=400)
                document.save(source)
            result = {
                "cells": [
                    {
                        "bbox": [10, 200, 20, 205],
                        "category": "Text",
                        "text": "This long OCR paragraph cannot fit in a ten point wide box.",
                        "__bbox_units": "pdf",
                    }
                ],
                "fallback_text": "",
                "filtered": False,
                "needs_retry": False,
                "image_size": None,
                "md_nohf_text": "This long OCR paragraph cannot fit in a ten point wide box.",
            }
            output_pdf = root / "output.pdf"
            output_images = root / "output_with_images.pdf"
            output_md = root / "output.md"
            with mock.patch.object(paddle_pipeline, "write_paddle_qc_report"):
                fallback_pages = paddle_pipeline.build_outputs(
                    source,
                    {0: result},
                    root / "work",
                    root / "raw",
                    output_pdf,
                    output_images,
                    output_md,
                )

            self.assertEqual(fallback_pages, [0])
            self.assertTrue(result["image_fallback_page"])
            self.assertTrue(result["layout_fit_failures"])
            with fitz.open(output_pdf) as output:
                self.assertEqual(output.page_count, 1)
                self.assertEqual(output[0].get_text().strip(), "")
                self.assertFalse(output[0].get_images(full=True))
            with fitz.open(output_images) as output_images_document:
                self.assertEqual(output_images_document.page_count, 1)
                self.assertTrue(output_images_document[0].get_images(full=True))


if __name__ == "__main__":
    unittest.main()
