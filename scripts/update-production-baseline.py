#!/usr/bin/env python3
"""Record the reviewed repository package identity as the production baseline."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import tempfile


def load_identity_module(repository_root: Path):
    module_path = repository_root / "src/ocr_pdf_rebuilder/code_identity.py"
    spec = importlib.util.spec_from_file_location("ocr_code_identity", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load code identity module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def atomic_write_json(path: Path, payload: object) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    repository_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repository-package",
        type=Path,
        default=repository_root / "src/ocr_pdf_rebuilder",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=repository_root / "provenance/production-baseline.json",
    )
    args = parser.parse_args()

    identity_module = load_identity_module(repository_root)
    identity = identity_module.package_source_identity(args.repository_package)
    payload = {
        "schema": 2,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "repository_path": "src/ocr_pdf_rebuilder",
        "package_identity": identity,
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(args.manifest, payload)
    print(
        f"RECORDED: {identity['files_sha256']}  "
        f"{identity['file_count']} Python files -> {args.manifest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
