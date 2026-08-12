from pathlib import Path
from difflib import SequenceMatcher
from functools import lru_cache
import codecs
import hashlib
import html as html_lib
import importlib.metadata
import json
import math
import os
import platform
import re
import selectors
import signal
import shutil
import subprocess
import sys
import time
import threading
import traceback
import unicodedata

import fitz
from PIL import Image


RUNTIME_ROOT = Path(
    os.environ.get("OCR_RUNTIME_ROOT", Path(__file__).resolve().parents[2])
).expanduser().resolve()
INPUT_DIR = RUNTIME_ROOT / "input"
OUTPUT_DIR = RUNTIME_ROOT / "pdf_mineru"
MINERU_OUTPUT_DIR = RUNTIME_ROOT / "mineru_output"
TMP_DIR = RUNTIME_ROOT / "tmp/mineru_textonly_pdf"
LOG_DIR = RUNTIME_ROOT / "logs_mineru"
QC_DIR = RUNTIME_ROOT / "qc_mineru"

MINERU_COMMAND = "mineru"

CJK_REGULAR_FONT = RUNTIME_ROOT / "fonts/SourceHanSerifCN-Regular.ttf"
CJK_MEDIUM_FONT = RUNTIME_ROOT / "fonts/SourceHanSerifCN-Medium.ttf"
CJK_BOLD_FONT = RUNTIME_ROOT / "fonts/SourceHanSerifCN-Bold.ttf"

LATIN_REGULAR_FONT = RUNTIME_ROOT / "fonts/SourceSerif4-Regular.ttf"
LATIN_BOLD_FONT = RUNTIME_ROOT / "fonts/SourceSerif4-Bold.ttf"
LATIN_ITALIC_FONT = RUNTIME_ROOT / "fonts/SourceSerif4-It.ttf"

GREEK_REGULAR_FONT = RUNTIME_ROOT / "fonts/DejaVuSerif.ttf"
GREEK_BOLD_FONT = RUNTIME_ROOT / "fonts/DejaVuSerif-Bold.ttf"
GREEK_ITALIC_FONT = RUNTIME_ROOT / "fonts/DejaVuSerif-Italic.ttf"

MONO_FONT = RUNTIME_ROOT / "fonts/NotoSansMono-Regular.ttf"

DPI = 200
OUTPUT_VERSION = "mineru-reportlab-cell-aware-render-v51-dual-text-image-outputs"

# MinerU execution. Leave values as None to keep MinerU's own defaults.
MINERU_API_URL = None
MINERU_BACKEND = "hybrid-engine"
MINERU_METHOD = "auto"
MINERU_LANG = None
MINERU_EFFORT = "medium"
MINERU_FORMULA = True
MINERU_TABLE = True
MINERU_IMAGE_ANALYSIS = False
MINERU_CLIENT_SIDE_OUTPUT_GENERATION = True
MINERU_EXTRA_ARGS = []
MINERU_MAX_PAGES_PER_TASK = 60
MINERU_MIN_PAGES_PER_TASK = 1
MINERU_CHECKPOINT_SCHEMA = 2
MINERU_MODEL_CACHE_ROOT = Path.home() / ".cache/modelscope/hub/models"
MINERU_PROCESS_TIMEOUT_SECONDS = 2 * 60 * 60
MINERU_PROCESS_IDLE_TIMEOUT_SECONDS = 20 * 60
MINERU_PROCESS_TERMINATE_GRACE_SECONDS = 20
MINERU_PROCESS_EXIT_CLEANUP_SECONDS = 5

USE_LINE_INFO_RENDERING = False
DOT_LEADER_KEEP = 24
DOT_LEADER_EXPLOSION_MIN_DOTS = 300
DOT_LEADER_RETRY_REASON = "overlong dot leader hallucination; MinerU batched quality retry needed"
REPEATED_TEXT_MIN_RUN = 3
REPEATED_TEXT_MAX_UNIT_CHARS = 240
REPEATED_TOKEN_MAX_NGRAM = 10
PSEUDOTEXT_RETRY_REASON = "high repeated pseudo text; MinerU batched quality retry needed"
PSEUDOTEXT_IMAGE_FALLBACK_REASON = "high repeated pseudo text remained after retry; rendered source page image fallback"
UNFITTING_LAYOUT_IMAGE_FALLBACK_REASON = "OCR layout block could not fit inside bbox; rendered source page image fallback"
TABLE_TEXT_RETRY_REASON = "table block could not fit bbox; retried page with MinerU table recognition disabled"
MISSING_OCR_IMAGE_FALLBACK_REASON = "nonblank source page has no MinerU page result; image variant required"
UNUSABLE_OCR_IMAGE_FALLBACK_REASON = "OCR remained unusable after retry (handwriting or low-quality scan); image variant required"
PSEUDOTEXT_MIN_RAW_CHARS = 160
PSEUDOTEXT_MAX_COMPRESSION_RATIO = 0.58
PSEUDOTEXT_MIN_COORD_TOKENS = 6
PSEUDOTEXT_MIN_HASH_MARKERS = 6
CLEAN_TMP_ON_SUCCESS = False
DELETE_INTERMEDIATE_ON_SUCCESS = False
SKIP_EXISTING = True
DEBUG_MARKDOWN_WARNINGS = False
PARSER_MAX_ATTEMPTS = 3
PARSER_RETRY_BACKOFF_SECONDS = 10
BLANK_PAGE_CHECK_DPI = 72
BLANK_PAGE_DARK_THRESHOLD = 245
BLANK_PAGE_MAX_DARK_RATIO = 0.0005
BODY_FONT_MIN = 8.8
BODY_FONT_MAX = 11.8
BODY_TEXT_FONT_FLOOR = 8.6
MAX_PAGE_FONT_SCALE = 5.0
BODY_TEXT_FLOOR_LINE_HEIGHT_MIN = 0.76
FLOW_BLOCK_GAP = 4.0
COMPRESSED_BODY_MIN_CHARS = 120
COMPRESSED_BODY_MIN_WIDTH_RATIO = 0.45
COMPRESSED_BODY_HEIGHT_EXPAND_RATIO = 1.28
REPORTLAB_FONTS_REGISTERED = False
FORMULA_RENDER_DEPS = None
AUTO_INSTALL_FORMULA_DEPS = True
FORMULA_APT_PACKAGES = ["texlive-latex-base", "texlive-latex-extra", "dvisvgm"]
FORMULA_PIP_PACKAGES = ["svglib"]
FORMULA_RENDER_DPI = 300
FORMULA_CROP_PADDING = 2.0
PARSER_RETRY_PATTERNS = [
    "cells post process error",
    "JSON parsing failed",
    "Unterminated string",
    "Expecting value",
]
TRANSIENT_MINERU_FAILURE_PATTERNS = [
    "address already in use",
    "broken pipe",
    "cannot allocate memory",
    "connection aborted",
    "connection closed",
    "connection refused",
    "connection reset",
    "database is locked",
    "engine core failed",
    "engine process failed",
    "enginecore failed",
    "failed to start",
    "nccl error",
    "port is already in use",
    "produced no stdout/stderr",
    "resource temporarily unavailable",
    "server disconnected",
    "service unavailable",
    "temporarily unavailable",
    "terminated by signal",
    "zmq.error",
]
SUSPICIOUS_LAYOUT_PHRASES = [
    "The New York Times",
    "January 1, 2020",
]
LAYOUT_CATEGORIES = [
    "Caption",
    "Footnote",
    "Formula",
    "List-item",
    "Page-footer",
    "Page-header",
    "Picture",
    "Section-header",
    "Table",
    "Text",
    "Title",
]
RADICAL_CHAR_RE = re.compile(r"[\u2e80-\u2fff\u31c0-\u31ef\uf900-\ufaff]")
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
PDF_UNSAFE_CHAR_RE = re.compile(r"[\ufffd\ufeff]")
FONT_TEST_TEXT_RE = re.compile(
    r"^\s*the\s+quick\s+brown\s+fox\s+jumps\s+over\s+the\s+lazy\s+dog\.?\s*$",
    re.IGNORECASE,
)
LATEX_RESIDUE_RE = re.compile(
    r"\\(?:frac|dfrac|tfrac|sqrt|text|textregistered|mathrm|mathbf|mathit|mathsf|operatorname|"
    r"times|cdot|alpha|beta|gamma|delta|epsilon|theta|lambda|mu|pi|sigma|omega|"
    r"leq|geq|neq|left|right)\b"
    r"|[\^_]\s*\{"
)


OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
MINERU_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
QC_DIR.mkdir(parents=True, exist_ok=True)
LOG_LOCK = threading.Lock()


def log(message):
    with LOG_LOCK:
        print(message, flush=True)


