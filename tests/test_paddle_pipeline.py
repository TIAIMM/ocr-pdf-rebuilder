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

from ocr_pdf_rebuilder import paddle_pipeline, paddle_worker, reportlab_renderer
from ocr_pdf_rebuilder.raster_geometry import bounded_render_dpi


class PaddlePipelineTests(unittest.TestCase):
    def test_paddle_model_protocol_version_is_supported(self):
        self.assertIn(paddle_pipeline.PADDLE_PIPELINE_VERSION, {"v1", "v1.5", "v1.6"})

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
        self.assertIn("output_searchable_pdf", captured)

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

    def test_raster_budget_preserves_normal_pages_and_caps_large_pages(self):
        normal = bounded_render_dpi(595, 842, 200, 24_000_000)
        self.assertEqual(normal["effective_dpi"], 200)
        self.assertFalse(normal["downscaled"])

        large = bounded_render_dpi(5000, 6000, 200, 1_000_000)
        self.assertTrue(large["downscaled"])
        self.assertTrue(large["low_resolution_risk"])
        self.assertLessEqual(large["estimated_pixels"], 1_000_000)
        with fitz.open() as document:
            page = document.new_page(width=5000, height=6000)
            scale = large["effective_dpi"] / 72.0
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            self.assertLessEqual(pixmap.width * pixmap.height, 1_000_000)

        with self.assertRaises(ValueError):
            bounded_render_dpi(595, 842, 200, 0)

    def test_worker_checkpoints_effective_raster_geometry(self):
        class Prediction:
            def save_to_json(self, save_path):
                (Path(save_path) / "prediction.json").write_text(
                    json.dumps({"parsing_res_list": [
                        {"block_label": "text", "block_content": "A page",
                         "block_bbox": [2, 2, 30, 12]}
                    ]}), encoding="utf-8"
                )

        class Pipeline:
            def predict(self, **_kwargs):
                return [Prediction()]

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.pdf"
            with fitz.open() as document:
                document.new_page(width=300, height=400)
                document.save(source)
            args = paddle_worker.build_parser().parse_args([
                "--pdf", str(source), "--output-dir", str(root / "raw"),
                "--dpi", "200", "--max-raster-pixels", "10000",
            ])
            with mock.patch.object(paddle_worker, "create_pipeline", return_value=Pipeline()):
                self.assertEqual(paddle_worker.run(args), 0)
            checkpoint = json.loads(
                (root / "raw/page_results/page_0001.json").read_text(encoding="utf-8")
            )
            raster = checkpoint["rasterization"]
            self.assertTrue(raster["downscaled"])
            self.assertLessEqual(raster["actual_pixels"], 10000)
            self.assertEqual(
                raster["actual_pixels"],
                checkpoint["image_size"][0] * checkpoint["image_size"][1],
            )

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
        self.assertEqual([cell["__source_id"] for cell in result["cells"]], [
            "p0001-b0001", "p0001-b0002"
        ])
        self.assertEqual([cell["__reading_order"] for cell in result["cells"]], [0, 1])

    def test_paddle_worker_normalizes_clear_columns_in_reading_order(self):
        raw = {
            "res": {
                "parsing_res_list": [
                    {
                        "block_label": "text",
                        "block_content": "R1",
                        "block_bbox": [560, 100, 900, 160],
                        "block_order": 0,
                    },
                    {
                        "block_label": "text",
                        "block_content": "L1",
                        "block_bbox": [100, 100, 440, 160],
                        "block_order": 1,
                    },
                    {
                        "block_label": "text",
                        "block_content": "R2",
                        "block_bbox": [560, 190, 900, 250],
                        "block_order": 2,
                    },
                    {
                        "block_label": "text",
                        "block_content": "L2",
                        "block_bbox": [100, 190, 440, 250],
                        "block_order": 3,
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

        self.assertEqual([cell["text"] for cell in result["cells"]], ["L1", "L2", "R1", "R2"])
        self.assertEqual(result["md_nohf_text"], "L1\n\nL2\n\nR1\n\nR2")
        self.assertEqual(
            [cell["__source_index"] for cell in result["cells"]], [1, 3, 0, 2]
        )
        block = paddle_pipeline.shared.cell_to_block(
            result["cells"][0], 0, 300, 400, result["image_size"]
        )
        self.assertEqual(block["source_id"], "p0001-b0002")
        self.assertEqual(block["recognition_order"], 1)

    def test_invalid_picture_bbox_is_reported_instead_of_silently_lost(self):
        raw = {"parsing_res_list": [
            {"block_label": "image", "block_bbox": [10, 10, 10, 50]},
            {"block_label": "text", "block_bbox": [10, 50, 100, 90],
             "block_content": "Valid text"},
        ]}
        result = paddle_worker.normalized_page_result(
            raw, page_index=0, image_width=200, image_height=300,
            raw_json_path=Path("raw.json"),
        )
        self.assertEqual(result["invalid_picture_bbox_count"], 1)
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.pdf"
            with fitz.open() as document:
                document.new_page(width=200, height=300)
                document.save(source)
            suspects = paddle_pipeline.shared.collect_qc_suspect_pages(
                source, {0: result}, [], 1
            )
            self.assertIn("picture_invalid_source_bbox", suspects[1])

    def test_picture_crop_cache_is_tied_to_bbox_and_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.pdf"
            with fitz.open() as document:
                page = document.new_page(width=200, height=100)
                page.draw_rect(fitz.Rect(0, 0, 100, 100), fill=(0, 0, 0))
                document.save(source)
            block = {
                "page_index": 0, "order": 0, "source_pdf_path": str(source),
                "picture_crop_dir": str(root / "crops"), "picture_crop_padding": 0,
                "picture_crop_dpi": 72,
            }
            left = reportlab_renderer.render_picture_crop(
                fitz.Rect(0, 0, 100, 100), block
            )
            self.assertFalse(block["picture_crop_cache_hit"])
            self.assertEqual(block["picture_crop_image_size"], [100, 100])
            reportlab_renderer.render_picture_crop(fitz.Rect(0, 0, 100, 100), block)
            self.assertTrue(block["picture_crop_cache_hit"])
            left.write_bytes(b"broken cached PNG")
            reportlab_renderer.render_picture_crop(fitz.Rect(0, 0, 100, 100), block)
            self.assertFalse(block["picture_crop_cache_hit"])
            self.assertGreater(left.stat().st_size, len(b"broken cached PNG"))
            right = reportlab_renderer.render_picture_crop(
                fitz.Rect(100, 0, 200, 100), block
            )
            self.assertNotEqual(left, right)
            self.assertFalse(block["picture_crop_cache_hit"])
            self.assertTrue(left.is_file() and right.is_file())
            source_pdf_path = block["source_pdf_path"]
            block["picture_crop_max_pixels"] = 5_000
            smaller = reportlab_renderer.render_picture_crop(
                fitz.Rect(100, 0, 200, 100), block
            )
            self.assertNotEqual(right, smaller)
            self.assertLessEqual(
                block["picture_crop_image_size"][0]
                * block["picture_crop_image_size"][1],
                5_000,
            )
            self.assertEqual(block["source_pdf_path"], source_pdf_path)

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
        self.assertEqual(
            command[command.index("--max-raster-pixels") + 1],
            str(paddle_pipeline.PADDLE_MAX_RASTER_PIXELS),
        )
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
            searchable = output_pdf.with_name("output_searchable.pdf")
            with fitz.open(searchable) as overlay:
                self.assertEqual(overlay.page_count, 2)
                self.assertIn("Paddle shared renderer", overlay[0].get_text())
                self.assertEqual(overlay[1].get_text().strip(), "")
                self.assertTrue(all(span["type"] == 0 for span in overlay[0].get_texttrace()))
            paddle_pipeline.shared.validate_searchable_pdf_visual_identity(source, searchable)
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

    def test_picture_crop_is_preserved_only_in_image_variant(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source-picture.pdf"
            with fitz.open() as document:
                page = document.new_page(width=300, height=400)
                page.insert_text((40, 55), "Visible heading", fontsize=12)
                page.draw_rect(
                    fitz.Rect(60, 100, 240, 280),
                    color=(0, 0, 0),
                    fill=(0, 0, 0),
                )
                page.insert_text((40, 330), "Picture caption", fontsize=10)
                document.save(source)

            result = {
                "cells": [
                    {
                        "bbox": [40, 30, 260, 65],
                        "category": "Text",
                        "text": "Visible heading",
                        "__bbox_units": "pdf",
                    },
                    {
                        "bbox": [60, 100, 240, 280],
                        "category": "Picture",
                        "text": "",
                        "__bbox_units": "pdf",
                    },
                    {
                        "bbox": [40, 300, 260, 345],
                        "category": "Text",
                        "text": "Picture caption",
                        "__bbox_units": "pdf",
                    },
                ],
                "fallback_text": "",
                "filtered": False,
                "needs_retry": False,
                "image_size": None,
                "md_nohf_text": "Visible heading\nPicture caption",
            }
            output_pdf = root / "output.pdf"
            output_images = root / "output_with_images.pdf"
            with mock.patch.object(paddle_pipeline, "write_paddle_qc_report"):
                fallback_pages = paddle_pipeline.build_outputs(
                    source,
                    {0: result},
                    root / "work",
                    root / "raw",
                    output_pdf,
                    output_images,
                    root / "output.md",
                )

            self.assertEqual(fallback_pages, [])
            trace = result["layout_trace"]
            self.assertEqual(trace["source_picture_cell_count"], 1)
            self.assertEqual(trace["rendered_picture_count"], 1)
            self.assertTrue(trace["picture_crops"][0]["path"])
            self.assertFalse(trace["source_order_available"])
            self.assertEqual(trace["final_picture_block_count"], 1)
            self.assertEqual(
                paddle_pipeline.shared.image_variant_page_indexes_from_results({0: result}),
                [0],
            )
            with fitz.open(output_pdf) as text_document:
                self.assertEqual(text_document.page_count, 1)
                self.assertIn("Visible heading", text_document[0].get_text())
                self.assertFalse(text_document[0].get_images(full=True))
            with fitz.open(output_images) as image_document:
                self.assertEqual(image_document.page_count, 1)
                self.assertTrue(image_document[0].get_images(full=True))
                picture = image_document[0].get_pixmap(
                    matrix=fitz.Matrix(1, 1),
                    clip=fitz.Rect(60, 100, 240, 280),
                    colorspace=fitz.csGRAY,
                    alpha=False,
                )
                self.assertLess(min(picture.samples), 30)
                self.assertIn("Picture caption", image_document[0].get_text())

    def test_visual_fallback_keeps_text_outputs_and_image_variant_facsimile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source-visual-fallback.pdf"
            fallback_png = root / "fallback.png"
            with fitz.open() as document:
                page = document.new_page(width=300, height=400)
                page.insert_text((40, 60), "Source visual heading", fontsize=12)
                page.draw_rect(
                    fitz.Rect(50, 100, 250, 300),
                    color=(0, 0, 0),
                    fill=(0, 0, 0),
                )
                pixmap = page.get_pixmap(alpha=False)
                pixmap.save(fallback_png)
                document.save(source)

            result = {
                "cells": [
                    {
                        "bbox": [40, 35, 260, 70],
                        "category": "Text",
                        "text": "Preserved OCR heading",
                        "__bbox_units": "pdf",
                    }
                ],
                "fallback_text": "",
                "filtered": False,
                "needs_retry": False,
                "image_size": None,
                "md_nohf_text": "Preserved OCR heading",
                "image_fallback_path": str(fallback_png),
                "image_fallback_page": True,
                "image_fallback_kind": "complex_layout",
                "visual_fallback_text_preserved": True,
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
            self.assertIn("Preserved OCR heading", output_md.read_text(encoding="utf-8"))
            with fitz.open(output_pdf) as text_document:
                self.assertIn("Preserved OCR heading", text_document[0].get_text())
                self.assertFalse(text_document[0].get_images(full=True))
            with fitz.open(output_images) as image_document:
                self.assertTrue(image_document[0].get_images(full=True))
                self.assertNotIn("Preserved OCR heading", image_document[0].get_text())
            searchable = output_pdf.with_name("output_searchable.pdf")
            with fitz.open(searchable) as searchable_document:
                self.assertIn("Preserved OCR heading", searchable_document[0].get_text())

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
