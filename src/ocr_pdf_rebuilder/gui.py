from __future__ import annotations

import argparse
import codecs
from collections import deque
import errno
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import ProxyHandler, build_opener
import webbrowser

from .process_control import LiveProcessController
from .signal_cleanup import termination_raises_keyboard_interrupt
from .task_lock import task_lock_is_held


DEFAULT_RUNTIME_ROOT = Path(
    os.environ.get("OCR_RUNTIME_ROOT", Path(__file__).resolve().parents[2])
).expanduser().resolve()
DEFAULT_INPUT_DIR = DEFAULT_RUNTIME_ROOT / "input"
DEFAULT_OUTPUT_DIR = DEFAULT_RUNTIME_ROOT / "pdf_mineru"
DEFAULT_SUMMARY_PATH = DEFAULT_RUNTIME_ROOT / "logs_mineru/batch_summary.json"
PIPELINES = {
    "mineru": {
        "module": "ocr_pdf_rebuilder.mineru_pipeline",
        "output_dir": DEFAULT_RUNTIME_ROOT / "pdf_mineru",
        "summary_path": DEFAULT_RUNTIME_ROOT / "logs_mineru/batch_summary.json",
        "label": "MinerU",
    },
    "paddle": {
        "module": "ocr_pdf_rebuilder.paddle_textonly_pdf",
        "output_dir": DEFAULT_RUNTIME_ROOT / "pdf_paddle",
        "summary_path": DEFAULT_RUNTIME_ROOT / "logs_paddle/batch_summary.json",
        "label": "PaddleOCR-VL",
    },
}
MAX_LOG_CHARS = 400_000
GUI_TASK_TERMINATE_GRACE_SECONDS = 45.0
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class GuiHttpServer(ThreadingHTTPServer):
    allow_reuse_address = True


def file_record(path: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "name": path.name,
        "size": stat.st_size,
        "modified_at": stat.st_mtime,
    }