def format_seconds(seconds):
    seconds = int(round(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{sec:02d}s"
    if minutes:
        return f"{minutes}m{sec:02d}s"
    return f"{sec}s"


def require_file(path, message):
    if not path.exists():
        raise RuntimeError(f"{message}: {path}")


def command_exists(name):
    return shutil.which(name) is not None


def python_module_exists(name):
    try:
        __import__(name)
        return True
    except Exception:
        return False


def command_prefix_for_system_install():
    try:
        is_root = subprocess.run(
            ["id", "-u"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        ).stdout.strip() == "0"
    except Exception:
        is_root = False
    if is_root:
        return []
    if command_exists("sudo"):
        return ["sudo", "-n"]
    return None


def run_install_command(cmd, label):
    log(f"    Formula dependency install: {label}: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    except Exception as exc:
        log(f"    Formula dependency install failed to start: {label}: {exc}")
        return False

    if result.returncode != 0:
        tail = "\n".join((result.stdout or "").splitlines()[-12:])
        log(f"    Formula dependency install failed: {label}, exit={result.returncode}")
        if tail:
            log(tail)
        return False
    return True


def install_formula_render_dependencies():
    if not AUTO_INSTALL_FORMULA_DEPS:
        return

    needs_system = not ((command_exists("latex") or command_exists("pdflatex")) and command_exists("dvisvgm"))
    if needs_system and command_exists("apt-get"):
        prefix = command_prefix_for_system_install()
        if prefix is None:
            log("    Formula dependency install skipped: apt-get requires root/sudo")
        else:
            run_install_command(prefix + ["apt-get", "update"], "apt-get update")
            run_install_command(
                prefix
                + [
                    "env",
                    "DEBIAN_FRONTEND=noninteractive",
                    "apt-get",
                    "install",
                    "-y",
                    *FORMULA_APT_PACKAGES,
                ],
                "apt-get install formula packages",
            )

    if not python_module_exists("svglib"):
        run_install_command(
            [sys.executable, "-m", "pip", "install", *FORMULA_PIP_PACKAGES],
            "pip install formula packages",
        )


def formula_render_dependencies():
    global FORMULA_RENDER_DEPS
    if FORMULA_RENDER_DEPS is not None:
        return FORMULA_RENDER_DEPS

    install_formula_render_dependencies()
    latex_engine = shutil.which("latex") or shutil.which("pdflatex")
    FORMULA_RENDER_DEPS = {
        "latex": latex_engine,
        "dvisvgm": shutil.which("dvisvgm"),
        "svglib": python_module_exists("svglib"),
    }
    missing = [key for key, value in FORMULA_RENDER_DEPS.items() if not value]
    if missing:
        log(
            "    Formula vector rendering unavailable; using text fallback. Missing: "
            + ", ".join(missing)
        )
    return FORMULA_RENDER_DEPS


def posix_process_group_exists(process_group_id):
    if os.name != "posix" or process_group_id is None:
        return False
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def wait_for_process_group_exit(process_group_id, timeout_seconds, process=None):
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while posix_process_group_exists(process_group_id):
        if process is not None:
            process.poll()
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def terminate_process_group(process, process_group_id, reason, grace_seconds=None):
    if grace_seconds is None:
        grace_seconds = MINERU_PROCESS_TERMINATE_GRACE_SECONDS
    grace_seconds = max(0.1, float(grace_seconds))

    if os.name == "posix" and process_group_id is not None:
        if not posix_process_group_exists(process_group_id):
            try:
                process.wait(timeout=0.1)
            except Exception:
                pass
            return
        log(
            f"    Terminating MinerU process group pgid={process_group_id}: {reason}"
        )
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            return
        if wait_for_process_group_exit(process_group_id, grace_seconds, process=process):
            try:
                process.wait(timeout=0.1)
            except Exception:
                pass
            return
        log(
            f"    MinerU process group pgid={process_group_id} did not exit within "
            f"{grace_seconds:.1f}s; sending SIGKILL"
        )
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        wait_for_process_group_exit(
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
    log(f"    Terminating MinerU process pid={process.pid}: {reason}")
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


def run_live_process(
    cmd,
    cwd,
    log_path,
    stream_to_console=True,
    timeout_seconds=MINERU_PROCESS_TIMEOUT_SECONDS,
    idle_timeout_seconds=MINERU_PROCESS_IDLE_TIMEOUT_SECONDS,
    termination_grace_seconds=MINERU_PROCESS_TERMINATE_GRACE_SECONDS,
):
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

        def flush_pending(force=False):
            nonlocal pending, pending_chars, last_flush
            if not pending:
                return
            now = time.monotonic()
            if not force and pending_chars < 8192 and len(pending) < 32 and now - last_flush < 0.25:
                return
            payload = "".join(pending)
            if stream_to_console:
                with LOG_LOCK:
                    sys.stdout.write(payload)
                    sys.stdout.flush()
            handle.write(payload)
            handle.flush()
            pending = []
            pending_chars = 0
            last_flush = now

        def append_output(payload):
            nonlocal pending_chars
            if not payload:
                return
            pending.append(payload)
            pending_chars += len(payload)

        def drain_available_output():
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
                    if timeout_seconds is not None and now - started_at >= float(timeout_seconds):
                        message = (
                            f"MinerU process exceeded total timeout of {float(timeout_seconds):.1f}s"
                        )
                        append_output(f"\n[controller] {message}\n")
                        flush_pending(force=True)
                        terminate_process_group(
                            process,
                            process_group_id,
                            message,
                            termination_grace_seconds,
                        )
                        raise RuntimeError(message)
                    if (
                        idle_timeout_seconds is not None
                        and now - last_activity >= float(idle_timeout_seconds)
                    ):
                        message = (
                            "MinerU process produced no stdout/stderr for "
                            f"{float(idle_timeout_seconds):.1f}s"
                        )
                        append_output(f"\n[controller] {message}\n")
                        flush_pending(force=True)
                        terminate_process_group(
                            process,
                            process_group_id,
                            message,
                            termination_grace_seconds,
                        )
                        raise RuntimeError(message)

                events = selector.select(timeout=0.25) if stdout_open else []
                received = False
                if events:
                    received = drain_available_output()
                flush_pending()

                if process.poll() is not None:
                    if parent_exit_seen_at is None:
                        parent_exit_seen_at = time.monotonic()
                    if stdout_open:
                        received = drain_available_output() or received
                    if not stdout_open or time.monotonic() - parent_exit_seen_at >= 1.0:
                        break

            append_output(decoder.decode(b"", final=True))
            flush_pending(force=True)
            returncode = process.wait()

            if process_group_id is not None and not wait_for_process_group_exit(
                process_group_id,
                MINERU_PROCESS_EXIT_CLEANUP_SECONDS,
                process=process,
            ):
                terminate_process_group(
                    process,
                    process_group_id,
                    "MinerU parent exited but child processes remained",
                    termination_grace_seconds,
                )
            return returncode
        except BaseException:
            flush_pending(force=True)
            terminate_process_group(
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

def parser_log_has_json_errors(log_path):
    if not log_path.exists():
        return False
    text = log_path.read_text(encoding="utf-8", errors="replace")
    return any(pattern in text for pattern in PARSER_RETRY_PATTERNS)


def retry_log_path(log_path, attempt):
    if attempt <= 1:
        return log_path
    return log_path.with_name(f"{log_path.stem}_retry{attempt - 1}{log_path.suffix}")


class MinerUParserAttemptsExhausted(RuntimeError):
    def __init__(self, message, transient_only=False, failures=None):
        super().__init__(message)
        self.transient_only = bool(transient_only)
        self.failures = list(failures or [])


def mineru_failure_is_transient(error, log_path=None):
    parts = [str(error)]
    if log_path and Path(log_path).exists():
        try:
            parts.append(Path(log_path).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    text = "\n".join(parts).lower()
    return any(pattern in text for pattern in TRANSIENT_MINERU_FAILURE_PATTERNS)


def bool_cli_value(value):
    return "true" if value else "false"


def mineru_parser_config(table_enabled=None):
    return {
        "backend": MINERU_BACKEND,
        "method": MINERU_METHOD,
        "lang": MINERU_LANG,
        "effort": MINERU_EFFORT,
        "formula": MINERU_FORMULA,
        "table": MINERU_TABLE if table_enabled is None else bool(table_enabled),
        "image_analysis": MINERU_IMAGE_ANALYSIS,
        "client_side_output_generation": MINERU_CLIENT_SIDE_OUTPUT_GENERATION,
        "extra_args": list(MINERU_EXTRA_ARGS),
    }


def mineru_parser_config_hash(table_enabled=None):
    payload = json.dumps(
        mineru_parser_config(table_enabled=table_enabled),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stable_json_hash(payload):
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1024)
def cached_file_sha256(resolved_path, size, mtime_ns, ctime_ns):
    digest = hashlib.sha256()
    with open(resolved_path, "rb") as handle:
        while True:
            chunk = handle.read(8 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def file_integrity_signature(path, relative_to=None):
    path = Path(path)
    resolved = path.resolve()
    before = resolved.stat()
    digest = cached_file_sha256(
        str(resolved),
        int(before.st_size),
        int(before.st_mtime_ns),
        int(before.st_ctime_ns),
    )
    after = resolved.stat()
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
    ):
        raise RuntimeError(f"File changed while its integrity hash was being computed: {resolved}")
    if relative_to is None:
        display_path = str(resolved)
    else:
        display_path = str(resolved.relative_to(Path(relative_to).resolve()))
    return {
        "path": display_path,
        "size": int(after.st_size),
        "mtime_ns": int(after.st_mtime_ns),
        "ctime_ns": int(after.st_ctime_ns),
        "sha256": digest,
    }


def file_content_signature(path, relative_to=None):
    signature = file_integrity_signature(path, relative_to=relative_to)
    return {
        "path": signature["path"],
        "size": signature["size"],
        "sha256": signature["sha256"],
    }


def source_file_signature(path):
    return file_integrity_signature(path)


def installed_package_versions():
    versions = {}
    for distribution in (
        "mineru",
        "magic-pdf",
        "PyMuPDF",
        "Pillow",
        "reportlab",
        "torch",
        "vllm",
    ):
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = None
    return versions


def mineru_model_identity():
    models = []
    if not MINERU_MODEL_CACHE_ROOT.exists():
        return models
    provider_root = MINERU_MODEL_CACHE_ROOT / "OpenDataLab"
    candidates_by_resolved_path = {}
    if provider_root.exists():
        for path in provider_root.iterdir():
            lowered_name = path.name.lower()
            if not path.is_dir() or not any(
                marker in lowered_name for marker in ("mineru", "pdf-extract-kit")
            ):
                continue
            candidates_by_resolved_path.setdefault(str(path.resolve()), path)
    candidates = [
        candidates_by_resolved_path[key]
        for key in sorted(candidates_by_resolved_path)
    ]
    for model_dir in candidates:
        files = []
        for path in sorted(model_dir.rglob("*")):
            if not path.is_file():
                continue
            files.append(file_content_signature(path, relative_to=model_dir))
        models.append(
            {
                "path": str(model_dir.resolve()),
                "files": files,
                "files_hash": stable_json_hash(files),
            }
        )
    return models


@lru_cache(maxsize=1)
def mineru_runtime_identity():
    executable = shutil.which(MINERU_COMMAND)
    packages = installed_package_versions()
    command_error = None if executable else f"command not found: {MINERU_COMMAND}"
    executable_signature = None
    if executable:
        try:
            executable_signature = file_content_signature(executable)
        except OSError as exc:
            command_error = command_error or f"could not hash executable: {exc}"
    identity = {
        "mineru_command": MINERU_COMMAND,
        "mineru_version_output": packages.get("mineru"),
        "mineru_command_error": command_error,
        "mineru_executable": executable_signature,
        "python": {
            "executable": str(Path(sys.executable).resolve()),
            "version": platform.python_version(),
            "platform": platform.platform(),
        },
        "packages": packages,
        "models": mineru_model_identity(),
    }
    return normalize_runtime_identity(identity)


def normalized_runtime_file_signature(signature):
    if not isinstance(signature, dict):
        return None
    if not all(key in signature for key in ("path", "size", "sha256")):
        raise ValueError("Runtime file identity is incomplete")
    return {
        "path": str(signature["path"]),
        "size": int(signature["size"]),
        "sha256": str(signature["sha256"]),
    }


def normalize_runtime_identity(identity):
    if not isinstance(identity, dict):
        raise ValueError("Runtime identity is missing")
    models = []
    for model in identity.get("models") or []:
        if not isinstance(model, dict) or "path" not in model:
            raise ValueError("Model runtime identity is incomplete")
        files = sorted(
            (
                normalized_runtime_file_signature(signature)
                for signature in model.get("files") or []
            ),
            key=lambda signature: signature["path"],
        )
        models.append(
            {
                "path": str(model["path"]),
                "files": files,
                "files_hash": stable_json_hash(files),
            }
        )
    models.sort(key=lambda model: model["path"])

    python_identity = identity.get("python") or {}
    packages = identity.get("packages") or {}
    return {
        "identity_schema": 2,
        "mineru_command": identity.get("mineru_command"),
        "mineru_version_output": identity.get("mineru_version_output"),
        "mineru_command_error": identity.get("mineru_command_error"),
        "mineru_executable": normalized_runtime_file_signature(
            identity.get("mineru_executable")
        ),
        "python": {
            "executable": python_identity.get("executable"),
            "version": python_identity.get("version"),
            "platform": python_identity.get("platform"),
        },
        "packages": dict(packages),
        "models": models,
    }


def mineru_runtime_identity_hash():
    return stable_json_hash(mineru_runtime_identity())


def completed_runtime_identity_matches(state):
    if not isinstance(state, dict):
        return False
    persisted_identity = state.get("runtime_identity")
    persisted_hash = state.get("runtime_identity_hash")
    if not isinstance(persisted_identity, dict) or not isinstance(persisted_hash, str):
        return False
    try:
        # Require the persisted hash to authenticate the exact old identity
        # before applying the timestamp-free compatibility normalization.
        if persisted_hash != stable_json_hash(persisted_identity):
            return False
        normalized_persisted = normalize_runtime_identity(persisted_identity)
    except (TypeError, ValueError):
        return False
    return stable_json_hash(normalized_persisted) == mineru_runtime_identity_hash()


def upgrade_completed_output_runtime_identity(state_path):
    state = read_checkpoint(state_path)
    if not completed_runtime_identity_matches(state):
        return False
    current_identity = mineru_runtime_identity()
    current_hash = mineru_runtime_identity_hash()
    if (
        state.get("runtime_identity") == current_identity
        and state.get("runtime_identity_hash") == current_hash
    ):
        return False
    state["runtime_identity"] = current_identity
    state["runtime_identity_hash"] = current_hash
    state["runtime_identity_updated_at"] = time.time()
    write_checkpoint(state_path, state)
    return True


def read_checkpoint(path):
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    stored_checksum = data.pop("_checkpoint_sha256", None)
    if not isinstance(stored_checksum, str):
        return None
    if stored_checksum != stable_json_hash(data):
        return None
    return data


def write_checkpoint(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if "_checkpoint_sha256" in payload:
        raise ValueError("Checkpoint payload uses reserved integrity field")
    record = dict(payload)
    record["_checkpoint_sha256"] = stable_json_hash(record)
    temp_path = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        except (AttributeError, OSError):
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def pdf_artifact_signature(path):
    signature = file_integrity_signature(path)
    with fitz.open(path) as document:
        signature["page_count"] = int(document.page_count)
    return signature


def completed_output_state_matches(
    state_path,
    pdf_path,
    text_pdf_path,
    markdown_path,
    image_pdf_path,
):
    state = read_checkpoint(state_path)
    if (
        not state
        or state.get("schema") != MINERU_CHECKPOINT_SCHEMA
        or state.get("output_version") != OUTPUT_VERSION
        or not completed_runtime_identity_matches(state)
    ):
        return False
    try:
        if state.get("source") != source_file_signature(pdf_path):
            return False
        with fitz.open(pdf_path) as source:
            expected_page_count = int(source.page_count)
        text_signature = pdf_artifact_signature(text_pdf_path)
        markdown_signature = file_integrity_signature(markdown_path)
    except (OSError, RuntimeError, ValueError, fitz.FileDataError):
        return False
    if text_signature != state.get("text_pdf"):
        return False
    if markdown_signature != state.get("markdown"):
        return False
    if text_signature.get("page_count") != expected_page_count:
        return False
    has_image_variant = bool(state.get("has_image_variant"))
    if has_image_variant != image_pdf_path.exists():
        return False
    fallback_pages = state.get("image_fallback_pages")
    if not isinstance(fallback_pages, list) or any(
        not isinstance(page_number, int)
        or page_number < 1
        or page_number > expected_page_count
        for page_number in fallback_pages
    ):
        return False
    if fallback_pages != sorted(set(fallback_pages)):
        return False
    if has_image_variant != bool(fallback_pages):
        return False
    if has_image_variant:
        try:
            image_signature = pdf_artifact_signature(image_pdf_path)
        except (OSError, RuntimeError, ValueError, fitz.FileDataError):
            return False
        if image_signature != state.get("image_pdf"):
            return False
        if image_signature.get("page_count") != expected_page_count:
            return False
    elif state.get("image_pdf") is not None or fallback_pages:
        return False
    return True


def write_completed_output_state(
    state_path,
    pdf_path,
    text_pdf_path,
    markdown_path,
    image_pdf_path,
    image_fallback_pages,
):
    text_signature = pdf_artifact_signature(text_pdf_path)
    markdown_signature = file_integrity_signature(markdown_path)
    image_signature = (
        pdf_artifact_signature(image_pdf_path) if image_fallback_pages else None
    )
    write_checkpoint(
        state_path,
        {
            "schema": MINERU_CHECKPOINT_SCHEMA,
            "output_version": OUTPUT_VERSION,
            "source": source_file_signature(pdf_path),
            "runtime_identity_hash": mineru_runtime_identity_hash(),
            "runtime_identity": mineru_runtime_identity(),
            "text_pdf": text_signature,
            "markdown": markdown_signature,
            "image_pdf": image_signature,
            "has_image_variant": bool(image_fallback_pages),
            "image_fallback_pages": [int(page_index) + 1 for page_index in image_fallback_pages],
            "updated_at": time.time(),
        },
    )


def checkpoint_identity(pdf_path, start_page, end_page, table_enabled=None):
    return {
        "schema": MINERU_CHECKPOINT_SCHEMA,
        "source": source_file_signature(pdf_path),
        "start_page": int(start_page),
        "end_page": int(end_page),
        "parser_config_hash": mineru_parser_config_hash(table_enabled=table_enabled),
        "runtime_identity_hash": mineru_runtime_identity_hash(),
    }


def checkpoint_matches(checkpoint, identity, status=None):
    if not checkpoint:
        return False
    if status is not None and checkpoint.get("status") != status:
        return False
    return all(checkpoint.get(key) == value for key, value in identity.items())


def mineru_output_has_results(output_dir):
    output_dir = Path(output_dir)
    if not output_dir.exists():
        return False
    try:
        _all_json_files, json_files, _branch_dir = current_mineru_result_files(output_dir)
    except Exception:
        return False
    return bool(json_files)


def optional_cli_args(table_enabled=None):
    args = []
    option_pairs = [
        ("--api-url", MINERU_API_URL),
        ("-b", MINERU_BACKEND),
        ("-m", MINERU_METHOD),
        ("-l", MINERU_LANG),
        ("--effort", MINERU_EFFORT),
        ("--client-side-output-generation", MINERU_CLIENT_SIDE_OUTPUT_GENERATION),
    ]
    for option, value in option_pairs:
        if value is None or value == "":
            continue
        args.extend([option, str(value)])

    bool_pairs = [
        ("-f", MINERU_FORMULA),
        ("-t", MINERU_TABLE if table_enabled is None else table_enabled),
        ("--image-analysis", MINERU_IMAGE_ANALYSIS),
    ]
    for option, value in bool_pairs:
        if value is None:
            continue
        args.extend([option, bool_cli_value(bool(value))])

    return args + list(MINERU_EXTRA_ARGS)


def run_mineru_parser(
    pdf_path,
    output_root,
    log_path,
    result_dir=None,
    table_enabled=None,
):
    if result_dir is None:
        result_dir = output_root / pdf_path.stem

    cmd = [
        MINERU_COMMAND,
        "-p",
        str(pdf_path),
        "-o",
        str(output_root),
        *optional_cli_args(table_enabled=table_enabled),
    ]

    log(f"    MinerU command: {' '.join(cmd)}")
    log(f"    MinerU log: {log_path}")
    log("    MinerU live progress:")

    start = time.monotonic()
    returncode = run_live_process(cmd, RUNTIME_ROOT, log_path, stream_to_console=True)
    with LOG_LOCK:
        sys.stdout.write("\n")
        sys.stdout.flush()
    if returncode != 0:
        raise RuntimeError(f"MinerU parser failed for {pdf_path}. See log: {log_path}")

    if not output_root.exists() or not any(output_root.rglob("*")):
        raise RuntimeError(f"MinerU finished but produced no output in: {output_root}")

    log(f"    MinerU finished in {format_seconds(time.monotonic() - start)}")


def run_mineru_parser_with_retries(
    pdf_path,
    output_root,
    log_path,
    result_dir=None,
    table_enabled=None,
    max_attempts=None,
):
    attempts = PARSER_MAX_ATTEMPTS if max_attempts is None else int(max_attempts)
    attempts = max(1, attempts)
    failures = []

    for attempt in range(1, attempts + 1):
        attempt_log = retry_log_path(log_path, attempt)
        if attempt > 1:
            delay = max(0.0, float(PARSER_RETRY_BACKOFF_SECONDS)) * (attempt - 1)
            log(
                f"    Restarting the same MinerU task, attempt {attempt}/{attempts} "
                f"after {delay:.1f}s backoff"
            )
            if delay:
                time.sleep(delay)
            if output_root.exists():
                shutil.rmtree(output_root)
            output_root.mkdir(parents=True, exist_ok=True)

        try:
            run_mineru_parser(
                pdf_path,
                output_root,
                attempt_log,
                result_dir=result_dir,
                table_enabled=table_enabled,
            )
            if parser_log_has_json_errors(attempt_log):
                raise RuntimeError(
                    f"MinerU log reported malformed/incomplete JSON output. See log: {attempt_log}"
                )
            if not mineru_output_has_results(output_root):
                raise RuntimeError(
                    f"MinerU finished without a usable middle/content JSON result: {output_root}"
                )
            result_manifest = build_mineru_result_manifest(pdf_path, output_root)
            if attempt > 1:
                log(f"    Same-task MinerU retry succeeded on attempt {attempt}/{attempts}")
            return result_manifest
        except RuntimeError as exc:
            transient = mineru_failure_is_transient(exc, attempt_log)
            failures.append(
                {
                    "attempt": attempt,
                    "error": str(exc),
                    "log_path": str(attempt_log),
                    "transient": transient,
                }
            )
            kind = "transient" if transient else "content/unknown"
            log(
                f"    MinerU same-task attempt {attempt}/{attempts} failed "
                f"({kind}): {exc}"
            )
            if attempt >= attempts:
                break

    transient_only = bool(failures) and all(item["transient"] for item in failures)
    details = " | ".join(
        f"attempt {item['attempt']}: {item['error']}" for item in failures
    )
    raise MinerUParserAttemptsExhausted(
        f"MinerU failed after {attempts} attempt(s) for the same task: {details}",
        transient_only=transient_only,
        failures=failures,
    )


def write_pdf_page_chunk(pdf_path, chunk_path, start_page, end_page):
    chunk_path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open(pdf_path) as src:
        out = fitz.open()
        out.insert_pdf(src, from_page=start_page, to_page=end_page)
        out.save(chunk_path, garbage=4, deflate=True)
        out.close()


def write_pdf_selected_pages(pdf_path, output_path, page_indices):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open(pdf_path) as src:
        out = fitz.open()
        for page_index in page_indices:
            if page_index < 0 or page_index >= src.page_count:
                raise IndexError(f"Selected page index {page_index} is outside {pdf_path}")
            out.insert_pdf(src, from_page=page_index, to_page=page_index)
        out.save(output_path, garbage=4, deflate=True)
        out.close()


def selected_pages_checkpoint_identity(pdf_path, page_indices, table_enabled=None):
    return {
        "schema": MINERU_CHECKPOINT_SCHEMA,
        "source": source_file_signature(pdf_path),
        "selected_pages": [int(page_index) for page_index in page_indices],
        "parser_config_hash": mineru_parser_config_hash(table_enabled=table_enabled),
        "runtime_identity_hash": mineru_runtime_identity_hash(),
    }


def run_or_reuse_mineru_selected_pages(
    pdf_path,
    page_indices,
    selected_pdf,
    output_root,
    log_path,
    checkpoint_path,
    table_enabled=None,
):
    page_indices = [int(page_index) for page_index in page_indices]
    identity = selected_pages_checkpoint_identity(
        pdf_path,
        page_indices,
        table_enabled=table_enabled,
    )
    checkpoint = read_checkpoint(checkpoint_path)
    if checkpoint_matches(checkpoint, identity, status="complete"):
        if not selected_pdf.exists():
            write_pdf_selected_pages(pdf_path, selected_pdf, page_indices)
        if mineru_result_manifest_matches(checkpoint, selected_pdf, output_root):
            log(
                f"    Resume: reusing one integrity-verified MinerU review batch containing "
                f"{len(page_indices)} page(s)"
            )
            return collect_page_results(selected_pdf, output_root)

    if selected_pdf.exists():
        selected_pdf.unlink()
    write_pdf_selected_pages(pdf_path, selected_pdf, page_indices)
    if output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    log(
        "    Running one MinerU review batch for original pages: "
        + ", ".join(str(page_index + 1) for page_index in page_indices)
    )
    result_manifest = run_mineru_parser_with_retries(
        selected_pdf,
        output_root,
        log_path,
        table_enabled=table_enabled,
    )
    write_checkpoint(
        checkpoint_path,
        {
            **identity,
            "status": "complete",
            "updated_at": time.time(),
            "output_dir": str(output_root),
            "result_manifest": result_manifest,
        },
    )
    return collect_page_results(selected_pdf, output_root)


def build_mineru_parser_runs(pdf_path, page_count, work_dir, mineru_dir, log_path):
    chunk_pdf_dir = work_dir / "mineru_pdf_chunks"
    checkpoint_dir = work_dir / "mineru_checkpoints"
    chunk_output_root = mineru_dir / "chunks"
    chunk_pdf_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    chunk_output_root.mkdir(parents=True, exist_ok=True)

    def run_chunk(start_page, end_page, chunk_id, use_full_paths=False):
        label = f"chunk_{chunk_id}_pages_{start_page + 1:04d}_{end_page + 1:04d}"
        checkpoint_path = checkpoint_dir / f"{label}.json"
        identity = checkpoint_identity(pdf_path, start_page, end_page)
        if use_full_paths:
            chunk_pdf = pdf_path
            chunk_output_dir = mineru_dir
            chunk_log = log_path
        else:
            chunk_pdf = chunk_pdf_dir / f"{pdf_path.stem}_{label}.pdf"
            chunk_output_dir = chunk_output_root / label
            chunk_log = log_path.with_name(f"{log_path.stem}_{label}{log_path.suffix}")

        page_total = end_page - start_page + 1
        log(
            f"    MinerU task {label}: original pages {start_page + 1}-{end_page + 1} "
            f"({page_total} page(s))"
        )

        checkpoint = read_checkpoint(checkpoint_path)
        if checkpoint_matches(checkpoint, identity, status="complete"):
            if not use_full_paths and not chunk_pdf.exists():
                write_pdf_page_chunk(pdf_path, chunk_pdf, start_page, end_page)
            if mineru_result_manifest_matches(checkpoint, chunk_pdf, chunk_output_dir):
                log(f"    Resume: reusing integrity-verified MinerU task {label}")
                return [
                    {
                        "pdf_path": chunk_pdf,
                        "output_dir": chunk_output_dir,
                        "log_path": chunk_log,
                        "page_offset": start_page,
                        "start_page": start_page,
                        "end_page": end_page,
                        "label": label,
                    }
                ]

        if checkpoint_matches(checkpoint, identity, status="split") and page_total > MINERU_MIN_PAGES_PER_TASK:
            midpoint = start_page + (page_total // 2) - 1
            log(
                f"    Resume: task {label} was already split; continuing directly with "
                f"pages {start_page + 1}-{midpoint + 1} and {midpoint + 2}-{end_page + 1}"
            )
            return (
                run_chunk(start_page, midpoint, f"{chunk_id}a")
                + run_chunk(midpoint + 1, end_page, f"{chunk_id}b")
            )

        if not use_full_paths:
            if chunk_pdf.exists():
                chunk_pdf.unlink()
            write_pdf_page_chunk(pdf_path, chunk_pdf, start_page, end_page)
        if chunk_output_dir.exists():
            shutil.rmtree(chunk_output_dir)
        chunk_output_dir.mkdir(parents=True, exist_ok=True)

        try:
            result_manifest = run_mineru_parser_with_retries(
                chunk_pdf,
                chunk_output_dir,
                chunk_log,
            )
        except RuntimeError as exc:
            if isinstance(exc, MinerUParserAttemptsExhausted) and exc.transient_only:
                log(
                    f"    MinerU task {label} exhausted same-task retries due only to "
                    "transient failures; preserving the original chunk instead of splitting"
                )
                raise RuntimeError(
                    f"MinerU transient failure persisted for task {label}; task was not split. "
                    "Retry this file later with the original chunk boundaries."
                ) from exc
            if page_total <= MINERU_MIN_PAGES_PER_TASK:
                raise RuntimeError(
                    f"MinerU failed even after adaptive splitting on original page {start_page + 1}. "
                    f"See log: {chunk_log}"
                ) from exc

            if chunk_output_dir.exists():
                shutil.rmtree(chunk_output_dir)
            midpoint = start_page + (page_total // 2) - 1
            log(
                f"    MinerU task {label} failed; splitting into "
                f"pages {start_page + 1}-{midpoint + 1} and {midpoint + 2}-{end_page + 1}"
            )
            write_checkpoint(
                checkpoint_path,
                {
                    **identity,
                    "status": "split",
                    "updated_at": time.time(),
                    "failure": str(exc),
                },
            )
            return (
                run_chunk(start_page, midpoint, f"{chunk_id}a")
                + run_chunk(midpoint + 1, end_page, f"{chunk_id}b")
            )

        write_checkpoint(
            checkpoint_path,
            {
                **identity,
                "status": "complete",
                "updated_at": time.time(),
                "output_dir": str(chunk_output_dir),
                "result_manifest": result_manifest,
            },
        )

        return [
            {
                "pdf_path": chunk_pdf,
                "output_dir": chunk_output_dir,
                "log_path": chunk_log,
                "page_offset": start_page,
                "start_page": start_page,
                "end_page": end_page,
                "label": label,
            }
        ]

    if page_count <= MINERU_MAX_PAGES_PER_TASK:
        return run_chunk(0, page_count - 1, "full", use_full_paths=True)

    total_chunks = math.ceil(page_count / MINERU_MAX_PAGES_PER_TASK)
    log(
        f"    Large PDF detected: {page_count} pages; splitting into {total_chunks} "
        f"initial MinerU task(s), max {MINERU_MAX_PAGES_PER_TASK} pages each"
    )

    runs = []
    for chunk_index, start_page in enumerate(range(0, page_count, MINERU_MAX_PAGES_PER_TASK), 1):
        end_page = min(page_count - 1, start_page + MINERU_MAX_PAGES_PER_TASK - 1)
        runs.extend(run_chunk(start_page, end_page, f"{chunk_index:04d}"))
    return runs

def render_pdf_page_to_png(pdf_path, page_index, image_path):
    image_path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open(pdf_path) as doc:
        page = doc[page_index]
        matrix = fitz.Matrix(DPI / 72, DPI / 72)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        pix.save(str(image_path))


def is_likely_math_text(text):
    text = str(text or "").strip()
    if not text or len(text) > 220:
        return False
    if re.search(r"\s{2,}", text):
        return False
    if re.fullmatch(r"[A-Za-zΑ-ω]", text):
        return True
    if re.search(r"[=+\-*/^_{}\\<>≤≥≈≠∈∉∂√∞∑∏∫]", text):
        return True
    if re.fullmatch(r"[A-Za-zΑ-ω0-9().,;: ]{1,80}", text) and re.search(r"[A-Za-zΑ-ω]", text):
        return True
    return False


def strip_known_inline_html(text):
    text = re.sub(r"(?is)<br\s*/?>", " ", text)
    text = re.sub(r"(?is)<(strong|b|em|i|code|mark|del|s|u|sup|sub)\b[^>]*>(.*?)</\1\s*>", r"\2", text)
    text = re.sub(r"(?is)</?(span|small|font)\b[^>]*>", "", text)
    return text


def normalize_inline_markup_aliases(text):
    text = str(text or "")
    text = re.sub(r"(?is)<br\s*/?>", " ", text)
    text = re.sub(r"(?is)<(strong|b)\b[^>]*>(.*?)</\1\s*>", r"**\2**", text)
    text = re.sub(r"(?is)<(em|i)\b[^>]*>(.*?)</\1\s*>", r"*\2*", text)
    text = re.sub(r"(?is)<code\b[^>]*>(.*?)</code\s*>", r"`\1`", text)
    text = re.sub(r"(?is)<(mark|u)\b[^>]*>(.*?)</\1\s*>", r"==\2==", text)
    text = re.sub(r"(?is)<(del|s)\b[^>]*>(.*?)</\1\s*>", r"~~\2~~", text)
    text = re.sub(r"(?is)<(sup|sub)\b[^>]*>(.*?)</\1\s*>", r"\2", text)
    text = re.sub(r"(?is)</?(span|small|font)\b[^>]*>", "", text)
    text = re.sub(r"\[\^([^\]\n]{1,24})\]", r"\1", text)
    text = re.sub(r"(?s)\\\((.{1,220}?)\\\)", r"$\1$", text)
    text = re.sub(r"(?s)\\\[(.{1,500}?)\\\]", r"$\1$", text)
    text = re.sub(r"(?<!\\)\$\$([^$\n]{1,500}?)(?<!\\)\$\$", r"$\1$", text)
    return text


def strip_dollar_math(text):
    def replace_display(match):
        return match.group(1).strip()

    def replace_inline(match):
        inner = match.group(1)
        return inner if is_likely_math_text(inner) else match.group(0)

    text = re.sub(r"(?<!\\)\$\$([^$\n]{1,500}?)(?<!\\)\$\$", replace_display, text)
    return re.sub(r"(?<!\\)\$([^$\n]{1,220}?)(?<!\\)\$", replace_inline, text)


def strip_markdown_inline(text):
    text = normalize_inline_markup_aliases(text)
    text = strip_known_inline_html(text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[\^([^\]\n]{1,24})\]", r"\1", text)
    text = re.sub(r"(?s)```[A-Za-z0-9_-]*\n?(.*?)```", r"\1", text)
    text = re.sub(r"(?m)^```[A-Za-z0-9_-]*\s*$", "", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"(?s)\\\((.{1,220}?)\\\)", r"\1", text)
    text = re.sub(r"(?s)\\\[(.{1,500}?)\\\]", r"\1", text)
    text = strip_dollar_math(text)
    text = re.sub(r"\*\*\*([^*\n]+?)\*\*\*", r"\1", text)
    text = re.sub(r"___([^_\n]+?)___", r"\1", text)
    text = re.sub(r"\*\*([^*\n]+?)\*\*", r"\1", text)
    text = re.sub(r"__([^_\n]+?)__", r"\1", text)
    text = re.sub(r"~~([^~\n]+?)~~", r"\1", text)
    text = re.sub(r"==([^=\n]+?)==", r"\1", text)
    text = re.sub(r"~([^~\n]{1,60}?)~", r"\1", text)
    text = re.sub(r"\^([^^\n]{1,60}?)\^", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!_)_([^_\n]+?)_(?!_)", r"\1", text)
    return text


def strip_html_tags(text):
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p\s*>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", "", text)
    return html_lib.unescape(text).strip()


def html_table_to_text(text):
    if not re.search(r"(?is)<\s*(table|tr|td|th)\b", text or ""):
        return text

    def convert_table(match):
        table = match.group(0)
        rows = []
        for row_match in re.finditer(r"(?is)<tr\b[^>]*>(.*?)</tr\s*>", table):
            row_html = row_match.group(1)
            cells = []
            for cell_match in re.finditer(r"(?is)<t[dh]\b[^>]*>(.*?)</t[dh]\s*>", row_html):
                cell = strip_html_tags(cell_match.group(1))
                cell = re.sub(r"[ \t\r\f\v]+", " ", cell)
                if cell:
                    cells.append(cell)
            if cells:
                rows.append("    ".join(cells))
        if rows:
            return "\n".join(rows)
        return strip_html_tags(table)

    text = re.sub(r"(?is)<table\b[^>]*>.*?</table\s*>", convert_table, text)
    text = re.sub(r"(?is)<tr\b[^>]*>", "\n", text)
    text = re.sub(r"(?is)</tr\s*>", "\n", text)
    text = re.sub(r"(?is)</t[dh]\s*>\s*<t[dh]\b[^>]*>", " | ", text)
    text = re.sub(r"(?is)</?t[dh]\b[^>]*>", "", text)
    return strip_html_tags(text)


DOT_CHARS_CLASS = r"\.．·•∙‧⋅・･"
SPACED_DOT_LEADER_RE = re.compile(
    rf"(?:[ \t\r\n]*[{DOT_CHARS_CLASS}][ \t\r\n]*){{{DOT_LEADER_KEEP + 1},}}"
)
PLAIN_DOT_LEADER_RE = re.compile(rf"[{DOT_CHARS_CLASS}]{{{DOT_LEADER_KEEP + 1},}}")
DOT_LEADER_EXPLOSION_RE = re.compile(
    rf"(?:[ \t\r\n]*[{DOT_CHARS_CLASS}][ \t\r\n]*){{{DOT_LEADER_EXPLOSION_MIN_DOTS},}}"
)
DOT_LEADER_REPLACEMENT = " ".join(["."] * DOT_LEADER_KEEP)


def normalize_long_dot_leaders(text):
    # OCR models can hallucinate a single scanned dot leader into thousands of spaced dots.
    if text is None:
        return ""
    text = str(text)
    text = SPACED_DOT_LEADER_RE.sub(" " + DOT_LEADER_REPLACEMENT + " ", text)
    text = PLAIN_DOT_LEADER_RE.sub("." * DOT_LEADER_KEEP, text)
    return text


def repeated_text_key(text):
    text = normalize_unicode_preserving_greek_spacing(str(text or ""))
    text = strip_markdown_inline(text)
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    text = re.sub(r"^[\W_]+|[\W_]+$", "", text, flags=re.UNICODE)
    return text


def collapse_consecutive_repeated_lines(text):
    lines = str(text or "").splitlines()
    if len(lines) < REPEATED_TEXT_MIN_RUN:
        return str(text or "")

    collapsed = []
    index = 0
    while index < len(lines):
        line = lines[index]
        key = repeated_text_key(line)
        if not key or len(line.strip()) > REPEATED_TEXT_MAX_UNIT_CHARS:
            collapsed.append(line)
            index += 1
            continue

        run_end = index + 1
        while run_end < len(lines) and repeated_text_key(lines[run_end]) == key:
            run_end += 1

        if run_end - index >= REPEATED_TEXT_MIN_RUN:
            collapsed.append(line)
        else:
            collapsed.extend(lines[index:run_end])
        index = run_end

    return "\n".join(collapsed)


def sentence_units_with_separators(text):
    parts = re.split(r"([^.!?。！？\n]{8,240}[.!?。！？]\s*)", str(text or ""))
    units = []
    for part in parts:
        if not part:
            continue
        if re.fullmatch(r"[^.!?。！？\n]{8,240}[.!?。！？]\s*", part):
            units.append(("sentence", part))
        else:
            units.append(("other", part))
    return units


def collapse_consecutive_repeated_sentences(text):
    units = sentence_units_with_separators(text)
    if len(units) < REPEATED_TEXT_MIN_RUN:
        return text

    result = []
    index = 0
    while index < len(units):
        kind, value = units[index]
        if kind != "sentence":
            result.append(value)
            index += 1
            continue

        key = repeated_text_key(value)
        if not key or len(value.strip()) > REPEATED_TEXT_MAX_UNIT_CHARS:
            result.append(value)
            index += 1
            continue

        run_end = index + 1
        while run_end < len(units):
            next_kind, next_value = units[run_end]
            if next_kind != "sentence" or repeated_text_key(next_value) != key:
                break
            run_end += 1

        if run_end - index >= REPEATED_TEXT_MIN_RUN:
            result.append(value)
        else:
            result.extend(unit_value for _, unit_value in units[index:run_end])
        index = run_end

    return "".join(result)


def collapse_repeated_token_runs_in_line(line):
    if not line or len(line) < 24:
        return line

    tokens = re.findall(r"\S+", line)
    if len(tokens) < REPEATED_TEXT_MIN_RUN:
        return line

    collapsed = []
    index = 0
    max_ngram = min(REPEATED_TOKEN_MAX_NGRAM, len(tokens) // REPEATED_TEXT_MIN_RUN)
    while index < len(tokens):
        matched = False
        for ngram_size in range(1, max_ngram + 1):
            pattern = tokens[index:index + ngram_size]
            if len(pattern) < ngram_size:
                continue
            pattern_text = " ".join(pattern)
            if len(pattern_text) > REPEATED_TEXT_MAX_UNIT_CHARS:
                continue

            run_end = index + ngram_size
            run_count = 1
            while tokens[run_end:run_end + ngram_size] == pattern:
                run_count += 1
                run_end += ngram_size

            if run_count >= REPEATED_TEXT_MIN_RUN:
                collapsed.extend(pattern)
                index = run_end
                matched = True
                break

        if not matched:
            collapsed.append(tokens[index])
            index += 1

    collapsed_line = " ".join(collapsed)
    # Keep the original line when nothing meaningful changed so spacing-sensitive
    # prose is not normalized unnecessarily.
    if len(collapsed_line) >= len(line) * 0.96:
        return line
    return collapsed_line


def looks_like_repeated_token_artifact(text):
    tokens = re.findall(r"\S+", str(text or ""))
    if len(tokens) < 8:
        return False

    lowered = [token.lower() for token in tokens]
    counts = {}
    for token in lowered:
        counts[token] = counts.get(token, 0) + 1

    max_count = max(counts.values(), default=0)
    unique_ratio = len(counts) / max(len(lowered), 1)
    coordinate_count = sum(
        1
        for token in lowered
        if re.fullmatch(r"[xy]\s*=?-?\d+(?:\.\d+)?", token)
    )
    hash_marker_count = sum(1 for token in lowered if token.strip() == "###")

    if coordinate_count >= 4:
        return True
    if hash_marker_count >= 3:
        return True
    return max_count / max(len(lowered), 1) >= 0.18 or unique_ratio <= 0.38


def collapse_repeated_token_runs(text):
    lines = str(text or "").splitlines()
    if not lines:
        return str(text or "")
    collapsed = "\n".join(collapse_repeated_token_runs_in_line(line) for line in lines)
    if looks_like_repeated_token_artifact(collapsed):
        compacted = collapse_repeated_token_runs_in_line(re.sub(r"\s+", " ", collapsed).strip())
        if len(compacted) < len(collapsed) * 0.90:
            return compacted
    return collapsed


def collapse_repeated_ocr_text(text):
    text = collapse_repeated_token_runs(text)
    text = collapse_consecutive_repeated_lines(text)
    text = collapse_consecutive_repeated_sentences(text)
    return text



def has_dot_leader_explosion(text):
    return bool(DOT_LEADER_EXPLOSION_RE.search(str(text or "")))


def sanitize_layout_cell(cell):
    if not isinstance(cell, dict):
        return cell
    fixed = dict(cell)
    for key in ("text", "content", "html", "latex"):
        value = fixed.get(key)
        if value:
            fixed[key] = normalize_long_dot_leaders(str(value))
    return fixed


def sanitize_layout_cells(cells):
    return [sanitize_layout_cell(cell) for cell in cells]


def strip_control_chars(text):
    if text is None:
        return ""
    text = str(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = CONTROL_CHAR_RE.sub("", text)
    return PDF_UNSAFE_CHAR_RE.sub("", text)


def normalize_fraction_markup(text):
    text = str(text or "")

    vulgar_fractions = {
        ("0", "3"): "↉",
        ("1", "2"): "½",
        ("1", "3"): "⅓",
        ("2", "3"): "⅔",
        ("1", "4"): "¼",
        ("3", "4"): "¾",
        ("1", "5"): "⅕",
        ("2", "5"): "⅖",
        ("3", "5"): "⅗",
        ("4", "5"): "⅘",
        ("1", "6"): "⅙",
        ("5", "6"): "⅚",
        ("1", "7"): "⅐",
        ("1", "8"): "⅛",
        ("3", "8"): "⅜",
        ("5", "8"): "⅝",
        ("7", "8"): "⅞",
        ("1", "9"): "⅑",
        ("1", "10"): "⅒",
    }

    def fraction_replacement(match):
        numerator = match.group(1).strip()
        denominator = match.group(2).strip()
        return vulgar_fractions.get((numerator, denominator), f"{numerator}/{denominator}")

    def loose_fraction_replacement(match):
        numerator = match.group(1).strip()
        denominator = match.group(2).strip()
        trailing_dot = "." if denominator.endswith(".") else ""
        denominator_key = denominator[:-1] if trailing_dot else denominator
        return vulgar_fractions.get(
            (numerator, denominator_key),
            f"{numerator}/{denominator_key}{trailing_dot}",
        )

    text = re.sub(
        r"\\(?:dfrac|tfrac|frac)\s*\{\s*([0-9]+)\s*\}\s*\{\s*([0-9]+)\s*\}",
        fraction_replacement,
        text,
    )
    text = re.sub(
        r"\\(?:dfrac|tfrac|frac)\s*\{\s*([0-9]+)\s*\}\s*\n\s*\{\s*([0-9]+\.?)\s*\}?",
        loose_fraction_replacement,
        text,
    )
    text = re.sub(
        r"\\(?:dfrac|tfrac|frac)\s*\{\s*([0-9]+)\s*\}\s*\{\s*([0-9]+\.?)\s*\}?",
        loose_fraction_replacement,
        text,
    )
    text = re.sub(
        r"\^\s*\{\s*([0-9]+)\s*\}\s*/\s*_\s*\{\s*([0-9]+)\s*\}",
        fraction_replacement,
        text,
    )
    text = re.sub(
        r"\^\s*([0-9]+)\s*/\s*_\s*([0-9]+)",
        fraction_replacement,
        text,
    )
    text = re.sub(
        r"\{\s*([0-9]{1,4})\s*/\s*([0-9]{1,4})\s*\}",
        fraction_replacement,
        text,
    )
    text = re.sub(
        r"(?<![0-9])([0-9])\s*\u2044\s*([0-9]{1,2})(?![0-9])",
        fraction_replacement,
        text,
    )
    return text


def normalize_citation_superscripts(text):
    text = str(text or "")
    # MinerU may encode a numeric reference marker as Markdown/LaTeX superscript.
    # Keep the citation number while removing syntax that should not appear in prose.
    text = re.sub(r"\\textsuperscript\s*\{\s*(\[[0-9][0-9,;，、\-– ]{0,80}\])\s*\}", r"\1", text)
    text = re.sub(r"\^\s*\{\s*(\[[0-9][0-9,;，、\-– ]{0,80}\])\s*\}", r"\1", text)
    text = re.sub(r"\^\s*\(\s*(\[[0-9][0-9,;，、\-– ]{0,80}\])\s*\)", r"\1", text)
    # OCR sometimes turns small inline text into LaTeX-style scripts, e.g.
    # "2lei]" -> "2^{lei}]". Keep the text and remove only the syntax.
    text = re.sub(r"([0-9A-Za-z])\^\s*\{\s*([^{}\n]{1,60})\s*\}", r"\1\2", text)
    text = re.sub(r"([0-9A-Za-z])_\s*\{\s*([^{}\n]{1,60})\s*\}", r"\1\2", text)
    text = re.sub(r"\^\s*\{\s*([^{}\n]{1,60})\s*\}", r"\1", text)
    text = re.sub(r"_\s*\{\s*([^{}\n]{1,60})\s*\}", r"\1", text)
    return text


GREEK_SPACING_MARKS = "\u1fbd\u1fbf\u1ffe\u1fef"
GREEK_SPACING_MARK_PLACEHOLDERS = {
    "\u1fbd": "\ue100",
    "\u1fbf": "\ue101",
    "\u1ffe": "\ue102",
    "\u1fef": "\ue103",
}
GREEK_SPACING_MARK_RESTORE = {
    placeholder: char for char, placeholder in GREEK_SPACING_MARK_PLACEHOLDERS.items()
}

VULGAR_FRACTION_CHARS = "¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞↉"
VULGAR_FRACTION_PLACEHOLDERS = {
    char: chr(0xE110 + index) for index, char in enumerate(VULGAR_FRACTION_CHARS)
}
VULGAR_FRACTION_RESTORE = {
    placeholder: char for char, placeholder in VULGAR_FRACTION_PLACEHOLDERS.items()
}

SCRIPT_FORM_CHARS = "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ"
SCRIPT_FORM_PLACEHOLDERS = {
    char: chr(0xE140 + index) for index, char in enumerate(SCRIPT_FORM_CHARS)
}
SCRIPT_FORM_RESTORE = {
    placeholder: char for char, placeholder in SCRIPT_FORM_PLACEHOLDERS.items()
}


def normalize_unicode_preserving_greek_spacing(text):
    text = str(text or "")
    for char, placeholder in GREEK_SPACING_MARK_PLACEHOLDERS.items():
        text = text.replace(char, placeholder)
    for char, placeholder in VULGAR_FRACTION_PLACEHOLDERS.items():
        text = text.replace(char, placeholder)
    for char, placeholder in SCRIPT_FORM_PLACEHOLDERS.items():
        text = text.replace(char, placeholder)
    text = unicodedata.normalize("NFKC", text)
    for placeholder, char in GREEK_SPACING_MARK_RESTORE.items():
        text = text.replace(placeholder, char)
    for placeholder, char in VULGAR_FRACTION_RESTORE.items():
        text = text.replace(placeholder, char)
    for placeholder, char in SCRIPT_FORM_RESTORE.items():
        text = text.replace(placeholder, char)
    return text


LATEX_SYMBOL_MAP = {
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "epsilon": "ε",
    "varepsilon": "ε",
    "zeta": "ζ",
    "eta": "η",
    "theta": "θ",
    "vartheta": "ϑ",
    "iota": "ι",
    "kappa": "κ",
    "lambda": "λ",
    "mu": "μ",
    "nu": "ν",
    "xi": "ξ",
    "pi": "π",
    "varpi": "ϖ",
    "rho": "ρ",
    "varrho": "ϱ",
    "sigma": "σ",
    "varsigma": "ς",
    "tau": "τ",
    "upsilon": "υ",
    "phi": "φ",
    "varphi": "ϕ",
    "chi": "χ",
    "psi": "ψ",
    "omega": "ω",
    "Gamma": "Γ",
    "Delta": "Δ",
    "Theta": "Θ",
    "Lambda": "Λ",
    "Xi": "Ξ",
    "Pi": "Π",
    "Sigma": "Σ",
    "Upsilon": "Υ",
    "Phi": "Φ",
    "Psi": "Ψ",
    "Omega": "Ω",
    "le": "≤",
    "leq": "≤",
    "ge": "≥",
    "geq": "≥",
    "neq": "≠",
    "ne": "≠",
    "approx": "≈",
    "sim": "∼",
    "simeq": "≃",
    "times": "×",
    "cdot": "·",
    "pm": "±",
    "mp": "∓",
    "infty": "∞",
    "partial": "∂",
    "nabla": "∇",
    "in": "∈",
    "notin": "∉",
    "subset": "⊂",
    "subseteq": "⊆",
    "supset": "⊃",
    "supseteq": "⊇",
    "cup": "∪",
    "cap": "∩",
    "to": "→",
    "rightarrow": "→",
    "leftarrow": "←",
    "Rightarrow": "⇒",
    "Leftarrow": "⇐",
    "leftrightarrow": "↔",
    "forall": "∀",
    "exists": "∃",
    "neg": "¬",
    "land": "∧",
    "lor": "∨",
    "textregistered": "®",
}

SUPERSCRIPT_MAP = str.maketrans({
    "0": "⁰",
    "1": "¹",
    "2": "²",
    "3": "³",
    "4": "⁴",
    "5": "⁵",
    "6": "⁶",
    "7": "⁷",
    "8": "⁸",
    "9": "⁹",
    "+": "⁺",
    "-": "⁻",
    "=": "⁼",
    "(": "⁽",
    ")": "⁾",
    "n": "ⁿ",
    "i": "ⁱ",
})

SUBSCRIPT_MAP = str.maketrans({
    "0": "₀",
    "1": "₁",
    "2": "₂",
    "3": "₃",
    "4": "₄",
    "5": "₅",
    "6": "₆",
    "7": "₇",
    "8": "₈",
    "9": "₉",
    "+": "₊",
    "-": "₋",
    "=": "₌",
    "(": "₍",
    ")": "₎",
    "a": "ₐ",
    "e": "ₑ",
    "h": "ₕ",
    "i": "ᵢ",
    "j": "ⱼ",
    "k": "ₖ",
    "l": "ₗ",
    "m": "ₘ",
    "n": "ₙ",
    "o": "ₒ",
    "p": "ₚ",
    "r": "ᵣ",
    "s": "ₛ",
    "t": "ₜ",
    "u": "ᵤ",
    "v": "ᵥ",
    "x": "ₓ",
})


def strip_latex_math_delimiters(text):
    text = str(text or "").strip()
    if text.startswith("$$") and text.endswith("$$") and len(text) >= 4:
        return text[2:-2].strip()
    pairs = [
        ("\\[", "\\]"),
        ("\\(", "\\)"),
        ("$", "$"),
    ]
    for start, end in pairs:
        if text.startswith(start) and text.endswith(end) and len(text) >= len(start) + len(end):
            return text[len(start):-len(end)].strip()
    return text


def translate_script_text(value, mapping):
    translated = value.translate(mapping)
    if len(translated) != len(value):
        return None
    for source_char, target_char in zip(value, translated):
        if source_char == target_char and source_char.strip():
            return None
    return translated


def latex_scripts_to_unicode(text):
    text = str(text or "")

    def replace_braced(match):
        base = match.group(1)
        marker = match.group(2)
        value = re.sub(r"\s+", "", match.group(3))
        mapping = SUPERSCRIPT_MAP if marker == "^" else SUBSCRIPT_MAP
        translated = translate_script_text(value, mapping)
        if translated is None:
            return match.group(0)
        return base + translated

    def replace_single(match):
        base = match.group(1)
        marker = match.group(2)
        value = match.group(3)
        mapping = SUPERSCRIPT_MAP if marker == "^" else SUBSCRIPT_MAP
        translated = translate_script_text(value, mapping)
        if translated is None:
            return match.group(0)
        return base + translated

    text = re.sub(
        r"([A-Za-zΑ-ω0-9\)\]])\s*([\^_])\s*\{\s*([^{}\n]{1,12})\s*\}",
        replace_braced,
        text,
    )
    text = re.sub(
        r"([A-Za-zΑ-ω0-9\)\]])\s*([\^_])\s*([0-9A-Za-z+\-=()])",
        replace_single,
        text,
    )
    return text


def replace_latex_symbols(text):
    text = str(text or "")

    def replacement(match):
        command = match.group(1)
        return LATEX_SYMBOL_MAP.get(command, match.group(0))

    return re.sub(r"\\([A-Za-z]+)\b", replacement, text)


def unwrap_latex_text_commands(text):
    text = str(text or "")
    command = r"(?:text|mathrm|mathbf|mathit|mathsf|operatorname)"
    for _ in range(4):
        updated = re.sub(
            rf"\\{command}\s*\{{\s*([^{{}}\n]{{1,240}})\s*\}}",
            r"\1",
            text,
        )
        if updated == text:
            break
        text = updated
    return text


def latex_grouped_number_to_superscript(match):
    base = match.group(1)
    value = re.sub(r"\s+", "", match.group(2))
    translated = translate_script_text(value, SUPERSCRIPT_MAP)
    if translated is None:
        return match.group(0)
    return base + translated


def normalize_generic_latex_fractions(text):
    def replacement(match):
        numerator = re.sub(r"\s+", " ", match.group(1).strip())
        denominator = re.sub(r"\s+", " ", match.group(2).strip())
        return f"({numerator})/({denominator})"

    return re.sub(
        r"\\(?:dfrac|tfrac|frac)\s*\{\s*([^{}\n]{1,100})\s*\}\s*\{\s*([^{}\n]{1,100})\s*\}",
        replacement,
        text,
    )


LATEX_FRACTION_COMMANDS = {"frac", "dfrac", "tfrac"}
LATEX_TEXT_COMMANDS = {"text", "mathrm", "mathbf", "mathit", "mathsf", "operatorname"}
LATEX_LAYOUT_COMMANDS = {
    "left", "right", "bigl", "bigr", "Bigl", "Bigr", "big", "Big",
    "displaystyle", "textstyle", "scriptstyle", "scriptscriptstyle",
}
LATEX_SPACE_COMMANDS = {",", ";", ":", "!", "quad", "qquad", "enspace", "hspace", "vspace"}
LATEX_OPERATOR_MAP = {
    "sum": "∑",
    "prod": "∏",
    "int": "∫",
    "iint": "∬",
    "iiint": "∭",
    "lim": "lim",
}
LATEX_VULGAR_FRACTION_MAP = {
    ("0", "3"): "↉", ("1", "2"): "½", ("1", "3"): "⅓", ("2", "3"): "⅔",
    ("1", "4"): "¼", ("3", "4"): "¾", ("1", "5"): "⅕", ("2", "5"): "⅖",
    ("3", "5"): "⅗", ("4", "5"): "⅘", ("1", "6"): "⅙", ("5", "6"): "⅚",
    ("1", "7"): "⅐", ("1", "8"): "⅛", ("3", "8"): "⅜", ("5", "8"): "⅝",
    ("7", "8"): "⅞", ("1", "9"): "⅑", ("1", "10"): "⅒",
}
LATEX_FALLBACK_COMMANDS = (
    LATEX_FRACTION_COMMANDS
    | LATEX_TEXT_COMMANDS
    | LATEX_LAYOUT_COMMANDS
    | LATEX_SPACE_COMMANDS
    | set(LATEX_SYMBOL_MAP)
    | set(LATEX_OPERATOR_MAP)
    | {"sqrt", "begin", "end"}
)


def read_latex_group(text, start):
    """Return (content, next_index, closed) for a possibly truncated {...} group."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "{" and (index == 0 or text[index - 1] != "\\"):
            depth += 1
        elif char == "}" and (index == 0 or text[index - 1] != "\\"):
            depth -= 1
            if depth == 0:
                return text[start + 1:index], index + 1, True
    return text[start + 1:], len(text), False


def skip_latex_space(text, start):
    while start < len(text) and text[start].isspace():
        start += 1
    return start


def linearize_latex_commands(text, depth=0):
    """Linearize LaTeX commands without requiring balanced OCR output."""
    text = str(text or "")
    if depth > 24:
        return re.sub(r"\\[A-Za-z]+\*?", "", text).replace("{", "").replace("}", "")

    output = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "{":
            group = read_latex_group(text, index)
            if group is None:
                index += 1
                continue
            content, next_index, _closed = group
            output.append(linearize_latex_commands(content, depth + 1))
            index = next_index
            continue
        if char == "}":
            index += 1
            continue
        if char != "\\":
            output.append(char)
            index += 1
            continue

        if index + 1 >= len(text):
            index += 1
            continue
        escaped = text[index + 1]
        if escaped in "{}$%#&_":
            output.append(escaped)
            index += 2
            continue
        if escaped == "\\":
            output.append(" ")
            index += 2
            continue
        if escaped in LATEX_SPACE_COMMANDS:
            output.append(" ")
            index += 2
            continue

        command_match = re.match(r"[A-Za-z]+\*?", text[index + 1:])
        if not command_match:
            index += 2
            continue
        raw_command = command_match.group(0)
        command = raw_command.rstrip("*")
        next_index = index + 1 + len(raw_command)
        argument_index = skip_latex_space(text, next_index)

        if command in LATEX_FRACTION_COMMANDS:
            numerator_group = read_latex_group(text, argument_index)
            if numerator_group is None:
                index = next_index
                continue
            numerator, after_numerator, numerator_closed = numerator_group
            numerator_text = linearize_latex_commands(numerator, depth + 1).strip()
            denominator_index = skip_latex_space(text, after_numerator)
            denominator_group = read_latex_group(text, denominator_index) if numerator_closed else None
            if denominator_group is None:
                # Truncated OCR such as "\\frac{400 - ..." has no denominator.
                # Preserve the recoverable payload, but never leak the command.
                output.append(numerator_text)
                index = after_numerator
                continue
            denominator, after_denominator, _denominator_closed = denominator_group
            denominator_text = linearize_latex_commands(denominator, depth + 1).strip()
            if numerator_text and denominator_text:
                output.append(
                    LATEX_VULGAR_FRACTION_MAP.get(
                        (numerator_text, denominator_text),
                        f"({numerator_text})/({denominator_text})",
                    )
                )
            else:
                output.append(numerator_text or denominator_text)
            index = after_denominator
            continue

        if command == "sqrt":
            group = read_latex_group(text, argument_index)
            if group is not None:
                content, after_group, _closed = group
                output.append(f"√({linearize_latex_commands(content, depth + 1).strip()})")
                index = after_group
                continue

        if command in LATEX_TEXT_COMMANDS:
            group = read_latex_group(text, argument_index)
            if group is not None:
                content, after_group, _closed = group
                output.append(linearize_latex_commands(content, depth + 1))
                index = after_group
                continue

        if command in {"begin", "end"}:
            group = read_latex_group(text, argument_index)
            index = group[1] if group is not None else next_index
            output.append(" ")
            continue
        if command in LATEX_SYMBOL_MAP:
            output.append(LATEX_SYMBOL_MAP[command])
        elif command in LATEX_OPERATOR_MAP:
            output.append(LATEX_OPERATOR_MAP[command])
        elif command in LATEX_SPACE_COMMANDS:
            output.append(" ")
        # Unknown and layout-only commands are intentionally discarded. Any
        # following braced payload is handled by the next loop iteration.
        index = next_index

    return "".join(output)


def strip_paired_latex_dollar_delimiters(text):
    text = str(text or "")

    def replacement(match):
        content = match.group(1) or match.group(2) or ""
        looks_like_math = bool(
            "\\" in content
            or re.search(r"[_^=+*/<>≤≥≠≈∑∏∫√]", content)
            or re.fullmatch(r"\s*[A-Za-zΑ-ω]\s*", content)
        )
        return content if looks_like_math else match.group(0)

    for _ in range(4):
        updated = re.sub(r"\$\$([^$\n]+?)\$\$|\$([^$\n]+?)\$", replacement, text)
        if updated == text:
            break
        text = updated
    return text


def contains_latex_fallback_command(text):
    for match in re.finditer(r"\\([A-Za-z]+)\*?", str(text or "")):
        if match.group(1) in LATEX_FALLBACK_COMMANDS:
            return True
    return False


def normalize_safe_latex_markup(text):
    text = str(text or "")
    should_linearize_commands = contains_latex_fallback_command(text)
    text = strip_paired_latex_dollar_delimiters(text)
    text = text.replace("\\(", "").replace("\\)", "").replace("\\[", "").replace("\\]", "")
    text = unwrap_latex_text_commands(text)
    text = latex_scripts_to_unicode(text)
    # Parse commands before the generic word{number}->superscript heuristic;
    # otherwise a valid command such as \\frac{900}{4006} is misread as the
    # word "frac" followed by a grouped exponent.
    if should_linearize_commands:
        text = linearize_latex_commands(text)
    text = re.sub(
        r"([A-Za-zΑ-ω]{2,})\s*\{\s*([0-9]{1,3})\s*\}",
        latex_grouped_number_to_superscript,
        text,
    )
    text = replace_latex_symbols(text)
    text = re.sub(r"\\(?:left|right|bigl|bigr|Bigl|Bigr|big|Big|displaystyle|textstyle)\b", "", text)
    text = normalize_fraction_markup(text)
    return text


def clean_latex_source(text):
    text = html_lib.unescape(str(text or ""))
    text = strip_control_chars(text)
    text = text.replace("\u00a0", " ")
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return strip_latex_math_delimiters(text)


def linearize_latex_formula(text):
    text = clean_latex_source(text)
    text = strip_paired_latex_dollar_delimiters(text)
    text = normalize_safe_latex_markup(text)
    text = linearize_latex_commands(text)
    text = re.sub(r"\\(?:left|right|bigl|bigr|Bigl|Bigr|big|Big|displaystyle|textstyle)\b", "", text)
    text = re.sub(r"\\(?:mathrm|mathbf|mathit|mathsf|text|operatorname)\s*\{\s*([^{}]{1,160})\s*\}", r"\1", text)
    text = re.sub(r"\\[A-Za-z]+\*?", "", text)
    text = text.replace("{", "").replace("}", "")
    text = text.replace("$", "")
    text = text.replace("~", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = normalize_unicode_preserving_greek_spacing(text)
    return strip_control_chars(text).strip()


def formula_source_text(block):
    return str(block.get("source_text") or block.get("text") or "")


FORMULA_STRUCTURAL_COMMAND_RE = re.compile(
    r"\\(?:frac|dfrac|tfrac|sqrt|sum|prod|int|iint|iiint|lim|begin|matrix|cases)\b"
)
FORMULA_OPERATOR_RE = re.compile(r"[=+*/<>≤≥≈≠∈∉∂√∞∑∏∫]|(?<!\w)-(?!\w)")
LATEX_CITATION_ONLY_RE = re.compile(
    r"^(?:\$\$?|\\\(|\\\[)?\s*\^\s*\{\s*[^{}]{1,8}\s*\}\s*(?:\$\$?|\\\)|\\\])?$"
)


def fully_delimited_math_body(text):
    text = str(text or "").strip()
    for start, end in (("$$", "$$"), ("\\[", "\\]"), ("\\(", "\\)"), ("$", "$")):
        if text.startswith(start) and text.endswith(end) and len(text) >= len(start) + len(end):
            return text[len(start):-len(end)].strip()
    return None


def formula_body_has_substantive_math(text):
    text = str(text or "").strip()
    if not text or LATEX_CITATION_ONLY_RE.fullmatch(text):
        return False
    if FORMULA_STRUCTURAL_COMMAND_RE.search(text):
        return True
    return bool(FORMULA_OPERATOR_RE.search(text) and re.search(r"[0-9A-Za-zΑ-ω]", text))


def is_formula_render_block(block):
    category = block.get("category")
    raw = formula_source_text(block).strip()
    if category == "Formula":
        return True
    if not raw or len(raw) > 180:
        return False
    if re.search(r"[\u4e00-\u9fff].{20,}", raw):
        return False

    delimited_body = fully_delimited_math_body(raw)
    if delimited_body is not None:
        return formula_body_has_substantive_math(delimited_body)

    # Recover a genuinely formula-like block that lost its outer delimiters,
    # but never promote prose merely because it contains an inline citation,
    # inline variable, or superscript edition marker.
    if not FORMULA_STRUCTURAL_COMMAND_RE.search(raw):
        return False
    prose_without_commands = re.sub(r"\\[A-Za-z]+\*?", " ", raw)
    prose_words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]{3,}", prose_without_commands)
    return len(prose_words) <= 1


def formula_to_unicode_if_simple(text):
    source = clean_latex_source(text)
    if not source or len(source) > 90:
        return None
    complex_commands = (
        "\\begin",
        "\\sum",
        "\\prod",
        "\\int",
        "\\lim",
        "\\matrix",
        "\\cases",
        "\\over",
        "\\underset",
        "\\overset",
    )
    if any(command in source for command in complex_commands):
        return None
    simplified = linearize_latex_formula(source)
    if not simplified:
        return None
    if re.search(r"[\\{}]", simplified):
        return None
    if len(simplified) > 110:
        return None
    return simplified


def latex_formula_document(formula):
    formula = clean_latex_source(formula)
    return "\n".join(
        [
            r"\documentclass[12pt]{article}",
            r"\usepackage[utf8]{inputenc}",
            r"\usepackage{amsmath,amssymb,bm}",
            r"\pagestyle{empty}",
            r"\begin{document}",
            r"\[",
            formula,
            r"\]",
            r"\end{document}",
            "",
        ]
    )


def render_latex_formula_to_svg(formula, cache_dir):
    deps = formula_render_dependencies()
    if not deps.get("latex") or not deps.get("dvisvgm") or not deps.get("svglib"):
        raise RuntimeError("formula vector dependencies missing")

    formula = clean_latex_source(formula)
    if not formula:
        raise RuntimeError("empty formula")

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(formula.encode("utf-8", errors="replace")).hexdigest()
    case_dir = cache_dir / digest
    svg_path = case_dir / "formula.svg"
    if svg_path.exists() and svg_path.stat().st_size > 0:
        return svg_path

    case_dir.mkdir(parents=True, exist_ok=True)
    tex_path = case_dir / "formula.tex"
    tex_path.write_text(latex_formula_document(formula), encoding="utf-8")

    latex_engine = deps["latex"]
    latex_cmd = [
        latex_engine,
        "-interaction=nonstopmode",
        "-halt-on-error",
        "formula.tex",
    ]
    latex_result = subprocess.run(
        latex_cmd,
        cwd=str(case_dir),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=45,
        check=False,
    )
    if latex_result.returncode != 0:
        tail = "\n".join((latex_result.stdout or "").splitlines()[-10:])
        raise RuntimeError(f"latex failed: {tail}")

    engine_name = Path(latex_engine).name.lower()
    if "pdflatex" in engine_name:
        formula_output = case_dir / "formula.pdf"
        dvisvgm_input_args = ["--pdf", str(formula_output)]
        if not formula_output.exists():
            raise RuntimeError("pdflatex did not create formula.pdf")
    else:
        formula_output = case_dir / "formula.dvi"
        dvisvgm_input_args = [str(formula_output)]
        if not formula_output.exists():
            raise RuntimeError("latex did not create formula.dvi")

    dvisvgm_cmd = [
        deps["dvisvgm"],
        "--no-fonts",
        "--exact",
        "--bbox=min",
        "-o",
        str(svg_path),
        *dvisvgm_input_args,
    ]
    dvisvgm_result = subprocess.run(
        dvisvgm_cmd,
        cwd=str(case_dir),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=45,
        check=False,
    )
    if dvisvgm_result.returncode != 0 or not svg_path.exists():
        tail = "\n".join((dvisvgm_result.stdout or "").splitlines()[-10:])
        raise RuntimeError(f"dvisvgm failed: {tail}")
    return svg_path


def draw_svg_in_rect(c, page_height, rect, svg_path):
    from reportlab.graphics import renderPDF
    from svglib.svglib import svg2rlg

    drawing = svg2rlg(str(svg_path))
    if drawing is None:
        return False
    width = float(getattr(drawing, "width", 0.0) or 0.0)
    height = float(getattr(drawing, "height", 0.0) or 0.0)
    if width <= 0 or height <= 0:
        return False

    scale = min(rect.width / width, rect.height / height)
    if scale <= 0:
        return False
    x = rect.x0 + (rect.width - width * scale) / 2
    y = page_height - rect.y1 + (rect.height - height * scale) / 2
    c.saveState()
    drawing.scale(scale, scale)
    renderPDF.draw(drawing, c, x, y)
    c.restoreState()
    return True


def reportlab_draw_hidden_plain_text(c, page_height, rect, text, fontsize=2.0):
    text = normalize_markdown_text(linearize_latex_formula(text))
    if not text:
        return
    fontsize = max(1.0, min(float(fontsize), max(1.0, rect.height * 0.45)))
    baseline = page_height - rect.y0 - fontsize
    cursor = rect.x0
    for seg in reportlab_plain_segments(text):
        seg_text = seg.get("text", "")
        if not seg_text:
            continue
        fontname, _fake_bold = reportlab_font_for_segment(seg, "normal")
        text_obj = c.beginText(cursor, baseline)
        text_obj.setFont(fontname, fontsize)
        text_obj.setTextRenderMode(3)
        text_obj.textOut(text_for_single_pdf_draw(seg_text))
        c.drawText(text_obj)
        cursor += reportlab_text_width(seg_text, fontname, fontsize)


def render_pdf_rect_to_png(pdf_path, page_index, rect, image_path, padding=FORMULA_CROP_PADDING, dpi=FORMULA_RENDER_DPI):
    image_path = Path(image_path)
    image_path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open(pdf_path) as doc:
        if page_index < 0 or page_index >= doc.page_count:
            raise RuntimeError(f"page index out of range: {page_index}")
        page = doc[page_index]
        clip = fitz.Rect(rect)
        clip.x0 = max(page.rect.x0, clip.x0 - padding)
        clip.y0 = max(page.rect.y0, clip.y0 - padding)
        clip.x1 = min(page.rect.x1, clip.x1 + padding)
        clip.y1 = min(page.rect.y1, clip.y1 + padding)
        matrix = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=matrix, clip=clip, alpha=False)
        pix.save(str(image_path))
    return image_path


def draw_formula_crop_fallback(c, page_height, rect, block):
    source_pdf = block.get("source_pdf_path")
    page_index = block.get("page_index")
    crop_dir = block.get("formula_crop_dir")
    if source_pdf is None or page_index is None or crop_dir is None:
        return False
    crop_path = Path(crop_dir) / f"page_{int(page_index) + 1:04d}_formula_{block.get('order', 0)}.png"
    render_pdf_rect_to_png(source_pdf, int(page_index), rect, crop_path)
    c.drawImage(
        str(crop_path),
        rect.x0,
        page_height - rect.y1,
        width=rect.width,
        height=rect.height,
        preserveAspectRatio=True,
        anchor="c",
        mask="auto",
    )
    reportlab_draw_hidden_plain_text(c, page_height, rect, formula_source_text(block))
    return True


def render_formula_block(c, page_height, rect, block, formula_work_dir, allow_image_fallback=True):
    source = formula_source_text(block)
    simple = formula_to_unicode_if_simple(source)
    if simple:
        fontsize = float(block.get("font_size", max(5.0, rect.height * 0.55)))
        min_size = float(block.get("min_font_size", max(3.2, fontsize * 0.72)))
        line_height = float(block.get("line_height", 1.0))
        fontsize, line_height, ok = reportlab_fit_box_params(
            page_height,
            rect,
            simple,
            fontsize,
            min_size,
            line_height,
            block_align(block),
        )
        if ok and reportlab_draw_markdown_textbox(c, page_height, rect, simple, fontsize, line_height, block_align(block)):
            block["formula_render_mode"] = "unicode_text"
            return True

    cache_dir = block.get("formula_cache_dir") or formula_work_dir
    try:
        svg_path = render_latex_formula_to_svg(source, cache_dir)
        if draw_svg_in_rect(c, page_height, rect, svg_path):
            reportlab_draw_hidden_plain_text(c, page_height, rect, source)
            block["formula_render_mode"] = "latex_svg"
            return True
    except Exception as exc:
        block["formula_render_error"] = str(exc)

    linear = linearize_latex_formula(source)
    if linear:
        fontsize = float(block.get("font_size", max(5.0, rect.height * 0.55)))
        min_size = float(block.get("min_font_size", max(3.2, fontsize * 0.65)))
        line_height = float(block.get("line_height", 1.0))
        fontsize, line_height, ok = reportlab_fit_box_params(
            page_height,
            rect,
            linear,
            fontsize,
            min_size,
            line_height,
            block_align(block),
        )
        if ok and reportlab_draw_markdown_textbox(c, page_height, rect, linear, fontsize, line_height, block_align(block)):
            block["formula_render_mode"] = "linear_text"
            return True

    if allow_image_fallback:
        try:
            if draw_formula_crop_fallback(c, page_height, rect, block):
                block["formula_render_mode"] = "image_crop"
                return True
        except Exception as exc:
            previous = block.get("formula_render_error")
            block["formula_render_error"] = f"{previous}; crop fallback failed: {exc}" if previous else str(exc)
    return False


def text_for_single_pdf_draw(text):
    return strip_control_chars(text).replace("\n", " ")


def normalize_ocr_markup_text(text):
    text = html_table_to_text(text)
    text = html_lib.unescape(text)
    text = normalize_safe_latex_markup(text)
    text = normalize_citation_superscripts(text)
    text = normalize_safe_latex_markup(text)
    text = strip_control_chars(text)
    text = normalize_long_dot_leaders(text)
    return collapse_repeated_ocr_text(text)


def markdown_table_cells(line):
    stripped = line.strip()
    if "|" not in stripped:
        return None
    if re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?", stripped):
        return []
    if stripped.startswith("|") or stripped.endswith("|") or stripped.count("|") >= 1:
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        cells = [cell for cell in cells if cell]
        if len(cells) >= 2:
            return cells
    return None


def source_text_from_cell(cell):
    for key in ("text", "content", "html", "latex"):
        value = cell.get(key)
        if value:
            return str(value)
    return ""


def extract_html_table_rows(text):
    if not re.search(r"(?is)<\s*(table|tr|td|th)\b", text or ""):
        return []

    rows = []
    for row_match in re.finditer(r"(?is)<tr\b[^>]*>(.*?)</tr\s*>", text):
        row_html = row_match.group(1)
        cells = []
        for cell_match in re.finditer(r"(?is)<t[dh]\b[^>]*>(.*?)</t[dh]\s*>", row_html):
            cell = strip_html_tags(cell_match.group(1))
            cell = re.sub(r"[ \t\r\f\v]+", " ", cell).strip()
            cells.append(normalize_text(cell) if cell else "")
        if not cells:
            continue
        nonempty = [cell for cell in cells if cell]
        if not nonempty:
            continue
        first_col = next((idx for idx, cell in enumerate(cells) if cell), 0)
        rows.append(
            {
                "cells": cells,
                "nonempty_cells": nonempty,
                "first_col": first_col,
                "col_count": len(cells),
                "source": "html",
            }
        )
    return rows


def extract_plain_table_rows(text):
    text = normalize_markdown_text(text)
    rows = []
    for line in re.split(r"\n+", text):
        line = line.strip()
        if not line:
            continue
        cells = markdown_table_cells(line)
        if cells == []:
            continue
        if cells is None:
            cells = [line]
        cells = [normalize_text(cell) for cell in cells]
        nonempty = [cell for cell in cells if cell]
        if not nonempty:
            continue
        first_col = next((idx for idx, cell in enumerate(cells) if cell), 0)
        rows.append(
            {
                "cells": cells,
                "nonempty_cells": nonempty,
                "first_col": first_col,
                "col_count": len(cells),
                "source": "plain",
            }
        )
    return rows


def extract_table_rows(text):
    return extract_html_table_rows(text) or extract_plain_table_rows(text)


def table_rows_from_text(text):
    return [row["nonempty_cells"] for row in extract_table_rows(text)]


def table_rows_for_block(block_or_text):
    if isinstance(block_or_text, dict):
        rows = block_or_text.get("table_rows") or extract_table_rows(block_or_text.get("source_text") or block_or_text.get("text", ""))
        return rows
    return extract_table_rows(block_or_text)


def table_row_parts(row):
    cells = row.get("nonempty_cells", row) if isinstance(row, dict) else row
    if len(cells) >= 2:
        last = cells[-1].strip()
        if re.fullmatch(r"\[?\d{1,4}\]?|[ivxlcdmIVXLCDM]{1,8}", last) or len(last) <= 8:
            return "  ".join(cells[:-1]).strip(), last
    return " | ".join(cells).strip(), ""


def table_row_level(row, left_text):
    if isinstance(row, dict) and row.get("source") == "html" and row.get("first_col", 0) > 0:
        return min(3, int(row["first_col"]))

    text = left_text.strip()
    if re.match(r"^[a-z]\.", text):
        return 2
    if re.match(r"^(?:[IVX]+)\.", text):
        return 1
    if re.match(r"^[A-Z]\.", text):
        return 0
    if re.match(r"^第[一二三四五六七八九十百0-9]+[章节部分]", text):
        return 0
    return 0


def is_page_number_like(text):
    return bool(re.fullmatch(r"\[?\d{1,4}\]?|[ivxlcdmIVXLCDM]{1,8}", str(text or "").strip()))


def is_toc_table(rows):
    if len(rows) < 2:
        return False
    page_like = 0
    usable = 0
    for row in rows:
        cells = row.get("nonempty_cells", [])
        if len(cells) < 2:
            continue
        usable += 1
        if is_page_number_like(cells[-1]):
            page_like += 1
    return usable > 0 and page_like / usable >= 0.60


def normalize_markdown_text(text):
    if text is None:
        return ""
    text = str(text)
    text = normalize_ocr_markup_text(text)
    text = normalize_unicode_preserving_greek_spacing(text)
    text = strip_control_chars(text)
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return strip_control_chars(text).strip()


def normalize_text(text):
    if text is None:
        return ""
    text = str(text)
    text = normalize_ocr_markup_text(text)
    text = normalize_unicode_preserving_greek_spacing(text)
    text = strip_control_chars(text)
    text = text.replace("\u00a0", " ")
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}$", "", text)
    text = strip_markdown_inline(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return strip_control_chars(text).strip()


def is_font_test_text(text):
    return bool(FONT_TEST_TEXT_RE.fullmatch(normalize_text(text or "")))


def normalize_draw_segment_text(text, strip_markdown=True):
    if text is None:
        return ""
    text = str(text)
    text = normalize_ocr_markup_text(text)
    text = normalize_unicode_preserving_greek_spacing(text)
    text = strip_control_chars(text)
    text = text.replace("\u00a0", " ")
    if strip_markdown:
        text = strip_markdown_inline(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return strip_control_chars(text)


def assert_no_radicals_in_text(text, context):
    chars = sorted(set(RADICAL_CHAR_RE.findall(text or "")))
    if chars:
        details = " ".join(f"{ch}(U+{ord(ch):04X})" for ch in chars)
        raise RuntimeError(f"Radical characters remain before PDF rendering in {context}: {details}")


def normalize_category(category):
    category = str(category or "Text").strip()
    return category or "Text"


def get_text_from_cell(cell):
    return normalize_markdown_text(source_text_from_cell(cell))


def repair_single_incomplete_layout_json(text):
    stripped = str(text or "").strip()
    if not stripped.startswith("[{"):
        return None

    bbox_match = re.search(r'"bbox"\s*:\s*\[([^\]]+)\]', stripped)
    category_match = re.search(r'"category"\s*:\s*"([^"]+)"', stripped)
    text_match = re.search(r'"text"\s*:\s*"', stripped)
    if not bbox_match or not category_match or not text_match:
        return None

    bbox = []
    for item in bbox_match.group(1).split(","):
        item = item.strip()
        try:
            bbox.append(int(float(item)))
        except ValueError:
            return None
    if len(bbox) != 4:
        return None

    content = stripped[text_match.end():]
    content = content.rstrip()
    while content.endswith("\\"):
        content = content[:-1]
    content = content.replace('\\"', '"').replace("\\\\", "\\")
    content = normalize_markdown_text(content)
    if not content:
        return None

    return [
        {
            "bbox": bbox,
            "category": category_match.group(1),
            "text": content,
            "__repaired_incomplete": True,
        }
    ]


def read_json(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    for _ in range(2):
        if not isinstance(data, str):
            break
        stripped = data.strip()
        if not stripped or stripped[0] not in "[{":
            break
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            repaired = repair_single_incomplete_layout_json(stripped)
            if repaired is None:
                raise
            return repaired
    return data


def load_jsonl(path):
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def markdown_to_text(path):
    if not path or not Path(path).exists():
        return ""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    text = normalize_markdown_text(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def collect_cell_texts(cells):
    texts = []
    for cell in cells:
        if isinstance(cell, dict):
            text = get_text_from_cell(cell).strip()
            if text:
                texts.append(text)
    return texts


def repeated_pseudotext_metrics(text):
    raw = strip_control_chars(str(text or ""))
    raw_len = len(raw)
    collapsed = collapse_repeated_ocr_text(raw)
    tokens = re.findall(r"\S+", raw)
    lowered = [token.lower() for token in tokens]
    coord_tokens = sum(
        1
        for token in lowered
        if re.fullmatch(r"[xy]\s*=?-?\d+(?:\.\d+)?", token)
    )
    hash_markers = sum(1 for token in lowered if token.strip() == "###")
    compression_ratio = len(collapsed) / max(raw_len, 1)
    return {
        "raw_len": raw_len,
        "collapsed_len": len(collapsed),
        "compression_ratio": compression_ratio,
        "coord_tokens": coord_tokens,
        "hash_markers": hash_markers,
        "token_count": len(tokens),
    }


def is_high_repeated_pseudotext(text):
    metrics = repeated_pseudotext_metrics(text)
    if metrics["raw_len"] < PSEUDOTEXT_MIN_RAW_CHARS:
        return False
    if metrics["coord_tokens"] >= PSEUDOTEXT_MIN_COORD_TOKENS:
        return True
    if metrics["hash_markers"] >= PSEUDOTEXT_MIN_HASH_MARKERS:
        return True
    if (
        metrics["compression_ratio"] <= PSEUDOTEXT_MAX_COMPRESSION_RATIO
        and looks_like_repeated_token_artifact(text)
    ):
        return True
    return False


def page_result_pseudotext_report(result):
    offenders = []
    if not result:
        return offenders

    for cell_index, cell in enumerate(result.get("cells") or []):
        if not isinstance(cell, dict):
            continue
        raw = source_text_from_cell(cell)
        if is_high_repeated_pseudotext(raw):
            metrics = repeated_pseudotext_metrics(raw)
            metrics["source"] = f"cell:{cell_index}"
            metrics["category"] = normalize_category(cell.get("category"))
            offenders.append(metrics)

    for key in ("fallback_text", "md_nohf_text"):
        raw = result.get(key)
        if raw and is_high_repeated_pseudotext(raw):
            metrics = repeated_pseudotext_metrics(raw)
            metrics["source"] = key
            metrics["category"] = "fallback"
            offenders.append(metrics)

    return offenders


def page_result_has_high_repeated_pseudotext(result):
    return bool(page_result_pseudotext_report(result))


def layout_retry_reasons(row, cells, fallback_text):
    reasons = []
    if row.get("filtered"):
        reasons.append("OCR parser filtered/cleaned invalid JSON")
    if fallback_text and not cells:
        reasons.append("using Markdown fallback instead of layout JSON")

    joined_text = "\n".join(collect_cell_texts(cells))
    for phrase in SUSPICIOUS_LAYOUT_PHRASES:
        if phrase in joined_text:
            reasons.append(f"suspicious hallucinated placeholder text: {phrase}")

    for cell in cells:
        if not isinstance(cell, dict):
            continue
        if has_dot_leader_explosion(source_text_from_cell(cell)):
            reasons.append(DOT_LEADER_RETRY_REASON)
            break

    if any(is_high_repeated_pseudotext(source_text_from_cell(cell)) for cell in cells if isinstance(cell, dict)):
        reasons.append(PSEUDOTEXT_RETRY_REASON)

    if fallback_text and has_dot_leader_explosion(fallback_text):
        reasons.append(DOT_LEADER_RETRY_REASON)

    if fallback_text and is_high_repeated_pseudotext(fallback_text):
        reasons.append(PSEUDOTEXT_RETRY_REASON)

    return reasons

MINERU_CATEGORY_MAP = {
    "caption": "Caption",
    "figure_caption": "Caption",
    "footnote": "Footnote",
    "page_footnote": "Footnote",
    "page-footnote": "Footnote",
    "formula": "Formula",
    "equation": "Formula",
    "interline_equation": "Formula",
    "isolate_formula": "Formula",
    "list": "List-item",
    "list-item": "List-item",
    "list_item": "List-item",
    "page-footer": "Page-footer",
    "page_footer": "Page-footer",
    "footer": "Page-footer",
    "page-header": "Page-header",
    "page_header": "Page-header",
    "header": "Page-header",
    "image": "Picture",
    "figure": "Picture",
    "picture": "Picture",
    "section-header": "Section-header",
    "section_header": "Section-header",
    "section title": "Section-header",
    "section_title": "Section-header",
    "table": "Table",
    "text": "Text",
    "plain_text": "Text",
    "paragraph": "Text",
    "page_number": "Page-footer",
    "paragraph_title": "Section-header",
    "title": "Title",
}

MINERU_JSON_PREFERENCE = {
    "middle": 0,
    "model": 1,
    "layout": 2,
    "content": 3,
}


def mineru_json_priority(path):
    name = path.name.lower()
    for token, priority in MINERU_JSON_PREFERENCE.items():
        if token in name:
            return priority
    return 10


def select_mineru_json_files(json_files):
    if not json_files:
        return []

    middle = [path for path in json_files if path.name.endswith("_middle.json")]
    if middle:
        return sorted(middle)

    content_list = [
        path for path in json_files
        if path.name.endswith("_content_list.json") and "_content_list_v2" not in path.name
    ]
    if content_list:
        return sorted(content_list)

    model = [path for path in json_files if path.name.endswith("_model.json")]
    if model:
        return sorted(model)

    return [sorted(json_files, key=lambda path: (mineru_json_priority(path), str(path)))[0]]


def mineru_result_branch_dir(primary_files, output_dir):
    if not primary_files:
        return output_dir
    return primary_files[0].parent


def files_in_mineru_result_branch(files, branch_dir):
    return sorted(path for path in files if path.parent == branch_dir)


def find_mineru_debug_pdf(branch_dir, kind):
    suffixes = [f"_{kind}.pdf"]
    if kind == "span":
        suffixes.append("_spans.pdf")
    matches = []
    for suffix in suffixes:
        matches.extend(sorted(path for path in branch_dir.glob(f"*{suffix}")))
    if matches:
        return sorted(matches)[0]
    for suffix in suffixes:
        matches.extend(sorted(path for path in branch_dir.rglob(f"*{suffix}")))
    return matches[0] if matches else None


def current_mineru_result_files(output_dir):
    all_json_files = sorted(output_dir.rglob("*.json"), key=lambda path: (mineru_json_priority(path), str(path)))
    json_files = select_mineru_json_files(all_json_files)
    if not json_files:
        return [], [], output_dir
    branch_dir = mineru_result_branch_dir(json_files, output_dir)
    branch_json_files = files_in_mineru_result_branch(all_json_files, branch_dir)
    branch_primary_files = files_in_mineru_result_branch(json_files, branch_dir)
    return branch_json_files, branch_primary_files, branch_dir


def select_mineru_supplement_json_files(json_files, primary_files):
    # Keep PDF reconstruction layout-driven. Supplemental hole filling only
    # reads MinerU middle.json preproc blocks, never flat content/model JSON.
    return sorted(path for path in json_files if path.name.endswith("_middle.json"))


def normalize_mineru_category(value):
    raw = str(value or "Text").strip()
    key = raw.lower().replace(" ", "_")
    return MINERU_CATEGORY_MAP.get(key, MINERU_CATEGORY_MAP.get(raw.lower(), raw or "Text"))


def mineru_page_index_from_value(key, value, page_hint=None):
    if value is None:
        return page_hint
    try:
        number = int(value)
    except (TypeError, ValueError):
        return page_hint
    if key in {"page_idx", "page_index", "page_id"}:
        return max(0, number)
    if key in {"page", "page_no", "page_number"}:
        return max(0, number - 1) if number > 0 else 0
    return page_hint


def extract_mineru_page_index(obj, page_hint=None):
    if not isinstance(obj, dict):
        return page_hint
    for key in ("page_idx", "page_index", "page_id", "page_no", "page_number", "page"):
        if key in obj:
            return mineru_page_index_from_value(key, obj.get(key), page_hint)
    return page_hint


def normalize_mineru_bbox(value):
    if isinstance(value, dict):
        keys = ("x0", "y0", "x1", "y1")
        if all(key in value for key in keys):
            value = [value[key] for key in keys]
        else:
            keys = ("left", "top", "right", "bottom")
            if all(key in value for key in keys):
                value = [value[key] for key in keys]
    if not isinstance(value, (list, tuple)):
        return None
    if len(value) == 4 and all(isinstance(item, (int, float)) for item in value):
        x0, y0, x1, y1 = [float(item) for item in value]
        if x1 > x0 and y1 > y0:
            return [x0, y0, x1, y1]
    if len(value) >= 4 and all(isinstance(item, (list, tuple)) and len(item) >= 2 for item in value[:4]):
        xs = [float(point[0]) for point in value]
        ys = [float(point[1]) for point in value]
        return [min(xs), min(ys), max(xs), max(ys)]
    return None


def extract_mineru_bbox(obj):
    if not isinstance(obj, dict):
        return None
    for key in ("bbox", "block_bbox", "layout_bbox", "poly", "polygon"):
        bbox = normalize_mineru_bbox(obj.get(key))
        if bbox:
            return bbox
    return None


def extract_mineru_text(obj):
    if isinstance(obj, str):
        return obj
    if isinstance(obj, list):
        parts = [extract_mineru_text(item) for item in obj]
        return "\n".join(part for part in parts if part.strip())
    if not isinstance(obj, dict):
        return ""

    for key in ("text", "content", "markdown", "html", "latex"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value

    parts = []
    for key in ("spans", "lines", "cells", "children", "blocks"):
        value = obj.get(key)
        if isinstance(value, list):
            text = extract_mineru_text(value)
            if text.strip():
                parts.append(text)
    return "\n".join(parts)


def bbox_overlap_ratio(source_bbox, target_bbox):
    source = normalize_mineru_bbox(source_bbox)
    target = normalize_mineru_bbox(target_bbox)
    if not source or not target:
        return 0.0
    area = bbox_area(source)
    if area <= 0:
        return 0.0
    return bbox_intersection_area(source, target) / area


def mineru_span_text(span):
    if not isinstance(span, dict):
        return ""
    for key in ("content", "text", "markdown", "latex"):
        value = span.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def mineru_span_type(span):
    if not isinstance(span, dict):
        return ""
    return str(span.get("type") or span.get("category") or span.get("span_type") or "").lower()


def normalize_mineru_span(span):
    text = mineru_span_text(span)
    if not text:
        return None
    span_type = mineru_span_type(span)
    style = "math" if "formula" in span_type or "equation" in span_type else "normal"
    return {
        "text": text,
        "bbox": extract_mineru_bbox(span),
        "type": span_type,
        "style": style,
    }


def is_cjk_char(ch):
    return bool(re.match(r"[\u3400-\u9fff\u3000-\u303f\uff00-\uffef]", ch))


def is_latin_greek_digit_char(ch):
    return bool(re.match(r"[A-Za-z0-9\u00C0-\u024F\u1E00-\u1EFF\u0370-\u03FF\u1F00-\u1FFF\u0400-\u052F\u2DE0-\u2DFF\uA640-\uA69F]", ch))


def text_needs_gap(left_text, right_text):
    if not left_text or not right_text:
        return False
    left = left_text[-1]
    right = right_text[0]
    if left.isspace() or right.isspace():
        return False
    if is_cjk_char(left) or is_cjk_char(right):
        return False
    return is_latin_greek_digit_char(left) or is_latin_greek_digit_char(right)


def join_mineru_spans(spans):
    if not spans:
        return ""

    parts = []
    previous = None
    for span in spans:
        text = span.get("text", "")
        if not text:
            continue

        if previous and text_needs_gap(previous.get("text", ""), text):
            previous_bbox = previous.get("bbox")
            current_bbox = span.get("bbox")
            if previous_bbox and current_bbox:
                gap = float(current_bbox[0]) - float(previous_bbox[2])
                height = max(1.0, float(previous_bbox[3]) - float(previous_bbox[1]))
                if gap > height * 0.20:
                    parts.append(" ")

        parts.append(text)
        previous = span
    return "".join(parts)


def mineru_line_text(line):
    if not isinstance(line, dict):
        return ""
    raw_spans = line.get("spans")
    if isinstance(raw_spans, list) and raw_spans:
        spans = [normalize_mineru_span(span) for span in raw_spans]
        spans = [span for span in spans if span]
        text = join_mineru_spans(spans)
        if text.strip():
            return text
    return str(line.get("content") or line.get("text") or "")


def mineru_lines_inside_block(obj):
    block_bbox = extract_mineru_bbox(obj)
    lines = obj.get("lines") if isinstance(obj, dict) else None
    if not block_bbox or not isinstance(lines, list):
        return []
    kept = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        line_bbox = extract_mineru_bbox(line)
        if line_bbox and bbox_overlap_ratio(line_bbox, block_bbox) < 0.50:
            continue
        raw_spans = line.get("spans") if isinstance(line.get("spans"), list) else []
        spans = [normalize_mineru_span(span) for span in raw_spans]
        spans = [span for span in spans if span]
        text = join_mineru_spans(spans) or mineru_line_text(line)
        if not text.strip():
            continue
        kept.append(
            {
                "bbox": line_bbox,
                "text": text,
                "spans": spans,
            }
        )
    return kept


def mineru_block_text(obj):
    if not isinstance(obj, dict):
        return extract_mineru_text(obj)
    for key in ("text", "content", "markdown", "html", "latex"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value
    lines = mineru_lines_inside_block(obj)
    if lines:
        return "\n".join(line["text"] for line in lines if line["text"].strip())
    return extract_mineru_text(obj)


def mineru_nested_table_html(obj):
    if not isinstance(obj, dict):
        return ""
    direct_html = obj.get("html")
    if isinstance(direct_html, str) and direct_html.strip():
        return direct_html.strip()

    fragments = []
    for block in obj.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        for line in block.get("lines") or []:
            if not isinstance(line, dict):
                continue
            for span in line.get("spans") or []:
                if not isinstance(span, dict):
                    continue
                html = span.get("html")
                if isinstance(html, str) and html.strip():
                    fragments.append(html.strip())
    return "\n".join(fragments)


def mineru_raw_block_category(obj):
    if not isinstance(obj, dict):
        return None
    return (
        obj.get("category")
        or obj.get("type")
        or obj.get("block_type")
        or obj.get("layout_type")
        or obj.get("label")
    )


def looks_like_mineru_block(obj):
    if not isinstance(obj, dict):
        return False
    text = mineru_block_text(obj).strip()
    if normalize_mineru_category(mineru_raw_block_category(obj)) == "Table":
        text = mineru_nested_table_html(obj) or text
    return extract_mineru_bbox(obj) is not None and bool(text)


def mineru_bbox_units(obj, bbox, source_path):
    name = source_path.name.lower()
    if "content_list" in name:
        return "mineru_1000"
    if "model" in name and bbox and max(abs(float(v)) for v in bbox) <= 1.5:
        return "relative"
    if "middle" in name:
        return "pdf"
    return None


def mineru_block_to_cell(obj, page_index, source_path, page_size=None):
    bbox = extract_mineru_bbox(obj)
    raw_category = mineru_raw_block_category(obj)
    category = normalize_mineru_category(raw_category)
    text = mineru_block_text(obj)
    table_html = mineru_nested_table_html(obj) if category == "Table" else ""
    if table_html:
        text = table_html
    if not bbox or not text.strip():
        return None
    if obj.get("text_level") is not None and category == "Text":
        category = "Section-header"
    cell = {
        "bbox": bbox,
        "category": category,
        "text": text,
        "source": "mineru",
        "source_json": str(source_path),
        "__bbox_units": mineru_bbox_units(obj, bbox, source_path),
        "__line_info": mineru_lines_inside_block(obj),
        "__block_type": raw_category,
    }
    if page_size and len(page_size) == 2:
        cell["__page_size"] = [float(page_size[0]), float(page_size[1])]
    if category == "Table":
        if table_html:
            # Keep the structured source ahead of the flattened fallback text
            # so cell_to_block() can reconstruct rows and columns.
            cell["text"] = table_html
            cell["html"] = table_html
        elif obj.get("latex"):
            cell["latex"] = str(obj.get("latex"))
    return page_index, cell


def merge_mineru_page_level_blocks(obj, source_path, page_index, page_size, include_preproc):
    found = []

    para_blocks = obj.get("para_blocks")
    if isinstance(para_blocks, list):
        found.extend(walk_mineru_json(para_blocks, source_path, page_index, page_size, include_preproc))

    discarded_blocks = obj.get("discarded_blocks")
    if isinstance(discarded_blocks, list):
        for item in discarded_blocks:
            if not isinstance(item, dict):
                continue
            category = normalize_mineru_category(item.get("type") or item.get("category"))
            if category in {"Footnote", "Page-header", "Page-footer"}:
                found.extend(walk_mineru_json(item, source_path, page_index, page_size, include_preproc))

    preproc_blocks = obj.get("preproc_blocks")
    if isinstance(preproc_blocks, list):
        preproc_cells = walk_mineru_json(preproc_blocks, source_path, page_index, page_size, True)
        existing_cells = [cell for _, cell in found]
        added_preproc = 0
        for preproc_page_index, preproc_cell in preproc_cells:
            if cell_region_covered(preproc_cell, existing_cells):
                continue
            preproc_cell["source"] = "mineru-preproc-hole-fill"
            found.append((preproc_page_index, preproc_cell))
            existing_cells.append(preproc_cell)
            added_preproc += 1
        if added_preproc:
            log(f"    MinerU middle preproc hole-fill blocks added on page {page_index + 1}: {added_preproc}")

    return found


def walk_mineru_json(obj, source_path, page_hint=None, page_size_hint=None, include_preproc=False):
    found = []
    if isinstance(obj, list):
        if obj and all(isinstance(item, list) for item in obj):
            for index, item in enumerate(obj):
                found.extend(walk_mineru_json(item, source_path, index, page_size_hint, include_preproc))
            return found
        for index, item in enumerate(obj):
            found.extend(walk_mineru_json(item, source_path, page_hint, page_size_hint, include_preproc))
        return found
    if not isinstance(obj, dict):
        return found

    page_index = extract_mineru_page_index(obj, page_hint)
    page_size = obj.get("page_size") or page_size_hint
    if looks_like_mineru_block(obj):
        item = mineru_block_to_cell(obj, page_index if page_index is not None else 0, source_path, page_size)
        if item:
            found.append(item)
        return found

    if (
        isinstance(obj.get("para_blocks"), list)
        or isinstance(obj.get("preproc_blocks"), list)
        or isinstance(obj.get("discarded_blocks"), list)
    ):
        return merge_mineru_page_level_blocks(
            obj,
            source_path,
            page_index if page_index is not None else 0,
            page_size,
            include_preproc,
        )

    for key, value in obj.items():
        if key == "pdf_info" and isinstance(value, list):
            for page_index, page_obj in enumerate(value):
                found.extend(walk_mineru_json(page_obj, source_path, page_index, page_obj.get("page_size"), include_preproc))
            continue
        if key == "para_blocks" and isinstance(value, list):
            found.extend(walk_mineru_json(value, source_path, page_index, page_size, include_preproc))
            continue
        if key == "discarded_blocks" and isinstance(value, list):
            for item in value:
                if not isinstance(item, dict):
                    continue
                category = normalize_mineru_category(item.get("type") or item.get("category"))
                if category in {"Footnote", "Page-header", "Page-footer"}:
                    found.extend(walk_mineru_json(item, source_path, page_index, page_size, include_preproc))
            continue
        if key == "preproc_blocks" and isinstance(value, list) and (include_preproc or not obj.get("para_blocks")):
            found.extend(walk_mineru_json(value, source_path, page_index, page_size, include_preproc))
            continue
        if key in {
            "layout_dets",
            "blocks",
            "layouts",
            "cells",
            "tables",
            "children",
        }:
            found.extend(walk_mineru_json(value, source_path, page_index, page_size, include_preproc))
    return found


def cell_relative_bbox(cell):
    bbox = cell.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        return None
    x0, y0, x1, y1 = [float(v) for v in bbox]
    units = cell.get("__bbox_units")
    if units == "relative":
        return [x0, y0, x1, y1]
    if units == "mineru_1000":
        return [x0 / 1000.0, y0 / 1000.0, x1 / 1000.0, y1 / 1000.0]
    if units == "pdf":
        page_size = cell.get("__page_size") or []
        if len(page_size) == 2 and page_size[0] > 0 and page_size[1] > 0:
            return [x0 / page_size[0], y0 / page_size[1], x1 / page_size[0], y1 / page_size[1]]
    max_coord = max(abs(x0), abs(y0), abs(x1), abs(y1))
    if max_coord <= 1.5:
        return [x0, y0, x1, y1]
    if max_coord <= 1200:
        return [x0 / 1000.0, y0 / 1000.0, x1 / 1000.0, y1 / 1000.0]
    return None


def bbox_intersection_area(a, b):
    left = max(a[0], b[0])
    top = max(a[1], b[1])
    right = min(a[2], b[2])
    bottom = min(a[3], b[3])
    return max(0.0, right - left) * max(0.0, bottom - top)


def bbox_area(bbox):
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def cell_region_covered(candidate, existing_cells):
    cand_bbox = cell_relative_bbox(candidate)
    if not cand_bbox:
        return False
    cand_area = bbox_area(cand_bbox)
    if cand_area <= 0:
        return False
    cand_text = normalize_text(source_text_from_cell(candidate))
    for cell in existing_cells:
        if not normalize_text(source_text_from_cell(cell)):
            continue
        cell_bbox = cell_relative_bbox(cell)
        if not cell_bbox:
            continue
        intersection = bbox_intersection_area(cand_bbox, cell_bbox)
        if intersection / cand_area >= 0.45:
            return True
        existing_text = normalize_text(source_text_from_cell(cell))
        if (
            cand_text
            and existing_text
            and len(cand_text) >= 30
            and len(existing_text) >= 30
            and not is_numeric_page_marker(cand_text)
            and not is_numeric_page_marker(existing_text)
            and (cand_text in existing_text or existing_text in cand_text)
        ):
            return True
    return False


def supplement_mineru_holes(pages, all_json_files, primary_files):
    added = 0
    for json_path in select_mineru_supplement_json_files(all_json_files, primary_files):
        try:
            data = read_json(json_path)
        except Exception as exc:
            log(f"    Warning: could not read MinerU supplement JSON {json_path}: {exc}")
            continue
        for page_index, cell in walk_mineru_json(data, json_path, include_preproc=True):
            text = normalize_text(source_text_from_cell(cell))
            if not text:
                continue
            page = pages.setdefault(
                page_index,
                {
                    "cells": [],
                    "fallback_text": "",
                    "filtered": False,
                    "needs_retry": False,
                    "retry_reason": "",
                    "image_size": None,
                    "json_path": json_path,
                    "image_path": None,
                    "md_nohf_text": "",
                    "md_nohf_path": None,
                },
            )
            if cell_region_covered(cell, page["cells"]):
                continue
            cell["source"] = "mineru-supplement"
            page["cells"].append(cell)
            added += 1
    if added:
        log(f"    MinerU supplemental hole-fill blocks added: {added}")


def find_mineru_markdown_files(output_dir):
    files = sorted(output_dir.rglob("*.md"))
    nohf = [path for path in files if "nohf" in path.name.lower()]
    return nohf or files


def page_index_from_filename(path):
    name = path.stem.lower()
    match = re.search(r"(?:page|p)[_-]?(\d+)", name)
    if not match:
        return None
    number = int(match.group(1))
    return max(0, number - 1)


def find_mineru_page_image(output_dir, page_index):
    patterns = [
        f"*page*{page_index + 1}*.jpg",
        f"*page*{page_index + 1}*.png",
        f"*_{page_index + 1}.jpg",
        f"*_{page_index + 1}.png",
    ]
    for pattern in patterns:
        matches = sorted(output_dir.rglob(pattern))
        if matches:
            return matches[0]
    return None


def collect_page_results(pdf_path, output_dir):
    all_json_files, json_files, branch_dir = current_mineru_result_files(output_dir)
    if not json_files:
        raise RuntimeError(f"No MinerU JSON result found in: {output_dir}")

    pages = {}
    seen = set()
    for json_path in json_files:
        try:
            data = read_json(json_path)
        except Exception as exc:
            log(f"    Warning: could not read MinerU JSON {json_path}: {exc}")
            continue

        for page_index, cell in walk_mineru_json(data, json_path):
            text_key = normalize_text(source_text_from_cell(cell))
            if not text_key:
                continue
            bbox_key = tuple(round(float(v), 2) for v in cell.get("bbox", []))
            dedupe_key = (page_index, cell.get("category"), bbox_key, text_key[:300])
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            page = pages.setdefault(
                page_index,
                {
                    "cells": [],
                    "fallback_text": "",
                    "filtered": False,
                    "needs_retry": False,
                    "retry_reason": "",
                    "image_size": None,
                    "json_path": json_path,
                    "image_path": None,
                    "md_nohf_text": "",
                    "md_nohf_path": None,
                },
            )
            page["cells"].append(cell)
            page["json_path"] = json_path

    supplement_mineru_holes(pages, all_json_files, json_files)

    for md_path in find_mineru_markdown_files(branch_dir):
        page_index = page_index_from_filename(md_path)
        if page_index is None:
            continue
        page = pages.setdefault(
            page_index,
            {
                "cells": [],
                "fallback_text": "",
                "filtered": False,
                "needs_retry": False,
                "retry_reason": "",
                "image_size": None,
                "json_path": None,
                "image_path": None,
                "md_nohf_text": "",
                "md_nohf_path": None,
            },
        )
        text = markdown_to_text(md_path)
        page["md_nohf_text"] = text
        page["md_nohf_path"] = md_path
        if not page["cells"]:
            page["fallback_text"] = text

    for page_index, page in pages.items():
        page["cells"] = sanitize_layout_cells(page["cells"])
        reasons = layout_retry_reasons(page, page["cells"], page.get("fallback_text", ""))
        if reasons:
            for reason in reasons:
                append_retry_reason(page, reason)
            page["needs_retry"] = True
        image_path = find_mineru_page_image(branch_dir, page_index)
        if image_path:
            page["image_path"] = image_path
            with Image.open(image_path) as img:
                page["image_size"] = img.size

    log(
        "    MinerU JSON source selected: "
        + ", ".join(str(path.relative_to(output_dir)) for path in json_files)
    )
    log(f"    MinerU result branch: {branch_dir.relative_to(output_dir) if branch_dir != output_dir else '.'}")
    log(f"    MinerU JSON files scanned: {len(json_files)} of {len(all_json_files)}")
    log(f"    MinerU pages with layout cells: {sum(1 for page in pages.values() if page['cells'])}")
    return pages


def build_mineru_result_manifest(pdf_path, output_dir):
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    all_json_files, primary_files, branch_dir = current_mineru_result_files(output_dir)
    if not primary_files:
        raise RuntimeError(f"No MinerU JSON result found in: {output_dir}")

    supplement_files = select_mineru_supplement_json_files(all_json_files, primary_files)
    consumed_files = sorted(set(primary_files) | set(supplement_files))
    for json_path in consumed_files:
        try:
            read_json(json_path)
        except Exception as exc:
            raise RuntimeError(f"Unreadable MinerU JSON result {json_path}: {exc}") from exc

    pages = collect_page_results(pdf_path, output_dir)
    with fitz.open(pdf_path) as source:
        expected_page_count = int(source.page_count)
        covered_pages = sorted(int(page_index) for page_index in pages)
        out_of_range_pages = [
            page_index
            for page_index in covered_pages
            if page_index < 0 or page_index >= expected_page_count
        ]
        if out_of_range_pages:
            raise RuntimeError(
                "MinerU result contains page indices outside the task PDF: "
                + ", ".join(str(page_index + 1) for page_index in out_of_range_pages)
            )
        missing_pages = sorted(set(range(expected_page_count)) - set(covered_pages))
        blank_missing_pages = [
            page_index for page_index in missing_pages if is_source_page_blank(source, page_index)
        ]
        nonblank_missing_pages = sorted(set(missing_pages) - set(blank_missing_pages))

    coverage = {
        "covered_pages": [page_index + 1 for page_index in covered_pages],
        "blank_source_pages_without_result": [
            page_index + 1 for page_index in blank_missing_pages
        ],
        "nonblank_source_pages_without_result": [
            page_index + 1 for page_index in nonblank_missing_pages
        ],
    }
    accounted_pages = (
        set(coverage["covered_pages"])
        | set(coverage["blank_source_pages_without_result"])
        | set(coverage["nonblank_source_pages_without_result"])
    )
    if accounted_pages != set(range(1, expected_page_count + 1)):
        raise RuntimeError(
            f"MinerU result coverage is incomplete for {pdf_path}: "
            f"accounted={len(accounted_pages)}, expected={expected_page_count}"
        )

    return {
        "schema": 1,
        "task_pdf": file_integrity_signature(pdf_path),
        "expected_page_count": expected_page_count,
        **coverage,
        "accounted_page_count": len(accounted_pages),
        "coverage_hash": stable_json_hash(coverage),
        "result_branch": str(branch_dir.resolve().relative_to(output_dir.resolve())),
        "primary_json": [
            file_integrity_signature(path, relative_to=output_dir)
            for path in primary_files
        ],
        "consumed_json": [
            file_integrity_signature(path, relative_to=output_dir)
            for path in consumed_files
        ],
    }


def mineru_result_manifest_matches(checkpoint, pdf_path, output_dir):
    expected_manifest = checkpoint.get("result_manifest") if checkpoint else None
    if not isinstance(expected_manifest, dict):
        return False
    try:
        current_manifest = build_mineru_result_manifest(pdf_path, output_dir)
    except Exception as exc:
        log(f"    Resume integrity check failed: {exc}")
        return False
    if current_manifest != expected_manifest:
        log("    Resume integrity check failed: MinerU result manifest changed")
        return False
    return True


def clone_page_result_with_offset(result, offset):
    cloned = dict(result)
    cloned["cells"] = [dict(cell) for cell in result.get("cells", [])]
    cloned["page_offset"] = offset
    for cell in cloned["cells"]:
        cell["__page_offset"] = offset
    return cloned


def collect_parser_run_results(pdf_path, parser_runs):
    if len(parser_runs) == 1 and parser_runs[0]["page_offset"] == 0:
        pages = collect_page_results(pdf_path, parser_runs[0]["output_dir"])
        mark_log_error_retry_pages(pages, parser_runs[0]["log_path"])
        return pages

    merged = {}
    for run in parser_runs:
        log(
            f"    Loading MinerU JSON for {run['label']}: "
            f"full pages {run['start_page'] + 1}-{run['end_page'] + 1}"
        )
        chunk_pages = collect_page_results(run["pdf_path"], run["output_dir"])
        mark_log_error_retry_pages(chunk_pages, run["log_path"])
        offset = int(run["page_offset"])
        for local_page_index, result in chunk_pages.items():
            full_page_index = local_page_index + offset
            if full_page_index in merged:
                append_retry_reason(
                    result,
                    f"duplicate page result while merging parser chunk {run['label']}",
                )
            merged[full_page_index] = clone_page_result_with_offset(result, offset)

    log(f"    Merged chunked MinerU results: {len(merged)} page(s)")
    return merged


def parser_log_cell_post_process_error_count(log_path):
    if not log_path.exists():
        return 0
    text = log_path.read_text(encoding="utf-8", errors="replace")
    return text.count("cells post process error")


def append_retry_reason(result, reason):
    existing = result.get("retry_reason")
    if existing:
        reasons = [item.strip() for item in existing.split(";") if item.strip()]
        if reason not in reasons:
            reasons.append(reason)
        result["retry_reason"] = "; ".join(reasons)
    else:
        result["retry_reason"] = reason


def mark_log_error_retry_pages(page_results, log_path):
    error_count = parser_log_cell_post_process_error_count(log_path)
    if not error_count:
        return

    marked = []
    for page_index, result in sorted(page_results.items()):
        reason = result.get("retry_reason", "")
        log_related = (
            result.get("filtered")
            or "layout JSON" in reason
            or "Markdown fallback" in reason
            or "suspicious hallucinated placeholder text" in reason
        )
        if not log_related:
            continue

        result["needs_retry"] = True
        append_retry_reason(
            result,
            f"main MinerU log contains cells post process error ({error_count} total)",
        )
        marked.append(page_index + 1)

    if marked:
        log(
            "    Main MinerU log reported cells post process errors; "
            "auto-retry candidate pages: " + ", ".join(str(page) for page in marked)
        )
    else:
        log(
            "    Warning: main MinerU log reported cells post process errors, "
            "but no page could be mapped from JSONL filtered/layout-fallback records"
        )


def rendered_page_dark_ratio(page):
    zoom = BLANK_PAGE_CHECK_DPI / 72
    pix = page.get_pixmap(
        matrix=fitz.Matrix(zoom, zoom),
        colorspace=fitz.csGRAY,
        alpha=False,
    )
    dark = sum(1 for value in pix.samples if value < BLANK_PAGE_DARK_THRESHOLD)
    return dark / max(1, pix.width * pix.height)


def is_source_page_blank(doc, page_index):
    page = doc[page_index]
    if page.get_text().strip():
        return False
    return rendered_page_dark_ratio(page) <= BLANK_PAGE_MAX_DARK_RATIO


def page_result_has_output(result):
    return bool(
        result.get("cells")
        or result.get("fallback_text")
        or result.get("needs_retry")
        or result.get("image_fallback_path")
    )


def page_result_only_font_test_text(result):
    parts = []
    for cell in result.get("cells") or []:
        text = normalize_text(source_text_from_cell(cell))
        if text:
            parts.append(text)
    for key in ("fallback_text", "md_nohf_text"):
        text = normalize_text(result.get(key, ""))
        if text:
            parts.append(text)

    if not parts:
        return False
    return all(is_font_test_text(part) for part in parts)


def clear_page_result_as_blank(result, reason):
    result["cells"] = []
    result["fallback_text"] = ""
    result["md_nohf_text"] = ""
    result["needs_retry"] = False
    result["blank_page"] = True
    append_retry_reason(result, reason)


def suppress_blank_page_outputs(pdf_path, page_results):
    candidate_indexes = [
        page_index
        for page_index, result in page_results.items()
        if page_result_has_output(result)
    ]
    if not candidate_indexes:
        return

    cleared = []
    font_test_cleared = []
    with fitz.open(pdf_path) as doc:
        for page_index in candidate_indexes:
            if page_index < 0 or page_index >= doc.page_count:
                continue

            result = page_results[page_index]
            if page_result_only_font_test_text(result):
                clear_page_result_as_blank(
                    result,
                    "OCR page contains only font test pangram; keeping blank page",
                )
                font_test_cleared.append(page_index + 1)
                continue

            if not is_source_page_blank(doc, page_index):
                continue

            clear_page_result_as_blank(
                result,
                "source page appears blank; keeping blank page",
            )
            cleared.append(page_index + 1)

    if cleared:
        log(
            "    Blank source pages detected; cleared OCR output and kept blank: "
            + ", ".join(str(page_no) for page_no in cleared)
        )
    if font_test_cleared:
        log(
            "    Font test only pages detected; cleared OCR output and kept blank: "
            + ", ".join(str(page_no) for page_no in font_test_cleared)
        )


def retry_result_has_usable_cells(result):
    if not result:
        return False
    if page_result_has_high_repeated_pseudotext(result):
        return False
    cells = result.get("cells") or []
    if not cells:
        return False
    joined_text = "\n".join(collect_cell_texts(cells))
    if not joined_text.strip():
        return False
    return not any(phrase in joined_text for phrase in SUSPICIOUS_LAYOUT_PHRASES)


def accept_repaired_retry_result(result):
    result["needs_retry"] = False
    result["filtered"] = False
    append_retry_reason(result, "accepted MinerU batched quality retry")


def collapse_repeated_text_in_split_band_result(result):
    if not result:
        return result
    for cell in result.get("cells", []) or []:
        if not isinstance(cell, dict):
            continue
        for key in ("text", "content", "html", "latex"):
            value = cell.get(key)
            if value:
                cell[key] = collapse_repeated_ocr_text(str(value))
    for key in ("fallback_text", "md_nohf_text"):
        value = result.get(key)
        if value:
            result[key] = collapse_repeated_ocr_text(str(value))
    return result


def retry_bad_pages(pdf_path, page_results, work_dir, log_path):
    bad_pages = sorted(
        page_index
        for page_index, result in page_results.items()
        if result.get("needs_retry")
    )
    if not bad_pages:
        return page_results

    log(
        "    Bad layout JSON pages detected: "
        + ", ".join(str(page_index + 1) for page_index in bad_pages)
    )
    for page_index in bad_pages:
        reason = page_results.get(page_index, {}).get("retry_reason") or "unknown reason"
        log(f"        Page {page_index + 1}: {reason}")
    log(f"    Retrying all {len(bad_pages)} bad page(s) in one MinerU batch")

    retry_root = work_dir / "page_retry_mineru_batch"
    retry_root.mkdir(parents=True, exist_ok=True)

    selected_pdf = retry_root / f"{pdf_path.stem}_bad_pages.pdf"
    retry_output_dir = retry_root / "output"
    retry_log = log_path.with_name(f"{log_path.stem}_bad_pages_batch_retry{log_path.suffix}")
    retry_checkpoint = retry_root / "bad_pages_batch_complete.json"
    try:
        retry_pages = run_or_reuse_mineru_selected_pages(
            pdf_path,
            bad_pages,
            selected_pdf,
            retry_output_dir,
            retry_log,
            retry_checkpoint,
        )
    except Exception as exc:
        log(f"    Batched MinerU quality retry failed; keeping sanitized originals: {exc}")
        for page_index in bad_pages:
            result = page_results.get(page_index) or {"cells": [], "fallback_text": "", "needs_retry": True}
            result["needs_retry"] = True
            append_retry_reason(result, f"batched quality retry failed: {exc}")
            page_results[page_index] = result
        return page_results

    for local_page_index, page_index in enumerate(bad_pages):
        original = page_results.get(page_index) or {"cells": [], "fallback_text": "", "needs_retry": True}
        retry_result = retry_pages.get(local_page_index)
        if retry_result is None:
            append_retry_reason(original, "batched quality retry returned no page result")
            page_results[page_index] = original
            log(f"        Page {page_index + 1}: no result in batched retry; kept original")
            continue

        retry_had_pseudotext = page_result_has_high_repeated_pseudotext(retry_result)
        retry_result = collapse_repeated_text_in_split_band_result(retry_result)
        if retry_had_pseudotext:
            retry_result["needs_retry"] = True
            retry_result["pseudotext_detected"] = True
            append_retry_reason(retry_result, PSEUDOTEXT_RETRY_REASON)
        elif retry_result_has_usable_cells(retry_result):
            accept_repaired_retry_result(retry_result)

        if retry_result.get("needs_retry"):
            append_retry_reason(original, "batched quality retry remained invalid")
            page_results[page_index] = original
            log(f"        Page {page_index + 1}: batched retry remained invalid; kept original")
            continue

        retry_result["source_page_retry"] = True
        retry_result["retry_page"] = page_index + 1
        retry_result["batch_retry_local_page"] = local_page_index + 1
        page_results[page_index] = retry_result
        log(f"        Page {page_index + 1}: accepted result from batched quality retry")

    return page_results


def force_table_cells_to_text(result):
    if not result:
        return result
    for cell in result.get("cells", []) or []:
        if not isinstance(cell, dict):
            continue
        if normalize_category(cell.get("category")) == "Table":
            cell["category"] = "Text"
            cell["__table_disabled_retry"] = True
    result["table_disabled_retry"] = True
    return result


def retry_unfitting_table_pages_as_text_batch(pdf_path, page_results, work_dir, log_path):
    candidates = []
    with fitz.open(pdf_path) as src:
        for page_index, result in sorted(page_results.items()):
            if page_index < 0 or page_index >= src.page_count:
                continue
            page = src[page_index]
            page_width = float(page.rect.width)
            page_height = float(page.rect.height)
            blocks = blocks_from_page_result(result, page_width, page_height)
            if not any(block.get("category") == "Table" for block in blocks):
                continue
            blocks.sort(key=lambda b: (is_header_footer(b), b["top"], b["left"], b["order"]))
            blocks = prepare_blocks(blocks, page_width, page_height)
            table_blocks = [block for block in blocks if block.get("category") == "Table"]
            if reportlab_page_fit_failures(page_height, table_blocks):
                candidates.append(page_index)

    if not candidates:
        return page_results

    log(
        f"    Table bbox preflight found {len(candidates)} page(s); "
        "retrying all of them in one MinerU batch with -t false"
    )
    retry_root = work_dir / "table_text_retry_mineru_batch"
    selected_pdf = retry_root / f"{pdf_path.stem}_table_fit_pages.pdf"
    retry_output_dir = retry_root / "output"
    retry_log = log_path.with_name(f"{log_path.stem}_table_fit_pages_batch_retry{log_path.suffix}")
    retry_checkpoint = retry_root / "table_fit_pages_batch_complete.json"
    try:
        retry_pages = run_or_reuse_mineru_selected_pages(
            pdf_path,
            candidates,
            selected_pdf,
            retry_output_dir,
            retry_log,
            retry_checkpoint,
            table_enabled=False,
        )
    except Exception as exc:
        log(f"    Batched table-off retry failed; affected pages will use image fallback: {exc}")
        for page_index in candidates:
            append_retry_reason(page_results[page_index], f"batched table-off retry failed: {exc}")
        return page_results

    with fitz.open(pdf_path) as src:
        for local_page_index, page_index in enumerate(candidates):
            retry_result = retry_pages.get(local_page_index)
            if not retry_result:
                append_retry_reason(page_results[page_index], "batched table-off retry returned no page result")
                continue
            retry_result = force_table_cells_to_text(retry_result)
            retry_result = collapse_repeated_text_in_split_band_result(retry_result)
            page = src[page_index]
            page_width = float(page.rect.width)
            page_height = float(page.rect.height)
            retry_blocks = blocks_from_page_result(retry_result, page_width, page_height)
            retry_blocks.sort(key=lambda b: (is_header_footer(b), b["top"], b["left"], b["order"]))
            retry_blocks = prepare_blocks(retry_blocks, page_width, page_height)
            retry_failures = reportlab_page_fit_failures(page_height, retry_blocks)
            if not retry_blocks or retry_failures:
                append_retry_reason(page_results[page_index], "batched table-off retry still could not fit bbox")
                continue

            retry_result["source_page_retry"] = True
            retry_result["retry_page"] = page_index + 1
            retry_result["batch_retry_local_page"] = local_page_index + 1
            append_retry_reason(retry_result, TABLE_TEXT_RETRY_REASON)
            page_results[page_index] = retry_result
            log(f"        Page {page_index + 1}: accepted batched table-off retry")

    return page_results


def degrade_repeated_pseudotext_pages_to_images(pdf_path, page_results, work_dir):
    candidates = []
    for page_index, result in sorted(page_results.items()):
        if not result:
            continue
        reason = result.get("retry_reason", "")
        if page_result_has_high_repeated_pseudotext(result) or PSEUDOTEXT_RETRY_REASON in reason:
            candidates.append(page_index)

    if not candidates:
        return page_results

    image_dir = work_dir / "image_fallback_pages"
    degraded = []
    with fitz.open(pdf_path) as doc:
        for page_index in candidates:
            if page_index < 0 or page_index >= doc.page_count:
                continue

            result = page_results.get(page_index) or {}
            if result.get("blank_page"):
                continue

            # Only degrade pages that still look bad after the MinerU batch retry.
            if result.get("source_page_retry") and not page_result_has_high_repeated_pseudotext(result):
                continue

            image_path = image_dir / f"page_{page_index + 1:04d}.png"
            render_pdf_page_to_png(pdf_path, page_index, image_path)
            result["cells"] = []
            result["fallback_text"] = ""
            result["md_nohf_text"] = ""
            result["needs_retry"] = False
            result["image_fallback_path"] = str(image_path)
            result["image_fallback_page"] = True
            result["pseudotext_detected"] = True
            append_retry_reason(result, PSEUDOTEXT_IMAGE_FALLBACK_REASON)
            page_results[page_index] = result
            degraded.append(page_index + 1)

    if degraded:
        log(
            "    High-repetition pseudo-text pages degraded to source page images: "
            + ", ".join(str(page_no) for page_no in degraded)
        )
    return page_results


def degrade_unusable_nonblank_pages_to_images(pdf_path, page_results, work_dir):
    image_dir = work_dir / "unusable_ocr_image_fallback_pages"
    degraded = []
    blank = []
    with fitz.open(pdf_path) as doc:
        for page_index in range(doc.page_count):
            result = page_results.get(page_index)
            if result and (result.get("image_fallback_path") or result.get("blank_page")):
                continue

            missing_result = result is None
            if result is None:
                result = {
                    "cells": [],
                    "fallback_text": "",
                    "md_nohf_text": "",
                    "filtered": False,
                    "needs_retry": False,
                    "image_size": None,
                }

            usable_text = normalize_text(page_result_text(result))
            unresolved = bool(result.get("needs_retry"))
            if not missing_result and usable_text and not unresolved:
                continue

            if is_source_page_blank(doc, page_index):
                clear_page_result_as_blank(
                    result,
                    "source page appears blank and has no usable OCR result; keeping blank page",
                )
                page_results[page_index] = result
                blank.append(page_index + 1)
                continue

            reason = (
                MISSING_OCR_IMAGE_FALLBACK_REASON
                if missing_result
                else UNUSABLE_OCR_IMAGE_FALLBACK_REASON
            )
            image_path = image_dir / f"page_{page_index + 1:04d}.png"
            render_pdf_page_to_png(pdf_path, page_index, image_path)
            result["cells"] = []
            result["fallback_text"] = ""
            result["md_nohf_text"] = ""
            result["needs_retry"] = False
            result["image_fallback_path"] = str(image_path)
            result["image_fallback_page"] = True
            result["image_fallback_kind"] = "missing_ocr" if missing_result else "handwriting_or_low_quality_scan"
            append_retry_reason(result, reason)
            page_results[page_index] = result
            degraded.append(page_index + 1)

    if blank:
        log(
            "    Pages without usable OCR confirmed blank: "
            + ", ".join(str(page_no) for page_no in blank)
        )
    if degraded:
        log(
            "    Nonblank pages without usable OCR moved to the image variant: "
            + ", ".join(str(page_no) for page_no in degraded)
        )
    return page_results


def cell_to_block(cell, order, page_width, page_height, image_size):
    bbox = cell.get("bbox")
    text = get_text_from_cell(cell)
    category = normalize_category(cell.get("category"))

    if category == "Picture" and not text:
        return None
    if not text and category not in {"Formula", "Table"}:
        return None
    if not isinstance(bbox, list) or len(bbox) != 4:
        return None

    x0, y0, x1, y1 = [float(v) for v in bbox]

    bbox_units = cell.get("__bbox_units")
    if bbox_units == "relative":
        image_width = 1.0
        image_height = 1.0
    elif bbox_units == "mineru_1000":
        image_width = 1000.0
        image_height = 1000.0
    elif bbox_units == "pdf" or (
        x1 <= page_width * 1.35 and y1 <= page_height * 1.35
    ):
        image_width = page_width
        image_height = page_height
    elif image_size:
        image_width, image_height = image_size
    else:
        image_width = page_width * DPI / 72
        image_height = page_height * DPI / 72

    scale_x = page_width / max(float(image_width), 1.0)
    scale_y = page_height / max(float(image_height), 1.0)

    left = max(0.0, min(x0 * scale_x, page_width))
    top = max(0.0, min(y0 * scale_y, page_height))
    right = max(left + 1.0, min(x1 * scale_x, page_width))
    bottom = max(top + 1.0, min(y1 * scale_y, page_height))

    return {
        "order": order,
        "category": category,
        "text": text,
        "source_text": source_text_from_cell(cell),
        "source_cell": cell,
        "table_rows": extract_table_rows(source_text_from_cell(cell)) if category == "Table" else [],
        "line_info": cell.get("__line_info") or [],
        "bbox_scale_x": scale_x,
        "bbox_scale_y": scale_y,
        "left": left,
        "top": top,
        "width": right - left,
        "height": bottom - top,
    }


def fallback_text_block(text, order, page_width, page_height):
    text = normalize_markdown_text(text)
    if not text:
        return None
    margin_x = page_width * 0.09
    margin_top = page_height * 0.08
    margin_bottom = page_height * 0.08
    return {
        "order": order,
        "category": "Text",
        "text": text,
        "left": margin_x,
        "top": margin_top,
        "width": page_width - margin_x * 2,
        "height": page_height - margin_top - margin_bottom,
    }


def image_fallback_block(image_path, page_width, page_height):
    return {
        "order": 0,
        "category": "ImageFallback",
        "text": "",
        "image_path": str(image_path),
        "left": 0.0,
        "top": 0.0,
        "width": float(page_width),
        "height": float(page_height),
    }


def blocks_from_page_result(result, page_width, page_height):
    blocks = []
    for order, cell in enumerate(result.get("cells", []) or []):
        block = cell_to_block(cell, order, page_width, page_height, result.get("image_size"))
        if block:
            blocks.append(block)
    return blocks


def has_cjk(text):
    return bool(re.search(r"[\u3400-\u9fff]", text or ""))


def inline_markdown_segments(text):
    text = normalize_inline_markup_aliases(text)
    segments = []
    i = 0
    patterns = [
        ("math", "$$", "$$"),
        ("math", "\\(", "\\)"),
        ("math", "\\[", "\\]"),
        ("strongitalic", "***", "***"),
        ("strongitalic", "___", "___"),
        ("strong", "**", "**"),
        ("strong", "__", "__"),
        ("strike", "~~", "~~"),
        ("mark", "==", "=="),
        ("code", "`", "`"),
        ("math", "$", "$"),
        ("sub", "~", "~"),
        ("sup", "^", "^"),
        ("italic", "*", "*"),
        ("italic", "_", "_"),
    ]

    while i < len(text):
        matched = False
        for style, start, end in patterns:
            if not text.startswith(start, i):
                continue
            if style in {"italic"}:
                if text.startswith(start * 2, i):
                    continue
            j = text.find(end, i + len(start))
            if j <= i + len(start):
                continue
            inner = text[i + len(start):j]
            if "\n" in inner:
                continue
            if inner.strip():
                if style == "math" and start == "$" and not is_likely_math_text(inner):
                    continue
                segments.append({"text": inner, "style": style})
                i = j + len(end)
                matched = True
                break
        if matched:
            continue

        next_positions = [
            pos for marker in ("$$", "\\(", "\\[", "***", "___", "**", "__", "~~", "==", "`", "$", "~", "^", "*", "_")
            for pos in [text.find(marker, i + 1)]
            if pos != -1
        ]
        next_i = min(next_positions) if next_positions else len(text)
        segments.append({"text": text[i:next_i], "style": "normal"})
        i = next_i

    return [seg for seg in segments if seg["text"]]


def markdown_line_to_segments(line):
    raw = line.rstrip()
    stripped = raw.strip()
    if not stripped:
        return [], 0, "normal"

    if re.fullmatch(r"(?:[-*_]\s*){3,}", stripped):
        return [], 0, "rule"

    if stripped.startswith("```"):
        return [], 0, "code"

    heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
    if heading:
        level = len(heading.group(1))
        return inline_markdown_segments(heading.group(2)), 0, f"heading{level}"

    footnote_def = re.match(r"^\[\^([^\]]{1,24})\]:\s*(.+)$", stripped)
    if footnote_def:
        prefix = f"{footnote_def.group(1)}. "
        return [{"text": prefix, "style": "normal"}] + inline_markdown_segments(footnote_def.group(2)), 14, "footnote"

    quote = re.match(r"^>\s*(.+)$", stripped)
    if quote:
        return inline_markdown_segments(quote.group(1)), 18, "quote"

    unordered = re.match(r"^[-+*]\s+(?:\[[ xX]\]\s*)?(.+)$", stripped)
    if unordered:
        return [{"text": "• ", "style": "normal"}] + inline_markdown_segments(unordered.group(1)), 14, "list"

    ordered = re.match(r"^(\d+)[.)]\s+(.+)$", stripped)
    if ordered:
        prefix = f"{ordered.group(1)}. "
        return [{"text": prefix, "style": "normal"}] + inline_markdown_segments(ordered.group(2)), 14, "list"

    table_cells = markdown_table_cells(stripped)
    if table_cells is not None:
        if not table_cells:
            return [], 0, "table-rule"
        segments = []
        for index, cell in enumerate(table_cells):
            if index:
                segments.append({"text": "    ", "style": "normal"})
            segments.extend(inline_markdown_segments(cell))
        return segments, 0, "table"

    return inline_markdown_segments(stripped), 0, "normal"


def line_font_scale(line_style):
    if line_style == "table":
        return 0.92
    return 1.0


def line_is_bold(line_style):
    return line_style in {"strong", "heading1", "heading2", "heading3", "heading4", "heading5", "heading6"}


def line_is_italic(line_style):
    return line_style in {"italic", "quote"}


def is_header_footer(block):
    return block["category"] in {"Page-header", "Page-footer"}


def is_title(block):
    return block["category"] in {"Title", "Section-header"}


def clean_block_text(block):
    return normalize_text(block.get("text", ""))


def is_numeric_page_marker(text):
    text = text.strip()
    return bool(re.fullmatch(r"\d{1,4}", text) or re.fullmatch(r"\[\s*\d{1,4}\s*\]", text))


def is_short_running_text(block):
    text = clean_block_text(block)
    return 1 <= len(text) <= 12


def is_body_text_block(block):
    if block.get("category") != "Text":
        return False
    text = clean_block_text(block)
    if len(text) < 30:
        return False
    if is_numeric_page_marker(text):
        return False
    return True


def is_misclassified_long_header_footer(block, page_width):
    if block.get("category") not in {"Page-header", "Page-footer"}:
        return False
    text = clean_block_text(block)
    if len(text) < 36 or is_numeric_page_marker(text):
        return False
    if block.get("width", 0) >= page_width * 0.45 and block.get("height", 0) >= 14:
        return False
    comma_list = len(re.findall(r"\d+[.,]?\d*", text)) >= 6
    prose_like = len(re.findall(r"[\u3400-\u9fffA-Za-zÀ-ÖØ-öø-ÿΑ-ω]", text)) >= 16
    too_wide = estimated_inline_width(text, max(6.0, min(9.0, block.get("height", 9.0)))) > block.get("width", 1) * 1.8
    return too_wide and (comma_list or prose_like)


def repair_misclassified_header_footer_block(block, page_width, page_height, body_size):
    if not is_misclassified_long_header_footer(block, page_width):
        return
    original_category = block["category"]
    block["category"] = "Text"
    block["misclassified_header_footer_repaired"] = original_category

    margin_x = page_width * 0.075
    current_right = block["left"] + block["width"]
    left = min(block["left"], margin_x)
    right = max(current_right, page_width - margin_x)
    block["left"] = max(0.0, left)
    block["width"] = min(page_width, right) - block["left"]

    font_size = clamp(estimate_bbox_font_size(block), body_size * 0.72, body_size * 0.92)
    line_height = 1.05
    needed = estimate_required_height(block, font_size, line_height)
    max_bottom = page_height * 0.92 if original_category == "Page-header" else page_height - 8
    block["height"] = min(max(block["height"], needed * 1.05, 14.0), max(14.0, max_bottom - block["top"]))


def line_count_for_text(text):
    return max(1, len([line for line in re.split(r"\n+", text) if line.strip()]))


def block_structural_line_count(block):
    line_info = block.get("line_info") or []
    count = len([line for line in line_info if str(line.get("text", "")).strip()])
    return count if count > 1 else None


def line_info_font_size_estimate(block):
    line_info = block.get("line_info") or []
    if len(line_info) < 2:
        return None

    scale_y = float(block.get("bbox_scale_y", 1.0))
    heights = []
    for line in line_info:
        bbox = line.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        height = max(0.0, float(bbox[3]) - float(bbox[1])) * scale_y
        if 2.5 <= height <= max(3.0, block["height"] * 0.45):
            heights.append(height)

    if len(heights) < 2:
        return None

    med = median(heights)
    if not med:
        return None
    if max(heights) / max(1.0, min(heights)) > 2.4:
        return None
    return med * 0.82


def estimate_bbox_font_size(block):
    height = max(block["height"], 1.0)
    text = block["text"]
    lines = block_structural_line_count(block) or line_count_for_text(text)
    by_height = height / (lines * 1.20)
    by_line_info = line_info_font_size_estimate(block)
    if by_line_info:
        return clamp(by_line_info, by_height * 0.75, by_height * 1.25)
    return by_height


def median(values):
    values = sorted(values)
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def page_font_scale(page_height):
    return clamp(float(page_height) / 842.0, 0.75, MAX_PAGE_FONT_SCALE)


def scaled_body_font_min(page_height):
    return BODY_FONT_MIN * page_font_scale(page_height)


def scaled_body_font_max(page_height):
    return BODY_FONT_MAX * page_font_scale(page_height)


def scaled_body_text_font_floor(page_height):
    return BODY_TEXT_FONT_FLOOR * page_font_scale(page_height)


def estimate_page_body_size(blocks, page_height):
    candidates = []
    min_size = scaled_body_font_min(page_height)
    max_size = scaled_body_font_max(page_height)
    for block in blocks:
        if block["category"] != "Text":
            continue
        text = clean_block_text(block)
        if len(text) < 80 or is_numeric_page_marker(text):
            continue
        candidates.append(clamp(estimate_bbox_font_size(block), min_size, max_size))
    return median(candidates) or (10.8 * page_font_scale(page_height))


def estimate_font_size(block, body_size):
    category = block["category"]
    clean_text = clean_block_text(block)
    by_height = estimate_bbox_font_size(block)

    if category == "Title":
        return clamp(by_height, body_size * 1.02, body_size * 1.35)
    if category == "Section-header":
        return clamp(by_height, body_size * 0.88, body_size * 1.16)
    if is_numeric_page_marker(clean_text):
        return clamp(by_height, body_size * 0.62, body_size * 0.82)
    if category == "Page-header":
        return clamp(by_height, body_size * 0.72, body_size * 0.92)
    if category == "Page-footer":
        return clamp(by_height, body_size * 0.55, body_size * 0.82)
    if category == "Caption":
        return clamp(by_height, body_size * 0.62, body_size * 0.86)
    if category == "Footnote":
        return clamp(by_height, body_size * 0.62, body_size * 0.78)
    if category == "List-item":
        return clamp(by_height, body_size * 0.82, body_size * 0.98)
    if category == "Text" and is_short_running_text(block):
        return clamp(by_height, body_size * 0.82, body_size * 1.05)
    if category == "Table":
        row_count = max(1, len(table_rows_for_block(block)))
        by_rows = block["height"] / max(row_count * 1.35, 1)
        return clamp(by_rows, body_size * 0.72, body_size * 1.08)
    if category == "Formula":
        return clamp(by_height, body_size * 0.65, body_size * 0.92)
    return clamp(by_height, body_size * 0.88, body_size * 1.00)


def clamp(value, low, high):
    return max(low, min(high, value))


def estimate_required_height(block, font_size, line_height):
    text = block["text"]
    if not text:
        return 0
    width = max(block["width"], 20)
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿΑ-ω]", text))
    total = max(len(text), 1)
    unit = 1.05 if cjk >= latin else 0.58
    chars_per_line = max(1, int(width / max(font_size * unit, 1)))
    explicit_lines = [line for line in re.split(r"\n+", text) if line.strip()]
    lines = 0
    for line in explicit_lines or [text]:
        lines += max(1, math.ceil(len(line) / chars_per_line))
    return lines * font_size * line_height


def estimate_visual_line_count(block, font_size):
    structural_lines = block_structural_line_count(block)
    if structural_lines:
        return structural_lines

    text = block["text"]
    clean_text = clean_block_text(block)
    if not clean_text:
        return 1

    width = max(block["width"], 20)
    cjk = len(re.findall(r"[\u3400-\u9fff]", clean_text))
    latin = len(re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿΑ-ω]", clean_text))
    unit = 1.05 if cjk >= latin else 0.58
    chars_per_line = max(1, int(width / max(font_size * unit, 1)))

    raw_lines = [line for line in re.split(r"\n+", text) if line.strip()]
    lines = 0
    for line in raw_lines or [clean_text]:
        line_text = strip_markdown_inline(line).strip()
        lines += max(1, math.ceil(len(line_text) / chars_per_line))
    return max(1, lines)


def estimate_line_height(block, font_size):
    visual_lines = estimate_visual_line_count(block, font_size)
    raw = block["height"] / max(font_size * visual_lines, 1)
    category = block["category"]
    clean_text = clean_block_text(block)

    if category in {"Title", "Section-header"}:
        return clamp(raw, 0.95, 1.20)
    if category in {"Page-header", "Page-footer"} or is_numeric_page_marker(clean_text):
        return clamp(raw, 0.90, 1.15)
    if category == "Caption":
        return clamp(raw, 0.95, 1.20)
    if category == "Footnote":
        return clamp(raw, 0.95, 1.55)
    if category == "List-item":
        return clamp(raw, 0.98, 1.75)
    if category in {"Formula", "Table"}:
        return clamp(raw, 0.95, 1.20)
    if is_short_running_text(block):
        return clamp(raw, 0.95, 1.20)
    return clamp(raw, 1.08, 2.35)


def estimated_inline_width(text, font_size):
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z0-9À-ÖØ-öø-ÿΑ-ω]", text))
    other = max(len(text) - cjk - latin, 0)
    return cjk * font_size + latin * font_size * 0.55 + other * font_size * 0.45


def horizontal_overlap_ratio(a, b):
    left = max(a["left"], b["left"])
    right = min(a["left"] + a["width"], b["left"] + b["width"])
    overlap = max(0.0, right - left)
    return overlap / max(1.0, min(a["width"], b["width"]))


def block_bottom(block):
    return block["top"] + block["height"]


TEXT_MERGE_CATEGORIES = {"Text", "List-item"}


def dedupe_text_key(text):
    text = normalize_text(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def append_text_with_overlap(base, addition):
    """Append OCR text while removing duplicated split retry overlap.

    Historical split retries cropped pages with vertical overlap. OCR engines can re-OCR the
    same paragraph in adjacent bands with slightly different bboxes and line
    breaks. This keeps the earlier text and appends only the non-overlap suffix
    from the later band.
    """
    base_norm = dedupe_text_key(base)
    add_norm = dedupe_text_key(addition)

    if not base_norm:
        return add_norm
    if not add_norm:
        return base_norm

    if add_norm in base_norm:
        return base_norm
    if base_norm in add_norm:
        return add_norm

    max_k = min(len(base_norm), len(add_norm), 1400)

    # Exact suffix-prefix overlap.
    for k in range(max_k, 39, -1):
        if base_norm[-k:] == add_norm[:k]:
            return (base_norm + " " + add_norm[k:].lstrip()).strip()

    # Fuzzy overlap for OCR differences, hyphenation differences and line breaks.
    tail = base_norm[-1400:]
    head = add_norm[:1400]
    matcher = SequenceMatcher(None, tail, head, autojunk=False)

    best = None
    for match in matcher.get_matching_blocks():
        if match.size < 45:
            continue
        touches_tail_end = match.a + match.size >= len(tail) - 8
        near_head_start = match.b <= 80
        if touches_tail_end and near_head_start:
            if best is None or match.size > best.size:
                best = match

    if best is not None:
        cut = best.b + best.size
        return (base_norm + " " + add_norm[cut:].lstrip()).strip()

    return (base_norm + " " + add_norm).strip()


def text_overlap_like(a, b):
    a_norm = dedupe_text_key(a)
    b_norm = dedupe_text_key(b)
    if not a_norm or not b_norm:
        return False
    if a_norm in b_norm or b_norm in a_norm:
        return True
    merged = append_text_with_overlap(a_norm, b_norm)
    naive = (a_norm + " " + b_norm).strip()
    return len(merged) <= len(naive) - 40


def retry_blocks_should_merge(a, b):
    if a.get("category") not in TEXT_MERGE_CATEGORIES:
        return False
    if b.get("category") not in TEXT_MERGE_CATEGORIES:
        return False

    h_overlap = horizontal_overlap_ratio(a, b)
    if h_overlap < 0.20:
        return False

    a_bottom = block_bottom(a)
    b_bottom = block_bottom(b)
    vertical_overlap = min(a_bottom, b_bottom) - max(a["top"], b["top"])
    gap = b["top"] - a_bottom

    if text_overlap_like(a.get("text", ""), b.get("text", "")):
        return True

    # Adjacent split retry crops can produce overlapping bboxes for the same paragraph
    # even when OCR text starts at a different point.
    if vertical_overlap > min(a["height"], b["height"]) * 0.10:
        return True

    # Boundary paragraphs can be nearly touching after coordinate restoration.
    if gap <= 4.0 and h_overlap >= 0.55:
        return True

    return False


def merge_retry_block_pair(a, b):
    a_right = a["left"] + a["width"]
    b_right = b["left"] + b["width"]
    left = min(a["left"], b["left"])
    top = min(a["top"], b["top"])
    right = max(a_right, b_right)
    bottom = max(block_bottom(a), block_bottom(b))

    merged_text = append_text_with_overlap(a.get("text", ""), b.get("text", ""))
    a["text"] = merged_text
    a["source_text"] = append_text_with_overlap(
        a.get("source_text", a.get("text", "")),
        b.get("source_text", b.get("text", "")),
    )
    a["left"] = left
    a["top"] = top
    a["width"] = right - left
    a["height"] = bottom - top
    a["category"] = "Text"
    a["band_merged"] = True
    return a


def merge_overlapping_retry_blocks(blocks):
    ordered = sorted(
        blocks,
        key=lambda b: (is_header_footer(b), b["top"], b["left"], b.get("order", 0)),
    )

    changed = True
    while changed:
        changed = False
        merged = []
        for block in ordered:
            if merged and retry_blocks_should_merge(merged[-1], block):
                merged[-1] = merge_retry_block_pair(merged[-1], block)
                changed = True
            else:
                merged.append(block)
        ordered = merged

    for index, block in enumerate(ordered):
        block["order"] = index
    return ordered


MARGIN_CLAMP_BLOCK_CATEGORIES = {
    "Text",
    "List-item",
    "Footnote",
    "Caption",
    "Formula",
    "Table",
}


def clamp_split_retry_block_to_page_margins(block, page_width):
    """Clamp abnormal split retry text boxes back into a safe body text area.

    This is intentionally applied only to pages whose OCR result came from
    split retry. It prevents a single over-wide band bbox from making the
    restored text run all the way to the physical page edge.
    """
    if block.get("category") not in MARGIN_CLAMP_BLOCK_CATEGORIES:
        return block

    safe_left = page_width * 0.065
    safe_right = page_width * 0.935

    left = float(block["left"])
    right = float(block["left"] + block["width"])
    original_left = left
    original_right = right

    if left < safe_left:
        left = safe_left
    if right > safe_right:
        right = safe_right

    if right <= left + 20:
        right = min(page_width - safe_left, left + max(20.0, float(block["width"])))

    if left != original_left or right != original_right:
        block["left"] = left
        block["width"] = right - left
        block["split_margin_clamped"] = True

    return block


def clamp_split_retry_blocks_to_page_margins(blocks, page_width):
    return [clamp_split_retry_block_to_page_margins(block, page_width) for block in blocks]


def min_line_height_for_block(block):
    category = block["category"]
    if category in {"Page-header", "Page-footer"} or is_numeric_page_marker(clean_block_text(block)):
        return 0.82
    if category in {"Title", "Section-header", "Caption"}:
        return 0.86
    if category in {"Footnote", "Formula", "Table"}:
        return 0.82
    if category == "List-item":
        return 0.88
    return 0.88


def preferred_line_height_for_block(block):
    category = block["category"]
    clean_text = clean_block_text(block)

    if category in {"Page-header", "Page-footer"} or is_numeric_page_marker(clean_text):
        return 1.00
    if category in {"Title", "Section-header"}:
        return 1.10
    if category == "Caption":
        return 1.08
    if category == "Footnote":
        return 1.08
    if category == "List-item":
        return 1.16
    if category in {"Formula", "Table"}:
        return 1.05
    if is_short_running_text(block):
        return 1.05
    return 1.20


def body_text_target_font_size(body_size):
    return float(body_size)


def block_has_meaningful_horizontal_overlap(a, b):
    return horizontal_overlap_ratio(a, b) >= 0.12


def next_flow_block_top(block, blocks):
    bottom_limit = None
    block_top = float(block["top"])
    for other in blocks:
        if other is block:
            continue
        if is_header_footer(other):
            continue
        if is_numeric_page_marker(clean_block_text(other)):
            continue
        if float(other.get("top", 0.0)) <= block_top + 2.0:
            continue
        if not block_has_meaningful_horizontal_overlap(block, other):
            continue
        other_top = float(other["top"])
        bottom_limit = other_top if bottom_limit is None else min(bottom_limit, other_top)
    return bottom_limit


def available_body_bottom(block, blocks, page_height):
    next_top = next_flow_block_top(block, blocks)
    if next_top is not None:
        return max(float(block["top"]) + 1.0, next_top - FLOW_BLOCK_GAP)
    return page_height * 0.92


def should_expand_compressed_body_block(block, page_width, body_size):
    if block.get("category") != "Text":
        return False
    clean_text = clean_block_text(block)
    if len(clean_text) < COMPRESSED_BODY_MIN_CHARS:
        return False
    if is_numeric_page_marker(clean_text):
        return False
    if float(block.get("width", 0.0)) < page_width * COMPRESSED_BODY_MIN_WIDTH_RATIO:
        return False

    target_font = body_text_target_font_size(body_size)
    target_line_height = preferred_line_height_for_block(block)
    needed = estimate_required_height(block, target_font, target_line_height)
    if needed <= float(block.get("height", 0.0)) * COMPRESSED_BODY_HEIGHT_EXPAND_RATIO:
        return False

    bbox_font = estimate_bbox_font_size(block)
    return bbox_font < target_font * 0.72


def expand_compressed_body_block(block, blocks, page_width, page_height, body_size):
    if not should_expand_compressed_body_block(block, page_width, body_size):
        return

    target_font = body_text_target_font_size(body_size)
    target_line_height = preferred_line_height_for_block(block)
    needed = estimate_required_height(block, target_font, target_line_height) * 1.04
    available_bottom = available_body_bottom(block, blocks, page_height)
    available_height = max(1.0, available_bottom - float(block["top"]))
    current_height = float(block["height"])

    if available_height <= current_height * 1.08:
        return

    expanded_height = min(needed, available_height)
    if expanded_height <= current_height * 1.08:
        return

    block["height"] = expanded_height
    block["compressed_body_bbox_expanded"] = True
    block["compressed_body_original_height"] = current_height


def textbox_rect(block):
    return fitz.Rect(
        block["left"],
        block["top"],
        block["left"] + block["width"],
        block["top"] + block["height"],
    )


def block_align(block):
    return fitz.TEXT_ALIGN_CENTER if block["category"] in {"Title", "Section-header", "Caption"} else fitz.TEXT_ALIGN_LEFT


def fit_block_to_bbox(block, page_height):
    text = normalize_markdown_text(block["text"])
    if not text:
        block["strict_bbox_fit"] = True
        return

    rect = textbox_rect(block)
    if rect.is_empty or rect.width <= 0 or rect.height <= 0:
        block["strict_bbox_fit"] = False
        return

    font_size, line_height, ok = reportlab_fit_box_params(
        page_height,
        rect,
        block,
        float(block["font_size"]),
        float(block["min_font_size"]),
        float(block["line_height"]),
        block_align(block),
    )

    block["font_size"] = round(font_size, 2)
    block["line_height"] = round(line_height, 3)
    block["strict_bbox_fit"] = ok


def reflow_overflow_pair(first, second, page_height):
    union_top = min(first["top"], second["top"])
    union_bottom = max(block_bottom(first), block_bottom(second))
    union_height = max(1.0, union_bottom - union_top)
    gap = min(FLOW_BLOCK_GAP, union_height * 0.03)
    available = max(1.0, union_height - gap)

    first_need = max(
        1.0,
        estimate_required_height(first, first["min_font_size"], min_line_height_for_block(first)),
    )
    second_need = max(
        1.0,
        estimate_required_height(second, second["min_font_size"], min_line_height_for_block(second)),
    )
    first_height = available * first_need / (first_need + second_need)
    second_height = available - first_height

    min_first = max(10.0, first["min_font_size"] * min_line_height_for_block(first))
    min_second = max(10.0, second["min_font_size"] * min_line_height_for_block(second))
    if first_height < min_first:
        first_height = min_first
        second_height = max(min_second, available - first_height)
    if second_height < min_second:
        second_height = min_second
        first_height = max(min_first, available - second_height)

    first["top"] = union_top
    first["height"] = min(first_height, page_height - first["top"])
    second["top"] = first["top"] + first["height"] + gap
    second["height"] = min(second_height, page_height - second["top"])

    fit_block_to_bbox(first, page_height)
    fit_block_to_bbox(second, page_height)
    first["pair_reflowed"] = True
    second["pair_reflowed"] = True


def resolve_strict_bbox_overflows(blocks, page_height):
    ordered = sorted(blocks, key=lambda b: (is_header_footer(b), b["top"], b["left"], b["order"]))
    flow_blocks = [block for block in ordered if not is_header_footer(block)]

    for index, block in enumerate(flow_blocks):
        if block.get("strict_bbox_fit"):
            continue
        partner = None
        for candidate in flow_blocks[index + 1:]:
            if horizontal_overlap_ratio(block, candidate) >= 0.20:
                partner = candidate
                break
        if partner is None:
            continue
        reflow_overflow_pair(block, partner, page_height)

    return sorted(ordered, key=lambda b: (is_header_footer(b), b["top"], b["left"], b["order"]))



def prepare_blocks(blocks, page_width, page_height):
    reportlab_register_fonts()
    body_size = estimate_page_body_size(blocks, page_height)
    body_font_floor = scaled_body_text_font_floor(page_height)
    prepared = []
    for block in blocks:
        repair_misclassified_header_footer_block(block, page_width, page_height, body_size)
        expand_compressed_body_block(block, blocks, page_width, page_height, body_size)
        block["original_left"] = block["left"]
        block["original_top"] = block["top"]
        block["original_width"] = block["width"]
        block["original_height"] = block["height"]
        font_size = estimate_font_size(block, body_size)
        if is_body_text_block(block):
            font_size = max(font_size, min(body_font_floor, body_size))
            block["enforce_body_font_floor"] = True
            block["body_text_font_floor"] = body_font_floor
        clean_text = clean_block_text(block)
        line_height = estimate_line_height(block, font_size)
        need_height = estimate_required_height(block, font_size, line_height)
        if block["category"] == "Footnote":
            min_font = max(4.8, font_size * 0.64)
        elif block["category"] == "Text":
            if block.get("enforce_body_font_floor"):
                min_font = max(5.2, font_size * 0.64)
            else:
                min_font = max(5.2, font_size * 0.64)
        else:
            min_font = max(5.0, font_size * 0.72)

        if block.get("enforce_body_font_floor") and need_height > block["height"] * 0.96:
            visual_lines = estimate_visual_line_count(block, font_size)
            compact_line_height = block["height"] * 0.96 / max(font_size * visual_lines, 1)
            compact_line_height = clamp(compact_line_height, BODY_TEXT_FLOOR_LINE_HEIGHT_MIN, line_height)
            line_height = compact_line_height
            need_height = estimate_required_height(block, font_size, line_height)

        while need_height > block["height"] * 0.96 and font_size > min_font:
            font_size -= 0.25
            need_height = estimate_required_height(block, font_size, line_height)

        block["font_size"] = round(font_size, 2)
        block["min_font_size"] = round(min_font, 2)
        block["line_height"] = line_height
        fit_block_to_bbox(block, page_height)
        if block["font_size"] < block["min_font_size"]:
            block["min_font_size"] = block["font_size"]
        prepared.append(block)
    return resolve_strict_bbox_overflows(prepared, page_height)


def table_block_to_markdown(block):
    rows = table_rows_for_block(block)
    if not rows:
        return normalize_text(block["text"])

    if is_toc_table(rows):
        lines = []
        for row in rows:
            left_text, right_text = table_row_parts(row)
            if not left_text and not right_text:
                continue
            indent = "  " * table_row_level(row, left_text)
            if right_text:
                lines.append(f"{indent}{left_text} ...... {right_text}".rstrip())
            else:
                lines.append(f"{indent}{left_text}".rstrip())
        return "\n".join(lines).strip()

    normalized_rows = []
    max_cols = 0
    for row in rows:
        cells = row.get("cells") or row.get("nonempty_cells") or []
        cells = [normalize_text(cell) for cell in cells]
        if any(cells):
            normalized_rows.append(cells)
            max_cols = max(max_cols, len(cells))
    if not normalized_rows:
        return ""

    padded = [row + [""] * (max_cols - len(row)) for row in normalized_rows]
    header = padded[0]
    separator = ["---"] * max_cols
    body = padded[1:]
    markdown_rows = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(separator) + " |",
    ]
    markdown_rows.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(markdown_rows).strip()


def blocks_to_markdown_page(blocks, page_index):
    lines = []
    for block in blocks:
        if block["category"] in {"Page-header", "Page-footer"}:
            continue
        category = block["category"]
        if category == "Table":
            text = table_block_to_markdown(block)
        else:
            text = normalize_text(block["text"])
        if not text:
            continue
        if category == "Title":
            lines.append(f"# {text}")
        elif category == "Section-header":
            lines.append(f"## {text}")
        elif category == "Caption":
            lines.append(f"*{text}*")
        elif category == "List-item":
            if re.match(r"^\s*(?:[-*+]|\d+[.)])\s+", text):
                lines.append(text)
            else:
                lines.append(f"- {text}")
        elif category == "Formula":
            lines.append("```")
            lines.append(text)
            lines.append("```")
        else:
            lines.append(text)
        lines.append("")
    return "\n".join(lines).strip()


def markdown_page_from_result(result, blocks, page_index):
    md_nohf_text = normalize_markdown_text(result.get("md_nohf_text", ""))
    if md_nohf_text:
        return md_nohf_text
    return blocks_to_markdown_page(blocks, page_index)


def write_book_markdown(md_path, markdown_pages):
    content = "\n\n".join(page for page in markdown_pages if page.strip())
    md_path.write_text(content + "\n", encoding="utf-8")


def category_render_policy(category):
    policies = {
        "Caption": "caption text block; centered in PDF; italicized in generated Markdown fallback",
        "Footnote": "small footnote block with stricter font bounds; included in PDF and nohf Markdown",
        "Formula": "formula text block; strict bbox rendering; fenced code block in generated Markdown fallback",
        "List-item": "list Markdown line; bullet/number formatting preserved",
        "Page-footer": "page footer rendered in PDF; excluded from generated Markdown fallback",
        "Page-header": "page header rendered in PDF; excluded from generated Markdown fallback",
        "Picture": "ignored unless OCR supplies textual content",
        "Section-header": "section heading; centered/bold PDF; ## in generated Markdown fallback",
        "Table": "cell-aware table rendering; TOC vs plain table auto-detected from original cell data; structured Markdown fallback",
        "Text": "body text block; strict bbox rendering",
        "Title": "title heading; centered/bold PDF; # in generated Markdown fallback",
    }
    return policies.get(category, "generic text block; strict bbox rendering")


def log_category_audit(category_counts, md_nohf_pages, total_pages):
    log("    Layout category audit:")
    for category in LAYOUT_CATEGORIES:
        count = category_counts.get(category, 0)
        log(f"        {category}: {count} - {category_render_policy(category)}")
    extra = sorted(set(category_counts) - set(LAYOUT_CATEGORIES))
    for category in extra:
        log(f"        {category}: {category_counts[category]} - {category_render_policy(category)}")
    log(f"    Markdown source: nohf pages={md_nohf_pages}/{total_pages}; fallback pages={total_pages - md_nohf_pages}")


def reportlab_register_fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    global REPORTLAB_FONTS_REGISTERED
    if REPORTLAB_FONTS_REGISTERED:
        return

    require_file(CJK_REGULAR_FONT, "CJK regular TTF font not found")
    require_file(CJK_MEDIUM_FONT, "CJK medium TTF font not found")
    require_file(CJK_BOLD_FONT, "CJK bold TTF font not found")
    require_file(LATIN_REGULAR_FONT, "Latin regular font not found")
    require_file(LATIN_BOLD_FONT, "Latin bold font not found")
    require_file(LATIN_ITALIC_FONT, "Latin italic font not found")
    require_file(GREEK_REGULAR_FONT, "Greek regular font not found")
    require_file(GREEK_BOLD_FONT, "Greek bold font not found")
    require_file(GREEK_ITALIC_FONT, "Greek italic font not found")
    require_file(MONO_FONT, "Mono font not found")

    pdfmetrics.registerFont(TTFont("SourceHanSerifCN-Regular", str(CJK_REGULAR_FONT)))
    pdfmetrics.registerFont(TTFont("SourceHanSerifCN-Medium", str(CJK_MEDIUM_FONT)))
    pdfmetrics.registerFont(TTFont("SourceHanSerifCN-Bold", str(CJK_BOLD_FONT)))

    pdfmetrics.registerFont(TTFont("Latin-Regular", str(LATIN_REGULAR_FONT)))
    pdfmetrics.registerFont(TTFont("Latin-Bold", str(LATIN_BOLD_FONT)))
    pdfmetrics.registerFont(TTFont("Latin-Italic", str(LATIN_ITALIC_FONT)))

    pdfmetrics.registerFont(TTFont("Greek-Regular", str(GREEK_REGULAR_FONT)))
    pdfmetrics.registerFont(TTFont("Greek-Bold", str(GREEK_BOLD_FONT)))
    pdfmetrics.registerFont(TTFont("Greek-Italic", str(GREEK_ITALIC_FONT)))

    pdfmetrics.registerFont(TTFont("Mono", str(MONO_FONT)))

    latin_chars = set(pdfmetrics.getFont("Latin-Regular").face.charToGlyph.keys())
    greek_chars = set(pdfmetrics.getFont("Greek-Regular").face.charToGlyph.keys())
    missing_fractions = [
        char for char in VULGAR_FRACTION_CHARS
        if ord(char) not in latin_chars and ord(char) not in greek_chars
    ]
    if missing_fractions:
        raise RuntimeError(
            "Registered Latin/Greek fallback fonts do not cover vulgar fraction characters: "
            + " ".join(f"{char}(U+{ord(char):04X})" for char in missing_fractions)
        )

    REPORTLAB_FONTS_REGISTERED = True

LATIN_LIKE_RE = re.compile(
    r"[A-Za-z0-9"
    r"\u00C0-\u024F"   # 拉丁扩展，含 ä é ö ü 等
    r"\u1E00-\u1EFF"   # 更多拉丁扩展
    r"\u0250-\u02AF"   # IPA extensions
    r"\u1D00-\u1DBF"   # Phonetic extensions
    r"\u2C60-\u2C7F"   # Latin Extended-C
    r"\uA720-\uA7FF"   # Latin Extended-D
    r"\uAB30-\uAB6F"   # Latin Extended-E
    r"]"
)

GREEK_LIKE_RE = re.compile(
    r"[\u0370-\u03FF"   # 希腊字母
    r"\u1F00-\u1FFF"   # 希腊扩展，含 polytonic Greek
    r"]"
)

CYRILLIC_LIKE_RE = re.compile(
    r"[\u0400-\u052F"   # Cyrillic + Supplement
    r"\u2DE0-\u2DFF"   # Cyrillic Extended-A
    r"\uA640-\uA69F"   # Cyrillic Extended-B
    r"\u1C80-\u1C8F"   # Cyrillic Extended-C
    r"\u1E030-\u1E08F" # Cyrillic Extended-D
    r"]"
)

COMBINING_MARK_RE = re.compile(
    r"[\u0300-\u036F"   # Combining Diacritical Marks
    r"\u1AB0-\u1AFF"   # Combining Diacritical Marks Extended
    r"\u1DC0-\u1DFF"   # Combining Diacritical Marks Supplement
    r"\u20D0-\u20FF"   # Combining Diacritical Marks for Symbols
    r"\uFE20-\uFE2F"   # Combining Half Marks
    r"]"
)

CJK_LIKE_RE = re.compile(
    r"[\u3400-\u9fff"
    r"\u3000-\u303f"
    r"\uff00-\uffef"
    r"]"
)

FONT_COVERAGE_CACHE = {}


def font_supports_char(fontname, ch):
    from reportlab.pdfbase import pdfmetrics

    if not ch:
        return True
    if fontname not in FONT_COVERAGE_CACHE:
        reportlab_register_fonts()
        font = pdfmetrics.getFont(fontname)
        FONT_COVERAGE_CACHE[fontname] = set(font.face.charToGlyph.keys())
    return ord(ch) in FONT_COVERAGE_CACHE[fontname]


def serif_script_for_char(ch):
    # SourceSerif is the primary unified serif family for Latin, Greek, and Cyrillic.
    # DejaVu Serif is used only as a glyph-safety fallback to avoid U+0000 extraction.
    if font_supports_char("Latin-Regular", ch):
        return "latin"
    if font_supports_char("Greek-Regular", ch):
        return "serif-fallback"
    return "latin"


def char_script(ch):
    if COMBINING_MARK_RE.match(ch):
        return "combining"
    if ch in GREEK_SPACING_MARKS:
        return "serif-fallback"
    if LATIN_LIKE_RE.match(ch) or GREEK_LIKE_RE.match(ch) or CYRILLIC_LIKE_RE.match(ch):
        return serif_script_for_char(ch)
    if CJK_LIKE_RE.match(ch):
        return "cjk"
    if ch.isspace():
        return "neutral"
    if ord(ch) < 128:
        return "neutral"
    if font_supports_char("Latin-Regular", ch):
        return "latin"
    if font_supports_char("Greek-Regular", ch):
        return "serif-fallback"
    return "cjk"


def split_reportlab_font_runs(segments):
    result = []

    for seg in segments:
        text = seg.get("text", "")
        if not text:
            continue

        current = ""
        current_script = None

        for ch in text:
            script = char_script(ch)

            if script == "combining":
                script = current_script or "latin"

            if script == "neutral":
                script = current_script or "cjk"

            if current and script != current_script:
                new_seg = dict(seg)
                new_seg["text"] = current
                new_seg["script"] = current_script
                result.append(new_seg)

                current = ch
                current_script = script
            else:
                current += ch
                current_script = script

        if current:
            new_seg = dict(seg)
            new_seg["text"] = current
            new_seg["script"] = current_script or "cjk"
            result.append(new_seg)

    return result

def reportlab_font_for_segment(seg, line_style):
    style = seg.get("style", "normal")
    script = seg.get("script", "cjk")

    if script == "latin":
        if style == "code":
            return "Mono", False
        if style == "math":
            return "Latin-Italic", False
        if style in {"strong", "strongitalic"} or line_is_bold(line_style):
            return "Latin-Bold", False
        if style == "italic" or line_is_italic(line_style):
            return "Latin-Italic", False
        return "Latin-Regular", False

    if script in {"greek", "serif-fallback"}:
        if style == "code":
            return "Greek-Regular", False
        if style == "math":
            return "Greek-Italic", False
        if style in {"strong", "strongitalic"} or line_is_bold(line_style):
            return "Greek-Bold", False
        if style == "italic" or line_is_italic(line_style):
            return "Greek-Italic", False
        return "Greek-Regular", False

    if style in {"strong", "strongitalic"} or line_style in {"strong", "heading1", "heading2"}:
        return "SourceHanSerifCN-Bold", False

    if line_style in {"heading3", "heading4", "heading5", "heading6"}:
        return "SourceHanSerifCN-Medium", False

    if style == "code":
        return "SourceHanSerifCN-Regular", False

    return "SourceHanSerifCN-Regular", False


@lru_cache(maxsize=32768)
def reportlab_text_width(text, fontname, fontsize):
    from reportlab.pdfbase import pdfmetrics

    return pdfmetrics.stringWidth(text, fontname, fontsize)


def reportlab_plain_segments(text):
    text = normalize_draw_segment_text(text)
    if not text:
        return []
    return split_reportlab_font_runs([{"text": text, "style": "normal"}])


@lru_cache(maxsize=8192)
def reportlab_plain_text_width(text, fontsize):
    total = 0.0
    for seg in reportlab_plain_segments(text):
        fontname, _fake_bold = reportlab_font_for_segment(seg, "normal")
        total += reportlab_text_width(seg["text"], fontname, fontsize)
    return total


def reportlab_draw_plain_text(c, x, baseline, text, fontsize):
    cursor = x
    for seg in reportlab_plain_segments(text):
        seg_text = seg["text"]
        if not seg_text:
            continue
        fontname, fake_bold = reportlab_font_for_segment(seg, "normal")
        reportlab_draw_text(c, cursor, baseline, seg_text, fontname, fontsize, fake_bold)
        cursor += reportlab_text_width(seg_text, fontname, fontsize)


def reportlab_split_to_fit(text, fontname, fontsize, available_width):
    if reportlab_text_width(text, fontname, fontsize) <= available_width:
        return text, ""
    if available_width <= fontsize:
        return "", text
    current = ""
    for ch in text:
        trial = current + ch
        if current and reportlab_text_width(trial, fontname, fontsize) > available_width:
            return current, text[len(current):]
        current = trial
    return current, ""


def reportlab_split_plain_lines(text, fontname, fontsize, available_width):
    text = normalize_draw_segment_text(text)
    if not text:
        return []

    lines = []
    pending = text
    while pending:
        if reportlab_plain_text_width(pending, fontsize) <= available_width:
            fit_text, rest = pending, ""
        else:
            fit_text = ""
            for index, ch in enumerate(pending):
                trial = fit_text + ch
                if fit_text and reportlab_plain_text_width(trial, fontsize) > available_width:
                    rest = pending[index:]
                    break
                fit_text = trial
            else:
                rest = ""
            if not fit_text:
                fit_text, rest = pending[:1], pending[1:]
        lines.append(fit_text)
        pending = rest
    return lines


def reportlab_toc_table_layout(rect, rows, fontsize, line_height):
    toc_rows = []
    for row in rows:
        left_text, right_text = table_row_parts(row)
        if left_text or right_text:
            toc_rows.append(
                {
                    "left_text": left_text,
                    "right_text": right_text,
                    "level": table_row_level(row, left_text),
                }
            )
    if not toc_rows:
        return []

    min_line_gap = fontsize * max(1.0, line_height)
    natural_line_gap = rect.height / max(len(toc_rows), 1)
    line_gap = clamp(natural_line_gap, min_line_gap, fontsize * 2.15)
    effective_fontsize = min(fontsize, line_gap * 0.72)

    right_width = 0.0
    for row in toc_rows:
        right_text = row["right_text"]
        if right_text:
            right_width = max(right_width, reportlab_plain_text_width(right_text, effective_fontsize))
    if right_width:
        right_width = min(right_width + effective_fontsize * 0.6, rect.width * 0.22)
    gap = effective_fontsize * 0.8 if right_width else 0.0
    left_width = rect.width - right_width - gap
    if left_width <= effective_fontsize * 3:
        return None

    y_top = rect.y0
    layout = []
    for row in toc_rows:
        indent = min(left_width * 0.28, row["level"] * effective_fontsize * 1.6)
        available_left_width = max(effective_fontsize * 3, left_width - indent)
        left_lines = reportlab_split_plain_lines(
            row["left_text"],
            None,
            effective_fontsize,
            available_left_width,
        ) or [""]
        row_height = len(left_lines) * line_gap
        if y_top + row_height > rect.y1 + 0.01:
            return None
        layout.append(
            {
                "y_top": y_top,
                "indent": indent,
                "left_lines": left_lines,
                "right_text": row["right_text"],
                "right_width": right_width,
                "line_gap": line_gap,
                "fontsize": effective_fontsize,
            }
        )
        y_top += row_height
    return {"mode": "toc", "rows": layout}


def table_column_count(rows):
    return max((row.get("col_count", len(row.get("cells", []))) for row in rows), default=1)


def reportlab_plain_table_layout(rect, rows, fontsize, line_height):
    if not rows:
        return []

    col_count = table_column_count(rows)
    col_gap = fontsize * 0.8
    usable_width = rect.width - col_gap * max(0, col_count - 1)
    if usable_width <= fontsize * col_count:
        return None

    col_width = usable_width / col_count
    line_gap = fontsize * line_height
    y_top = rect.y0
    layout = []
    for row in rows:
        cells = row.get("cells") or row.get("nonempty_cells") or []
        cell_lines = []
        max_lines = 1
        for col_index in range(col_count):
            cell = cells[col_index] if col_index < len(cells) else ""
            lines = reportlab_split_plain_lines(cell, None, fontsize, col_width) or [""]
            cell_lines.append(lines)
            max_lines = max(max_lines, len(lines))
        row_height = max_lines * line_gap
        if y_top + row_height > rect.y1 + 0.01:
            return None
        layout.append(
            {
                "y_top": y_top,
                "cell_lines": cell_lines,
                "col_width": col_width,
                "col_gap": col_gap,
                "line_gap": line_gap,
                "fontsize": fontsize,
            }
        )
        y_top += row_height
    return {"mode": "plain", "rows": layout}


def reportlab_grid_table_layout(rect, rows, fontsize, line_height):
    if not rows:
        return []

    line_gap = fontsize * max(0.92, min(line_height, 1.10))
    y_top = rect.y0
    layout = []
    for row in rows:
        cells = row.get("cells") or row.get("nonempty_cells") or []
        nonempty = [normalize_text(cell) for cell in cells if normalize_text(cell)]
        if not nonempty:
            continue
        indent = min(rect.width * 0.22, float(row.get("first_col", 0)) * fontsize * 1.3)
        row_text = "    ".join(nonempty)
        lines = reportlab_split_plain_lines(row_text, None, fontsize, max(fontsize * 3, rect.width - indent)) or [""]
        row_height = len(lines) * line_gap
        if y_top + row_height > rect.y1 + 0.01:
            return None
        layout.append(
            {
                "y_top": y_top,
                "indent": indent,
                "lines": lines,
                "line_gap": line_gap,
                "fontsize": fontsize,
            }
        )
        y_top += row_height
    return {"mode": "grid", "rows": layout}


def reportlab_table_layout(rect, block_or_text, fontsize, line_height):
    rows = table_rows_for_block(block_or_text)
    if not rows:
        return []
    if is_toc_table(rows):
        layout = reportlab_toc_table_layout(rect, rows, fontsize, line_height)
        if layout is not None:
            return layout
    layout = reportlab_plain_table_layout(rect, rows, fontsize, line_height)
    if layout is not None:
        return layout
    return reportlab_grid_table_layout(rect, rows, fontsize, line_height)


def reportlab_draw_table_block(c, page_height, rect, block_or_text, fontsize, line_height, dry_run=False):
    text = block_or_text.get("text", "") if isinstance(block_or_text, dict) else str(block_or_text or "")
    text = normalize_markdown_text(text)
    assert_no_radicals_in_text(text, "reportlab table block")
    layout = reportlab_table_layout(rect, block_or_text, fontsize, line_height)
    if layout is None:
        return False
    if dry_run:
        return True

    if layout == []:
        return True

    if layout["mode"] == "plain":
        for row in layout["rows"]:
            y_top = row["y_top"]
            line_gap = row["line_gap"]
            fontsize = row["fontsize"]
            for col_index, lines in enumerate(row["cell_lines"]):
                x = rect.x0 + col_index * (row["col_width"] + row["col_gap"])
                for line_index, line in enumerate(lines):
                    if not line:
                        continue
                    baseline = page_height - (y_top + line_index * line_gap) - fontsize
                    reportlab_draw_plain_text(c, x, baseline, line, fontsize)
        return True

    if layout["mode"] == "grid":
        if isinstance(block_or_text, dict):
            block_or_text["table_structural_fallback"] = True
        for row in layout["rows"]:
            y_top = row["y_top"]
            line_gap = row["line_gap"]
            fontsize = row["fontsize"]
            x = rect.x0 + row.get("indent", 0.0)
            for line_index, line in enumerate(row["lines"]):
                if not line:
                    continue
                baseline = page_height - (y_top + line_index * line_gap) - fontsize
                reportlab_draw_plain_text(c, x, baseline, line, fontsize)
        return True

    for row in layout["rows"]:
        y_top = row["y_top"]
        line_gap = row["line_gap"]
        fontsize = row["fontsize"]
        for line_index, left_text in enumerate(row["left_lines"]):
            baseline = page_height - y_top - fontsize
            x_left = rect.x0 + row.get("indent", 0.0)
            if left_text:
                reportlab_draw_plain_text(c, x_left, baseline, left_text, fontsize)
            if line_index == 0 and row["right_text"]:
                right_width = reportlab_plain_text_width(row["right_text"], fontsize)
                right_x = rect.x1 - right_width
                left_width = reportlab_plain_text_width(left_text, fontsize) if left_text else 0.0
                dot_start = x_left + left_width + fontsize * 0.7
                dot_end = right_x - fontsize * 0.7
                dot_width = reportlab_text_width(".", "Latin-Regular", fontsize)
                if dot_end > dot_start + dot_width:
                    dot_count = int((dot_end - dot_start) / dot_width)
                    reportlab_draw_text(c, dot_start, baseline, "." * dot_count, "Latin-Regular", fontsize)
                reportlab_draw_plain_text(c, right_x, baseline, row["right_text"], fontsize)
            y_top += line_gap
    return True


def reportlab_draw_text(c, x, y, text, fontname, fontsize, fake_bold=False):
    text = text_for_single_pdf_draw(text)
    if not text:
        return
    if fake_bold:
        c.saveState()
        c.setLineWidth(max(0.25, fontsize * 0.035))
        c.setStrokeColorRGB(0, 0, 0)
        c.setFillColorRGB(0, 0, 0)
        text_obj = c.beginText(x, y)
        text_obj.setFont(fontname, fontsize)
        text_obj.setTextRenderMode(2)
        text_obj.textOut(text)
        c.drawText(text_obj)
        c.restoreState()
        return
    c.setFont(fontname, fontsize)
    c.drawString(x, y, text)


def block_page_rect(block):
    return fitz.Rect(
        block["left"],
        block["top"],
        block["left"] + block["width"],
        block["top"] + block["height"],
    )


def line_info_rect(block, line):
    bbox = line.get("bbox") if isinstance(line, dict) else None
    if not isinstance(bbox, list) or len(bbox) != 4:
        return None

    scale_x = float(block.get("bbox_scale_x", 1.0))
    scale_y = float(block.get("bbox_scale_y", 1.0))
    x0, y0, x1, y1 = [float(value) for value in bbox]
    rect = fitz.Rect(x0 * scale_x, y0 * scale_y, x1 * scale_x, y1 * scale_y)
    parent = block_page_rect(block)
    rect = rect & parent
    if rect.is_empty or rect.width <= 0 or rect.height <= 0:
        return None
    return rect


def usable_line_info(block):
    if not USE_LINE_INFO_RENDERING:
        return []
    if block.get("category") == "Table":
        return []
    lines = []
    for line in block.get("line_info") or []:
        text = normalize_markdown_text(line.get("text", ""))
        if not text.strip():
            continue
        rect = line_info_rect(block, line)
        if rect is None:
            continue
        text_parts = [part.strip() for part in re.split(r"\n+", text) if part.strip()]
        if len(text_parts) > 1:
            part_height = rect.height / len(text_parts)
            for part_index, part in enumerate(text_parts):
                part_rect = fitz.Rect(
                    rect.x0,
                    rect.y0 + part_height * part_index,
                    rect.x1,
                    rect.y0 + part_height * (part_index + 1),
                )
                lines.append({"text": part, "rect": part_rect, "spans": []})
            continue
        lines.append({"text": text, "rect": rect, "spans": line.get("spans") or []})

    if not lines:
        return []

    clean_text = clean_block_text(block)
    if len(lines) == 1:
        # MinerU preproc sometimes stores a whole paragraph as one giant line.
        # Treat that as paragraph text, not as a real line coordinate.
        if len(clean_text) > 80 and lines[0]["rect"].height > block["height"] * 0.45:
            return []
        return lines

    lines.sort(key=lambda item: (item["rect"].y0, item["rect"].x0))
    return lines


def reportlab_segments_for_line(text):
    segments, indent, line_style = markdown_line_to_segments(text)
    return split_reportlab_font_runs(segments), indent, line_style


def reportlab_measure_segments(segments, line_style, fontsize):
    total = 0.0
    line_fontsize = fontsize * line_font_scale(line_style)
    for seg in segments:
        seg_text = normalize_draw_segment_text(seg["text"], strip_markdown=seg["style"] != "code")
        if not seg_text:
            continue
        fontname, _fake_bold = reportlab_font_for_segment(seg, line_style)
        total += reportlab_text_width(seg_text, fontname, line_fontsize)
    return total


def reportlab_fit_line_fontsize(rect, text, start_fontsize, min_fontsize):
    segments, indent, line_style = reportlab_segments_for_line(text)
    if not segments:
        return start_fontsize, segments, indent, line_style

    fontsize = min(float(start_fontsize), max(3.2, rect.height * 0.86))
    min_fontsize = max(3.2, float(min_fontsize))
    while fontsize >= min_fontsize - 0.001:
        line_fontsize = fontsize * line_font_scale(line_style)
        width = reportlab_measure_segments(segments, line_style, fontsize)
        if line_fontsize <= rect.height + 0.01 and width + indent <= rect.width + 0.01:
            return fontsize, segments, indent, line_style
        fontsize -= 0.2

    emergency = min(min_fontsize, max(2.6, rect.height * 0.82))
    while emergency >= 2.6:
        line_fontsize = emergency * line_font_scale(line_style)
        width = reportlab_measure_segments(segments, line_style, emergency)
        if line_fontsize <= rect.height + 0.01 and width + indent <= rect.width + 0.01:
            return emergency, segments, indent, line_style
        emergency -= 0.15
    return None, segments, indent, line_style


def reportlab_draw_line_in_rect(c, page_height, rect, text, start_fontsize, min_fontsize, align, dry_run=False):
    fontsize, segments, indent, line_style = reportlab_fit_line_fontsize(rect, text, start_fontsize, min_fontsize)
    if fontsize is None:
        return False
    if not segments:
        return True

    line_fontsize = fontsize * line_font_scale(line_style)
    line_width = reportlab_measure_segments(segments, line_style, fontsize)
    available_width = max(1.0, rect.width - indent)
    if align == fitz.TEXT_ALIGN_CENTER and line_width < available_width:
        x = rect.x0 + indent + (available_width - line_width) / 2
    else:
        x = rect.x0 + indent
    y_top = rect.y0 + max(0.0, (rect.height - line_fontsize) / 2)
    baseline = page_height - y_top - line_fontsize

    if dry_run:
        return True

    for seg in segments:
        seg_text = normalize_draw_segment_text(seg["text"], strip_markdown=seg["style"] != "code")
        if not seg_text:
            continue
        fontname, fake_bold = reportlab_font_for_segment(seg, line_style)
        width = reportlab_text_width(seg_text, fontname, line_fontsize)
        if x + width > rect.x1 + 0.01:
            return False
        reportlab_draw_text(c, x, baseline, seg_text, fontname, line_fontsize, fake_bold)
        x += width
    return True


def reportlab_draw_line_info_block(c, page_height, block, dry_run=False, fontsize=None, min_fontsize=None):
    lines = usable_line_info(block)
    if not lines:
        return None

    align = block_align(block)
    start_fontsize = float(fontsize if fontsize is not None else block["font_size"])
    min_fontsize = float(
        min_fontsize
        if min_fontsize is not None
        else block.get("min_font_size", max(3.8, start_fontsize * 0.72))
    )

    for line in lines:
        if not reportlab_draw_line_in_rect(
            c,
            page_height,
            line["rect"],
            line["text"],
            start_fontsize,
            min_fontsize,
            align,
            dry_run=dry_run,
        ):
            return False
    return True


def normalize_narrow_vertical_text(text):
    text = normalize_markdown_text(text)
    return re.sub(r"\s+", " ", text).strip()


def is_narrow_vertical_text_block(block_or_text, rect, text):
    if isinstance(block_or_text, dict) and block_or_text.get("category") == "Table":
        return False
    if rect.width > 24.0:
        return False
    if rect.height < rect.width * 4.0:
        return False
    text = normalize_narrow_vertical_text(text)
    if not text:
        return False
    if len(text) > 160:
        return False
    if is_numeric_page_marker(text):
        return False
    return True


def reportlab_rotated_narrow_fontsize(rect, text, start_fontsize, min_fontsize):
    text = normalize_narrow_vertical_text(text)
    segments, indent, line_style = reportlab_segments_for_line(text)
    if not segments:
        return None, [], 0.0, "normal", 0.0

    scale = max(0.01, line_font_scale(line_style))
    available_length = max(1.0, rect.height - 1.5)
    available_thickness = max(1.0, rect.width - 1.0)
    max_by_thickness = available_thickness * 0.78 / scale

    current = min(float(start_fontsize), max_by_thickness)
    emergency_floor = 2.2
    min_fontsize = min(float(min_fontsize), current)
    min_fontsize = max(emergency_floor, min_fontsize)

    while current >= emergency_floor - 0.001:
        line_fontsize = current * scale
        measured = indent + reportlab_measure_segments(segments, line_style, current)
        if line_fontsize <= available_thickness + 0.01 and measured <= available_length + 0.01:
            return current, segments, indent, line_style, measured
        current -= 0.15 if current < min_fontsize else 0.25

    return None, segments, indent, line_style, 0.0


def reportlab_draw_rotated_narrow_textbox(
    c,
    page_height,
    rect,
    block_or_text,
    fontsize,
    min_fontsize,
    dry_run=False,
):
    text = block_or_text.get("text", "") if isinstance(block_or_text, dict) else str(block_or_text or "")
    text = normalize_narrow_vertical_text(text)
    if not is_narrow_vertical_text_block(block_or_text, rect, text):
        return False

    fontsize, segments, indent, line_style, measured = reportlab_rotated_narrow_fontsize(
        rect,
        text,
        fontsize,
        min_fontsize,
    )
    if fontsize is None:
        return False
    if dry_run:
        return True

    line_fontsize = fontsize * line_font_scale(line_style)
    baseline_x = rect.x0 + rect.width * 0.78
    start_y = page_height - rect.y1 + max(0.0, (rect.height - measured) / 2.0)
    cursor = indent

    c.saveState()
    c.translate(baseline_x, start_y)
    c.rotate(90)
    for seg in segments:
        seg_text = normalize_draw_segment_text(seg["text"], strip_markdown=seg["style"] != "code")
        if not seg_text:
            continue
        fontname, fake_bold = reportlab_font_for_segment(seg, line_style)
        width = reportlab_text_width(seg_text, fontname, line_fontsize)
        reportlab_draw_text(c, cursor, 0, seg_text, fontname, line_fontsize, fake_bold)
        cursor += width
    c.restoreState()
    return True


def reportlab_markdown_textbox_layout(page_height, rect, text, fontsize, line_height, align):
    text = normalize_markdown_text(text)
    assert_no_radicals_in_text(text, "reportlab markdown textbox")
    lines = re.split(r"\n+", text) if text else []
    y_top = rect.y0
    layout = []

    for line in lines:
        segments, indent, line_style = markdown_line_to_segments(line)
        segments = split_reportlab_font_runs(segments)
        line_fontsize = fontsize * line_font_scale(line_style)
        line_gap = line_fontsize * line_height
        if not segments:
            y_top += line_gap
            continue
        if y_top + line_gap > rect.y1:
            return None

        pending = segments[:]
        visual_line_index = 0
        while pending:
            if y_top + line_gap > rect.y1:
                return None
            effective_indent = indent
            x = rect.x0 + effective_indent
            max_x = rect.x1
            draw_items = []
            line_pending = []
            while pending:
                seg = pending.pop(0)
                seg_text = normalize_draw_segment_text(seg["text"], strip_markdown=seg["style"] != "code")
                if not seg_text:
                    continue
                fontname, fake_bold = reportlab_font_for_segment(seg, line_style)
                fit_text, rest = reportlab_split_to_fit(seg_text, fontname, line_fontsize, max_x - x)
                if not fit_text:
                    line_pending.insert(0, seg)
                    break
                width = reportlab_text_width(fit_text, fontname, line_fontsize)
                draw_items.append((fit_text, fontname, fake_bold, width, line_fontsize))
                x += width
                if rest:
                    line_pending.insert(0, {
                        "text": rest,
                        "style": seg["style"],
                        "script": seg.get("script", "cjk"),
                    })
                    break

            line_width = sum(item[3] for item in draw_items)
            if align == fitz.TEXT_ALIGN_CENTER and line_width < (rect.width - effective_indent):
                x = rect.x0 + effective_indent + ((rect.width - effective_indent) - line_width) / 2
            else:
                x = rect.x0 + effective_indent

            baseline = page_height - y_top - line_fontsize
            row_items = []
            for fit_text, fontname, fake_bold, width, item_fontsize in draw_items:
                row_items.append((x, baseline, fit_text, fontname, item_fontsize, fake_bold))
                x += width
            layout.append(row_items)
            pending = line_pending + pending
            y_top += line_gap
            visual_line_index += 1
    return layout


def reportlab_draw_markdown_textbox(c, page_height, rect, text, fontsize, line_height, align, dry_run=False):
    layout = reportlab_markdown_textbox_layout(page_height, rect, text, fontsize, line_height, align)
    if layout is None:
        return False
    if dry_run:
        return True

    for row_items in layout:
        for x, baseline, fit_text, fontname, item_fontsize, fake_bold in row_items:
            reportlab_draw_text(c, x, baseline, fit_text, fontname, item_fontsize, fake_bold)
    return True


def reportlab_fit_fontsize(page_height, rect, text, fontsize, min_size, line_height, align):
    fontsize, _line_height, _ok = reportlab_fit_box_params(
        page_height,
        rect,
        text,
        fontsize,
        min_size,
        line_height,
        align,
    )
    return fontsize


def reportlab_box_fits(page_height, rect, block_or_text, fontsize, line_height, align):
    if isinstance(block_or_text, dict) and block_or_text.get("category") == "Table":
        return reportlab_table_fits(page_height, rect, block_or_text, fontsize, line_height)
    text = block_or_text.get("text", "") if isinstance(block_or_text, dict) else str(block_or_text or "")
    min_fontsize = (
        block_or_text.get("min_font_size", max(3.2, float(fontsize) * 0.72))
        if isinstance(block_or_text, dict)
        else max(3.2, float(fontsize) * 0.72)
    )
    if reportlab_draw_rotated_narrow_textbox(
        None,
        page_height,
        rect,
        block_or_text,
        fontsize,
        min_fontsize,
        dry_run=True,
    ):
        return True
    if isinstance(block_or_text, dict):
        line_ok = reportlab_draw_line_info_block(
            None,
            page_height,
            block_or_text,
            dry_run=True,
            fontsize=fontsize,
            min_fontsize=block_or_text.get("min_font_size"),
        )
        if line_ok is True:
            return True
    return reportlab_textbox_fits(page_height, rect, text, fontsize, line_height, align)


def line_height_candidates(start_line_height, preferred_line_height, min_line_height, line_step):
    candidates = []

    current = max(start_line_height, preferred_line_height)
    while current >= preferred_line_height - 0.001:
        candidates.append(current)
        current -= line_step

    current = min(preferred_line_height - line_step, start_line_height - line_step)
    while current >= min_line_height - 0.001:
        candidates.append(current)
        current -= line_step

    candidates.append(min_line_height)
    result = []
    seen = set()
    for value in candidates:
        value = round(max(min_line_height, value), 3)
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def reportlab_fit_box_params(page_height, rect, block_or_text, fontsize, min_size, line_height, align):
    if isinstance(block_or_text, dict):
        min_line_height = min_line_height_for_block(block_or_text)
        preferred_line_height = preferred_line_height_for_block(block_or_text)
        enforce_body_font_floor = bool(block_or_text.get("enforce_body_font_floor"))
    else:
        min_line_height = 0.82
        preferred_line_height = 1.16
        enforce_body_font_floor = False

    min_size = max(3.8, float(min_size))
    start_fontsize = float(fontsize)
    start_line_height = max(float(line_height), preferred_line_height)
    font_step = 0.25 if start_fontsize < 9 else 0.35
    line_step = 0.035
    body_font_floor = (
        float(block_or_text.get("body_text_font_floor", BODY_TEXT_FONT_FLOOR))
        if isinstance(block_or_text, dict)
        else BODY_TEXT_FONT_FLOOR
    )
    soft_font_floor = min(start_fontsize, body_font_floor) if enforce_body_font_floor else min_size
    first_pass_min_size = soft_font_floor if enforce_body_font_floor else min_size

    for current_line_height in line_height_candidates(
        start_line_height,
        preferred_line_height,
        min_line_height,
        line_step,
    ):
        current_font = start_fontsize
        while current_font >= first_pass_min_size - 0.001:
            if reportlab_box_fits(page_height, rect, block_or_text, current_font, current_line_height, align):
                return current_font, current_line_height, True
            current_font -= font_step

    if enforce_body_font_floor:
        emergency_min_line_height = BODY_TEXT_FLOOR_LINE_HEIGHT_MIN
        current_line_height = min_line_height - line_step
        while current_line_height >= emergency_min_line_height - 0.001:
            current_font = start_fontsize
            while current_font >= soft_font_floor - 0.001:
                if reportlab_box_fits(page_height, rect, block_or_text, current_font, current_line_height, align):
                    return current_font, current_line_height, True
                current_font -= font_step
            current_line_height -= line_step

    emergency_min_size = 3.2
    emergency_min_line_height = 0.68
    current_line_height = min_line_height - line_step
    while current_line_height >= emergency_min_line_height - 0.001:
        current_font = min(soft_font_floor - font_step, start_fontsize) if enforce_body_font_floor else min(min_size, start_fontsize)
        while current_font >= emergency_min_size - 0.001:
            if reportlab_box_fits(page_height, rect, block_or_text, current_font, current_line_height, align):
                return current_font, current_line_height, True
            current_font -= font_step
        current_line_height -= line_step

    return emergency_min_size, emergency_min_line_height, False


def reportlab_textbox_fits(page_height, rect, text, fontsize, line_height, align):
    from reportlab.pdfgen import canvas
    from io import BytesIO

    c = canvas.Canvas(BytesIO(), pagesize=(rect.x1 + 1, page_height))
    return reportlab_draw_markdown_textbox(
        c,
        page_height,
        rect,
        text,
        fontsize,
        line_height,
        align,
        dry_run=True,
    )


def reportlab_table_fits(page_height, rect, block_or_text, fontsize, line_height):
    from reportlab.pdfgen import canvas
    from io import BytesIO

    c = canvas.Canvas(BytesIO(), pagesize=(rect.x1 + 1, page_height))
    return reportlab_draw_table_block(
        c,
        page_height,
        rect,
        block_or_text,
        fontsize,
        line_height,
        dry_run=True,
    )


def reportlab_block_fit_failure(page_height, block):
    if block.get("category") == "ImageFallback":
        return None

    text = normalize_markdown_text(block.get("text", ""))
    if not text:
        return None

    rect = fitz.Rect(
        block["left"],
        block["top"],
        block["left"] + block["width"],
        block["top"] + block["height"],
    )
    if rect.is_empty or rect.width <= 0 or rect.height <= 0:
        return None

    fontsize = float(block["font_size"])
    min_size = float(block.get("min_font_size", max(6.0, fontsize * 0.86)))
    line_height = float(block["line_height"])
    align = block_align(block)
    _fontsize, _line_height, ok = reportlab_fit_box_params(
        page_height,
        rect,
        block,
        fontsize,
        min_size,
        line_height,
        align,
    )
    if ok:
        return None

    snippet = re.sub(r"\s+", " ", text).strip()[:120]
    return {
        "category": block.get("category"),
        "order": block.get("order"),
        "bbox": [round(rect.x0, 1), round(rect.y0, 1), round(rect.x1, 1), round(rect.y1, 1)],
        "text": snippet,
    }


def reportlab_page_fit_failures(page_height, blocks):
    reportlab_register_fonts()
    failures = []
    for block in blocks:
        failure = reportlab_block_fit_failure(page_height, block)
        if failure:
            failures.append(failure)
    return failures


def render_blocks_to_pdf_reportlab(
    page_specs,
    output_pdf,
    formula_work_dir=None,
    include_full_page_images=True,
    allow_formula_image_fallback=True,
):
    from reportlab.pdfgen import canvas

    reportlab_register_fonts()
    formula_blocks_present = any(
        is_formula_render_block(block)
        for _page_width, _page_height, blocks in page_specs
        for block in blocks
    )
    if formula_blocks_present:
        formula_render_dependencies()
    if formula_work_dir is None:
        formula_work_dir = TMP_DIR / "formula_render_cache"

    c = canvas.Canvas(str(output_pdf), pagesize=(page_specs[0][0], page_specs[0][1]))
    for page_no, (page_width, page_height, blocks) in enumerate(page_specs):
        c.setPageSize((page_width, page_height))
        for block in blocks:
            if block.get("category") == "ImageFallback":
                if include_full_page_images:
                    image_path = block.get("image_path")
                    if image_path and Path(image_path).exists():
                        c.drawImage(
                            str(image_path),
                            0,
                            0,
                            width=page_width,
                            height=page_height,
                            preserveAspectRatio=True,
                            anchor="c",
                            mask="auto",
                        )
                continue

            rect = fitz.Rect(
                block["left"],
                block["top"],
                block["left"] + block["width"],
                block["top"] + block["height"],
            )
            if rect.is_empty or rect.width <= 0 or rect.height <= 0:
                continue

            if is_formula_render_block(block):
                if render_formula_block(
                    c,
                    page_height,
                    rect,
                    block,
                    formula_work_dir,
                    allow_image_fallback=allow_formula_image_fallback,
                ):
                    continue

            text = normalize_markdown_text(block.get("text", ""))
            if LATEX_RESIDUE_RE.search(text) or (not text and is_formula_render_block(block)):
                text = normalize_markdown_text(linearize_latex_formula(text or formula_source_text(block)))
                block["text"] = text
                # Structured line fragments still contain the pre-normalized
                # OCR payload and would bypass the safe textbox text below.
                block["line_info"] = []
            if LATEX_RESIDUE_RE.search(text):
                raise RuntimeError(
                    "LaTeX residue remains after formula fallback normalization: "
                    f"page={page_no + 1}, order={block.get('order')}, text={text[:160]!r}"
                )
            assert_no_radicals_in_text(text, f"page block order={block.get('order')}")
            if not text:
                continue

            fontsize = float(block["font_size"])
            min_size = float(block.get("min_font_size", max(6.0, fontsize * 0.86)))
            line_height = float(block["line_height"])
            align = block_align(block)
            fontsize, line_height, ok = reportlab_fit_box_params(
                page_height,
                rect,
                block,
                fontsize,
                min_size,
                line_height,
                align,
            )
            if not ok:
                snippet = re.sub(r"\s+", " ", text).strip()[:120]
                raise RuntimeError(
                    "Unable to fit OCR text inside its page bbox without clipping: "
                    f"page={page_no + 1}, category={block.get('category')}, "
                    f"order={block.get('order')}, bbox="
                    f"[{rect.x0:.1f}, {rect.y0:.1f}, {rect.x1:.1f}, {rect.y1:.1f}], "
                    f"text={snippet!r}"
                )

            if reportlab_draw_rotated_narrow_textbox(
                c,
                page_height,
                rect,
                block,
                fontsize,
                min_size,
                dry_run=True,
            ):
                drawn = reportlab_draw_rotated_narrow_textbox(
                    c,
                    page_height,
                    rect,
                    block,
                    fontsize,
                    min_size,
                )
            elif block["category"] == "Table":
                drawn = reportlab_draw_table_block(c, page_height, rect, block, fontsize, line_height)
            else:
                line_drawn = reportlab_draw_line_info_block(
                    c,
                    page_height,
                    block,
                    fontsize=fontsize,
                    min_fontsize=min_size,
                )
                if line_drawn is None:
                    drawn = reportlab_draw_markdown_textbox(c, page_height, rect, text, fontsize, line_height, align)
                elif line_drawn is False:
                    drawn = reportlab_draw_markdown_textbox(c, page_height, rect, text, fontsize, line_height, align)
                else:
                    drawn = line_drawn
            if not drawn:
                snippet = re.sub(r"\s+", " ", text).strip()[:120]
                raise RuntimeError(
                    "ReportLab draw refused a block that passed fitting: "
                    f"page={page_no + 1}, category={block.get('category')}, "
                    f"order={block.get('order')}, text={snippet!r}"
                )
        # Commit every source page explicitly. ReportLab's save() only commits
        # the current page when it has drawing commands, so a trailing blank
        # source page would otherwise be omitted from the output PDF.
        c.showPage()
    c.save()


SUSPECTED_MARKDOWN_RE = re.compile(
    r"("
    r"\*\*[^*\n]{1,120}\*\*"
    r"|__[^_\n]{1,120}__"
    r"|(?<!\*)\*[^*\n]{1,120}\*(?!\*)"
    r"|^\s{0,3}#{1,6}\s+\S+"
    r"|`[^`\n]{1,120}`"
    r")",
    re.MULTILINE,
)


def scan_pdf_validation(pdf_path, include_markdown=None, checks=None):
    if include_markdown is None:
        include_markdown = DEBUG_MARKDOWN_WARNINGS
    if checks is None:
        checks = {"radicals", "control_chars", "latex_residue"}
    else:
        checks = set(checks)
    if include_markdown:
        checks.add("suspected_markdown")

    scan = {
        "page_count": 0,
        "image_pages": [],
        "text_char_counts": [],
        "radical_offenders": {},
        "control_char_offenders": {},
        "latex_residue_offenders": {},
        "suspected_markdown_offenders": {},
    }
    with fitz.open(pdf_path) as doc:
        scan["page_count"] = doc.page_count
        if not checks:
            return scan
        for page_index, page in enumerate(doc):
            page_no = page_index + 1
            text = page.get_text()
            scan["text_char_counts"].append(len(text.strip()))
            if page.get_images(full=True):
                scan["image_pages"].append(page_no)

            if "radicals" in checks:
                radical_chars = sorted(set(RADICAL_CHAR_RE.findall(text)))
                if radical_chars:
                    scan["radical_offenders"][page_no] = radical_chars

            if "control_chars" in checks:
                control_chars = sorted(set(CONTROL_CHAR_RE.findall(text)))
                if control_chars:
                    scan["control_char_offenders"][page_no] = control_chars

            if "latex_residue" in checks:
                latex_matches = []
                for match in LATEX_RESIDUE_RE.finditer(text):
                    snippet = text[max(0, match.start() - 45): match.end() + 45]
                    latex_matches.append(re.sub(r"\s+", " ", snippet).strip())
                    if len(latex_matches) >= 4:
                        break
                if latex_matches:
                    scan["latex_residue_offenders"][page_no] = latex_matches

            if "suspected_markdown" in checks:
                markdown_matches = sorted(set(SUSPECTED_MARKDOWN_RE.findall(text)))
                if markdown_matches:
                    scan["suspected_markdown_offenders"][page_no] = markdown_matches[:5]
    return scan


def validate_pdf_has_no_radicals(pdf_path, validation_scan=None):
    validation_scan = (
        validation_scan
        if validation_scan is not None
        else scan_pdf_validation(pdf_path, checks={"radicals"})
    )
    offenders = validation_scan["radical_offenders"]

    if offenders:
        details = "; ".join(
            f"page {page_no}: {''.join(chars)}" for page_no, chars in offenders.items()
        )
        raise RuntimeError(
            "Output PDF still contains Kangxi/CJK radical characters after normalization: "
            f"{details}. Delete the output PDF and regenerate after checking normalize_text()."
        )


def validate_pdf_page_count(pdf_path, expected_page_count, validation_scan=None):
    validation_scan = (
        validation_scan
        if validation_scan is not None
        else scan_pdf_validation(pdf_path, include_markdown=False, checks=set())
    )
    actual_page_count = validation_scan["page_count"]
    if actual_page_count != expected_page_count:
        raise RuntimeError(
            "Output PDF page count does not match the source PDF: "
            f"expected={expected_page_count}, actual={actual_page_count}, output={pdf_path}"
        )


def validate_pdf_has_no_raster_images(pdf_path, validation_scan=None):
    validation_scan = validation_scan if validation_scan is not None else scan_pdf_validation(pdf_path)
    image_pages = validation_scan.get("image_pages", [])
    if image_pages:
        raise RuntimeError(
            "Text-only output PDF contains raster images on pages: "
            + ", ".join(str(page_no) for page_no in image_pages[:40])
        )


def validate_pdf_pages_are_blank(pdf_path, page_indices, validation_scan=None):
    validation_scan = validation_scan if validation_scan is not None else scan_pdf_validation(pdf_path)
    text_counts = validation_scan.get("text_char_counts", [])
    image_pages = set(validation_scan.get("image_pages", []))
    offenders = []
    for page_index in page_indices:
        page_no = int(page_index) + 1
        text_count = text_counts[page_index] if 0 <= page_index < len(text_counts) else None
        if text_count not in (0, None) or page_no in image_pages:
            offenders.append(page_no)
    if offenders:
        raise RuntimeError(
            "Text-only output did not keep fallback pages blank: "
            + ", ".join(str(page_no) for page_no in offenders)
        )


def validate_pdf_has_images_on_pages(pdf_path, page_indices, validation_scan=None):
    validation_scan = validation_scan if validation_scan is not None else scan_pdf_validation(pdf_path)
    image_pages = set(validation_scan.get("image_pages", []))
    missing = [int(page_index) + 1 for page_index in page_indices if int(page_index) + 1 not in image_pages]
    if missing:
        raise RuntimeError(
            "Image-variant PDF is missing required source-page images on pages: "
            + ", ".join(str(page_no) for page_no in missing)
        )


def validate_pdf_has_no_control_chars(pdf_path, validation_scan=None):
    validation_scan = (
        validation_scan
        if validation_scan is not None
        else scan_pdf_validation(pdf_path, checks={"control_chars"})
    )
    offenders = validation_scan["control_char_offenders"]

    if offenders:
        details = "; ".join(
            f"page {page_no}: "
            + " ".join(f"U+{ord(ch):04X}" for ch in chars)
            for page_no, chars in offenders.items()
        )
        raise RuntimeError(
            "Output PDF still contains control characters after normalization: "
            f"{details}. Delete the output PDF and regenerate after checking line splitting."
        )


def validate_pdf_has_no_latex_residue(pdf_path, validation_scan=None):
    validation_scan = (
        validation_scan
        if validation_scan is not None
        else scan_pdf_validation(pdf_path, checks={"latex_residue"})
    )
    offenders = validation_scan["latex_residue_offenders"]

    if offenders:
        details = "; ".join(
            f"page {page_no}: {matches}" for page_no, matches in offenders.items()
        )
        raise RuntimeError(
            "Output PDF still contains LaTeX/formula markup after normalization: "
            f"{details}. Delete the output PDF and regenerate after checking normalize_safe_latex_markup()."
        )


def warn_pdf_suspected_markdown(pdf_path, validation_scan=None):
    if not DEBUG_MARKDOWN_WARNINGS:
        return

    validation_scan = (
        validation_scan
        if validation_scan is not None
        else scan_pdf_validation(
            pdf_path,
            include_markdown=True,
            checks={"suspected_markdown"},
        )
    )
    offenders = validation_scan["suspected_markdown_offenders"]

    if offenders:
        details = "; ".join(
            f"page {page_no}: {matches}" for page_no, matches in offenders.items()
        )
        log(f"    Debug suspected markdown warnings: {details}")


def path_for_report(path, base=None):
    if not path:
        return None
    try:
        return str(path.relative_to(base)) if base else str(path)
    except ValueError:
        return str(path)


def page_result_text(result):
    parts = []
    for cell in result.get("cells") or []:
        text = normalize_text(source_text_from_cell(cell))
        if text:
            parts.append(text)
    for key in ("fallback_text", "md_nohf_text"):
        text = normalize_text(result.get(key, ""))
        if text:
            parts.append(text)
    return "\n".join(parts)


def collect_qc_suspect_pages(pdf_path, page_results, page_specs, page_count):
    suspect = {}
    with fitz.open(pdf_path) as doc:
        for page_index in range(page_count):
            issues = []
            result = page_results.get(page_index, {})
            text = page_result_text(result)
            if result.get("source_page_retry"):
                issues.append("page_range_retry")
            if result.get("pseudotext_detected") or page_result_has_high_repeated_pseudotext(result):
                issues.append("high_repeated_pseudotext")
            if result.get("image_fallback_page") or result.get("image_fallback_path"):
                issues.append("image_fallback_page")
            if result.get("layout_fit_failures"):
                issues.append("layout_block_could_not_fit_bbox")
            if result.get("table_disabled_retry"):
                issues.append("table_recognition_disabled_page_retry")
            if result.get("blank_page"):
                issues.append("blank_page_output_cleared")
            if page_result_only_font_test_text(result):
                issues.append("font_test_pangram")
            if result.get("needs_retry"):
                issues.append("retry_still_needed")
            if result.get("fallback_text") and not result.get("cells"):
                issues.append("markdown_fallback_only")
            if text and RADICAL_CHAR_RE.search(text):
                issues.append("radical_chars_in_ocr_text")
            if text and CONTROL_CHAR_RE.search(text):
                issues.append("control_chars_in_ocr_text")
            if any(phrase in text for phrase in SUSPICIOUS_LAYOUT_PHRASES):
                issues.append("suspicious_placeholder_text")
            if text and len(normalize_text(text)) < 12 and not is_source_page_blank(doc, page_index):
                issues.append("very_low_text_nonblank_source")

            if page_index < len(page_specs):
                _page_width, _page_height, blocks = page_specs[page_index]
                if any(block.get("table_structural_fallback") for block in blocks):
                    issues.append("table_structural_fallback")
                if any(block.get("misclassified_header_footer_repaired") for block in blocks):
                    issues.append("misclassified_header_footer_repaired")
                if any(block.get("category") == "Table" and not block.get("table_rows") for block in blocks):
                    issues.append("table_without_cell_rows")
                formula_modes = {
                    block.get("formula_render_mode")
                    for block in blocks
                    if block.get("formula_render_mode")
                }
                if "unicode_text" in formula_modes:
                    issues.append("formula_unicode_text")
                if "latex_svg" in formula_modes:
                    issues.append("formula_svg_rendered")
                if "linear_text" in formula_modes:
                    issues.append("formula_linear_fallback")
                if "image_crop" in formula_modes:
                    issues.append("formula_image_crop_fallback")
                if any(block.get("formula_render_error") for block in blocks):
                    issues.append("formula_render_error")
                if any(
                    "dependencies missing" in str(block.get("formula_render_error", ""))
                    or "dependency" in str(block.get("formula_render_error", "")).lower()
                    for block in blocks
                ):
                    issues.append("formula_render_dependency_missing")

            if issues:
                suspect[page_index + 1] = sorted(set(issues))
    return suspect


def render_debug_pdf_pages(debug_pdf, pages, output_dir, label):
    rendered = {}
    if not debug_pdf or not debug_pdf.exists():
        return rendered
    output_dir.mkdir(parents=True, exist_ok=True)
    with fitz.open(debug_pdf) as doc:
        for page_no in pages:
            page_index = page_no - 1
            if page_index < 0 or page_index >= doc.page_count:
                continue
            output_path = output_dir / f"page_{page_no:04d}_{label}.png"
            pix = doc[page_index].get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
            pix.save(str(output_path))
            rendered[str(page_no)] = str(output_path)
    return rendered


def mineru_debug_sources(mineru_dir, parser_runs=None):
    runs = parser_runs or [
        {
            "output_dir": mineru_dir,
            "page_offset": 0,
            "start_page": 0,
            "end_page": None,
            "label": "full",
        }
    ]
    sources = []
    for run in runs:
        _all_json_files, json_files, branch_dir = current_mineru_result_files(run["output_dir"])
        layout_pdf = find_mineru_debug_pdf(branch_dir, "layout")
        span_pdf = find_mineru_debug_pdf(branch_dir, "span")
        sources.append(
            {
                "label": run.get("label", "full"),
                "output_dir": run["output_dir"],
                "page_offset": int(run.get("page_offset", 0)),
                "start_page": int(run.get("start_page", 0)),
                "end_page": run.get("end_page"),
                "branch_dir": branch_dir,
                "json_files": json_files,
                "layout_pdf": layout_pdf,
                "span_pdf": span_pdf,
            }
        )
    return sources


def render_debug_pdf_pages_from_sources(sources, pages, output_dir, label):
    rendered = {}
    output_dir.mkdir(parents=True, exist_ok=True)
    for source in sources:
        debug_pdf = source.get(f"{label}_pdf")
        if not debug_pdf or not debug_pdf.exists():
            continue
        offset = int(source.get("page_offset", 0))
        start_page = int(source.get("start_page", 0))
        end_page = source.get("end_page")
        with fitz.open(debug_pdf) as doc:
            for page_no in pages:
                full_index = page_no - 1
                if full_index < start_page:
                    continue
                if end_page is not None and full_index > int(end_page):
                    continue
                local_page_index = full_index - offset
                if local_page_index < 0 or local_page_index >= doc.page_count:
                    continue
                output_path = output_dir / f"page_{page_no:04d}_{label}.png"
                pix = doc[local_page_index].get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                pix.save(str(output_path))
                rendered[str(page_no)] = str(output_path)
    return rendered


def write_mineru_qc_report(
    pdf_path,
    mineru_dir,
    page_results,
    page_specs,
    output_pdf,
    output_image_pdf=None,
    image_fallback_pages=None,
    parser_runs=None,
    output_validation_scan=None,
):
    sources = mineru_debug_sources(mineru_dir, parser_runs)
    first_source = sources[0] if sources else {}
    layout_pdf = first_source.get("layout_pdf")
    span_pdf = first_source.get("span_pdf")

    with fitz.open(pdf_path) as src:
        source_page_count = src.page_count
    output_page_count = None
    if output_pdf.exists():
        if output_validation_scan is not None:
            output_page_count = output_validation_scan["page_count"]
        else:
            with fitz.open(output_pdf) as out:
                output_page_count = out.page_count

    suspect_pages = collect_qc_suspect_pages(pdf_path, page_results, page_specs, source_page_count)
    if output_page_count != source_page_count:
        suspect_pages.setdefault(1, []).append("output_page_count_mismatch")

    qc_book_dir = QC_DIR / pdf_path.stem
    page_numbers = sorted(suspect_pages)
    rendered_layout = render_debug_pdf_pages_from_sources(sources, page_numbers, qc_book_dir, "layout")
    rendered_span = render_debug_pdf_pages_from_sources(sources, page_numbers, qc_book_dir, "span")

    source_reports = []
    for source in sources:
        output_dir = source["output_dir"]
        source_reports.append(
            {
                "label": source["label"],
                "page_start": source["start_page"] + 1,
                "page_end": None if source["end_page"] is None else int(source["end_page"]) + 1,
                "output_dir": str(output_dir),
                "mineru_result_branch": path_for_report(source["branch_dir"], output_dir),
                "primary_json": [path_for_report(path, output_dir) for path in source["json_files"]],
                "layout_pdf": path_for_report(source["layout_pdf"], output_dir),
                "span_pdf": path_for_report(source["span_pdf"], output_dir),
            }
        )

    report = {
        "input_pdf": str(pdf_path),
        "output_pdf": str(output_pdf),
        "output_text_pdf": str(output_pdf),
        "output_image_pdf": str(output_image_pdf) if output_image_pdf else None,
        "image_fallback_pages": [int(page_index) + 1 for page_index in (image_fallback_pages or [])],
        "mineru_output_dir": str(mineru_dir),
        "mineru_sources": source_reports,
        "source_page_count": source_page_count,
        "output_page_count": output_page_count,
        "debug_pdf_page_count_match": {
            "layout": None,
            "span": None,
        },
        "suspect_pages": suspect_pages,
        "rendered_debug_pages": {
            "layout": rendered_layout,
            "span": rendered_span,
        },
    }

    for key, debug_pdf in (("layout", layout_pdf), ("span", span_pdf)):
        if debug_pdf and debug_pdf.exists():
            with fitz.open(debug_pdf) as doc:
                if len(sources) == 1:
                    report["debug_pdf_page_count_match"][key] = doc.page_count == source_page_count
                else:
                    report["debug_pdf_page_count_match"][key] = None

    report_path = LOG_DIR / f"{pdf_path.stem}_qc.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"    QC report: {report_path}")
    if page_numbers:
        log("    QC suspect pages: " + ", ".join(str(page) for page in page_numbers))
    if not any(source.get("span_pdf") for source in sources):
        log("    QC note: MinerU span.pdf was not found; using middle.json line/span data only")
    return report


def same_job_intermediate_files(name):
    mineru_log_prefix = f"{name}_mineru"
    for path in LOG_DIR.iterdir():
        if path.name == f"{name}_qc.json":
            yield path
            continue
        if path.name.startswith(mineru_log_prefix) and path.suffix == ".log":
            yield path


def cleanup_success_artifacts(name, work_dir, mineru_dir, log_path):
    cleanup_paths = [
        work_dir,
        mineru_dir,
        QC_DIR / name,
        *same_job_intermediate_files(name),
    ]
    if log_path not in cleanup_paths:
        cleanup_paths.append(log_path)

    removed = 0
    for cleanup_path in cleanup_paths:
        try:
            if cleanup_path.is_dir():
                shutil.rmtree(cleanup_path)
                removed += 1
            elif cleanup_path.exists():
                cleanup_path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    log(f"    Cleaned intermediate files for this PDF: {removed} path(s)")


def prepare_resumable_job_workspace(pdf_path, work_dir, mineru_dir):
    state_path = work_dir / "job_state.json"
    expected = {
        "schema": MINERU_CHECKPOINT_SCHEMA,
        "source": source_file_signature(pdf_path),
        "parser_config_hash": mineru_parser_config_hash(),
        "runtime_identity_hash": mineru_runtime_identity_hash(),
    }
    existing = read_checkpoint(state_path)
    can_resume = (
        existing is not None
        and all(existing.get(key) == value for key, value in expected.items())
        and work_dir.exists()
        and mineru_dir.exists()
    )

    if can_resume:
        log(f"    Resume workspace found: {work_dir}")
        return True

    if work_dir.exists():
        shutil.rmtree(work_dir)
    if mineru_dir.exists():
        shutil.rmtree(mineru_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    mineru_dir.mkdir(parents=True, exist_ok=True)
    write_checkpoint(
        state_path,
        {
            **expected,
            "status": "active",
            "runtime_identity": mineru_runtime_identity(),
            "updated_at": time.time(),
        },
    )
    return False


def process_pdf(pdf_path, index, total):
    name = pdf_path.stem
    output_pdf = OUTPUT_DIR / f"{name}_mineru.pdf"
    output_image_pdf = OUTPUT_DIR / f"{name}_mineru_with_images.pdf"
    output_md = OUTPUT_DIR / f"{name}_mineru.md"
    output_version = OUTPUT_DIR / f"{name}_mineru.version"
    if (
        SKIP_EXISTING
        and completed_output_state_matches(
            output_version,
            pdf_path,
            output_pdf,
            output_md,
            output_image_pdf,
        )
    ):
        if upgrade_completed_output_runtime_identity(output_version):
            log(f"    Upgraded legacy completion runtime identity: {output_version}")
        state = read_checkpoint(output_version) or {}
        variant_note = " plus image variant" if state.get("has_image_variant") else ""
        log(
            f"[{index}/{total}] Skip existing current-version text output{variant_note} "
            f"before MinerU/VLM startup: {output_pdf}"
        )
        return {
            "status": "skipped",
            "output_text_pdf": str(output_pdf),
            "output_image_pdf": str(output_image_pdf) if state.get("has_image_variant") else None,
        }
    elif SKIP_EXISTING and output_pdf.exists():
        log(f"[{index}/{total}] Existing output is missing current version marker; regenerating: {output_pdf}")

    work_dir = TMP_DIR / name
    mineru_dir = MINERU_OUTPUT_DIR / name
    log_path = LOG_DIR / f"{name}_mineru.log"

    start = time.monotonic()
    log(f"[{index}/{total}] Start: {pdf_path.name}")
    log(f"    Input:  {pdf_path}")
    log(f"    Output: {output_pdf}")
    prepare_resumable_job_workspace(pdf_path, work_dir, mineru_dir)

    src = fitz.open(pdf_path)
    page_count = src.page_count
    if page_count == 0:
        src.close()
        raise RuntimeError(f"Empty PDF: {pdf_path}")
    page_width = float(src[0].rect.width)
    page_height = float(src[0].rect.height)
    src.close()

    log(f"    Pages:  {page_count}")
    log("    [1/5] Running MinerU parser")
    parser_runs = build_mineru_parser_runs(pdf_path, page_count, work_dir, mineru_dir, log_path)

    log("    [2/5] Loading MinerU JSON")
    page_results = collect_parser_run_results(pdf_path, parser_runs)
    page_results = retry_bad_pages(pdf_path, page_results, work_dir, log_path)
    page_results = retry_unfitting_table_pages_as_text_batch(
        pdf_path,
        page_results,
        work_dir,
        log_path,
    )
    suppress_blank_page_outputs(pdf_path, page_results)
    page_results = degrade_repeated_pseudotext_pages_to_images(pdf_path, page_results, work_dir)
    page_results = degrade_unusable_nonblank_pages_to_images(pdf_path, page_results, work_dir)

    log(f"    [3/5] Building layout pages: 0/{page_count}")
    src = fitz.open(pdf_path)

    page_specs = []
    markdown_pages = []
    total_blocks = 0
    category_counts = {}
    md_nohf_pages = 0
    image_fallback_pages = []
    for page_index in range(page_count):
        page = src[page_index]
        page_width = float(page.rect.width)
        page_height = float(page.rect.height)

        result = page_results.get(
            page_index,
            {"cells": [], "fallback_text": "", "filtered": False, "image_size": None},
        )
        blocks = []
        if result.get("image_fallback_path"):
            blocks.append(image_fallback_block(result["image_fallback_path"], page_width, page_height))
            log(f"        Page {page_index + 1}: used source page image fallback")
        else:
            blocks = blocks_from_page_result(result, page_width, page_height)

            retry_reason = result.get("retry_reason", "")
            is_split_retry_page = (
                "split-band" in retry_reason
                and not result.get("source_page_retry")
            )
            if is_split_retry_page and blocks:
                before_count = len(blocks)
                blocks = merge_overlapping_retry_blocks(blocks)
                after_count = len(blocks)
                if after_count < before_count:
                    log(
                        f"        Page {page_index + 1}: merged overlapping split-band blocks "
                        f"{before_count} -> {after_count}"
                    )

                clamped_before = sum(1 for block in blocks if block.get("split_margin_clamped"))
                blocks = clamp_split_retry_blocks_to_page_margins(blocks, page_width)
                clamped_after = sum(1 for block in blocks if block.get("split_margin_clamped"))
                if clamped_after > clamped_before:
                    log(
                        f"        Page {page_index + 1}: clamped split-band block margins "
                        f"for {clamped_after - clamped_before} block(s)"
                    )

            if not blocks and result.get("fallback_text"):
                block = fallback_text_block(result["fallback_text"], 0, page_width, page_height)
                if block:
                    blocks.append(block)
                    log(f"        Page {page_index + 1}: used Markdown fallback because layout JSON was invalid")
            elif result.get("filtered"):
                log(f"        Page {page_index + 1}: MinerU reported filtered/cleaned JSON")

            blocks.sort(key=lambda b: (is_header_footer(b), b["top"], b["left"], b["order"]))
            blocks = prepare_blocks(blocks, page_width, page_height)

            table_blocks = [block for block in blocks if block.get("category") == "Table"]
            fit_failures = reportlab_page_fit_failures(page_height, table_blocks)
            if fit_failures:
                first_failure = fit_failures[0]
                image_dir = work_dir / "layout_image_fallback_pages"
                image_path = image_dir / f"page_{page_index + 1:04d}.png"
                render_pdf_page_to_png(pdf_path, page_index, image_path)
                result["image_fallback_path"] = str(image_path)
                result["image_fallback_page"] = True
                result["layout_fit_failures"] = fit_failures
                append_retry_reason(result, UNFITTING_LAYOUT_IMAGE_FALLBACK_REASON)
                blocks = [image_fallback_block(image_path, page_width, page_height)]
                log(
                    f"        Page {page_index + 1}: used source page image fallback "
                    f"because {first_failure.get('category')} block order={first_failure.get('order')} "
                    "still could not fit after the batched table-off review"
                )
        for block in blocks:
            block["page_index"] = page_index
            block["source_pdf_path"] = str(pdf_path)
            block["formula_cache_dir"] = str(work_dir / "formula_render_cache")
            block["formula_crop_dir"] = str(work_dir / "formula_image_crops")
            category_counts[block["category"]] = category_counts.get(block["category"], 0) + 1
        if any(block.get("category") == "ImageFallback" for block in blocks):
            image_fallback_pages.append(page_index)
        total_blocks += len(blocks)
        log(f"        Layout page {page_index + 1}/{page_count}: blocks={len(blocks)}")
        page_specs.append((page_width, page_height, blocks))
        if normalize_markdown_text(result.get("md_nohf_text", "")):
            md_nohf_pages += 1
        markdown_pages.append(markdown_page_from_result(result, blocks, page_index))

    src.close()

    log_category_audit(category_counts, md_nohf_pages, page_count)
    write_book_markdown(output_md, markdown_pages)

    log("    [4/5] Rendering text-only PDF with ReportLab")
    render_blocks_to_pdf_reportlab(
        page_specs,
        output_pdf,
        formula_work_dir=work_dir / "formula_render_cache",
        include_full_page_images=False,
        allow_formula_image_fallback=False,
    )
    output_validation_scan = scan_pdf_validation(output_pdf)
    validate_pdf_page_count(output_pdf, page_count, output_validation_scan)
    validate_pdf_has_no_raster_images(output_pdf, output_validation_scan)
    validate_pdf_pages_are_blank(output_pdf, image_fallback_pages, output_validation_scan)

    output_image_validation_scan = None
    if image_fallback_pages:
        log(
            f"    Rendering image-variant PDF for {len(image_fallback_pages)} fallback page(s): "
            f"{output_image_pdf}"
        )
        # Rendering records formula fallback modes on blocks for QC. Keep the
        # pure-text page specifications authoritative and isolate mutations
        # made while producing the companion image variant.
        image_page_specs = [
            (page_width, page_height, [dict(block) for block in blocks])
            for page_width, page_height, blocks in page_specs
        ]
        render_blocks_to_pdf_reportlab(
            image_page_specs,
            output_image_pdf,
            formula_work_dir=work_dir / "formula_render_cache",
            include_full_page_images=True,
            allow_formula_image_fallback=True,
        )
        output_image_validation_scan = scan_pdf_validation(output_image_pdf)
        validate_pdf_page_count(output_image_pdf, page_count, output_image_validation_scan)
        validate_pdf_has_images_on_pages(
            output_image_pdf,
            image_fallback_pages,
            output_image_validation_scan,
        )
        validate_pdf_has_no_radicals(output_image_pdf, output_image_validation_scan)
        validate_pdf_has_no_control_chars(output_image_pdf, output_image_validation_scan)
        validate_pdf_has_no_latex_residue(output_image_pdf, output_image_validation_scan)
    elif output_image_pdf.exists():
        output_image_pdf.unlink()
        log(f"    Removed stale image variant because this run has no fallback pages: {output_image_pdf}")

    write_mineru_qc_report(
        pdf_path,
        mineru_dir,
        page_results,
        page_specs,
        output_pdf,
        output_image_pdf=output_image_pdf if image_fallback_pages else None,
        image_fallback_pages=image_fallback_pages,
        parser_runs=parser_runs,
        output_validation_scan=output_validation_scan,
    )
    validate_pdf_has_no_radicals(output_pdf, output_validation_scan)
    validate_pdf_has_no_control_chars(output_pdf, output_validation_scan)
    validate_pdf_has_no_latex_residue(output_pdf, output_validation_scan)
    warn_pdf_suspected_markdown(output_pdf, output_validation_scan)

    log("    [5/5] Done")
    size_mb = output_pdf.stat().st_size / 1024 / 1024
    log(f"    Saved: {output_pdf} ({size_mb:.2f} MB), blocks={total_blocks}, time={format_seconds(time.monotonic() - start)}")
    if image_fallback_pages:
        image_size_mb = output_image_pdf.stat().st_size / 1024 / 1024
        log(
            f"    Saved image variant: {output_image_pdf} ({image_size_mb:.2f} MB), "
            f"fallback_pages={len(image_fallback_pages)}"
        )
    log(f"    Saved Markdown: {output_md}")
    write_completed_output_state(
        output_version,
        pdf_path,
        output_pdf,
        output_md,
        output_image_pdf,
        image_fallback_pages,
    )

    if DELETE_INTERMEDIATE_ON_SUCCESS:
        cleanup_success_artifacts(name, work_dir, mineru_dir, log_path)
    elif CLEAN_TMP_ON_SUCCESS and work_dir.exists():
        shutil.rmtree(work_dir)

    return {
        "status": "completed",
        "output_text_pdf": str(output_pdf),
        "output_image_pdf": str(output_image_pdf) if image_fallback_pages else None,
        "image_fallback_pages": [page_index + 1 for page_index in image_fallback_pages],
    }


def write_batch_summary(started_at, pdfs, results, status, finished_at=None):
    counts = {
        key: sum(1 for result in results if result.get("status") == key)
        for key in ("completed", "skipped", "failed")
    }
    payload = {
        "schema": 1,
        "status": status,
        "started_at": started_at,
        "updated_at": time.time(),
        "finished_at": finished_at,
        "input_dir": str(INPUT_DIR),
        "output_dir": str(OUTPUT_DIR),
        "total_files": len(pdfs),
        "processed_files": len(results),
        "counts": counts,
        "results": results,
    }
    summary_path = LOG_DIR / "batch_summary.json"
    write_checkpoint(summary_path, payload)
    return summary_path, payload


def main():
    pdfs = sorted(INPUT_DIR.glob("*.pdf"))
    if not pdfs:
        log(f"No PDF files found in: {INPUT_DIR}")
        return

    log("MinerU text-only PDF production")
    log(f"Input:  {INPUT_DIR}")
    log(f"Output: {OUTPUT_DIR}")
    log(f"Raw:    {MINERU_OUTPUT_DIR}")
    log(f"PDFs:   {len(pdfs)}")

    started_at = time.time()
    results = []
    write_batch_summary(started_at, pdfs, results, "running")
    for index, pdf_path in enumerate(pdfs, 1):
        file_start = time.monotonic()
        try:
            result = process_pdf(pdf_path, index, len(pdfs)) or {"status": "completed"}
            result = dict(result)
            result["input_pdf"] = str(pdf_path)
            result["elapsed_seconds"] = round(time.monotonic() - file_start, 3)
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            traceback_text = traceback.format_exc()
            log(f"[{index}/{len(pdfs)}] FAILED: {pdf_path.name}: {error_text}")
            log(traceback_text.rstrip())
            result = {
                "status": "failed",
                "input_pdf": str(pdf_path),
                "error": error_text,
                "traceback": traceback_text,
                "log_path": str(LOG_DIR / f"{pdf_path.stem}_mineru.log"),
                "elapsed_seconds": round(time.monotonic() - file_start, 3),
            }
        results.append(result)
        write_batch_summary(started_at, pdfs, results, "running")

    failed = [result for result in results if result.get("status") == "failed"]
    final_status = "completed_with_failures" if failed else "completed"
    summary_path, summary = write_batch_summary(
        started_at,
        pdfs,
        results,
        final_status,
        finished_at=time.time(),
    )
    counts = summary["counts"]
    log(
        "Batch done: "
        f"completed={counts['completed']}, skipped={counts['skipped']}, failed={counts['failed']}"
    )
    log(f"Batch summary: {summary_path}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
