"""Subprocess lifecycle and process-group cleanup for OCR engine commands."""

from __future__ import annotations

from collections.abc import Callable
import codecs
import os
from pathlib import Path
import queue
import selectors
import signal
import subprocess
import sys
import threading
import time

from .windows_process import WindowsJob


def attach_windows_job(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        try:
            process._ocr_windows_job = WindowsJob(process)
        except BaseException:
            process.kill()
            process.wait(timeout=5)
            raise


def close_windows_job(process: subprocess.Popen[bytes]) -> None:
    job = getattr(process, "_ocr_windows_job", None)
    if job is not None:
        job.close()
        process._ocr_windows_job = None


def _format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    minutes, second = divmod(total_seconds, 60)
    hours, minute = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minute:02d}m{second:02d}s"
    if minutes:
        return f"{minutes}m{second:02d}s"
    return f"{second}s"


class LiveProcessController:
    """Run a streaming subprocess and guarantee descendant cleanup on failure."""

    def __init__(
        self,
        *,
        logger: Callable[[str], None],
        console_lock: threading.Lock,
        exit_cleanup_seconds: float,
        process_label: str = "MinerU",
    ) -> None:
        self.logger = logger
        self.console_lock = console_lock
        self.exit_cleanup_seconds = exit_cleanup_seconds
        self.process_label = process_label

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
        job = getattr(process, "_ocr_windows_job", None)
        if os.name == "nt" and job is not None:
            self.logger(f"    Terminating {self.process_label} Windows job pid={process.pid}: {reason}")
            job.terminate()
            try:
                process.wait(timeout=min(grace_seconds, 5.0))
            except subprocess.TimeoutExpired:
                pass
            return
        if os.name == "posix" and process_group_id is not None:
            if not self.posix_process_group_exists(process_group_id):
                try:
                    process.wait(timeout=0.1)
                except Exception:
                    pass
                return
            self.logger(
                f"    Terminating {self.process_label} process group pgid={process_group_id}: {reason}"
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
                f"    {self.process_label} process group pgid={process_group_id} did not exit within "
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
        self.logger(f"    Terminating {self.process_label} process pid={process.pid}: {reason}")
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
        heartbeat_seconds: float | None,
        termination_grace_seconds: float,
    ) -> int:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as handle:
            handle.write("$ " + " ".join(cmd) + "\n\n")
            handle.flush()

            process = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                bufsize=0,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                start_new_session=(os.name == "posix"),
            )
            attach_windows_job(process)
            process_group_id = os.getpgid(process.pid) if os.name == "posix" else None
            assert process.stdout is not None
            stdout_fd = process.stdout.fileno()
            if os.name == "posix":
                os.set_blocking(stdout_fd, False)
            selector = selectors.DefaultSelector() if os.name == "posix" else None
            output_queue: queue.Queue[bytes | None] | None = None
            if selector is not None:
                selector.register(process.stdout, selectors.EVENT_READ)
            else:
                output_queue = queue.Queue()
                def read_pipe() -> None:
                    try:
                        while chunk := process.stdout.read(65536):
                            output_queue.put(chunk)
                    except (OSError, ValueError):
                        pass
                    finally:
                        output_queue.put(None)
                threading.Thread(target=read_pipe, daemon=True).start()
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            pending = []
            pending_chars = 0
            last_flush = time.monotonic()
            started_at = last_flush
            last_activity = last_flush
            last_heartbeat = last_flush
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
                        try:
                            sys.stdout.write(payload)
                        except UnicodeEncodeError:
                            encoding = sys.stdout.encoding or "utf-8"
                            sys.stdout.write(payload.encode(encoding, errors="replace").decode(encoding))
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
                if output_queue is not None:
                    while stdout_open:
                        try:
                            chunk = output_queue.get_nowait()
                        except queue.Empty:
                            break
                        if chunk is None:
                            stdout_open = False
                            break
                        received = True
                        last_activity = time.monotonic()
                        append_output(decoder.decode(chunk))
                    return received
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
                                f"{self.process_label} process exceeded total timeout of {timeout_seconds:.1f}s"
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
                                f"{self.process_label} process produced no stdout/stderr for "
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
                        if (
                            heartbeat_seconds is not None
                            and heartbeat_seconds > 0
                            and now - last_activity >= heartbeat_seconds
                            and now - last_heartbeat >= heartbeat_seconds
                        ):
                            append_output(
                                "\n[controller] "
                                f"{self.process_label} still running: "
                                f"elapsed={_format_duration(now - started_at)}, "
                                f"no new output={_format_duration(now - last_activity)}\n"
                            )
                            flush_pending(force=True)
                            last_heartbeat = now

                    events = selector.select(timeout=0.25) if selector is not None and stdout_open else []
                    if events:
                        drain_available_output()
                    elif output_queue is not None:
                        drain_available_output()
                        time.sleep(0.05)
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
                        f"{self.process_label} parent exited but child processes remained",
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
                    if selector is not None:
                        selector.close()
                finally:
                    close_windows_job(process)
                    process.stdout.close()
