from __future__ import annotations

import os
from pathlib import Path
import fitz
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from ocr_pdf_rebuilder.mineru_api_session import MinerUApiSession
from ocr_pdf_rebuilder.process_control import LiveProcessController
from support import load_pipeline


FAKE_API_CODE = r"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

parser = argparse.ArgumentParser()
parser.add_argument("--host", required=True)
parser.add_argument("--port", required=True, type=int)
parser.add_argument("--enable-vlm-preload")
args = parser.parse_args()

child_pid_file = os.environ.get("FAKE_API_CHILD_PID_FILE")
if child_pid_file:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path(child_pid_file).write_text(str(child.pid), encoding="utf-8")

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps({
            "status": "healthy",
            "protocol_version": "test",
            "max_concurrent_requests": int(
                os.environ.get("MINERU_API_MAX_CONCURRENT_REQUESTS", "0")
            ),
            "pid": os.getpid(),
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return

server = ThreadingHTTPServer((args.host, args.port), Handler)

def stop_on_stdin_eof():
    sys.stdin.buffer.read()
    server.shutdown()

threading.Thread(target=stop_on_stdin_eof, daemon=True).start()
server.serve_forever()
server.server_close()
"""


class MinerUApiSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.messages: list[str] = []

    def tearDown(self):
        self.temp.cleanup()

    def session(self, **overrides):
        arguments = {
            "api_url": None,
            "api_command": [sys.executable, "-u", "-c", FAKE_API_CODE],
            "host": "127.0.0.1",
            "enable_vlm_preload": True,
            "max_concurrent_requests": 1,
            "startup_timeout_seconds": 5,
            "health_timeout_seconds": 0.2,
            "shutdown_grace_seconds": 2,
            "start_attempts": 2,
            "max_restarts": 2,
            "heartbeat_seconds": 0.05,
            "cwd": self.root,
            "server_output_root": self.root / "api-output",
            "log_path": self.root / "api.log",
            "logger": self.messages.append,
            "process_controller": LiveProcessController(
                logger=self.messages.append,
                console_lock=threading.Lock(),
                exit_cleanup_seconds=0.5,
                process_label="test MinerU API",
            ),
        }
        arguments.update(overrides)
        return MinerUApiSession(**arguments)

    @staticmethod
    def assert_process_exited(process_id: int) -> None:
        if os.name != "posix":
            return
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(process_id, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        raise AssertionError(f"process {process_id} did not exit")

    def test_session_is_lazy_when_all_parser_checkpoints_are_reused(self):
        session = self.session()
        with session:
            self.assertFalse(session.started)
            self.assertEqual(session.generation, 0)

        self.assertFalse((self.root / "api.log").exists())
        self.assertFalse(any("Starting batch-owned" in line for line in self.messages))

    def test_all_requests_reuse_one_owned_server_and_context_exit_reclaims_it(self):
        session = self.session()
        with session:
            first_url = session.ensure_ready()
            first_pid = session.process_id
            second_url = session.ensure_ready()
            self.assertEqual(second_url, first_url)
            self.assertEqual(session.process_id, first_pid)
            self.assertEqual(session.generation, 1)
            self.assertEqual(session.restart_count, 0)

        self.assertIsNotNone(first_pid)
        self.assert_process_exited(first_pid)
        self.assertFalse((self.root / "api-output").exists())
        self.assertTrue(any("Stopped batch-owned" in line for line in self.messages))

    def test_restart_replaces_poisoned_server_and_remains_bounded(self):
        session = self.session(max_restarts=1)
        with session:
            first_url = session.ensure_ready()
            first_pid = session.process_id
            second_url = session.restart("EngineDeadError")
            second_pid = session.process_id
            self.assertNotEqual(second_url, first_url)
            self.assertNotEqual(second_pid, first_pid)
            self.assertEqual(session.generation, 2)
            self.assertEqual(session.restart_count, 1)
            self.assert_process_exited(first_pid)
            with self.assertRaisesRegex(RuntimeError, "restart limit exhausted"):
                session.restart("another failure")

        self.assert_process_exited(second_pid)

    def test_success_resets_per_task_restart_budget(self):
        session = self.session(max_restarts=1)
        with session:
            session.ensure_ready()
            session.restart("first task failure")
            self.assertEqual(session.restart_streak, 1)
            session.mark_task_success()
            self.assertEqual(session.restart_streak, 0)
            session.restart("later task failure")
            self.assertEqual(session.restart_count, 2)
            self.assertEqual(session.restart_streak, 1)

    def test_context_exit_reclaims_api_descendants(self):
        child_pid_file = self.root / "api-child.pid"
        with mock.patch.dict(
            os.environ,
            {"FAKE_API_CHILD_PID_FILE": str(child_pid_file)},
        ):
            with self.session() as session:
                session.ensure_ready()
                child_pid = int(child_pid_file.read_text(encoding="utf-8"))

        self.assert_process_exited(child_pid)

    def test_external_service_does_not_require_or_spawn_local_command(self):
        session = self.session(
            api_url="http://127.0.0.1:9/",
            api_command="command-that-does-not-exist",
        )
        with session, self.assertRaisesRegex(RuntimeError, "not healthy"):
            session.ensure_ready()
        self.assertFalse(session.started)

    def test_command_falls_back_to_current_python_bin_directory(self):
        python_path = self.root / "environment/bin/python"
        sibling = python_path.parent / "fake-mineru-api"
        sibling.parent.mkdir(parents=True)
        sibling.write_text("fixture", encoding="utf-8")
        with (
            mock.patch(
                "ocr_pdf_rebuilder.mineru_api_session.shutil.which",
                return_value=None,
            ),
            mock.patch(
                "ocr_pdf_rebuilder.mineru_api_session.sys.executable",
                str(python_path),
            ),
        ):
            prefix = MinerUApiSession._command_prefix("fake-mineru-api")
        self.assertEqual(prefix, [str(sibling.resolve())])


class MinerURunnerSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pipeline = load_pipeline(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_dynamic_api_url_is_passed_to_cli_once(self):
        args = self.pipeline.optional_cli_args(api_url="http://127.0.0.1:43210")
        self.assertEqual(args.count("--api-url"), 1)
        position = args.index("--api-url")
        self.assertEqual(args[position + 1], "http://127.0.0.1:43210")

    def test_historical_cuda_engine_death_is_transient(self):
        log_path = self.root / "mineru.log"
        log_path.write_text(
            "EngineCore encountered a fatal error\n"
            "torch.AcceleratorError: CUDA error: unknown error\n"
            "EngineDeadError",
            encoding="utf-8",
        )
        self.assertTrue(
            self.pipeline.mineru_failure_is_transient("parser failed", log_path)
        )

    def test_transient_retry_restarts_owned_api_before_second_attempt(self):
        source = self.root / "source.pdf"
        output = self.root / "output"
        log_path = self.root / "mineru.log"
        source.write_bytes(b"fixture")
        api_session = mock.Mock()
        with (
            mock.patch.object(
                self.pipeline,
                "run_mineru_parser",
                side_effect=[RuntimeError("EngineDeadError"), None],
            ) as run_parser,
            mock.patch.object(self.pipeline, "mineru_output_has_results", return_value=True),
            mock.patch.object(
                self.pipeline,
                "build_mineru_result_manifest",
                return_value={"manifest": "ok"},
            ),
            mock.patch.object(self.pipeline, "PARSER_RETRY_BACKOFF_SECONDS", 0),
            mock.patch.object(self.pipeline, "log"),
        ):
            result = self.pipeline.run_mineru_parser_with_retries(
                source,
                output,
                log_path,
                max_attempts=2,
                api_session=api_session,
            )

        self.assertEqual(result, {"manifest": "ok"})
        api_session.recover_after_failure.assert_called_once()
        api_session.mark_task_success.assert_called_once_with()
        self.assertIs(
            run_parser.call_args_list[0].kwargs["api_session"],
            api_session,
        )
        self.assertIs(
            run_parser.call_args_list[1].kwargs["api_session"],
            api_session,
        )

    def test_all_initial_chunks_receive_the_same_api_session(self):
        source = self.root / "source.pdf"
        document = fitz.open()
        document.new_page()
        document.new_page()
        document.save(source)
        document.close()
        api_session = object()
        with (
            mock.patch.object(self.pipeline, "MINERU_MAX_PAGES_PER_TASK", 1),
            mock.patch.object(
                self.pipeline,
                "checkpoint_identity",
                return_value={"schema": 2},
            ),
            mock.patch.object(self.pipeline, "read_checkpoint", return_value=None),
            mock.patch.object(
                self.pipeline,
                "run_mineru_parser_with_retries",
                return_value={"files": []},
            ) as parser,
            mock.patch.object(self.pipeline, "write_checkpoint"),
            mock.patch.object(self.pipeline, "log"),
        ):
            runs = self.pipeline.build_mineru_parser_runs(
                source,
                2,
                self.root / "work",
                self.root / "raw",
                self.root / "mineru.log",
                api_session=api_session,
            )

        self.assertEqual(len(runs), 2)
        self.assertEqual(parser.call_count, 2)
        self.assertTrue(
            all(
                call.kwargs["api_session"] is api_session
                for call in parser.call_args_list
            )
        )


if __name__ == "__main__":
    unittest.main()
