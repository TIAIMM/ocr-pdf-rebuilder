from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from support import load_pipeline
from ocr_pdf_rebuilder.batch_runner import PdfBatchRunner


class BatchAndProcessStabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pipeline = load_pipeline(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_one_bad_file_does_not_stop_later_files(self):
        input_dir = self.root / "input"
        input_dir.mkdir()
        pdfs = [input_dir / name for name in ("a.pdf", "b.pdf", "c.pdf")]
        for path in pdfs:
            path.write_bytes(b"fixture")

        calls = []

        def fake_process(path, index, total):
            calls.append(path.name)
            if path.name == "b.pdf":
                raise RuntimeError("bad input")
            return {"status": "completed"}

        log_dir = self.root / "logs"
        runner = PdfBatchRunner(
            input_dir=input_dir,
            output_dir=self.root / "output",
            mineru_output_dir=self.root / "raw",
            log_dir=log_dir,
            process_pdf=fake_process,
            write_checkpoint=self.pipeline.write_checkpoint,
            logger=lambda _message: None,
        )
        with (
            self.assertRaises(SystemExit) as exit_context,
        ):
            runner.run()

        self.assertEqual(exit_context.exception.code, 1)
        self.assertEqual(calls, ["a.pdf", "b.pdf", "c.pdf"])
        summary = self.pipeline.read_checkpoint(log_dir / "batch_summary.json")
        self.assertEqual(summary["status"], "completed_with_failures")
        self.assertEqual(summary["counts"], {"completed": 2, "skipped": 0, "failed": 1})

    @unittest.skipUnless(os.name == "posix", "process-group semantics require POSIX")
    def test_total_timeout_kills_descendant_process_group(self):
        child_pid_file = self.root / "child.pid"
        child_code = (
            "import os,signal,time,pathlib;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            f"pathlib.Path({str(child_pid_file)!r}).write_text(str(os.getpid()));"
            "time.sleep(60)"
        )
        parent_code = (
            "import subprocess,sys,time;"
            f"subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
            "print('child started', flush=True);"
            "time.sleep(60)"
        )
        with self.assertRaisesRegex(RuntimeError, "total timeout"):
            self.pipeline.run_live_process(
                [sys.executable, "-c", parent_code],
                self.root,
                self.root / "timeout.log",
                stream_to_console=False,
                timeout_seconds=0.5,
                idle_timeout_seconds=None,
                termination_grace_seconds=0.2,
            )

        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail(f"descendant process {child_pid} survived process-group cleanup")

    @unittest.skipUnless(os.name == "posix", "process-group semantics require POSIX")
    def test_normal_parent_exit_reclaims_lingering_descendant(self):
        child_pid_file = self.root / "normal-exit-child.pid"
        child_code = (
            "import os,time,pathlib;"
            f"pathlib.Path({str(child_pid_file)!r}).write_text(str(os.getpid()));"
            "time.sleep(60)"
        )
        parent_code = (
            "import subprocess,sys;"
            f"subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
            "print('parent exiting',flush=True)"
        )
        returncode = self.pipeline.run_live_process(
            [sys.executable, "-c", parent_code],
            self.root,
            self.root / "normal-exit.log",
            stream_to_console=False,
            timeout_seconds=5,
            idle_timeout_seconds=None,
            termination_grace_seconds=0.2,
            process_label="test OCR worker",
        )
        self.assertEqual(returncode, 0)
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail(f"descendant process {child_pid} survived normal parent exit")

    def test_transient_exhaustion_does_not_create_split_checkpoint(self):
        source = self.root / "source.pdf"
        source.write_bytes(b"fixture")
        work = self.root / "work"
        raw = self.root / "raw"
        log = self.root / "run.log"
        error = self.pipeline.MinerUParserAttemptsExhausted(
            "engine core failed",
            transient_only=True,
            failures=[{"attempt": 1, "transient": True}],
        )
        with (
            mock.patch.object(
                self.pipeline, "checkpoint_identity", return_value={"schema": 2}
            ),
            mock.patch.object(
                self.pipeline, "run_mineru_parser_with_retries", side_effect=error
            ),
            mock.patch.object(self.pipeline, "log"),
            self.assertRaisesRegex(RuntimeError, "was not split"),
        ):
            self.pipeline.build_mineru_parser_runs(source, 4, work, raw, log)

        checkpoints = list((work / "mineru_checkpoints").glob("*.json"))
        self.assertEqual(checkpoints, [])


if __name__ == "__main__":
    unittest.main()
