"""Explicit source selection and persistent run binding; no source staging."""

import json
import os
import hashlib
from pathlib import Path


MANIFEST_NAME = ".ocr-run.json"


def state_root(selected):
    return Path(selected.get("state_root") or Path(selected["output_dir"]) / ".ocr-state")


def manifest_path(selected):
    return (state_root(selected) if selected.get("schema", 1) >= 2
            else Path(selected["output_dir"])) / MANIFEST_NAME


def validate_selection(input_files):
    if not isinstance(input_files, list) or not input_files:
        raise ValueError("input_files must be a nonempty list of absolute PDF paths")
    paths = []
    stems = set()
    for value in input_files:
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError("Every input file must be an absolute native path")
        path = Path(value).resolve(strict=True)
        if not path.is_file() or path.suffix.lower() != ".pdf":
            raise ValueError(f"Input is not a PDF file: {path}")
        if path.stem.casefold() in stems:
            raise ValueError("Selected PDFs must have distinct basenames (case insensitive)")
        stems.add(path.stem.casefold())
        paths.append(path)
    return paths


def bind_run(input_files, output_dir, run_id, pipeline):
    if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 256:
        raise ValueError("run_id must be a nonempty string of at most 256 characters")
    if not isinstance(output_dir, str) or not Path(output_dir).is_absolute():
        raise ValueError("output_dir must be an absolute native path")
    paths = validate_selection(input_files)
    output = Path(output_dir).resolve()
    if any(path.is_relative_to(output) for path in paths):
        raise ValueError("Source PDFs must be outside output_dir")
    legacy_manifest = output / MANIFEST_NAME
    legacy = (json.loads(legacy_manifest.read_text(encoding="utf-8"))
              if legacy_manifest.exists() else None)
    is_legacy = legacy is not None and legacy.get("run_id") == run_id
    payload = {
        "schema": 1 if is_legacy else 2, "run_id": run_id, "pipeline": pipeline,
        "output_dir": str(output), "input_files": [str(path) for path in paths],
        "sources": [{"size": p.stat().st_size, "mtime_ns": p.stat().st_mtime_ns} for p in paths],
    }
    if not is_legacy:
        key = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:24]
        payload["state_root"] = str(output / ".ocr-state" / key)
    manifest = manifest_path(payload)
    if manifest.exists():
        if json.loads(manifest.read_text(encoding="utf-8")) != payload:
            raise ValueError("Run manifest differs; use a new run identifier")
    else:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        with manifest.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
    return payload


def selected_manifest():
    value = os.environ.get("OCR_SELECTED_RUN_MANIFEST")
    if not value:
        return None
    payload = json.loads(Path(value).read_text(encoding="utf-8"))
    # Revalidate the source binding immediately before executing the child.
    return bind_run(payload["input_files"], payload["output_dir"], payload["run_id"], payload["pipeline"])


def run_paths(runtime_root, pipeline):
    selected = selected_manifest()
    if selected is None:
        return (runtime_root / f"pdf_{pipeline}", runtime_root / f"{pipeline}_output",
                runtime_root / f"tmp/{pipeline}_textonly_pdf", runtime_root / f"logs_{pipeline}",
                runtime_root / f"qc_{pipeline}")
    output = Path(selected["output_dir"])
    state = state_root(selected) / pipeline
    return output, state / "raw", state / "tmp", state / "logs", state / "qc"
