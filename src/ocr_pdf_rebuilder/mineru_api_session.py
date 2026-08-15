"""Document-owned persistent MinerU API lifecycle management."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from typing import BinaryIO
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .process_control import LiveProcessController


def _bool_cli_value(value: bool) -> str:
    return "true" if value else "false"


def _normalize_api_url(value: str) -> str:
    return str(value).strip().rstrip("/")


class MinerUApiSession:
    """Lazily own one local MinerU API, or health-check an external one.

    A local server is deliberately scoped to one document. Its stdin remains
    connected to this process so MinerU's official EOF watcher can shut it
    down even if the pipeline is interrupted. A dedicated process group is a
    second cleanup boundary for vLLM descendants.
    """

    def __init__(
        self,
        *,
        api_url: str | None,
        api_command: str | Sequence[str],
        host: str,
        enable_vlm_preload: bool,
        max_concurrent_requests: int,
        startup_timeout_seconds: float,
        health_timeout_seconds: float,
        shutdown_grace_seconds: float,
        start_attempts: int,
        max_restarts: int,
        heartbeat_seconds: float,
        cwd: Path,
        server_output_root: Path,
        log_path: Path,
        logger: Callable[[str], None],
        process_controller: LiveProcessController,
    ) -> None:
        normalized_url = _normalize_api_url(api_url) if api_url else None
        self._external_url = normalized_url
        self._api_command = (
            [] if normalized_url is not None else self._command_prefix(api_command)
        )
        self._host = str(host)
        self._enable_vlm_preload = bool(enable_vlm_preload)
        self._max_concurrent_requests = max(1, int(max_concurrent_requests))
        self._startup_timeout_seconds = max(0.1, float(startup_timeout_seconds))
        self._health_timeout_seconds = max(0.1, float(health_timeout_seconds))
        self._shutdown_grace_seconds = max(0.1, float(shutdown_grace_seconds))
        self._start_attempts = max(1, int(start_attempts))
        self._max_restarts = max(0, int(max_restarts))
        self._heartbeat_seconds = max(0.0, float(heartbeat_seconds))
        self._cwd = Path(cwd)
        self._server_output_root = Path(server_output_root)
        self._log_path = Path(log_path)
        self._logger = logger
        self._process_controller = process_controller

        self._process: subprocess.Popen[bytes] | None = None
        self._process_group_id: int | None = None
        self._log_handle: BinaryIO | None = None
        self._url = normalized_url
        self._generation = 0
        self._restart_count = 0
        self._restart_streak = 0
        self._external_checked = False
        self._closed = False

    @staticmethod
    def _command_prefix(command: str | Sequence[str]) -> list[str]:
        if isinstance(command, (str, os.PathLike)):
            executable = shutil.which(str(command))
            if executable is None:
                candidate = Path(command).expanduser()
                sibling = Path(sys.executable).resolve().parent / str(command)
                if not candidate.exists() and sibling.exists():
                    candidate = sibling
                if not candidate.exists():
                    raise RuntimeError(f"MinerU API command not found: {command}")
                executable = str(candidate.resolve())
            return [executable]
        prefix = [str(item) for item in command]
        if not prefix:
            raise ValueError("MinerU API command cannot be empty")
        return prefix

    @property
    def owns_process(self) -> bool:
        return self._external_url is None

    @property
    def started(self) -> bool:
        return self._process is not None

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def restart_count(self) -> int:
        return self._restart_count

    @property
    def restart_streak(self) -> int:
        return self._restart_streak

    @property
    def process_id(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def __enter__(self) -> "MinerUApiSession":
        if self._closed:
            raise RuntimeError("MinerU API session is already closed")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    @staticmethod
    def _reserve_port(host: str) -> int:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as listener:
            listener.bind((host, 0))
            return int(listener.getsockname()[1])

    def _health_payload(self, url: str) -> dict[str, object]:
        opener = build_opener(ProxyHandler({}))
        request = Request(f"{url}/health", method="GET")
        with opener.open(request, timeout=self._health_timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("status") != "healthy":
            raise RuntimeError(f"MinerU API returned an unhealthy payload: {payload!r}")
        return payload

    def _is_healthy(self, url: str) -> tuple[bool, dict[str, object] | None]:
        try:
            return True, self._health_payload(url)
        except (HTTPError, URLError, OSError, TimeoutError, ValueError, RuntimeError):
            return False, None

    def _log_tail(self, max_bytes: int = 32_768) -> str:
        try:
            with self._log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - max_bytes), os.SEEK_SET)
                return handle.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    def _server_command(self, port: int) -> list[str]:
        return [
            *self._api_command,
            "--host",
            self._host,
            "--port",
            str(port),
            "--enable-vlm-preload",
            _bool_cli_value(self._enable_vlm_preload),
        ]

    def _server_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["MINERU_API_MAX_CONCURRENT_REQUESTS"] = str(
            self._max_concurrent_requests
        )
        environment["MINERU_API_SHUTDOWN_ON_STDIN_EOF"] = "1"
        environment["MINERU_API_DISABLE_ACCESS_LOG"] = "1"
        environment["MINERU_API_OUTPUT_ROOT"] = str(self._server_output_root)
        environment["PYTHONUNBUFFERED"] = "1"
        return environment

    def _clear_server_output(self) -> None:
        try:
            if self._server_output_root.exists():
                shutil.rmtree(self._server_output_root)
        except OSError as exc:
            raise RuntimeError(
                f"Could not clear MinerU API task output: "
                f"{self._server_output_root}: {exc}"
            ) from exc

    def _start_once(self, port: int) -> str:
        self._cwd.mkdir(parents=True, exist_ok=True)
        self._clear_server_output()
        self._server_output_root.mkdir(parents=True, exist_ok=True)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        mode = "wb" if self._generation == 0 else "ab"
        self._log_handle = self._log_path.open(mode, buffering=0)
        command = self._server_command(port)
        if mode == "ab":
            self._log_handle.write(b"\n\n")
        self._log_handle.write(
            ("$ " + " ".join(command) + "\n\n").encode("utf-8", errors="replace")
        )
        self._logger(f"    MinerU API command: {' '.join(command)}")
        self._logger(f"    MinerU API log: {self._log_path}")

        try:
            process = subprocess.Popen(
                command,
                cwd=str(self._cwd),
                env=self._server_environment(),
                stdin=subprocess.PIPE,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                text=False,
                bufsize=0,
                start_new_session=(os.name == "posix"),
            )
        except BaseException:
            self._log_handle.close()
            self._log_handle = None
            raise
        self._process = process
        self._process_group_id = (
            os.getpgid(process.pid) if os.name == "posix" else None
        )
        url = f"http://{self._host}:{port}"
        started_at = time.monotonic()
        last_heartbeat = started_at
        deadline = started_at + self._startup_timeout_seconds

        while time.monotonic() < deadline:
            returncode = process.poll()
            if returncode is not None:
                tail = self._log_tail()
                raise RuntimeError(
                    f"MinerU API exited during startup with code {returncode}. "
                    f"See log: {self._log_path}\n{tail[-4000:]}"
                )
            healthy, payload = self._is_healthy(url)
            if healthy:
                self._url = url
                self._generation += 1
                elapsed = time.monotonic() - started_at
                concurrency = (payload or {}).get("max_concurrent_requests")
                self._logger(
                    f"    MinerU API ready: {url} (pid={process.pid}, "
                    f"generation={self._generation}, max_concurrency={concurrency}, "
                    f"startup={elapsed:.1f}s)"
                )
                return url
            now = time.monotonic()
            if (
                self._heartbeat_seconds > 0
                and now - last_heartbeat >= self._heartbeat_seconds
            ):
                self._logger(
                    f"    MinerU API still starting: elapsed={now - started_at:.1f}s "
                    f"(pid={process.pid})"
                )
                last_heartbeat = now
            time.sleep(0.25)

        raise RuntimeError(
            f"MinerU API did not become healthy within "
            f"{self._startup_timeout_seconds:.1f}s. See log: {self._log_path}"
        )

    def _stop_current(self, reason: str) -> None:
        process = self._process
        process_group_id = self._process_group_id
        log_handle = self._log_handle
        self._process = None
        self._process_group_id = None
        self._log_handle = None
        self._url = self._external_url
        if process is None:
            if log_handle is not None:
                log_handle.close()
            return

        try:
            if process.stdin is not None and not process.stdin.closed:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=self._shutdown_grace_seconds)
            except subprocess.TimeoutExpired:
                self._process_controller.terminate_process_group(
                    process,
                    process_group_id,
                    reason,
                    self._shutdown_grace_seconds,
                )
            else:
                if (
                    process_group_id is not None
                    and not self._process_controller.wait_for_process_group_exit(
                        process_group_id,
                        self._process_controller.exit_cleanup_seconds,
                        process=process,
                    )
                ):
                    self._process_controller.terminate_process_group(
                        process,
                        process_group_id,
                        f"{reason}; API parent exited but vLLM descendants remained",
                        self._shutdown_grace_seconds,
                    )
        finally:
            if log_handle is not None:
                log_handle.close()

    def _start_owned(self) -> str:
        last_error: RuntimeError | None = None
        for bind_attempt in range(1, self._start_attempts + 1):
            port = self._reserve_port(self._host)
            try:
                return self._start_once(port)
            except RuntimeError as exc:
                last_error = exc
                tail = self._log_tail().lower()
                self._stop_current("MinerU API startup failed")
                address_conflict = any(
                    marker in tail
                    for marker in ("address already in use", "port is already in use")
                )
                if address_conflict and bind_attempt < self._start_attempts:
                    self._logger(
                        f"    MinerU API port race on startup attempt "
                        f"{bind_attempt}/{self._start_attempts}; selecting a new port"
                    )
                    continue
                raise
        assert last_error is not None
        raise last_error

    def ensure_ready(self) -> str:
        if self._closed:
            raise RuntimeError("MinerU API session is already closed")

        if self._external_url is not None:
            healthy, payload = self._is_healthy(self._external_url)
            if not healthy:
                raise RuntimeError(
                    f"Configured external MinerU API is not healthy: {self._external_url}"
                )
            if not self._external_checked:
                self._logger(
                    f"    Using configured external MinerU API: {self._external_url} "
                    f"(max_concurrency={(payload or {}).get('max_concurrent_requests')})"
                )
                self._external_checked = True
            return self._external_url

        if self._process is None:
            self._logger(
                "    Starting batch-owned MinerU API; one model load will be shared "
                "by this document's chunks and retries"
            )
            return self._start_owned()

        healthy, _payload = self._is_healthy(self._url or "")
        if self._process.poll() is None and healthy and self._url:
            return self._url

        return self.restart("API process or health endpoint was unavailable before a task")

    def restart(self, reason: str) -> str:
        if self._external_url is not None:
            raise RuntimeError(
                "The configured external MinerU API is unhealthy and is not owned by this batch"
            )
        if self._restart_streak >= self._max_restarts:
            raise RuntimeError(
                f"MinerU API restart limit exhausted ({self._max_restarts}): {reason}"
            )
        self._restart_count += 1
        self._restart_streak += 1
        self._logger(
            f"    Restarting batch-owned MinerU API "
            f"({self._restart_streak}/{self._max_restarts} for this task, "
            f"total={self._restart_count}): {reason}"
        )
        self._stop_current("MinerU API restart")
        return self._start_owned()

    def recover_after_failure(self, error: BaseException, log_path: Path | None = None) -> bool:
        detail = str(error).strip() or error.__class__.__name__
        if log_path:
            detail = f"{detail}; parser log={log_path}"
        if self._external_url is not None:
            healthy, _payload = self._is_healthy(self._external_url)
            state = "healthy" if healthy else "unhealthy"
            self._logger(
                f"    External MinerU API is {state} after a transient parser failure; "
                "this batch will not restart a service it does not own"
            )
            return False
        self.restart(f"transient parser/engine failure: {detail}")
        return True

    def mark_task_success(self) -> None:
        self._restart_streak = 0

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._external_url is not None:
            return
        if self._process is not None:
            process_id = self._process.pid
            self._stop_current("document-owned MinerU API session ended")
            self._logger(
                f"    Stopped batch-owned MinerU API pid={process_id}; "
                f"generations={self._generation}, restarts={self._restart_count}"
            )
        try:
            self._clear_server_output()
        except RuntimeError as exc:
            self._logger(f"    Warning: {exc}")


__all__ = ["MinerUApiSession"]
