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
                self.assertEqual(output.page_count, 2)
                self.assertIn("Paddle shared renderer", output[0].get_text())
                self.assertEqual(output[1].get_text().strip(), "")
                self.assertFalse(output[0].get_images(full=True))


if __name__ == "__main__":
    unittest.main()
