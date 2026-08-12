"""Central runtime paths and production settings for the MinerU pipeline."""

from pathlib import Path
import os
import re

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
OUTPUT_VERSION = "mineru-reportlab-cell-aware-render-v52-forward-page-leak-repair"

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
FORWARD_PAGE_LEAK_RETRY_REASON = "forward page content leak detected; isolated MinerU retry needed"
FORWARD_PAGE_LEAK_SOURCE_TEXT_FALLBACK_REASON = (
    "isolated MinerU retry did not pass page-local validation; used source PDF page text"
)
FORWARD_PAGE_LEAK_IMAGE_FALLBACK_REASON = (
    "isolated MinerU retry did not pass page-local validation and source page had no usable text; "
    "image variant required"
)
FORWARD_PAGE_LEAK_LOOKAHEAD = 5
FORWARD_PAGE_LEAK_NGRAM_SIZE = 16
FORWARD_PAGE_LEAK_MIN_SOURCE_CHARS = 120
FORWARD_PAGE_LEAK_MIN_NEIGHBOR_CHARS = 500
FORWARD_PAGE_LEAK_MIN_OUTPUT_SOURCE_RATIO = 1.25
FORWARD_PAGE_LEAK_MIN_NEIGHBOR_COVERAGE = 0.60
FORWARD_PAGE_LEAK_MIN_RETRY_OWN_COVERAGE = 0.50
FORWARD_PAGE_LEAK_MAX_RETRY_SOURCE_RATIO = 1.35
FORWARD_PAGE_LEAK_RETRY_BATCH_SIZE = 24
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


def ensure_runtime_directories() -> None:
    for directory in (OUTPUT_DIR, MINERU_OUTPUT_DIR, TMP_DIR, LOG_DIR, QC_DIR):
        directory.mkdir(parents=True, exist_ok=True)


__all__ = [name for name in globals() if name.isupper()] + ["ensure_runtime_directories"]
