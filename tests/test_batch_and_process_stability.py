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

        self.pipeline.INPUT_DIR = input_dir
        self.pipeline.LOG_DIR = self.root / "logs"
        self.pipeline.OUTPUT_DIR = self.root / "output"
        with (
            mock.patch.object(self.pipeline, "process_pdf", side_effect=fake_process),
            mock.patch.object(self.pipeline, "log"),
            self.assertRaises(SystemExit) as exit_context,
        ):
            self.pipeline.main()

        self.assertEqual(exit_context.exception.code, 1)
        self.assertEqual(calls, ["a.pdf", "b.pdf", "c.pdf"])
        summary = self.pipeline.read_checkpoint(self.pipeline.LOG_DIR / "batch_summary.json")
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
