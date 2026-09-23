from __future__ import annotations

import ctypes
import os
from pathlib import Path
import re
import sys
import tempfile
from threading import Lock
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from ocr_pdf_rebuilder import cli, paddle_pipeline
from ocr_pdf_rebuilder.gui import PIPELINES, GuiController
from ocr_pdf_rebuilder.process_control import LiveProcessController
from ocr_pdf_rebuilder.task_lock import CrossProcessTaskLock, TaskLockBusyError, task_lock_is_held


class NativeDefaultsTests(unittest.TestCase):
    def test_windows_defaults_are_local_transformers(self):
        if os.name != "nt":
            self.skipTest("Windows defaults")
        self.assertEqual(paddle_pipeline.PADDLE_BACKEND, "native")
        self.assertEqual(paddle_pipeline.PADDLE_ENGINE, "transformers")
        self.assertIn("--engine", paddle_pipeline.worker_command(Path("x.pdf"), Path("raw")))
        self.assertIn("--layout-model-dir", paddle_pipeline.worker_command(Path("x.pdf"), Path("raw")))
        self.assertEqual(tuple(PIPELINES), ("paddle",))

    def test_engine_and_model_paths_change_cache_identity(self):
        with mock.patch.object(paddle_pipeline, "WINDOWS_NATIVE", True):
            original = paddle_pipeline.paddle_config_hash()
            with mock.patch.object(paddle_pipeline, "PADDLE_ENGINE", "paddle"):
                self.assertNotEqual(original, paddle_pipeline.paddle_config_hash())
            with mock.patch.object(paddle_pipeline, "PADDLE_LAYOUT_MODEL_DIR", Path("different")):
                self.assertNotEqual(original, paddle_pipeline.paddle_config_hash())

    def test_windows_preflight_reports_missing_models(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(paddle_pipeline, "WINDOWS_NATIVE", True), mock.patch.object(
                paddle_pipeline, "PADDLE_BACKEND", "native"
            ), mock.patch.object(paddle_pipeline, "PADDLE_ENGINE", "transformers"), mock.patch.object(
                paddle_pipeline, "PADDLE_LAYOUT_MODEL_DIR", Path(temporary) / "missing"
            ):
                with self.assertRaisesRegex(RuntimeError, "prerequisites missing"):
                    paddle_pipeline.preflight_windows_native()

    def test_default_cli_uses_wsl_paddle_and_requires_explicit_windows_opt_in(self):
        with mock.patch.object(cli, "os", SimpleNamespace(name="nt")):
            with self.assertRaisesRegex(SystemExit, "opt-in fallback"):
                cli.main()
            with mock.patch("ocr_pdf_rebuilder.paddle_textonly_pdf.main") as run:
                cli.windows_native_main()
                run.assert_called_once()
        with mock.patch.object(cli, "os", SimpleNamespace(name="posix")):
            with mock.patch("ocr_pdf_rebuilder.paddle_textonly_pdf.main") as run:
                cli.main()
                run.assert_called_once()
            with self.assertRaisesRegex(SystemExit, "must run on Windows"):
                cli.windows_native_main()


@unittest.skipUnless(os.name == "nt", "Windows process and lock coverage")
class WindowsLifecycleTests(unittest.TestCase):
    @staticmethod
    def assert_process_stopped(pid: int) -> None:
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        api.OpenProcess.restype = ctypes.c_void_p
        api.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        api.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = api.OpenProcess(0x1000, False, pid)
        if handle:
            try:
                exit_code = ctypes.c_uint32()
                api.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                if exit_code.value == 259:
                    raise AssertionError(f"process {pid} remains active")
            finally:
                api.CloseHandle(handle)

    def test_task_lock_excludes_second_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "task.lock"
            one = CrossProcessTaskLock(path, engine_name="one", input_dir=Path(temporary), output_dir=Path(temporary))
            two = CrossProcessTaskLock(path, engine_name="two", input_dir=Path(temporary), output_dir=Path(temporary))
            self.assertFalse(task_lock_is_held(path))
            with one:
                self.assertTrue(task_lock_is_held(path))
                with self.assertRaises(TaskLockBusyError):
                    with two:
                        pass
            self.assertFalse(task_lock_is_held(path))

    def test_timeout_reclaims_descendant_after_parent_exits(self):
        code = (
            "import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(90)']); "
            "print('CHILD_PID='+str(p.pid),flush=True); time.sleep(90)"
        )
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "child.log"
            controller = LiveProcessController(logger=lambda _: None, console_lock=Lock(),
                                               exit_cleanup_seconds=0.1, process_label="test")
            with self.assertRaisesRegex(RuntimeError, "total timeout"):
                controller.run([sys.executable, "-u", "-c", code], Path(temporary), log_path,
                               stream_to_console=False, timeout_seconds=2,
                               idle_timeout_seconds=None, heartbeat_seconds=None,
                               termination_grace_seconds=1)
            match = re.search(r"CHILD_PID=(\d+)", log_path.read_text(encoding="utf-8"))
            self.assertIsNotNone(match)
            self.assert_process_stopped(int(match.group(1)))

    def test_normal_parent_exit_reclaims_descendant(self):
        code = ("import subprocess,sys; "
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(90)']); "
                "print('CHILD_PID='+str(p.pid),flush=True)")
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "parent.log"
            controller = LiveProcessController(logger=lambda _: None, console_lock=Lock(),
                                               exit_cleanup_seconds=0.1, process_label="test")
            result = controller.run([sys.executable, "-u", "-c", code], Path(temporary), log_path,
                                    stream_to_console=False, timeout_seconds=8,
                                    idle_timeout_seconds=None, heartbeat_seconds=None,
                                    termination_grace_seconds=1)
            self.assertEqual(result, 0)
            match = re.search(r"CHILD_PID=(\d+)", log_path.read_text(encoding="utf-8"))
            self.assertIsNotNone(match)
            self.assert_process_stopped(int(match.group(1)))

    def test_gui_safe_stop_reclaims_worker_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            (input_dir / "sample.pdf").write_bytes(b"fixture")
            child_pid_file = root / "child.pid"
            code = ("import pathlib,subprocess,sys,time; "
                    "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(90)']); "
                    f"pathlib.Path({str(child_pid_file)!r}).write_text(str(p.pid)); "
                    "print('child ready',flush=True); time.sleep(90)")
            controller = GuiController(input_dir=input_dir, output_dir=root / "output",
                                       summary_path=root / "summary.json",
                                       command_factory=lambda: [sys.executable, "-u", "-c", code])
            with mock.patch("ocr_pdf_rebuilder.gui.GUI_TASK_TERMINATE_GRACE_SECONDS", 1.0):
                controller.start()
                deadline = time.monotonic() + 5
                while not child_pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(child_pid_file.exists())
                controller.stop()
                deadline = time.monotonic() + 8
                while controller.status()["running"] and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(controller.status()["running"])
                self.assert_process_stopped(int(child_pid_file.read_text()))
