"""Deterministic source identity for the production Python package."""

from __future__ import annotations

from functools import lru_cache
import hashlib
import json
from pathlib import Path


IDENTITY_SCHEMA = 1
IDENTITY_ALGORITHM = "sha256-canonical-file-manifest-v1"
PACKAGE_NAME = "ocr_pdf_rebuilder"


def _stable_json_hash(payload: object) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_source_identity(package_root: Path | str | None = None) -> dict[str, object]:
    """Return a path-independent identity for every shipped Python source file."""

    root = (
        Path(package_root).expanduser().resolve()
        if package_root is not None
        else Path(__file__).resolve().parent
    )
    if not root.is_dir():
        raise FileNotFoundError(f"Python package directory was not found: {root}")

    files = []
    for path in sorted(root.rglob("*.py"), key=lambda item: item.relative_to(root).as_posix()):
        if "__pycache__" in path.parts or not path.is_file():
            continue
        relative_path = path.relative_to(root).as_posix()
        size = int(path.stat().st_size)
        files.append(
            {
                "path": relative_path,
                "size": size,
                "sha256": _file_sha256(path),
            }
        )
    if not files:
        raise RuntimeError(f"Python package contains no source files: {root}")

    files_sha256 = _stable_json_hash(files)
    return {
        "schema": IDENTITY_SCHEMA,
        "algorithm": IDENTITY_ALGORITHM,
        "package": PACKAGE_NAME,
        "file_count": len(files),
        "files_sha256": files_sha256,
        "files": files,
    }


@lru_cache(maxsize=1)
def current_package_source_identity() -> dict[str, object]:
    return package_source_identity()


def current_package_source_identity_hash() -> str:
    return str(current_package_source_identity()["files_sha256"])


__all__ = [
    "IDENTITY_ALGORITHM",
    "IDENTITY_SCHEMA",
    "PACKAGE_NAME",
    "current_package_source_identity",
    "current_package_source_identity_hash",
    "package_source_identity",
]