class GuiController:
    def __init__(
        self,
        input_dir: Path = DEFAULT_INPUT_DIR,
        output_dir: Path = DEFAULT_OUTPUT_DIR,
        summary_path: Path = DEFAULT_SUMMARY_PATH,
        command_factory=None,
    ):
        self.input_dir = Path(input_dir)
        self.output_dir = Path(output_dir)
        self.summary_path = Path(summary_path)
        self.command_factory = command_factory
        self._dynamic_pipeline_paths = (
            self.output_dir == DEFAULT_OUTPUT_DIR
            and self.summary_path == DEFAULT_SUMMARY_PATH
        )
        self._pipeline = "mineru"
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._process_group_id: int | None = None
        self._status = "idle"
        self._returncode: int | None = None
        self._started_at: float | None = None
        self._finished_at: float | None = None
        self._run_id = 0
        self._log_chunks: deque[str] = deque()
        self._log_chars = 0
        self._log_start = 0
        self._progress_line_buffer = ""
        self._file_progress: list[dict[str, object]] = []
        self._active_file_index: int | None = None
        self._active_parser_page_start = 1

    @staticmethod
    def _default_command(pipeline: str = "mineru") -> list[str]:
        module = str(PIPELINES[pipeline]["module"])
        return [sys.executable, "-u", "-m", module]

    def _command(self, pipeline: str) -> list[str]:
        if self.command_factory is not None:
            return self.command_factory()
        return self._default_command(pipeline)

    def _select_pipeline(self, pipeline: str) -> None:
        if pipeline not in PIPELINES:
            raise RuntimeError(f"未知 OCR 管线：{pipeline}")
        self._pipeline = pipeline
        if self._dynamic_pipeline_paths:
            self.output_dir = Path(PIPELINES[pipeline]["output_dir"])
            self.summary_path = Path(PIPELINES[pipeline]["summary_path"])

    @staticmethod
    def _source_environment() -> dict[str, str]:
        env = os.environ.copy()
        source_root = str(Path(__file__).resolve().parents[1])
        python_bin_dir = str(Path(sys.executable).resolve().parent)
        current = env.get("PYTHONPATH")
        env["PYTHONPATH"] = source_root if not current else os.pathsep.join((source_root, current))
        current_path = env.get("PATH")
        env["PATH"] = (
            python_bin_dir
            if not current_path
            else os.pathsep.join((python_bin_dir, current_path))
        )
        env["PYTHONUNBUFFERED"] = "1"
        env["OCR_RUNTIME_ROOT"] = str(DEFAULT_RUNTIME_ROOT)
        return env

    def _append_log(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._consume_progress_text(text)
            self._log_chunks.append(text)
            self._log_chars += len(text)
            while self._log_chars > MAX_LOG_CHARS and self._log_chunks:
                removed = self._log_chunks.popleft()
                self._log_chars -= len(removed)
                self._log_start += len(removed)

    def _clear_log(self) -> None:
        self._log_chunks.clear()
        self._log_chars = 0
        self._log_start = 0
        self._progress_line_buffer = ""

    def _reset_file_progress(self, inputs: list[dict[str, object]]) -> None:
        self._file_progress = [
            {
                "name": str(record["name"]),
                "status": "pending",
                "stage": "等待处理",
                "percent": 0.0,
                "page_current": 0,
                "page_total": None,
            }
            for record in inputs
        ]
        self._active_file_index = None
        self._active_parser_page_start = 1

    def _progress_record(self, one_based_index: int | None = None) -> dict[str, object] | None:
        index = self._active_file_index if one_based_index is None else one_based_index - 1
        if index is None or index < 0 or index >= len(self._file_progress):
            return None
        return self._file_progress[index]

    @staticmethod
    def _set_progress(
        record: dict[str, object],
        *,
        status: str | None = None,
        stage: str | None = None,
        percent: float | None = None,
        page_current: int | None = None,
        page_total: int | None = None,
    ) -> None:
        if status is not None:
            record["status"] = status
        if stage is not None:
            record["stage"] = stage
        if percent is not None:
            previous = float(record.get("percent") or 0.0)
            record["percent"] = round(max(previous, min(100.0, percent)), 1)
        if page_total is not None and page_total > 0:
            record["page_total"] = int(page_total)
        if page_current is not None:
            total = record.get("page_total")
            current = max(0, int(page_current))
            if isinstance(total, int):
                current = min(current, total)
            record["page_current"] = current

    def _consume_progress_text(self, text: str) -> None:
        normalized = (self._progress_line_buffer + text).replace("\r", "\n")
        lines = normalized.split("\n")
        self._progress_line_buffer = lines.pop()
        for raw_line in lines:
            line = ANSI_ESCAPE_RE.sub("", raw_line).strip()
            if line:
                self._consume_progress_line(line)

    def _flush_progress_text(self) -> None:
        line = ANSI_ESCAPE_RE.sub("", self._progress_line_buffer).strip()
        self._progress_line_buffer = ""
        if line:
            self._consume_progress_line(line)

    def _consume_progress_line(self, line: str) -> None:
        match = re.search(r"^\[(\d+)/(\d+)\]\s+Start:\s+(.+\.pdf)\s*$", line)
        if match:
            index = int(match.group(1))
            previous = self._progress_record()
            if previous and previous.get("status") == "running":
                self._set_progress(previous, status="completed", stage="已完成", percent=100.0)
            self._active_file_index = index - 1
            self._active_parser_page_start = 1
            record = self._progress_record(index)
            if record is not None:
                record["name"] = Path(match.group(3)).name
                self._set_progress(
                    record,
                    status="running",
                    stage="准备文件",
                    percent=0.0,
                    page_current=0,
                )
            return

        match = re.search(r"^\[(\d+)/(\d+)\]\s+Skip existing", line)
        if match:
            record = self._progress_record(int(match.group(1)))
            if record is not None:
                self._set_progress(record, status="skipped", stage="已复用现有成品", percent=100.0)
            return

        match = re.search(r"^\[(\d+)/(\d+)\]\s+FAILED:\s+(.+?\.pdf):", line)
        if match:
            record = self._progress_record(int(match.group(1)))
            if record is not None:
                self._set_progress(record, status="failed", stage="处理失败")
            return

        record = self._progress_record()
        if record is None:
            return

        match = re.search(
            r"^\[controller\]\s+.+? still running: "
            r"elapsed=([^,]+), no new output=(.+)$",
            line,
        )
        if match:
            stage = str(record.get("stage") or "OCR 子进程")
            stage = stage.split(" · 已运行 ", 1)[0]
            self._set_progress(
                record,
                stage=(
                    f"{stage} · 已运行 {match.group(1)}，"
                    f"静默 {match.group(2)}（进程存活）"
                ),
            )
            return

        match = re.search(r"Pages:\s+(\d+)", line)
        if match:
            self._set_progress(record, page_total=int(match.group(1)))
            return

        if "[1/5] Running MinerU parser" in line:
            self._set_progress(record, stage="MinerU 解析", percent=0.1)
            return

        if "Starting batch-owned MinerU API" in line:
            self._set_progress(record, stage="启动并预热 MinerU 服务", percent=0.1)
            return

        if "MinerU API still starting:" in line:
            match = re.search(r"elapsed=([0-9.]+s)", line)
            elapsed = f" · 已运行 {match.group(1)}" if match else ""
            self._set_progress(
                record,
                stage=f"启动并预热 MinerU 服务{elapsed}",
                percent=0.1,
            )
            return

        if "MinerU API ready:" in line:
            self._set_progress(record, stage="MinerU 服务已就绪，准备解析", percent=0.1)
            return

        if "Restarting batch-owned MinerU API" in line:
            self._set_progress(record, stage="重启并预热 MinerU 服务", percent=0.1)
            return

        if "[1/5] Running PaddleOCR-VL parser" in line:
            self._set_progress(record, stage="PaddleOCR-VL 逐页识别", percent=0.1)
            return

        match = re.search(r"PaddleOCR page (\d+)/(\d+):", line)
        if match:
            current = int(match.group(1))
            total = int(match.group(2))
            self._set_progress(
                record,
                stage="PaddleOCR-VL 逐页识别",
                percent=65.0 * current / total,
                page_current=current,
                page_total=total,
            )
            return

        match = re.search(r"MinerU task .*original pages (\d+)-(\d+)", line)
        if match:
            self._active_parser_page_start = int(match.group(1))
            total = record.get("page_total")
            current = self._active_parser_page_start - 1
            percent = 65.0 * current / total if isinstance(total, int) and total else None
            self._set_progress(
                record,
                stage=f"MinerU 解析第 {match.group(1)}–{match.group(2)} 页",
                percent=percent,
                page_current=current,
            )
            return

        match = re.search(r"Processing pages:.*?(\d+)/(\d+)", line)
        if match and str(record.get("stage", "")).startswith("MinerU 解析"):
            current = self._active_parser_page_start - 1 + int(match.group(1))
            total = record.get("page_total")
            percent = 65.0 * current / total if isinstance(total, int) and total else None
            self._set_progress(record, percent=percent, page_current=current)
            return

        if "[2/5] Loading MinerU JSON" in line:
            total = record.get("page_total")
            self._set_progress(
                record,
                stage="加载与修复识别结果",
                percent=65.0,
                page_current=total if isinstance(total, int) else None,
            )
            return

        if "[2/5] Loading and validating PaddleOCR page results" in line:
            total = record.get("page_total")
            self._set_progress(
                record,
                stage="加载与修复 PaddleOCR 结果",
                percent=65.0,
                page_current=total if isinstance(total, int) else None,
            )
            return

        if "Running isolated MinerU repair batch" in line:
            self._set_progress(record, stage="隔离重跑疑似跨页页面", percent=70.0)
            return

        if "[3/5] Building layout pages" in line:
            self._set_progress(record, stage="构建页面布局", percent=75.0, page_current=0)
            return

        match = re.search(r"Layout page (\d+)/(\d+)", line)
        if match:
            current = int(match.group(1))
            total = int(match.group(2))
            self._set_progress(
                record,
                stage="构建页面布局",
                percent=75.0 + 10.0 * current / total,
                page_current=current,
                page_total=total,
            )
            return

        if "[4/5] Rendering text-only PDF" in line:
            self._set_progress(
                record,
                stage="渲染纯文字 PDF",
                percent=85.0,
                page_current=0,
            )
            return

        match = re.search(r"Render text PDF page (\d+)/(\d+)", line)
        if match:
            current = int(match.group(1))
            total = int(match.group(2))
            self._set_progress(
                record,
                stage="渲染纯文字 PDF",
                percent=85.0 + 5.0 * current / total,
                page_current=current,
                page_total=total,
            )
            return

        match = re.search(r"Validate text PDF page (\d+)/(\d+)", line)
        if match:
            current = int(match.group(1))
            total = int(match.group(2))
            self._set_progress(
                record,
                stage="验证纯文字 PDF",
                percent=90.0 + 3.0 * current / total,
                page_current=current,
                page_total=total,
            )
            return

        if "Rendering image-variant PDF" in line:
            self._set_progress(
                record,
                stage="渲染带图片 PDF",
                percent=93.0,
                page_current=0,
            )
            return

        match = re.search(r"Render image PDF page (\d+)/(\d+)", line)
        if match:
            current = int(match.group(1))
            total = int(match.group(2))
            self._set_progress(
                record,
                stage="渲染带图片 PDF",
                percent=93.0 + 2.0 * current / total,
                page_current=current,
                page_total=total,
            )
            return

        match = re.search(r"Validate image PDF page (\d+)/(\d+)", line)
        if match:
            current = int(match.group(1))
            total = int(match.group(2))
            self._set_progress(
                record,
                stage="验证带图片 PDF",
                percent=95.0 + 3.0 * current / total,
                page_current=current,
                page_total=total,
            )
            return
        if line.startswith("QC report:"):
            self._set_progress(record, stage="生成并验证 QC", percent=98.0)
            return
        if "[5/5] Done" in line:
            total = record.get("page_total")
            self._set_progress(
                record,
                status="completed",
                stage="已完成",
                percent=100.0,
                page_current=total if isinstance(total, int) else None,
            )

    def list_inputs(self) -> list[dict[str, object]]:
        if not self.input_dir.exists():
            return []
        return [file_record(path) for path in sorted(self.input_dir.glob("*.pdf")) if path.is_file()]

    def list_outputs(self) -> list[dict[str, object]]:
        if not self.output_dir.exists():
            return []
        records = []
        for path in sorted(self.output_dir.glob("*.pdf"), key=lambda item: item.stat().st_mtime, reverse=True):
            if path.is_file():
                record = file_record(path)
                record["download_url"] = "/files/output/" + quote(path.name)
                records.append(record)
        return records

    def read_summary(self) -> dict[str, object] | None:
        try:
            payload = json.loads(self.summary_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        payload.pop("_checkpoint_sha256", None)
        process = self._process
        controller_running = process is not None and process.poll() is None
        task_lock_path = self.input_dir.parent / "tmp/ocr_pdf_rebuilder/task.lock"
        if (
            payload.get("status") == "running"
            and not controller_running
            and not task_lock_is_held(task_lock_path)
        ):
            payload["status"] = "interrupted"
            payload["stale_running"] = True
            payload["interrupted_reason"] = (
                "摘要遗留为 running，但当前没有任务进程或任务锁"
            )
        return payload

    def start(self, pipeline: str = "mineru") -> None:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("生成任务已经在运行")
            self._select_pipeline(pipeline)
            inputs = self.list_inputs()
            if not inputs:
                raise RuntimeError(f"输入目录中没有 PDF：{self.input_dir}")

            self.input_dir.mkdir(parents=True, exist_ok=True)
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self._clear_log()
            self._reset_file_progress(inputs)
            self._run_id += 1
            self._status = "starting"
            self._returncode = None
            self._started_at = time.time()
            self._finished_at = None

            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            process = subprocess.Popen(
                self._command(pipeline),
                cwd=str(Path(__file__).resolve().parents[2]),
                env=self._source_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                creationflags=creationflags,
                start_new_session=(os.name == "posix"),
            )
            self._process = process
            self._process_group_id = os.getpgid(process.pid) if os.name == "posix" else None
            self._status = "running"
            self._append_log(
                f"[GUI] 已启动生成任务，PID={process.pid}，输入 PDF={len(inputs)}\n"
            )
            threading.Thread(target=self._monitor, args=(process,), daemon=True).start()

    def _monitor(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stdout is not None
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                chunk = process.stdout.read(65536)
                if not chunk:
                    break
                self._append_log(decoder.decode(chunk))
            self._append_log(decoder.decode(b"", final=True))
            with self._lock:
                self._flush_progress_text()
            returncode = process.wait()
        except Exception as exc:
            self._append_log(f"\n[GUI] 读取任务输出失败：{type(exc).__name__}: {exc}\n")
            returncode = process.poll()
            if returncode is None:
                returncode = -1
            self._reclaim_process(process, "GUI output monitor failed")
        finally:
            process.stdout.close()

        self._reclaim_process(process, "GUI task parent finished")

        with self._lock:
            if self._process is process:
                was_stopping = self._status == "stopping"
                self._returncode = returncode
                self._finished_at = time.time()
                if returncode == 0:
                    self._status = "completed"
                elif was_stopping or returncode in (130, -signal.SIGINT):
                    self._status = "interrupted"
                else:
                    self._status = "failed"
                self._process = None
                self._process_group_id = None
                record = self._progress_record()
                if record is not None and record.get("status") == "running":
                    if returncode == 0:
                        total = record.get("page_total")
                        self._set_progress(
                            record,
                            status="completed",
                            stage="已完成",
                            percent=100.0,
                            page_current=total if isinstance(total, int) else None,
                        )
                    elif self._status == "interrupted":
                        self._set_progress(
                            record,
                            status="interrupted",
                            stage="已安全停止",
                        )
                    else:
                        self._set_progress(record, status="failed", stage="任务异常结束")
        if returncode == 0:
            label = "正常完成"
        elif self._status == "interrupted":
            label = f"已安全停止，退出码={returncode}"
        else:
            label = f"结束，退出码={returncode}"
        self._append_log(f"\n[GUI] 任务{label}\n")

    def stop(self) -> None:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                raise RuntimeError("当前没有正在运行的任务")
            if self._status == "stopping":
                return
            self._status = "stopping"
            self._append_log(
                "\n[GUI] 正在请求安全停止；OCR 子进程清理最多可能需要约 20 秒……\n"
            )
            if os.name == "posix":
                try:
                    os.killpg(self._process_group_id or process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
            elif hasattr(signal, "CTRL_BREAK_EVENT"):
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.terminate()

    def _reclaim_process(self, process: subprocess.Popen[bytes], reason: str) -> None:
        with self._lock:
            process_group_id = (
                self._process_group_id if self._process is process else None
            )
        controller = LiveProcessController(
            logger=lambda message: self._append_log(message + "\n"),
            console_lock=self._lock,
            exit_cleanup_seconds=1.0,
            process_label="GUI OCR task",
        )
        if process.poll() is None or controller.posix_process_group_exists(process_group_id):
            controller.terminate_process_group(
                process,
                process_group_id,
                reason,
                grace_seconds=GUI_TASK_TERMINATE_GRACE_SECONDS,
            )

    def force_stop(self) -> None:
        """Escalate shutdown and reclaim the complete GUI task process group."""

        with self._lock:
            process = self._process
        if process is not None:
            self._reclaim_process(process, "GUI shutdown deadline reached")

    def status(self) -> dict[str, object]:
        with self._lock:
            process = self._process
            return {
                "status": self._status,
                "running": process is not None and process.poll() is None,
                "pid": process.pid if process is not None and process.poll() is None else None,
                "returncode": self._returncode,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "run_id": self._run_id,
                "pipeline": self._pipeline,
                "pipeline_label": PIPELINES[self._pipeline]["label"],
                "pipelines": [
                    {"id": key, "label": value["label"]}
                    for key, value in PIPELINES.items()
                ],
                "input_dir": str(self.input_dir),
                "output_dir": str(self.output_dir),
                "inputs": self.list_inputs(),
                "outputs": self.list_outputs(),
                "file_progress": [dict(record) for record in self._file_progress],
                "summary": self.read_summary(),
            }

    def log_since(self, offset: int) -> dict[str, object]:
        with self._lock:
            current_end = self._log_start + self._log_chars
            reset = offset < self._log_start or offset > current_end
            if reset:
                offset = self._log_start
            skip = offset - self._log_start
            text = "".join(self._log_chunks)
            return {
                "text": text[skip:],
                "next_offset": current_end,
                "reset": reset,
                "run_id": self._run_id,
            }

    def output_file(self, encoded_name: str) -> Path | None:
        name = unquote(encoded_name)
        if not name or Path(name).name != name or not name.lower().endswith(".pdf"):
            return None
        candidate = self.output_dir / name
        try:
            if not candidate.is_file() or candidate.resolve().parent != self.output_dir.resolve():
                return None
        except OSError:
            return None
        return candidate


HTML_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>OCR PDF 生成器</title>
  <style>
    :root { color-scheme: light; --ink:#17212b; --muted:#607080; --line:#dce3e8; --brand:#176b5b; --brand2:#e8f4f1; --danger:#b63838; }
    * { box-sizing:border-box; }
    body { margin:0; background:#f4f7f6; color:var(--ink); font-family:system-ui,"Microsoft YaHei",sans-serif; }
    main { width:min(1100px,calc(100% - 32px)); margin:28px auto 48px; }
    header { display:flex; align-items:flex-end; justify-content:space-between; gap:16px; margin-bottom:18px; }
    h1 { margin:0; font-size:25px; font-weight:720; letter-spacing:.02em; }
    .subtitle,.path,.hint { color:var(--muted); font-size:13px; }
    .status { padding:7px 12px; border-radius:999px; background:#e8ecef; font-size:13px; font-weight:650; white-space:nowrap; }
    .status.running,.status.starting { color:#075d4c; background:#d9f2eb; }
    .status.stopping { color:#875800; background:#fff0c9; }
    .status.failed { color:#9b2727; background:#fde2e2; }
    .grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
    .card { background:white; border:1px solid var(--line); border-radius:12px; padding:17px; box-shadow:0 2px 10px rgba(24,45,42,.035); }
    .wide { grid-column:1/-1; }
    .card h2 { margin:0 0 12px; font-size:16px; }
    .actions { display:flex; gap:10px; flex-wrap:wrap; margin:15px 0 8px; }
    button { border:0; border-radius:8px; padding:10px 17px; font:inherit; font-weight:650; cursor:pointer; background:var(--brand); color:white; }
    select { width:100%; margin-top:7px; border:1px solid var(--line); border-radius:8px; padding:9px 10px; background:white; color:var(--ink); font:inherit; }
    button.secondary { background:white; color:var(--danger); border:1px solid #e1baba; }
    button:disabled { opacity:.45; cursor:not-allowed; }
    ul { list-style:none; padding:0; margin:0; max-height:240px; overflow:auto; }
    li { display:flex; justify-content:space-between; gap:12px; padding:8px 0; border-top:1px solid #edf0f2; font-size:13px; }
    li:first-child { border-top:0; }
    a { color:var(--brand); text-decoration:none; font-weight:600; }
    a:hover { text-decoration:underline; }
    .empty { color:var(--muted); padding:16px 0; font-size:13px; }
    .summary { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; }
    .metric { background:#f5f8f7; padding:10px; border-radius:8px; }
    .metric b { display:block; font-size:20px; margin-top:2px; }
    .progress-list { display:grid; gap:12px; }
    .progress-item { padding:12px; border:1px solid #e5eae9; border-radius:9px; background:#fbfcfc; }
    .progress-head,.progress-detail { display:flex; align-items:center; justify-content:space-between; gap:12px; }
    .progress-name { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:13px; font-weight:650; }
    .progress-percent { color:var(--brand); font-size:13px; font-weight:720; white-space:nowrap; }
    .progress-track { height:9px; margin:9px 0 7px; overflow:hidden; border-radius:999px; background:#e4ebe9; }
    .progress-fill { height:100%; width:0; border-radius:inherit; background:linear-gradient(90deg,#278776,var(--brand)); transition:width .3s ease; }
    .progress-item.failed .progress-fill { background:var(--danger); }
    .progress-item.skipped .progress-fill { background:#7b8b91; }
    .progress-detail { color:var(--muted); font-size:12px; }
    pre { min-height:260px; max-height:460px; overflow:auto; margin:0; padding:14px; border-radius:9px; background:#101817; color:#dcebe7; font:12px/1.55 Consolas,monospace; white-space:pre-wrap; word-break:break-word; }
    .message { min-height:20px; margin-top:8px; color:var(--danger); font-size:13px; }
    @media (max-width:760px) { .grid { grid-template-columns:1fr; } .wide { grid-column:auto; } header { align-items:flex-start; flex-direction:column; } .summary { grid-template-columns:1fr 1fr; } }
  </style>
</head>
<body>
<main>
  <header><div><h1>OCR PDF 生成器</h1><div class="subtitle">MinerU / PaddleOCR-VL 纯文本 PDF 批处理控制面板</div></div><div id="status" class="status">读取中</div></header>
  <section class="grid">
    <div class="card">
      <h2>控制</h2>
      <div id="inputPath" class="path"></div>
      <label class="path" for="pipeline">主识别管线</label>
      <select id="pipeline"><option value="mineru">MinerU</option><option value="paddle">PaddleOCR-VL</option></select>
      <div class="actions"><button id="start">开始生成</button><button id="stop" class="secondary">安全停止</button></div>
      <div class="hint">每次处理输入目录中的全部 PDF；已完成且完整性匹配的文件会复用检查点。MinerU 首次加载模型及单个分块推理可能数分钟不输出页码；实时日志每 30 秒显示一次“进程存活”心跳，请勿仅因进度条暂时不动而重启。</div>
      <div id="message" class="message"></div>
    </div>
    <div class="card">
      <h2>批次摘要</h2>
      <div id="summary" class="summary"></div>
    </div>
    <div class="card wide">
      <h2>文件总进度</h2>
      <div id="fileProgress" class="progress-list"><div class="empty">任务尚未启动</div></div>
    </div>
    <div class="card">
      <h2>输入文件</h2>
      <ul id="inputs"></ul>
    </div>
    <div class="card">
      <h2>生成结果</h2>
      <ul id="outputs"></ul>
    </div>
    <div class="card wide">
      <h2>实时日志</h2>
      <pre id="log">等待任务启动……</pre>
    </div>
  </section>
</main>
<script>
const csrf = __CSRF_TOKEN__;
let logOffset = 0, runId = null;
const $ = id => document.getElementById(id);
const fmtSize = n => n < 1048576 ? `${(n/1024).toFixed(1)} KB` : `${(n/1048576).toFixed(1)} MB`;
const labels = {idle:'空闲',starting:'正在启动',running:'正在生成',stopping:'正在安全停止',completed:'已完成',interrupted:'已安全停止',failed:'已结束（有错误）'};
function renderFiles(id, files, downloadable=false) {
  const target=$(id); target.textContent='';
  if (!files.length) { const d=document.createElement('div'); d.className='empty'; d.textContent='暂无 PDF'; target.append(d); return; }
  files.forEach(file => { const li=document.createElement('li'); const name=document.createElement(downloadable?'a':'span'); name.textContent=file.name; if(downloadable) name.href=file.download_url; const size=document.createElement('span'); size.className='path'; size.textContent=fmtSize(file.size); li.append(name,size); target.append(li); });
}
function renderSummary(summary) {
  const counts=(summary&&summary.counts)||{}; const values=[['状态',summary?summary.status:'暂无'],['已完成',counts.completed||0],['已跳过',counts.skipped||0],['中断',counts.interrupted||0],['失败',counts.failed||0]];
  $('summary').textContent=''; values.forEach(([k,v])=>{const d=document.createElement('div');d.className='metric';const s=document.createElement('span');s.className='path';s.textContent=k;const b=document.createElement('b');b.textContent=v;d.append(s,b);$('summary').append(d);});
}
function renderFileProgress(files) {
  const target=$('fileProgress'); target.textContent='';
  if (!files.length) { const d=document.createElement('div'); d.className='empty'; d.textContent='任务尚未启动'; target.append(d); return; }
  const stateLabels={pending:'等待',running:'处理中',completed:'完成',skipped:'复用',interrupted:'已停止',failed:'失败'};
  files.forEach(file=>{
    const item=document.createElement('div'); item.className='progress-item '+file.status;
    const head=document.createElement('div'); head.className='progress-head';
    const name=document.createElement('div'); name.className='progress-name'; name.title=file.name; name.textContent=file.name;
    const percent=document.createElement('div'); percent.className='progress-percent'; percent.textContent=`${Number(file.percent||0).toFixed(1)}%`;
    head.append(name,percent);
    const track=document.createElement('div'); track.className='progress-track'; track.setAttribute('role','progressbar'); track.setAttribute('aria-label',file.name); track.setAttribute('aria-valuemin','0'); track.setAttribute('aria-valuemax','100'); track.setAttribute('aria-valuenow',String(file.percent||0));
    const fill=document.createElement('div'); fill.className='progress-fill'; fill.style.width=`${Math.max(0,Math.min(100,Number(file.percent||0)))}%`; track.append(fill);
    const detail=document.createElement('div'); detail.className='progress-detail';
    const stage=document.createElement('span'); stage.textContent=`${stateLabels[file.status]||file.status} · ${file.stage||''}`;
    const pages=document.createElement('span'); pages.textContent=file.page_total?`${file.page_current||0}/${file.page_total} 页`:'';
    detail.append(stage,pages); item.append(head,track,detail); target.append(item);
  });
}
async function refresh() {
  try { const r=await fetch('/api/status',{cache:'no-store'}); const s=await r.json();
    $('status').textContent=labels[s.status]||s.status; $('status').className='status '+s.status; $('start').disabled=s.running||!s.inputs.length; $('stop').disabled=!s.running||s.status==='stopping'; $('pipeline').disabled=s.running; if(s.pipeline&&(s.running||runId===null))$('pipeline').value=s.pipeline; $('inputPath').textContent='输入：'+s.input_dir; renderFiles('inputs',s.inputs); renderFiles('outputs',s.outputs,true); renderSummary(s.summary); renderFileProgress(s.file_progress||[]);
    if(runId!==s.run_id){runId=s.run_id;logOffset=0;$('log').textContent='';}
    const lr=await fetch('/api/log?offset='+logOffset,{cache:'no-store'}); const l=await lr.json(); if(l.reset)$('log').textContent=''; if(l.text){$('log').textContent+=l.text;$('log').scrollTop=$('log').scrollHeight;} logOffset=l.next_offset;
  } catch(e) { $('message').textContent='无法连接 GUI 服务：'+e; }
}
async function action(path) { $('message').textContent=''; try { const r=await fetch(path,{method:'POST',headers:{'X-CSRF-Token':csrf}}); const data=await r.json(); if(!r.ok) throw new Error(data.error||r.statusText); await refresh(); } catch(e){$('message').textContent=e.message;} }
$('start').onclick=()=>action('/api/start?pipeline='+encodeURIComponent($('pipeline').value)); $('stop').onclick=()=>action('/api/stop');
refresh(); setInterval(refresh,1500);
</script>
</body></html>
"""


def make_handler(controller: GuiController, csrf_token: str):
    html_page = HTML_PAGE.replace("__CSRF_TOKEN__", json.dumps(csrf_token))

    class Handler(BaseHTTPRequestHandler):
        server_version = "OcrPdfGui/1.0"

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: object) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send(HTTPStatus.OK, html_page.encode("utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/status":
                self._json(HTTPStatus.OK, controller.status())
                return
            if parsed.path == "/api/log":
                try:
                    query = parsed.query.split("offset=", 1)[1].split("&", 1)[0]
                    offset = max(0, int(query))
                except (IndexError, ValueError):
                    offset = 0
                self._json(HTTPStatus.OK, controller.log_since(offset))
                return
            prefix = "/files/output/"
            if parsed.path.startswith(prefix):
                path = controller.output_file(parsed.path[len(prefix):])
                if path is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "文件不存在"})
                    return
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(path.stat().st_size))
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(path.name)}")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        self.wfile.write(chunk)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "未找到"})

        def do_POST(self) -> None:
            if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), csrf_token):
                self._json(HTTPStatus.FORBIDDEN, {"error": "请求令牌无效"})
                return
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/api/start":
                    pipeline = parse_qs(parsed.query).get("pipeline", ["mineru"])[0]
                    controller.start(pipeline)
                elif parsed.path == "/api/stop":
                    controller.stop()
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "未找到"})
                    return
            except RuntimeError as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            except OSError as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"操作失败：{exc}"})
                return
            self._json(HTTPStatus.OK, {"ok": True})

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def existing_gui_is_responding(url: str) -> bool:
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(url.rstrip("/") + "/api/status", timeout=1.5) as response:
            payload = json.load(response)
    except (OSError, ValueError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("input_dir") == str(DEFAULT_INPUT_DIR)
        and payload.get("pipeline") in PIPELINES
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OCR PDF Rebuilder 本地网页 GUI")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认：127.0.0.1）")
    parser.add_argument("--port", type=int, default=18765, help="监听端口（默认：18765）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    return parser.parse_args(argv)


def _serve_gui(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    controller = GuiController()
    csrf_token = secrets.token_urlsafe(24)
    host_for_url = "localhost" if args.host in {"0.0.0.0", "127.0.0.1"} else args.host
    requested_url = f"http://{host_for_url}:{args.port}/"
    try:
        server = GuiHttpServer((args.host, args.port), make_handler(controller, csrf_token))
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        if existing_gui_is_responding(requested_url):
            print(f"OCR PDF GUI 已在运行：{requested_url}", flush=True)
            if not args.no_browser:
                webbrowser.open(requested_url)
            return
        raise SystemExit(
            f"无法启动 OCR PDF GUI：端口 {args.port} 已被其他程序占用。"
        ) from None
    url = f"http://{host_for_url}:{server.server_port}/"
    print(f"OCR PDF GUI 已启动：{url}", flush=True)
    print("按 Ctrl+C 关闭 GUI；正在运行的生成任务需先在页面中安全停止。", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nGUI 已关闭。", flush=True)
    finally:
        if controller.status()["running"]:
            print("正在安全停止生成任务……", flush=True)
            try:
                controller.stop()
            except (RuntimeError, OSError):
                pass
            deadline = time.monotonic() + 30
            while controller.status()["running"] and time.monotonic() < deadline:
                time.sleep(0.1)
            if controller.status()["running"]:
                controller.force_stop()
        server.server_close()


def main(argv: list[str] | None = None) -> None:
    try:
        with termination_raises_keyboard_interrupt():
            _serve_gui(argv)
    except KeyboardInterrupt:
        # Covers termination arriving before the HTTP server enters its own
        # serve_forever try/finally block.
        print("\nGUI 已关闭。", flush=True)


if __name__ == "__main__":
    main()
