#!/usr/bin/env python3
"""Compare the tracked package, recorded baseline and deployed package."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path


def load_identity_module(repository_root: Path):
    module_path = repository_root / "src/ocr_pdf_rebuilder/code_identity.py"
    spec = importlib.util.spec_from_file_location("ocr_code_identity", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load code identity module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    repository_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repository-package",
        type=Path,
        default=repository_root / "src/ocr_pdf_rebuilder",
    )
    parser.add_argument(
        "--production-package",
        type=Path,
        default=repository_root / ".production/src/ocr_pdf_rebuilder",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=repository_root / "provenance/production-baseline.json",
    )
    args = parser.parse_args()

    if not args.manifest.is_file():
        parser.error(f"missing baseline manifest: {args.manifest}")
    identity_module = load_identity_module(repository_root)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    expected = manifest.get("package_identity")
    if manifest.get("schema") != 2 or not isinstance(expected, dict):
        raise SystemExit("FAIL: production baseline manifest schema is obsolete or invalid")

    try:
        repository = identity_module.package_source_identity(args.repository_package)
    except (OSError, RuntimeError) as exc:
        raise SystemExit(f"FAIL: could not inspect repository package: {exc}") from exc
    try:
        production = identity_module.package_source_identity(args.production_package)
    except (OSError, RuntimeError) as exc:
        raise SystemExit(f"FAIL: could not inspect deployed production package: {exc}") from exc

    print(f"recorded baseline : {expected.get('files_sha256')}  {args.manifest}")
    print(f"repository package: {repository['files_sha256']}  {args.repository_package}")
    print(f"production package: {production['files_sha256']}  {args.production_package}")

    if repository != expected:
        raise SystemExit("FAIL: repository package differs from the recorded baseline")
    if production != repository:
        raise SystemExit("FAIL: deployed production package differs from the repository package")
    print(
        "PASS: baseline, repository and deployed production package are byte-identical "
        f"across {repository['file_count']} Python files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
