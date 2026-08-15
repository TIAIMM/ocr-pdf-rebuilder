"""Runtime identity, fingerprints, checkpoints and completion-state validation."""

from functools import lru_cache
from pathlib import Path
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import sys
import threading
import time

import fitz

from .code_identity import (
    current_package_source_identity,
    current_package_source_identity_hash,
)
from .component_runtime import ComponentRuntime
from .pipeline_config import *

def bool_cli_value(value):
    return "true" if value else "false"


def mineru_parser_config(table_enabled=None):
    return {
        "api": {
            "mode": "external" if MINERU_API_URL else "document_owned_local",
            "url": MINERU_API_URL,
            "host": None if MINERU_API_URL else MINERU_API_HOST,
            "enable_vlm_preload": (
                None if MINERU_API_URL else MINERU_API_ENABLE_VLM_PRELOAD
            ),
            "max_concurrent_requests": (
                None if MINERU_API_URL else MINERU_API_MAX_CONCURRENT_REQUESTS
            ),
        },
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
    api_executable = shutil.which(MINERU_API_COMMAND)
    packages = installed_package_versions()
    command_error = None if executable else f"command not found: {MINERU_COMMAND}"
    api_command_error = (
        None if api_executable else f"command not found: {MINERU_API_COMMAND}"
    )
    executable_signature = None
    api_executable_signature = None
    if executable:
        try:
            executable_signature = file_content_signature(executable)
        except OSError as exc:
            command_error = command_error or f"could not hash executable: {exc}"
    if api_executable:
        try:
            api_executable_signature = file_content_signature(api_executable)
        except OSError as exc:
            api_command_error = api_command_error or f"could not hash executable: {exc}"
    identity = {
        "mineru_command": MINERU_COMMAND,
        "mineru_version_output": packages.get("mineru"),
        "mineru_command_error": command_error,
        "mineru_executable": executable_signature,
        "mineru_api_command": MINERU_API_COMMAND,
        "mineru_api_command_error": api_command_error,
        "mineru_api_executable": api_executable_signature,
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
        "identity_schema": 3,
        "mineru_command": identity.get("mineru_command"),
        "mineru_version_output": identity.get("mineru_version_output"),
        "mineru_command_error": identity.get("mineru_command_error"),
        "mineru_executable": normalized_runtime_file_signature(
            identity.get("mineru_executable")
        ),
        "mineru_api_command": identity.get("mineru_api_command"),
        "mineru_api_command_error": identity.get("mineru_api_command_error"),
        "mineru_api_executable": normalized_runtime_file_signature(
            identity.get("mineru_api_executable")
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
        or state.get("implementation_identity_hash")
        != current_package_source_identity_hash()
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
            "implementation_identity_hash": current_package_source_identity_hash(),
            "implementation_identity": current_package_source_identity(),
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
        "implementation_identity_hash": current_package_source_identity_hash(),
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




_COMPONENT_EXPORTS = (
    "bool_cli_value",
    "mineru_parser_config",
    "mineru_parser_config_hash",
    "stable_json_hash",
    "current_package_source_identity",
    "current_package_source_identity_hash",
    "cached_file_sha256",
    "file_integrity_signature",
    "file_content_signature",
    "source_file_signature",
    "installed_package_versions",
    "mineru_model_identity",
    "mineru_runtime_identity",
    "normalized_runtime_file_signature",
    "normalize_runtime_identity",
    "mineru_runtime_identity_hash",
    "completed_runtime_identity_matches",
    "upgrade_completed_output_runtime_identity",
    "read_checkpoint",
    "write_checkpoint",
    "pdf_artifact_signature",
    "completed_output_state_matches",
    "write_completed_output_state",
    "checkpoint_identity",
    "checkpoint_matches",
    "mineru_output_has_results",
)
_COMPONENT_RUNTIME = ComponentRuntime(globals(), _COMPONENT_EXPORTS)


def component_exports():
    return _COMPONENT_RUNTIME.exports()


def invoke_component(name, namespace, *args, **kwargs):
    return _COMPONENT_RUNTIME.invoke(name, namespace, *args, **kwargs)
