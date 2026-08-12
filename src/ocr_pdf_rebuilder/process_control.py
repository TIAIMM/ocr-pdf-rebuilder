"""Subprocess lifecycle and process-group cleanup for OCR engine commands."""

from __future__ import annotations

from collections.abc import Callable
import codecs
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import threading
import time


class LiveProcessController:
    """Run a streaming subprocess and guarantee descendant cleanup on failure."""

    def __init__(
        self,
        *,
        logger: Callable[[str], None],
        console_lock: threading.Lock,
        exit_cleanup_seconds: float,
    ) -> None:
        self.logger = logger
        self.console_lock = console_lock
        self.exit_cleanup_seconds = exit_cleanup_seconds

    @staticmethod
    def posix_process_group_exists(process_group_id: int | None) -> bool:
        if os.name != "posix" or process_group_id is None:
            return False
        try:
            os.killpg(process_group_id, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def wait_for_process_group_exit(
        self,
        process_group_id: int | None,
        timeout_seconds: float,
        process: subprocess.Popen[bytes] | None = None,
    ) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while self.posix_process_group_exists(process_group_id):
            if process is not None:
                process.poll()
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)
        return True

    def terminate_process_group(
        self,
        process: subprocess.Popen[bytes],
        process_group_id: int | None,
        reason: str,
        grace_seconds: float,
    ) -> None:
        grace_seconds = max(0.1, float(grace_seconds))
        if os.name == "posix" and process_group_id is not None:
            if not self.posix_process_group_exists(process_group_id):
                try:
                    process.wait(timeout=0.1)
                except Exception:
                    pass
                return
            self.logger(
                f"    Terminating MinerU process group pgid={process_group_id}: {reason}"
            )
            try:
                os.killpg(process_group_id, signal.SIGTERM)
            except ProcessLookupError:
                return
            if self.wait_for_process_group_exit(
                process_group_id,
                grace_seconds,
                process=process,
            ):
                try:
                    process.wait(timeout=0.1)
                except Exception:
                    pass
                return
            self.logger(
                f"    MinerU process group pgid={process_group_id} did not exit within "
                f"{grace_seconds:.1f}s; sending SIGKILL"
            )
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.wait_for_process_group_exit(
                process_group_id,
                min(5.0, grace_seconds),
                process=process,
            )
            try:
                process.wait(timeout=0.5)
            except Exception:
                pass
            return

        if process.poll() is not None:
            return
        self.logger(f"    Terminating MinerU process pid={process.pid}: {reason}")
        try:
            process.terminate()
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        except ProcessLookupError:
            pass

    def run(
        self,
        cmd: list[str],
        cwd: Path,
        log_path: Path,
        *,
        stream_to_console: bool,
        timeout_seconds: float | None,
        idle_timeout_seconds: float | None,
        termination_grace_seconds: float,
    ) -> int:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as handle:
            handle.write("$ " + " ".join(cmd) + "\n\n")
            handle.flush()

            process = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                bufsize=0,
                start_new_session=(os.name == "posix"),
            )
            process_group_id = os.getpgid(process.pid) if os.name == "posix" else None
            assert process.stdout is not None
            stdout_fd = process.stdout.fileno()
            if os.name == "posix":
                os.set_blocking(stdout_fd, False)
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            pending = []
            pending_chars = 0
            last_flush = time.monotonic()
            started_at = last_flush
            last_activity = last_flush
            parent_exit_seen_at = None
            stdout_open = True

            def flush_pending(force: bool = False) -> None:
                nonlocal pending, pending_chars, last_flush
                if not pending:
                    return
                now = time.monotonic()
                if (
                    not force
                    and pending_chars < 8192
                    and len(pending) < 32
                    and now - last_flush < 0.25
                ):
                    return
                payload = "".join(pending)
                if stream_to_console:
                    with self.console_lock:
                        sys.stdout.write(payload)
                        sys.stdout.flush()
                handle.write(payload)
                handle.flush()
                pending = []
                pending_chars = 0
                last_flush = now

            def append_output(payload: str) -> None:
                nonlocal pending_chars
                if payload:
                    pending.append(payload)
                    pending_chars += len(payload)

            def drain_available_output() -> bool:
                nonlocal last_activity, stdout_open
                received = False
                while stdout_open:
                    try:
                        chunk = os.read(stdout_fd, 65536)
                    except BlockingIOError:
                        break
                    if not chunk:
                        stdout_open = False
                        try:
                            selector.unregister(process.stdout)
                        except Exception:
                            pass
                        break
                    received = True
                    last_activity = time.monotonic()
                    append_output(decoder.decode(chunk))
                return received

            try:
                while True:
                    now = time.monotonic()
                    if process.poll() is None:
                        if timeout_seconds is not None and now - started_at >= timeout_seconds:
                            message = (
                                f"MinerU process exceeded total timeout of {timeout_seconds:.1f}s"
                            )
                            append_output(f"\n[controller] {message}\n")
                            flush_pending(force=True)
                            self.terminate_process_group(
                                process,
                                process_group_id,
                                message,
                                termination_grace_seconds,
                            )
                            raise RuntimeError(message)
                        if (
                            idle_timeout_seconds is not None
                            and now - last_activity >= idle_timeout_seconds
                        ):
                            message = (
                                "MinerU process produced no stdout/stderr for "
                                f"{idle_timeout_seconds:.1f}s"
                            )
                            append_output(f"\n[controller] {message}\n")
                            flush_pending(force=True)
                            self.terminate_process_group(
                                process,
                                process_group_id,
                                message,
                                termination_grace_seconds,
                            )
                            raise RuntimeError(message)

                    events = selector.select(timeout=0.25) if stdout_open else []
                    if events:
                        drain_available_output()
                    flush_pending()

                    if process.poll() is not None:
                        if parent_exit_seen_at is None:
                            parent_exit_seen_at = time.monotonic()
                        if stdout_open:
                            drain_available_output()
                        if not stdout_open or time.monotonic() - parent_exit_seen_at >= 1.0:
                            break

                append_output(decoder.decode(b"", final=True))
                flush_pending(force=True)
                returncode = process.wait()
                if process_group_id is not None and not self.wait_for_process_group_exit(
                    process_group_id,
                    self.exit_cleanup_seconds,
                    process=process,
                ):
                    self.terminate_process_group(
                        process,
                        process_group_id,
                        "MinerU parent exited but child processes remained",
                        termination_grace_seconds,
                    )
                return returncode
            except BaseException:
                flush_pending(force=True)
                self.terminate_process_group(
                    process,
                    process_group_id,
                    "controller exception or interruption",
                    termination_grace_seconds,
                )
                raise
            finally:
                try:
                    selector.close()
                finally:
                    process.stdout.close()
