from __future__ import annotations

import json
import importlib.util
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ocr_pdf_rebuilder.batch_runner import PdfBatchRunner
from ocr_pdf_rebuilder.gui import GuiController, make_handler
from ocr_pdf_rebuilder.selected_run import bind_run, run_paths, MANIFEST_NAME, manifest_path, state_root
from ocr_pdf_rebuilder.task_lock import CrossProcessTaskLock


class SelectedRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "原文.pdf"
        self.source.write_bytes(b"%PDF-fixture")
        self.output = self.root / "selected"
        self.args = dict(input_files=[str(self.source)], output_dir=str(self.output),
                         run_id="queue-123", pipeline="paddle")
        self.controller = GuiController(self.root / "input", self.root / "legacy",
            self.root / "logs/batch_summary.json", command_factory=lambda: [sys.executable, "-c", "pass"])

    def wait(self):
        deadline = time.monotonic() + 10
        while self.controller._process is not None and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertIsNone(self.controller._process)

    def test_binding_resume_and_source_change(self):
        first = bind_run(**self.args)
        self.assertEqual(first, bind_run(**self.args))
        second = bind_run(**{**self.args, "run_id": "different"})
        self.assertNotEqual(state_root(first), state_root(second))
        self.source.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            bind_run(**self.args)

    def test_selection_and_shared_nonempty_destination(self):
        for files in ([], "oops", ["relative.pdf"], [str(self.source)] * 2):
            with self.subTest(files=files), self.assertRaises(ValueError):
                bind_run(**{**self.args, "input_files": files})
        self.output.mkdir()
        (self.output / "keep.txt").write_text("keep")
        bind_run(**self.args)
        self.assertEqual((self.output / "keep.txt").read_text(), "keep")

    def test_child_manifest_no_input_scan_and_legacy_restore(self):
        command = [sys.executable, "-c",
            "import json,os;from pathlib import Path;"
            "m=json.loads(Path(os.environ['OCR_SELECTED_RUN_MANIFEST']).read_text(encoding='utf-8'));"
            "assert len(m['input_files'])==1;print(m['run_id'])"]
        self.controller.command_factory = lambda: command
        with patch.object(Path, "glob", side_effect=AssertionError("input scan")):
            self.controller.start(**self.args)
            self.wait()
            status = self.controller.status()
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["external_run_id"], "queue-123")
        self.assertIn("queue-123", self.controller.log_since(0)["text"])
        self.assertFalse(self.controller.input_dir.exists())
        self.controller.start(**self.args)
        self.wait()
        self.assertEqual(self.controller.status()["external_run_id"], "queue-123")
        self.controller.input_dir.mkdir()
        (self.controller.input_dir / "legacy.pdf").write_bytes(b"fixture")
        self.controller.command_factory = lambda: [sys.executable, "-c",
            "import os;assert 'OCR_SELECTED_RUN_MANIFEST' not in os.environ"]
        self.controller.start()
        self.wait()
        self.assertEqual(self.controller.output_dir, self.root / "legacy")
        self.assertIsNone(self.controller.status()["external_run_id"])

    def test_runner_uses_only_selection_and_preserves_shared_lock(self):
        manifest = bind_run(**self.args)
        calls, summaries = [], []
        with patch.dict(os.environ, {"OCR_SELECTED_RUN_MANIFEST": str(manifest_path(bind_run(**self.args)))}):
            runner = PdfBatchRunner(input_dir=self.root / "input", output_dir=self.output,
                mineru_output_dir=self.output / "raw", log_dir=self.output / "logs",
                process_pdf=lambda path, *_: calls.append(path),
                write_checkpoint=lambda path, payload: summaries.append(payload), logger=lambda _: None)
            with patch.object(Path, "glob", side_effect=AssertionError("directory discovery")):
                runner.run()
            self.assertEqual(run_paths(self.root, "paddle")[0], self.output)
        self.assertEqual(calls, [self.source])
        self.assertEqual(summaries[-1]["external_run_id"], manifest["run_id"])
        self.assertEqual(runner.lock_path, self.root / "tmp/ocr_pdf_rebuilder/task.lock")
        with CrossProcessTaskLock(runner.lock_path, engine_name="other", input_dir=self.root, output_dir=self.root):
            with self.assertRaises(SystemExit) as caught:
                runner.run()
            self.assertEqual(caught.exception.code, 2)

    def test_default_paths_unchanged_without_manifest(self):
        with patch.dict(os.environ, {}, clear=True):
            for engine in ("paddle", "mineru"):
                output, raw, tmp, logs, qc = run_paths(self.root, engine)
                self.assertEqual(output, self.root / f"pdf_{engine}")
                self.assertEqual(raw, self.root / f"{engine}_output")
                self.assertEqual(tmp, self.root / f"tmp/{engine}_textonly_pdf")
                self.assertEqual(logs, self.root / f"logs_{engine}")
                self.assertEqual(qc, self.root / f"qc_{engine}")

    @unittest.skipUnless(all(importlib.util.find_spec(name) for name in ("fitz", "reportlab", "svglib")),
                         "pipeline dependencies required")
    def test_actual_pipeline_factories_receive_selected_paths(self):
        bind_run(**self.args)
        env = os.environ.copy()
        env.update(OCR_SELECTED_RUN_MANIFEST=str(manifest_path(bind_run(**self.args))),
                   OCR_RUNTIME_ROOT=str(self.root / "runtime"),
                   PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        result = subprocess.run([sys.executable, "-c", "\n".join([
            "import os,json",
            "from pathlib import Path",
            "from ocr_pdf_rebuilder import paddle_pipeline, mineru_pipeline",
            "root=Path(os.environ['OCR_RUNTIME_ROOT'])",
            "manifest=json.loads(Path(os.environ['OCR_SELECTED_RUN_MANIFEST']).read_text(encoding='utf-8'))",
            "output=Path(manifest['output_dir'])",
            "for engine,runner in [('paddle',paddle_pipeline.batch_runner()),('mineru',mineru_pipeline.pdf_batch_runner())]:",
            " assert runner.output_dir == output",
            " assert runner.log_dir == Path(manifest['state_root']) / engine / 'logs'",
            " assert runner.input_files == [Path(p) for p in manifest['input_files']]",
            " assert runner.lock_path == root / 'tmp/ocr_pdf_rebuilder/task.lock'",
            "assert paddle_pipeline.shared.CJK_REGULAR_FONT.parent == root / 'fonts'",
        ])], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_stop_guard_and_spawn_failure(self):
        with patch("ocr_pdf_rebuilder.gui.subprocess.Popen", side_effect=OSError("spawn failed")):
            with self.assertRaises(OSError):
                self.controller.start(**self.args)
        self.assertEqual(self.controller.status()["status"], "failed")
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            self.controller.stop("wrong")
        self.controller.start(**self.args)
        self.wait()

    @unittest.skipUnless(os.name == "posix", "POSIX signal integration")
    def test_selected_stop_resume_and_busy_exclusion(self):
        self.controller.command_factory = lambda: [sys.executable, "-u", "-c",
            "import time;print('ready',flush=True);time.sleep(30)"]
        self.controller.start(**self.args)
        try:
            deadline = time.monotonic() + 5
            while "ready" not in self.controller.log_since(0)["text"] and time.monotonic() < deadline:
                time.sleep(.02)
            with self.assertRaises(RuntimeError):
                self.controller.start(**self.args)
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                self.controller.stop("wrong")
            self.assertTrue(self.controller.status()["running"])
            self.controller.stop("queue-123")
            self.wait()
            self.assertEqual(self.controller.status()["status"], "interrupted")
            self.controller.command_factory = lambda: [sys.executable, "-c", "pass"]
            self.controller.start(**self.args)
            self.wait()
            self.assertEqual(self.controller.status()["external_run_id"], "queue-123")
            self.assertEqual(self.controller.status()["status"], "completed")
        finally:
            self.controller.force_stop()
            self.wait()

    def test_http_selected_contract_and_markdown_download(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.controller, "csrf", "secret"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        headers = {"Authorization": "Bearer secret", "Content-Type": "application/json"}
        try:
            connection.request("GET", "/workbench/v1/health", headers=headers)
            response = connection.getresponse()
            self.assertTrue(json.loads(response.read())["selected_inputs"])
            connection.request("POST", "/workbench/v1/start", json.dumps(self.args), headers)
            response = connection.getresponse()
            body = json.loads(response.read())
            self.assertEqual(response.status, 200, body)
            self.assertEqual(body["external_run_id"], "queue-123")
            self.wait()
            markdown = self.output / "原文_paddle.md"
            markdown.write_text("Readable OCR", encoding="utf-8")
            output = self.controller.list_outputs()[0]
            self.assertEqual(output["path"], str(markdown))
            connection.request("GET", output["download_url"])
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"Readable OCR")
            connection.request("POST", "/workbench/v1/start", json.dumps({**self.args, "run_id": None}), headers)
            response = connection.getresponse()
            self.assertEqual(response.status, 409)
            response.read()
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join()

    def test_restart_restores_identity_and_marks_unfinished_interrupted(self):
        self.controller.start(**self.args)
        self.wait()
        kwargs = dict(input_dir=self.controller.input_dir, output_dir=self.root / "legacy",
                      summary_path=self.root / "logs/batch_summary.json")
        restored = GuiController(**kwargs)
        self.assertEqual(restored.status()["external_run_id"], "queue-123")
        self.assertEqual(restored.status()["status"], "completed")
        self.controller._status = "running"
        self.controller._persist_run_state()
        restored = GuiController(**kwargs)
        status = restored.status()
        self.assertEqual(status["status"], "interrupted")
        self.assertFalse(status["running"])
        self.assertEqual(status["output_dir"], str(self.output))
        self.assertEqual(status["input_files"], [str(self.source)])


if __name__ == "__main__":
    unittest.main()
