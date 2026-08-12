#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$repo_root"

mapfile -t files < <(git diff --cached --name-only --diff-filter=ACMR)
(( ${#files[@]} > 0 )) || { printf 'FAIL: no staged files\n' >&2; exit 1; }

forbidden='(^|/)(input|pdf(_[^/]*)?|mineru_output|paddle_output|paddle_test_output|dots_output|qc_[^/]*|logs_[^/]*|tmp|models|fonts|work|artifacts|\.cache|\.deploy-backups)/|\.(pdf|png|jpe?g|tiff?|bmp|safetensors|pth|onnx|engine|gguf|ttf|ttc|otf|log|pyc)$|(^|/)(job_state|batch_summary)\.json$'
if printf '%s\n' "${files[@]}" | grep -Ei "$forbidden"; then
    printf 'FAIL: forbidden runtime/generated content is staged\n' >&2
    exit 2
fi

largest=0
largest_path=""
for path in "${files[@]}"; do
    size="$(git cat-file -s ":$path")"
    if (( size > largest )); then
        largest="$size"
        largest_path="$path"
    fi
    if (( size > 1048576 )); then
        printf 'FAIL: staged file exceeds 1 MiB: %s (%s bytes)\n' "$path" "$size" >&2
        exit 3
    fi
done

secret_pattern='AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,}|(api[_-]?key|password)[[:space:]]*=[[:space:]]*["'"'][^"'"']+["'"']'
if git grep --cached -nEI -- "$secret_pattern"; then
    printf 'FAIL: potential credential found in staged content\n' >&2
    exit 4
fi

if git diff --cached --numstat | awk '$1 == "-" || $2 == "-" {print; found=1} END {exit found ? 0 : 1}'; then
    printf 'FAIL: unexpected binary content is staged\n' >&2
    exit 5
fi

printf 'PASS: %d staged files audited\n' "${#files[@]}"
printf 'PASS: no runtime/generated/model/font/document artifacts\n'
printf 'PASS: no credential pattern or binary diff detected\n'
printf 'largest staged file: %s bytes  %s\n' "$largest" "$largest_path"
