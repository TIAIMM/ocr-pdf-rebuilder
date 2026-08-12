#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
source_path="$repo_root/src/ocr_pdf_rebuilder/pipeline_runtime.py"
target_root="${OCR_DEPLOY_ROOT:-$repo_root/src/ocr_pdf_rebuilder}"
apply=0

usage() {
    cat <<'EOF'
Usage: deploy-to-wsl.sh [--apply] [--target DIRECTORY]

Without --apply the command is a read-only dry run. An applied deployment
backs up changed content and installs the tested source with an atomic rename.
EOF
}

while (( $# > 0 )); do
    case "$1" in
        --apply) apply=1; shift ;;
        --target)
            [[ $# -ge 2 ]] || { usage >&2; exit 2; }
            target_root="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

[[ -f "$source_path" ]] || { printf 'missing source: %s\n' "$source_path" >&2; exit 2; }
target_root="$(realpath -m -- "$target_root")"
case "$target_root/" in
    "$repo_root/"*) ;;
    *) printf 'refusing target outside repository: %s\n' "$target_root" >&2; exit 2 ;;
esac

target_path="$target_root/pipeline_runtime.py"
python_bin="${OCR_PYTHON:-}"
if [[ -z "$python_bin" ]]; then
    if [[ -x "$HOME/miniconda3/envs/mineru/bin/python" ]]; then
        python_bin="$HOME/miniconda3/envs/mineru/bin/python"
    else
        python_bin="$(command -v python3 || true)"
    fi
fi
[[ -n "$python_bin" && -x "$python_bin" ]] || { printf 'missing Python; set OCR_PYTHON\n' >&2; exit 2; }
"$python_bin" - "$source_path" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
compile(path.read_text(encoding="utf-8"), str(path), "exec")
PY

source_hash="$(sha256sum "$source_path" | awk '{print $1}')"
target_hash="missing"
if [[ -f "$target_path" ]]; then
    target_hash="$(sha256sum "$target_path" | awk '{print $1}')"
fi

printf 'source : %s  %s\n' "$source_hash" "$source_path"
printf 'target : %s  %s\n' "$target_hash" "$target_path"

if [[ "$source_hash" == "$target_hash" ]]; then
    printf 'NOOP: target already matches tested source\n'
    exit 0
fi
if (( apply == 0 )); then
    printf 'DRY RUN: rerun with --apply to deploy this exact source\n'
    exit 0
fi

mkdir -p -- "$target_root"
backup_root="$target_root/.deploy-backups"
if [[ -f "$target_path" ]]; then
    mkdir -p -- "$backup_root"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    backup_path="$backup_root/pipeline_runtime.py.$stamp.$target_hash"
    cp --preserve=mode,timestamps -- "$target_path" "$backup_path"
    printf 'backup : %s\n' "$backup_path"
fi

temp_path="$(mktemp "$target_root/.pipeline_runtime.py.deploy.XXXXXX")"
cleanup() { rm -f -- "$temp_path"; }
trap cleanup EXIT
install -m 0644 -- "$source_path" "$temp_path"
temp_hash="$(sha256sum "$temp_path" | awk '{print $1}')"
[[ "$temp_hash" == "$source_hash" ]] || { printf 'temporary copy hash mismatch\n' >&2; exit 1; }
mv -f -- "$temp_path" "$target_path"
trap - EXIT

deployed_hash="$(sha256sum "$target_path" | awk '{print $1}')"
[[ "$deployed_hash" == "$source_hash" ]] || { printf 'deployed hash mismatch\n' >&2; exit 1; }
printf 'DEPLOYED: %s\n' "$deployed_hash"
