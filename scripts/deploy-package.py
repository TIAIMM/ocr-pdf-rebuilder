#!/usr/bin/env python3
"""Atomically deploy the complete reviewed Python package."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import py_compile
import shutil
import tempfile


def load_identity_module(repository_root: Path):
    module_path = repository_root / "src/ocr_pdf_rebuilder/code_identity.py"
    spec = importlib.util.spec_from_file_location("ocr_code_identity", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load code identity module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_target(source: Path, target: Path) -> None:
    if source == target:
        raise SystemExit("refusing to deploy over the repository source package")
    if target.name != source.name:
        raise SystemExit(
            f"deployment target must end with {source.name!r}: {target}"
        )
    forbidden = {Path("/"), Path.home().resolve(), source.parent.resolve()}
    if target in forbidden:
        raise SystemExit(f"refusing unsafe deployment target: {target}")


def compile_sources(source: Path, files: list[dict[str, object]]) -> None:
    with tempfile.TemporaryDirectory(prefix="ocr-compile-") as temporary:
        cache_root = Path(temporary)
        for record in files:
            relative = Path(str(record["path"]))
            compiled_path = cache_root / relative.with_suffix(".pyc")
            compiled_path.parent.mkdir(parents=True, exist_ok=True)
            py_compile.compile(
                str(source / relative),
                cfile=str(compiled_path),
                doraise=True,
            )


def copy_manifest_files(
    source: Path,
    destination: Path,
    files: list[dict[str, object]],
) -> None:
    for record in files:
        relative = Path(str(record["path"]))
        source_path = source / relative
        destination_path = destination / relative
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)


def main() -> int:
    repository_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--source",
        type=Path,
        default=repository_root / "src/ocr_pdf_rebuilder",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=repository_root / ".production/src/ocr_pdf_rebuilder",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=repository_root / "provenance/production-baseline.json",
    )
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    target = args.target.expanduser().resolve()
    validate_target(source, target)
    if not args.manifest.is_file():
        parser.error(f"missing baseline manifest: {args.manifest}")

    identity_module = load_identity_module(repository_root)
    source_identity = identity_module.package_source_identity(source)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    expected = manifest.get("package_identity")
    if manifest.get("schema") != 2 or source_identity != expected:
        raise SystemExit(
            "FAIL: repository package is not the recorded production baseline; "
            "run tests, review the change, then update the baseline explicitly"
        )
    compile_sources(source, source_identity["files"])

    try:
        target_identity = identity_module.package_source_identity(target)
    except (FileNotFoundError, RuntimeError):
        target_identity = None
    print(f"source : {source_identity['files_sha256']}  {source}")
    print(
        "target : "
        f"{target_identity['files_sha256'] if target_identity else 'missing'}  {target}"
    )
    if target_identity == source_identity:
        print("NOOP: deployed package already matches the reviewed source package")
        return 0
    if not args.apply:
        print("DRY RUN: rerun with --apply to deploy the complete reviewed package")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.deploy.", dir=target.parent))
    backup = None
    installed = False
    try:
        copy_manifest_files(source, staging, source_identity["files"])
        staged_identity = identity_module.package_source_identity(staging)
        if staged_identity != source_identity:
            raise RuntimeError("staged package identity does not match source")

        if target.exists():
            backup_root = target.parent / ".deploy-backups"
            backup_root.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            old_hash = target_identity["files_sha256"] if target_identity else "unknown"
            backup = backup_root / f"{target.name}.{stamp}.{old_hash}"
            os.replace(target, backup)
            print(f"backup : {backup}")
        os.replace(staging, target)
        installed = True
    except BaseException:
        if backup is not None and backup.exists() and not target.exists():
            os.replace(backup, target)
        raise
    finally:
        if not installed and staging.exists():
            shutil.rmtree(staging)

    deployed_identity = identity_module.package_source_identity(target)
    if deployed_identity != source_identity:
        raise RuntimeError("deployed package identity does not match source")
    print(
        f"DEPLOYED: {deployed_identity['files_sha256']}  "
        f"{deployed_identity['file_count']} Python files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
