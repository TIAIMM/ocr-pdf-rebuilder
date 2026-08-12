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

from ocr_pdf_rebuilder.gui import GuiController, make_handler  # noqa: E402


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

    @unittest.skipUnless(os.name == "posix", "safe interrupt uses POSIX process groups")
    def test_stop_interrupts_running_controller(self):
        (self.input_dir / "sample.pdf").write_bytes(b"fixture")
        command = [sys.executable, "-u", "-c", "import time; print('ready'); time.sleep(60)"]
        controller = self.controller(command)
        controller.start()

        controller.stop()
        self.wait_until_finished(controller)

        self.assertEqual(controller.status()["status"], "failed")
        self.assertIn("正在请求安全停止", controller.log_since(0)["text"])

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


if __name__ == "__main__":
    unittest.main()
