"""Compare normalized OCR page results from Windows and WSL runs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main() -> None:
    sys.stdout.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows", type=Path, default=Path(__file__).resolve().parents[1] /
                        "paddle_output/Le_Gout_du_secret/page_results")
    parser.add_argument("--wsl", type=Path, default=Path(
        r"\\wsl.localhost\Ubuntu-24.04-OCR\home\ocr\ocr-pdf-rebuilder\paddle_output\Le_Gout_du_secret\page_results"
    ))
    parser.add_argument("--detail-limit", type=int, default=0)
    args = parser.parse_args()
    mismatch_pages = []
    block_count_mismatch_pages = []
    exact_aligned_blocks = 0
    windows_blocks = 0
    wsl_blocks = 0
    differences = []
    for number in range(1, 139):
        filename = f"page_{number:04d}.json"
        windows = json.loads((args.windows / filename).read_text(encoding="utf-8"))
        wsl = json.loads((args.wsl / filename).read_text(encoding="utf-8"))
        left = [(cell["category"], cell.get("text")) for cell in windows["cells"]]
        right = [(cell["category"], cell.get("text")) for cell in wsl["cells"]]
        windows_blocks += len(left)
        wsl_blocks += len(right)
        exact_aligned_blocks += sum(a == b for a, b in zip(left, right))
        if len(differences) < args.detail_limit:
            for index, (a, b) in enumerate(zip(left, right), 1):
                if a != b and len(differences) < args.detail_limit:
                    differences.append({"page": number, "block": index,
                                        "windows": str(a)[:180], "wsl": str(b)[:180]})
        if len(left) != len(right):
            block_count_mismatch_pages.append(number)
        if left != right:
            mismatch_pages.append(number)
    print(json.dumps({
        "pages": 138,
        "exact_page_results": 138 - len(mismatch_pages),
        "block_count_mismatch_pages": block_count_mismatch_pages,
        "mismatch_pages": mismatch_pages,
        "windows_blocks": windows_blocks,
        "wsl_blocks": wsl_blocks,
        "exact_aligned_blocks": exact_aligned_blocks,
        "sample_differences": differences,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
