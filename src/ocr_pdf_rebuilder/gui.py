from __future__ import annotations

import argparse
import codecs
from collections import deque
import errno
from functools import lru_cache
import hashlib
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

from .process_control import LiveProcessController, attach_windows_job, close_windows_job
from .signal_cleanup import termination_raises_keyboard_interrupt
from .task_lock import task_lock_is_held
from .selected_run import bind_run, MANIFEST_NAME, state_root, manifest_path
from .gui_static import HTML_PAGE


DEFAULT_RUNTIME_ROOT = Path(
    os.environ.get("OCR_RUNTIME_ROOT", Path(__file__).resolve().parents[2])
).expanduser().resolve()
DEFAULT_INPUT_DIR = DEFAULT_RUNTIME_ROOT / "input"
DEFAULT_OUTPUT_DIR = DEFAULT_RUNTIME_ROOT / "pdf_paddle"
DEFAULT_SUMMARY_PATH = DEFAULT_RUNTIME_ROOT / "logs_paddle/batch_summary.json"
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
if os.name == "nt":
    PIPELINES.pop("mineru")
    PIPELINES["paddle"]["label"] = "PaddleOCR-VL · Windows Transformers"
MAX_LOG_CHARS = 400_000
GUI_TASK_TERMINATE_GRACE_SECONDS = 45.0
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CAPABILITIES = {"service_instance_identity": True, "conditional_stop": True,
                "conditional_shutdown": True, "identity_log_cursor": True,
                "output_provenance": True}


class GuiHttpServer(ThreadingHTTPServer):
    allow_reuse_address = True


def file_record(path: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "name": path.name,
        "size": stat.st_size,
        "modified_at": stat.st_mtime,
    }


def _file_stamp(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "ctime_ns": stat.st_ctime_ns}


@lru_cache(maxsize=1024)
def _content_hash(path: str, size: int, mtime_ns: int, ctime_ns: int) -> str:
    with Path(path).open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if _file_stamp(Path(path)) != {"size": size, "mtime_ns": mtime_ns, "ctime_ns": ctime_ns}:
        raise ValueError("File changed while checking provenance")
    return digest


