"""Cross-process exclusion for one OCR runtime tree."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sys
from typing import TextIO


class TaskLockBusyError(RuntimeError):
    """Raised when another OCR batch owns the runtime lock."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CrossProcessTaskLock:
    """Non-blocking POSIX advisory lock with human-readable owner metadata."""

    def __init__(
        self,
        path: Path,
        *,
        engine_name: str,
        input_dir: Path,
        output_dir: Path,
    ) -> None:
        self.path = Path(path)
        self.engine_name = engine_name
        self.input_dir = Path(input_dir)
        self.output_dir = Path(output_dir)
        self._stream: TextIO | None = None
        self._started_at: str | None = None

    def _metadata(self, status: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": 1,
            "status": status,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "engine": self.engine_name,
            "input_dir": str(self.input_dir.resolve()),
            "output_dir": str(self.output_dir.resolve()),
            "python": str(Path(sys.executable).resolve()),
            "command": list(sys.argv),
            "started_at": self._started_at,
            "updated_at": _utc_now(),
        }
        if status == "released":
            payload["finished_at"] = payload["updated_at"]
        return payload

    def _write_metadata(self, status: str) -> None:
        if self._stream is None:
            return
        self._stream.seek(0)
        self._stream.truncate()
        json.dump(self._metadata(status), self._stream, ensure_ascii=False, indent=2)
        self._stream.write("\n")
        self._stream.flush()
        os.fsync(self._stream.fileno())

    @staticmethod
    def _read_owner(stream: TextIO) -> str:
        try:
            stream.seek(0)
            payload = json.load(stream)
        except Exception:
            return "owner metadata unavailable"
        details = [
            f"pid={payload.get('pid', 'unknown')}",
            f"engine={payload.get('engine', 'unknown')}",
            f"started_at={payload.get('started_at', 'unknown')}",
        ]
        return ", ".join(details)

    def __enter__(self) -> "CrossProcessTaskLock":
        if os.name != "posix":
            raise RuntimeError("Cross-process OCR task locking requires a POSIX runtime")
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+", encoding="utf-8", newline="\n")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            owner = self._read_owner(stream)
            stream.close()
            raise TaskLockBusyError(
                f"Another OCR task is already using this runtime ({owner}); "
                f"lock={self.path}"
            ) from exc
        except BaseException:
            stream.close()
            raise

        self._stream = stream
        self._started_at = _utc_now()
        try:
            self._write_metadata("running")
        except BaseException:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()
                self._stream = None
            raise
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        if self._stream is None:
            return
        import fcntl

        try:
            self._write_metadata("released")
        finally:
            try:
                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            finally:
                self._stream.close()
                self._stream = None


__all__ = ["CrossProcessTaskLock", "TaskLockBusyError"]
