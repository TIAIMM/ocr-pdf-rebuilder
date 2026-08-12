#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
repository_source="${1:-$repo_root/src/ocr_pdf_rebuilder/pipeline_runtime.py}"
production_source="${2:-$repository_source}"
manifest="$repo_root/provenance/production-baseline.json"

for path in "$repository_source" "$production_source" "$manifest"; do
    if [[ ! -f "$path" ]]; then
        printf 'missing required file: %s\n' "$path" >&2
        exit 2
    fi
done

expected_hash="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["sha256"])' "$manifest")"
repository_hash="$(sha256sum "$repository_source" | awk '{print $1}')"
production_hash="$(sha256sum "$production_source" | awk '{print $1}')"

printf 'recorded baseline : %s\n' "$expected_hash"
printf 'repository source : %s  %s\n' "$repository_hash" "$repository_source"
printf 'production source : %s  %s\n' "$production_hash" "$production_source"

if [[ "$repository_hash" != "$expected_hash" ]]; then
    printf 'FAIL: repository source differs from the recorded baseline\n' >&2
    exit 1
fi
if [[ "$production_hash" != "$repository_hash" ]]; then
    printf 'FAIL: production and repository sources are not byte-identical\n' >&2
    exit 1
fi

printf 'PASS: baseline, repository and production scripts are byte-identical\n'