def _signature_matches(signature: object) -> bool:
    if not isinstance(signature, dict) or not isinstance(signature.get("path"), str):
        return False
    path = Path(signature["path"])
    if not path.is_absolute() or not isinstance(signature.get("sha256"), str):
        return False
    try:
        stamp = _file_stamp(path)
        return (stamp["size"] == signature.get("size")
                and _content_hash(str(path.resolve()), **stamp) == signature["sha256"])
    except (OSError, ValueError):
        return False


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
        self._legacy_paths = (self.output_dir, self.summary_path)
        self._selected_run = None
        self._dynamic_pipeline_paths = (
            self.output_dir == DEFAULT_OUTPUT_DIR
            and self.summary_path == DEFAULT_SUMMARY_PATH
        )
        self._pipeline = "paddle"
        self._lock = threading.RLock()
        self.service_instance_id = secrets.token_hex(16)
        self._closing = False
        self._process: subprocess.Popen[bytes] | None = None
        self._process_group_id: int | None = None
        self._status = "idle"
        self._returncode: int | None = None
        self._started_at: float | None = None
        self._finished_at: float | None = None
        self._run_id = 0
        self._history = []
        self._run_sources = {}
        self._log_chunks: deque[str] = deque()
        self._log_chars = 0
        self._log_start = 0
        self._progress_line_buffer = ""
        self._file_progress: list[dict[str, object]] = []
        self._active_file_index: int | None = None
        self._active_parser_page_start = 1
        self._run_state_path = self.input_dir.parent / "tmp/ocr_pdf_rebuilder/gui-run.json"
        self._restore_run_state()

    def _restore_run_state(self) -> None:
        try:
            state = json.loads(self._run_state_path.read_text(encoding="utf-8"))
            history = state.get("history", [])
            if isinstance(history, list):
                self._history = [item for item in history if isinstance(item, dict)][-30:]
            sources = state.get("run_sources", {})
            if isinstance(sources, dict):
                self._run_sources = sources
            selected = state.get("selected_run")
            if isinstance(selected, dict) and selected.get("pipeline") in PIPELINES:
                output = Path(selected["output_dir"])
                if not output.is_absolute() or not isinstance(selected["input_files"], list):
                    return
                self._selected_run = selected
                self._pipeline = selected["pipeline"]
                self.output_dir = output
                self.summary_path = state_root(selected) / self._pipeline / "logs/batch_summary.json"
            elif selected is None and state.get("pipeline") in PIPELINES:
                self._select_pipeline(state["pipeline"])
                if state.get("output_dir") != str(self.output_dir):
                    return
            else:
                return
            self._run_id = int(state.get("run_id", 0))
            self._status = state.get("status", "interrupted")
            if self._status not in {"completed", "failed", "interrupted"}:
                self._status = "interrupted"
            self._started_at = state.get("started_at")
            self._finished_at = state.get("finished_at")
            self._returncode = state.get("returncode")
            self._reset_file_progress(self.list_inputs())
        except (OSError, ValueError, KeyError, TypeError):
            return

    def _persist_run_state(self) -> None:
        self._run_state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._run_state_path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps({
            "selected_run": self._selected_run, "run_id": self._run_id,
            "status": self._status, "started_at": self._started_at,
            "finished_at": self._finished_at, "returncode": self._returncode,
            "history": self._history,
            "run_sources": self._run_sources,
            "pipeline": self._pipeline, "output_dir": str(self.output_dir),
        }, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self._run_state_path)

    @staticmethod
    def _default_command(pipeline: str = "paddle") -> list[str]:
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
        env["PYTHONIOENCODING"] = "utf-8"
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
        if "Rendering searchable OCR-layer PDF:" in line:
            self._set_progress(record, stage="生成原版隐形文字层 PDF", percent=98.0, page_current=0)
            return
        match = re.search(r"(Searchable PDF|Validate searchable PDF) page (\d+)/(\d+)", line)
        if match:
            validating = match.group(1).startswith("Validate")
            current, total = int(match.group(2)), int(match.group(3))
            if total > 0:
                self._set_progress(
                    record,
                    stage="验证原版隐形文字层 PDF" if validating else "生成原版隐形文字层 PDF",
                    percent=(98.8 if validating else 98.0) + 0.8 * current / total,
                    page_current=current, page_total=total,
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
        if self._selected_run is not None:
            return [{"name": Path(path).name, "path": path, **source}
                    for path, source in zip(self._selected_run["input_files"], self._selected_run["sources"])]
        return self.list_directory_inputs()

    def list_directory_inputs(self) -> list[dict[str, object]]:
        if not self.input_dir.exists():
            return []
        return [file_record(path) for path in sorted(self.input_dir.glob("*.pdf")) if path.is_file()]

    def default_output_dir(self, pipeline):
        return Path(PIPELINES[pipeline]["output_dir"]) if self._dynamic_pipeline_paths else self._legacy_paths[0]

    def start_directory(self, pipeline, input_names, output_dir=None):
        """Resolve UI checkboxes on the server, without accepting arbitrary paths."""
        with self._lock:
            if self._process is not None or self._closing:
                raise RuntimeError("任务正在运行或 GUI 正在关闭")
            if not isinstance(pipeline, str) or pipeline not in PIPELINES:
                raise ValueError("Unknown pipeline")
            if not isinstance(input_names, list) or not input_names:
                raise ValueError("请至少选择一个 PDF")
            paths = []
            for name in input_names:
                if (not isinstance(name, str) or not name or Path(name).name != name
                        or "/" in name or "\\" in name or Path(name).suffix.lower() != ".pdf"):
                    raise ValueError("输入目录文件名无效")
                path = (self.input_dir / name).resolve(strict=True)
                if path.parent != self.input_dir.resolve() or not path.is_file():
                    raise ValueError("文件不属于默认输入目录")
                paths.append(str(path))
            if len(set(paths)) != len(paths):
                raise ValueError("文件选择包含重复项")
            run_id = "standalone-" + secrets.token_hex(12)
            target = str(self.default_output_dir(pipeline).resolve()) if output_dir is None else output_dir
            self.start(pipeline, input_files=paths, output_dir=target, run_id=run_id)

    def list_outputs(self) -> list[dict[str, object]]:
        if not self.output_dir.exists():
            return []
        summary = self.read_summary() or {}
        manifests = []
        for candidate in self.output_dir.iterdir():
            if candidate.suffix != ".version" or candidate.resolve().parent != self.output_dir.resolve():
                continue
            try:
                manifest = json.loads(candidate.read_text(encoding="utf-8"))
                if not isinstance(manifest, dict):
                    continue
                checksum = manifest.pop("_checkpoint_sha256", None)
                if checksum is not None:
                    digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=False,
                        sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                    if checksum != digest:
                        continue
                manifests.append((candidate, manifest))
            except (OSError, ValueError):
                continue
        records = []
        for path in sorted(self.output_dir.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
            if path.suffix.lower() not in {".pdf", ".md"}:
                continue
            if path.is_file() and path.resolve().parent == self.output_dir.resolve():
                record = file_record(path)
                record["path"] = str(path.resolve())
                record["download_url"] = "/files/output/" + quote(path.name)
                record["provenance"] = self._output_provenance(path, summary, manifests)
                record["current_run"] = self._proven_current_run(record["provenance"], summary)
                records.append(record)
        return records

    def _proven_current_run(self, provenance: dict, summary: dict) -> bool:
        if provenance["status"] != "verified" or self._started_at is None:
            return False
        started = summary.get("started_at")
        if type(started) not in (int, float) or not self._started_at <= started <= time.time():
            return False
        if self._finished_at is not None and started > self._finished_at:
            return False
        external = self._selected_run["run_id"] if self._selected_run else None
        if summary.get("external_run_id") != external:
            return False
        if summary.get("output_dir") != str(self.output_dir):
            return False
        source = provenance["source_path"]
        expected = self._run_sources.get(source)
        if not isinstance(expected, dict) or not expected:
            return False
        try:
            current = _file_stamp(Path(source))
            return all(current.get(key) == value for key, value in expected.items())
        except OSError:
            return False

    def _output_provenance(self, path: Path, summary: dict, manifests: list) -> dict[str, object]:
        fields = ("output_text_pdf", "output_image_pdf", "output_searchable_pdf", "output_markdown",
                  "output_pdf", "markdown_path", "searchable_pdf", "image_variant_pdf")
        results = summary.get("results", [])
        reported = {"status": "unverified", "source_path": None}
        for result in results if isinstance(results, list) else []:
            if not isinstance(result, dict):
                continue
            source = result.get("input_pdf")
            if not isinstance(source, str) or not Path(source).is_absolute():
                continue
            outputs = {key: str(Path(value).resolve()) for key in fields
                       if isinstance(value := result.get(key), str) and Path(value).is_absolute()}
            source = str(Path(source).resolve())
            for key, value in outputs.items():
                if value == str(path.resolve()):
                    reported = {"status": "reported", "source_path": source,
                                "evidence_path": str(self.summary_path), "field": key}
            if result.get("status") not in {"completed", "skipped"}:
                continue
            for manifest_path, manifest in manifests:
                signature = manifest.get("source")
                if not isinstance(signature, dict) or signature.get("path") != source:
                    continue
                aliases = {"text_pdf": "output_pdf", "image_pdf": "output_image_pdf",
                           "searchable_pdf": "output_searchable_pdf", "markdown": "output_md"}
                artifacts = {key: value for key, alias in aliases.items()
                             if isinstance(value := manifest.get(key, manifest.get(alias)), dict)}
                # A report must explicitly name an artifact in this completion
                # manifest. Names/prefixes and directory membership prove nothing.
                if not any(value.get("path") in outputs.values() for value in artifacts.values()):
                    continue
                for key, artifact in artifacts.items():
                    if artifact.get("path") != str(path.resolve()):
                        continue
                    if _signature_matches(signature) and _signature_matches(artifact):
                        return {"status": "verified", "source_path": source,
                                "source_sha256": signature["sha256"], "artifact_sha256": artifact["sha256"],
                                "evidence_path": str(manifest_path), "report_path": str(self.summary_path),
                                "field": key}
        return reported

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
        if self._selected_run is not None:
            task_lock_path = DEFAULT_RUNTIME_ROOT / "tmp/ocr_pdf_rebuilder/task.lock"
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

    def start(self, pipeline: str = "paddle", *, input_files=None, output_dir=None, run_id=None) -> None:
        with self._lock:
            if self._closing:
                raise RuntimeError("GUI service is shutting down")
            if self._process is not None:
                raise RuntimeError("生成任务已经在运行")
            if not isinstance(pipeline, str) or pipeline not in PIPELINES:
                raise ValueError("Unknown pipeline")
            if task_lock_is_held(DEFAULT_RUNTIME_ROOT / "tmp/ocr_pdf_rebuilder/task.lock"):
                raise RuntimeError("Another OCR task owns the runtime GPU lock")
            selected = None
            if any(value is not None for value in (input_files, output_dir, run_id)):
                selected = bind_run(input_files, output_dir, run_id, pipeline)
            self._selected_run = selected
            self.output_dir, self.summary_path = self._legacy_paths
            self._select_pipeline(pipeline)
            if selected is not None:
                self.output_dir = Path(selected["output_dir"])
                self.summary_path = state_root(selected) / pipeline / "logs/batch_summary.json"
            inputs = self.list_inputs()
            if not inputs:
                raise RuntimeError(f"输入目录中没有 PDF：{self.input_dir}")

            source_paths = (selected["input_files"] if selected else
                            [str((self.input_dir / record["name"]).resolve()) for record in inputs])
            self._run_sources = {str(Path(path).resolve()): _file_stamp(Path(path)) for path in source_paths}

            if selected is None:
                self.input_dir.mkdir(parents=True, exist_ok=True)
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self._clear_log()
            self._reset_file_progress(inputs)
            self._run_id += 1
            self._status = "starting"
            self._returncode = None
            self._started_at = time.time()
            self._finished_at = None
            self._persist_run_state()

            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            env = self._source_environment()
            env.pop("OCR_SELECTED_RUN_MANIFEST", None)
            if selected is not None:
                env["OCR_SELECTED_RUN_MANIFEST"] = str(manifest_path(selected))
            try:
                process = subprocess.Popen(
                    self._command(pipeline),
                    cwd=str(Path(__file__).resolve().parents[2]),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=0,
                    creationflags=creationflags,
                    start_new_session=(os.name == "posix"),
                )
            except OSError:
                self._status = "failed"
                self._finished_at = time.time()
                self._persist_run_state()
                raise
            attach_windows_job(process)
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
            close_windows_job(process)
            process.stdout.close()

        if os.name == "posix":
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
                self._history.append({"service_instance_id": self.service_instance_id,
                    "run_id": self._run_id,
                    "external_run_id": self._selected_run["run_id"] if self._selected_run else None,
                    "status": self._status, "pipeline": self._pipeline,
                    "started_at": self._started_at, "finished_at": self._finished_at,
                    "output_dir": str(self.output_dir)})
                self._history = self._history[-30:]
                # Finish the old log before releasing the lock for a new start.
                label = "正常完成" if returncode == 0 else f"结束：{self._status}，退出码={returncode}"
                self._append_log(f"\n[GUI] 任务{label}\n")
                try:
                    self._persist_run_state()
                except OSError as exc:
                    self._append_log(f"[GUI] Could not persist final run state: {exc}\n")
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

    def _check_identity(self, expected_service_instance_id=None, expected_run_id=None, expected_instance_id=None) -> None:
        """Caller holds _lock through validation and the resulting mutation."""
        if expected_instance_id is not None:
            if expected_service_instance_id is not None and expected_instance_id != expected_service_instance_id:
                raise ValueError("Conflicting instance identity conditions")
            expected_service_instance_id = expected_instance_id
        if expected_service_instance_id is not None:
            if not isinstance(expected_service_instance_id, str) or not expected_service_instance_id:
                raise ValueError("expected_service_instance_id must be a nonempty string")
            if expected_service_instance_id != self.service_instance_id:
                raise RuntimeError("service_instance_id does not match the active service")
        if expected_run_id is not None:
            if type(expected_run_id) is not int or expected_run_id < 0:
                raise ValueError("expected_run_id must be a nonnegative integer")
            if expected_run_id != self._run_id:
                raise RuntimeError("expected_run_id does not match the active run")

    def shutdown(self, *, expected_instance_id=None, expected_run_id=None, expected_service_instance_id=None) -> None:
        with self._lock:
            if (expected_service_instance_id is None and expected_instance_id is None) or expected_run_id is None:
                raise ValueError("Both shutdown identity conditions are required")
            self._check_identity(expected_service_instance_id, expected_run_id, expected_instance_id)
            if self._process is not None:
                raise RuntimeError("Stop the OCR task and wait for cleanup before closing the GUI")
            self._closing = True

    def stop(self, run_id=None, *, expected_service_instance_id=None, expected_run_id=None, expected_instance_id=None) -> None:
        with self._lock:
            self._check_identity(expected_service_instance_id, expected_run_id, expected_instance_id)
            if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
                raise ValueError("run_id must be a nonempty string")
            if run_id is not None and (self._selected_run is None or run_id != self._selected_run["run_id"]):
                raise RuntimeError("run_id does not match the active run")
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
                def escalate() -> None:
                    with self._lock:
                        active = self._process is process
                    if active:
                        self._reclaim_process(process, "GUI safe stop deadline reached")
                timer = threading.Timer(GUI_TASK_TERMINATE_GRACE_SECONDS, escalate)
                timer.daemon = True
                timer.start()
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
        if (process.poll() is None or controller.posix_process_group_exists(process_group_id)
                or getattr(process, "_ocr_windows_job", None) is not None):
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
                "instance_id": self.service_instance_id,
                "service_instance_id": self.service_instance_id,
                "capabilities": dict(CAPABILITIES),
                "closing": self._closing,
                "status": self._status,
                "running": process is not None,
                "pid": process.pid if process is not None and process.poll() is None else None,
                "returncode": self._returncode,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "run_id": self._run_id,
                "external_run_id": self._selected_run["run_id"] if self._selected_run else None,
                "input_files": self._selected_run["input_files"] if self._selected_run else None,
                "pipeline": self._pipeline,
                "pipeline_label": PIPELINES[self._pipeline]["label"],
                "pipelines": [
                    {"id": key, "label": value["label"]}
                    for key, value in PIPELINES.items()
                ],
                "input_dir": str(self.input_dir),
                "default_input_count": len(self.list_inputs()) if self._selected_run is None else None,
                "output_dir": str(self.output_dir),
                "default_output_dir": str(self.default_output_dir(self._pipeline)),
                "default_output_dirs": {key: str(self.default_output_dir(key)) for key in PIPELINES},
                "inputs": self.list_inputs(),
                "outputs": self.list_outputs(),
                "file_progress": [dict(record) for record in self._file_progress],
                "summary": self.read_summary(),
                "history": [dict(item) for item in reversed(self._history)],
            }

    def log_since(self, offset: int, *, expected_service_instance_id=None, expected_run_id=None, expected_instance_id=None) -> dict[str, object]:
        with self._lock:
            current_end = self._log_start + self._log_chars
            reset = (offset < self._log_start or offset > current_end
                     or expected_service_instance_id not in (None, self.service_instance_id)
                     or expected_instance_id not in (None, self.service_instance_id)
                     or expected_run_id not in (None, self._run_id))
            if reset:
                offset = self._log_start
            skip = offset - self._log_start
            text = "".join(self._log_chunks)
            return {
                "instance_id": self.service_instance_id,
                "service_instance_id": self.service_instance_id,
                "text": text[skip:],
                "next_offset": current_end,
                "reset": reset,
                "run_id": self._run_id,
                "external_run_id": self._selected_run["run_id"] if self._selected_run else None,
            }

    def output_file(self, encoded_name: str) -> Path | None:
        name = unquote(encoded_name)
        if not name or Path(name).name != name or Path(name).suffix.lower() not in {".pdf", ".md"}:
            return None
        candidate = self.output_dir / name
        try:
            if not candidate.is_file() or candidate.resolve().parent != self.output_dir.resolve():
                return None
        except OSError:
            return None
        return candidate


def make_handler(controller: GuiController, csrf_token: str, workbench_token: str | None = None):
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

        def _workbench_authorized(self) -> bool:
            if not workbench_token or not secrets.compare_digest(
                self.headers.get("Authorization", ""), f"Bearer {workbench_token}"
            ):
                self._json(HTTPStatus.FORBIDDEN, {"error": "工作台令牌无效"})
                return False
            if self.headers.get("Host", "").lower() not in {
                f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"
            }:
                self._json(HTTPStatus.FORBIDDEN, {"error": "请求主机无效"})
                return False
            return True

        def _logs(self, query):
            params = parse_qs(query)
            try:
                offset = max(0, int(params.get("offset", ["0"])[0]))
                expected = params.get("expected_run_id", [None])[0]
                expected = int(expected) if expected is not None else None
            except ValueError:
                self._json(HTTPStatus.CONFLICT, {"error": "Invalid log cursor"})
                return
            self._json(HTTPStatus.OK, controller.log_since(offset,
                expected_instance_id=params.get("expected_instance_id", [None])[0],
                expected_service_instance_id=params.get("expected_service_instance_id", [None])[0],
                expected_run_id=expected))

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/workbench/v1/"):
                if not self._workbench_authorized():
                    return
                if parsed.path == "/workbench/v1/health":
                    self._json(HTTPStatus.OK, {"api_version": 1, "component": "ocr", "runtime_root": str(DEFAULT_RUNTIME_ROOT), "pid": os.getpid(), "selected_inputs": True,
                        "service_instance_id": controller.service_instance_id,
                        "instance_id": controller.service_instance_id,
                        "capabilities": dict(CAPABILITIES)})
                elif parsed.path == "/workbench/v1/operations":
                    self._json(HTTPStatus.OK, {"api_version": 1, "operations": {
                        "status": {"schema": {"type": "object", "properties": {}}},
                        "log": {"schema": {"type": "object", "properties": {
                            "offset": {"type": "integer", "minimum": 0},
                            "expected_instance_id": {"type": "string"},
                            "expected_run_id": {"type": "integer", "minimum": 0}}}},
                        "start": {"schema": {"type": "object", "additionalProperties": False,
                            "properties": {"pipeline": {"enum": list(PIPELINES)},
                                "input_files": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                                "output_dir": {"type": "string"}, "run_id": {"type": "string", "minLength": 1, "maxLength": 256}},
                            "required": ["pipeline"], "dependentRequired": {
                                "input_files": ["output_dir", "run_id"], "output_dir": ["input_files", "run_id"],
                                "run_id": ["input_files", "output_dir"]}}},
                        "stop": {"schema": {"type": "object", "additionalProperties": False,
                            "properties": {"run_id": {"type": "string", "minLength": 1},
                                "expected_instance_id": {"type": "string", "minLength": 1},
                                "expected_service_instance_id": {"type": "string", "minLength": 1},
                                "expected_run_id": {"type": "integer", "minimum": 0}}}},
                    }})
                elif parsed.path == "/workbench/v1/status":
                    self._json(HTTPStatus.OK, controller.status())
                elif parsed.path == "/workbench/v1/log":
                    self._logs(parsed.query)
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "未找到"})
                return
            if parsed.path == "/":
                self._send(HTTPStatus.OK, html_page.encode("utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/status":
                with controller._lock:
                    state = controller.status()
                    state["directory_inputs"] = controller.list_directory_inputs()
                self._json(HTTPStatus.OK, state)
                return
            if parsed.path == "/api/log":
                self._logs(parsed.query)
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
            parsed = urlparse(self.path)
            if parsed.path.startswith("/workbench/v1/"):
                if not self._workbench_authorized():
                    return
                try:
                    size = int(self.headers.get("Content-Length", ""))
                    if not 2 <= size <= 1048576 or self.headers.get_content_type() != "application/json":
                        raise ValueError("请求体无效")
                    data = json.loads(self.rfile.read(size))
                    if not isinstance(data, dict):
                        raise ValueError("参数无效")
                    with controller._lock:
                        if parsed.path == "/workbench/v1/start" and set(data) == {"pipeline"}:
                            controller.start(data["pipeline"])
                        elif parsed.path == "/workbench/v1/start" and set(data) == {"pipeline", "input_files", "output_dir", "run_id"}:
                            if any(data[key] is None for key in data):
                                raise ValueError("Selected run fields cannot be null")
                            controller.start(**data)
                        elif parsed.path == "/workbench/v1/stop" and set(data) <= {"run_id", "expected_instance_id", "expected_service_instance_id", "expected_run_id"}:
                            if any(value is None for value in data.values()):
                                raise ValueError("Stop conditions cannot be null")
                            controller.stop(**data)
                        elif parsed.path == "/workbench/v1/shutdown" and set(data) in ({"expected_instance_id", "expected_run_id"}, {"expected_service_instance_id", "expected_run_id"}):
                            controller.shutdown(**data)
                            threading.Thread(target=self.server.shutdown, daemon=True).start()
                        else:
                            raise ValueError("未知操作或参数")
                        state = controller.status()
                        self._json(HTTPStatus.OK, {"ok": True, "run_id": state["run_id"],
                            "instance_id": state["instance_id"],
                            "service_instance_id": state["service_instance_id"],
                            "external_run_id": state["external_run_id"], "output_dir": state["output_dir"]})
                except (ValueError, RuntimeError, KeyError, OSError, TypeError) as exc:
                    self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), csrf_token):
                self._json(HTTPStatus.FORBIDDEN, {"error": "请求令牌无效"})
                return
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/api/start":
                    if self.headers.get("Content-Length", "0") != "0":
                        size = int(self.headers.get("Content-Length", "0"))
                        if not 2 <= size <= 1048576 or self.headers.get_content_type() != "application/json":
                            raise ValueError("Invalid start body")
                        data = json.loads(self.rfile.read(size))
                        if not isinstance(data, dict) or set(data) not in ({"pipeline"}, {"pipeline", "input_names"}, {"pipeline", "input_names", "output_dir"}, {"pipeline", "input_files", "output_dir", "run_id"}):
                            raise ValueError("Invalid start fields")
                        if any(value is None for value in data.values()):
                            raise ValueError("Start fields cannot be null")
                        if "input_names" in data:
                            controller.start_directory(**data)
                        else:
                            controller.start(**data)
                    else:
                        pipeline = parse_qs(parsed.query).get("pipeline", ["paddle"])[0]
                        controller.start(pipeline)
                elif parsed.path == "/api/stop":
                    params = parse_qs(parsed.query, keep_blank_values=True)
                    allowed = {"expected_instance_id", "expected_service_instance_id", "expected_run_id"}
                    if not set(params) <= allowed or any(len(v) != 1 for v in params.values()):
                        raise ValueError("Invalid stop conditions")
                    conditions = {key: value[0] for key, value in params.items()}
                    if "expected_run_id" in conditions:
                        conditions["expected_run_id"] = int(conditions["expected_run_id"])
                    controller.stop(**conditions)
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "未找到"})
                    return
            except (RuntimeError, ValueError) as exc:
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
    token_file = DEFAULT_RUNTIME_ROOT / "tmp" / "workbench-api-token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    if token_file.is_file():
        workbench_token = token_file.read_text(encoding="ascii").strip()
    else:
        workbench_token = secrets.token_urlsafe(48)
        fd = os.open(token_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as token_stream:
            token_stream.write(workbench_token)
    if len(workbench_token) < 32:
        raise RuntimeError("OCR 工作台令牌无效")
    host_for_url = "localhost" if args.host in {"0.0.0.0", "127.0.0.1"} else args.host
    requested_url = f"http://{host_for_url}:{args.port}/"
    try:
        server = GuiHttpServer((args.host, args.port), make_handler(controller, csrf_token, workbench_token))
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
