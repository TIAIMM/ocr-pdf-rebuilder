from __future__ import annotations

import json
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from ocr_pdf_rebuilder.gui import (  # noqa: E402
    GuiController,
    GuiHttpServer,
    make_handler,
)


class GuiControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.input_dir = self.root / "input"
        self.output_dir = self.root / "output"
        self.summary_path = self.root / "logs/batch_summary.json"
        self.input_dir.mkdir()
        self.output_dir.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def controller(self, command):
        return GuiController(
            input_dir=self.input_dir,
            output_dir=self.output_dir,
            summary_path=self.summary_path,
            command_factory=lambda: command,
        )

    @staticmethod
    def wait_until_finished(controller: GuiController, timeout: float = 5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not controller.status()["running"]:
                return
            time.sleep(0.02)
        raise AssertionError("GUI child process did not finish")

    def test_start_requires_an_input_pdf(self):
        controller = self.controller([sys.executable, "-c", "pass"])
        with self.assertRaisesRegex(RuntimeError, "没有 PDF"):
            controller.start()

    def test_child_path_prioritizes_current_python_environment(self):
        env = GuiController._source_environment()
        first_path = Path(env["PATH"].split(os.pathsep, 1)[0])
        self.assertEqual(first_path, Path(sys.executable).resolve().parent)

    def test_default_command_selects_engine_specific_entry(self):
        self.assertEqual(
            GuiController._default_command("mineru")[-1],
            "ocr_pdf_rebuilder.mineru_pipeline",
        )
        self.assertEqual(
            GuiController._default_command("paddle")[-1],
            "ocr_pdf_rebuilder.paddle_textonly_pdf",
        )

    def test_run_captures_utf8_log_and_exit_state(self):
        (self.input_dir / "sample.pdf").write_bytes(b"fixture")
        command = [sys.executable, "-u", "-c", "print('生成完成')"]
        controller = self.controller(command)

        controller.start()
        self.wait_until_finished(controller)

        status = controller.status()
        log = controller.log_since(0)["text"]
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["returncode"], 0)
        self.assertIn("生成完成", log)
        self.assertIn("任务正常完成", log)

    def test_tracks_total_progress_for_each_file_from_pipeline_log(self):
        for name in ("first.pdf", "second.pdf"):
            (self.input_dir / name).write_bytes(b"fixture")
        script = (
            "print('PDFs:   2');"
            "print('[1/2] Start: first.pdf');"
            "print('    Pages:  100');"
            "print('    [1/5] Running MinerU parser');"
            "print('    MinerU task chunk_0001_pages_0001_0060: original pages 1-60 (60 page(s))');"
            "print('Processing pages: 100%|##########| 60/60');"
            "print('    [2/5] Loading MinerU JSON');"
            "print('    [3/5] Building layout pages: 0/100');"
            "print('        Layout page 50/100: blocks=8');"
            "print('        Layout page 100/100: blocks=7');"
            "print('    [4/5] Rendering text-only PDF with ReportLab');"
            "print('    [5/5] Done');"
            "print('[2/2] Skip existing current-version text output: second.pdf')"
        )
        controller = self.controller([sys.executable, "-u", "-c", script])

        controller.start()
        self.wait_until_finished(controller)

        progress = controller.status()["file_progress"]
        self.assertEqual([item["name"] for item in progress], ["first.pdf", "second.pdf"])
        self.assertEqual(progress[0]["status"], "completed")
        self.assertEqual(progress[0]["percent"], 100.0)
        self.assertEqual(progress[0]["page_current"], 100)
        self.assertEqual(progress[0]["page_total"], 100)
        self.assertEqual(progress[1]["status"], "skipped")
        self.assertEqual(progress[1]["percent"], 100.0)

    def test_running_progress_combines_parser_and_layout_page_counts(self):
        (self.input_dir / "sample.pdf").write_bytes(b"fixture")
        controller = self.controller([sys.executable, "-c", "pass"])
        inputs = controller.list_inputs()
        with controller._lock:
            controller._reset_file_progress(inputs)
            controller._consume_progress_text(
                "[1/1] Start: sample.pdf\n"
                "    Pages:  200\n"
                "    [1/5] Running MinerU parser\n"
                "    MinerU task chunk_0002_pages_0061_0120: original pages 61-120 (60 page(s))\n"
                "Processing pages:  50%|#####| 30/60\n"
            )
        parser_progress = controller.status()["file_progress"][0]
        self.assertEqual(parser_progress["page_current"], 90)
        self.assertEqual(parser_progress["percent"], 29.2)

        with controller._lock:
            controller._consume_progress_text(
                "    [3/5] Building layout pages: 0/200\n"
                "        Layout page 100/200: blocks=12\n"
            )
        layout_progress = controller.status()["file_progress"][0]
        self.assertEqual(layout_progress["stage"], "构建页面布局")
        self.assertEqual(layout_progress["page_current"], 100)
        self.assertEqual(layout_progress["percent"], 80.0)

    def test_heartbeat_marks_active_stage_as_alive_without_advancing_percent(self):
        (self.input_dir / "sample.pdf").write_bytes(b"fixture")
        controller = self.controller([sys.executable, "-c", "pass"])
        with controller._lock:
            controller._reset_file_progress(controller.list_inputs())
            controller._consume_progress_text(
                "[1/1] Start: sample.pdf\n"
                "    Pages:  200\n"
                "    [1/5] Running MinerU parser\n"
                "    MinerU task chunk: original pages 1-60 (60 page(s))\n"
                "[controller] MinerU still running: elapsed=5m00s, "
                "no new output=4m30s\n"
            )

        progress = controller.status()["file_progress"][0]
        self.assertEqual(progress["percent"], 0.1)
        self.assertIn("已运行 5m00s", progress["stage"])
        self.assertIn("静默 4m30s（进程存活）", progress["stage"])

    def test_tracks_owned_mineru_api_startup_and_restart(self):
        (self.input_dir / "sample.pdf").write_bytes(b"fixture")
        controller = self.controller([sys.executable, "-c", "pass"])
        with controller._lock:
            controller._reset_file_progress(controller.list_inputs())
            controller._consume_progress_text(
                "[1/1] Start: sample.pdf\n"
                "    Pages:  399\n"
                "    [1/5] Running MinerU parser\n"
                "    Starting batch-owned MinerU API; one model load will be shared\n"
                "    MinerU API still starting: elapsed=30.0s (pid=123)\n"
            )
        progress = controller.status()["file_progress"][0]
        self.assertEqual(progress["stage"], "启动并预热 MinerU 服务 · 已运行 30.0s")
        self.assertEqual(progress["percent"], 0.1)

        with controller._lock:
            controller._consume_progress_text(
                "    MinerU API ready: http://127.0.0.1:43210 (pid=123)\n"
                "    Restarting batch-owned MinerU API (1/2): EngineDeadError\n"
            )
        progress = controller.status()["file_progress"][0]
        self.assertEqual(progress["stage"], "重启并预热 MinerU 服务")

    def test_tracks_paddleocr_page_progress(self):
        (self.input_dir / "sample.pdf").write_bytes(b"fixture")
        controller = self.controller([sys.executable, "-c", "pass"])
        with controller._lock:
            controller._reset_file_progress(controller.list_inputs())
            controller._consume_progress_text(
                "[1/1] Start: sample.pdf\n"
                "    Pages:  10\n"
                "    [1/5] Running PaddleOCR-VL parser\n"
                "PaddleOCR page 4/10: blocks=12\n"
            )
        progress = controller.status()["file_progress"][0]
        self.assertEqual(progress["stage"], "PaddleOCR-VL 逐页识别")
        self.assertEqual(progress["page_current"], 4)
        self.assertEqual(progress["percent"], 26.0)

    def test_tracks_stage_four_render_and_validation_page_progress(self):
        (self.input_dir / "sample.pdf").write_bytes(b"fixture")
        controller = self.controller([sys.executable, "-c", "pass"])
        with controller._lock:
            controller._reset_file_progress(controller.list_inputs())
            controller._consume_progress_text(
                "[1/1] Start: sample.pdf\n"
                "    Pages:  200\n"
                "    [4/5] Rendering text-only PDF with ReportLab\n"
                "        Render text PDF page 50/200\n"
            )
        progress = controller.status()["file_progress"][0]
        self.assertEqual(progress["stage"], "渲染纯文字 PDF")
        self.assertEqual(progress["page_current"], 50)
        self.assertEqual(progress["page_total"], 200)
        self.assertEqual(progress["percent"], 86.2)

        with controller._lock:
            controller._consume_progress_text(
                "        Validate text PDF page 100/200\n"
            )
        progress = controller.status()["file_progress"][0]
        self.assertEqual(progress["stage"], "验证纯文字 PDF")
        self.assertEqual(progress["page_current"], 100)
        self.assertEqual(progress["percent"], 91.5)

        with controller._lock:
            controller._consume_progress_text(
                "    Rendering image-variant PDF for 2 fallback page(s): output.pdf\n"
                "        Render image PDF page 100/200\n"
                "        Validate image PDF page 50/200\n"
            )
        progress = controller.status()["file_progress"][0]
        self.assertEqual(progress["stage"], "验证带图片 PDF")
        self.assertEqual(progress["page_current"], 50)
        self.assertEqual(progress["percent"], 95.8)

    def test_lists_outputs_and_reads_summary(self):
        output = self.output_dir / "结果 文件.pdf"
        output.write_bytes(b"pdf")
        self.summary_path.parent.mkdir()
        self.summary_path.write_text(
            json.dumps({"status": "completed", "_checkpoint_sha256": "secret"}),
            encoding="utf-8",
        )
        controller = self.controller([sys.executable, "-c", "pass"])

        outputs = controller.list_outputs()
        summary = controller.read_summary()

        self.assertEqual(outputs[0]["name"], output.name)
        self.assertIn("%E7%BB%93%E6%9E%9C", outputs[0]["download_url"])
        self.assertEqual(summary, {"status": "completed"})
        self.assertEqual(controller.output_file("..%2Fsecret.pdf"), None)

    def test_stale_running_summary_is_reported_as_interrupted(self):
        self.summary_path.parent.mkdir()
        self.summary_path.write_text(
            json.dumps({"status": "running", "counts": {}}),
            encoding="utf-8",
        )
        controller = self.controller([sys.executable, "-c", "pass"])

        summary = controller.read_summary()

        self.assertEqual(summary["status"], "interrupted")
        self.assertTrue(summary["stale_running"])
        self.assertIn("没有任务进程或任务锁", summary["interrupted_reason"])

    @unittest.skipUnless(os.name == "posix", "safe interrupt uses POSIX process groups")
    def test_stop_interrupts_running_controller(self):
        (self.input_dir / "sample.pdf").write_bytes(b"fixture")
        command = [sys.executable, "-u", "-c", "import time; print('ready'); time.sleep(60)"]
        controller = self.controller(command)
        controller.start()

        controller.stop()
        self.wait_until_finished(controller)

        self.assertEqual(controller.status()["status"], "interrupted")
        self.assertIn("正在请求安全停止", controller.log_since(0)["text"])

    @unittest.skipUnless(os.name == "posix", "descendant cleanup uses POSIX groups")
    def test_stop_reclaims_gui_task_descendants(self):
        (self.input_dir / "sample.pdf").write_bytes(b"fixture")
        child_pid_file = self.root / "child.pid"
        child_code = "import time;time.sleep(60)"
        script = (
            "import pathlib,subprocess,sys,time;"
            f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
            f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid));"
            "print('ready',flush=True);time.sleep(60)"
        )
        controller = self.controller([sys.executable, "-u", "-c", script])
        controller.start()
        deadline = time.monotonic() + 5
        while not child_pid_file.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(child_pid_file.is_file())
        child_pid = int(child_pid_file.read_text())

        controller.stop()
        self.wait_until_finished(controller)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail(f"GUI descendant process {child_pid} survived cleanup")

    def test_http_status_and_csrf_protection(self):
        controller = self.controller([sys.executable, "-c", "pass"])
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(controller, "test-token"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request("GET", "/api/status")
            response = connection.getresponse()
            status_payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(status_payload["status"], "idle")

            connection.request("POST", "/api/start")
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 403)

            connection.request("POST", "/api/start", headers={"X-CSRF-Token": "test-token"})
            response = connection.getresponse()
            error_payload = json.loads(response.read())
            self.assertEqual(response.status, 409)
            self.assertIn("没有 PDF", error_payload["error"])
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_gui_server_enables_safe_address_reuse(self):
        self.assertTrue(GuiHttpServer.allow_reuse_address)


if __name__ == "__main__":
    unittest.main()
