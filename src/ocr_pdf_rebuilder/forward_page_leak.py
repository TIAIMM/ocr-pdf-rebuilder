"""Detection and page-local validation for MinerU forward-page text leaks.

This module deliberately knows nothing about MinerU process orchestration,
checkpoints, output directories, or ReportLab.  The production pipeline injects
its text normalizer and page-result helpers, which keeps the analysis reusable
and prevents an import cycle with the CLI entry module.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import unicodedata

import fitz


PageResult = dict[str, object]


@dataclass(frozen=True)
class ForwardPageLeakConfig:
    retry_reason: str
    lookahead: int = 5
    ngram_size: int = 16
    min_source_chars: int = 120
    min_neighbor_chars: int = 500
    min_output_source_ratio: float = 1.25
    min_neighbor_coverage: float = 0.60
    min_retry_own_coverage: float = 0.50
    max_retry_source_ratio: float = 1.35


class ForwardPageLeakAnalyzer:
    """Analyze parsed page text against the source PDF's page-local text layer."""

    def __init__(
        self,
        config: ForwardPageLeakConfig,
        *,
        normalize_text: Callable[[object], str],
        source_text_from_cell: Callable[[dict[str, object]], str],
        retry_result_has_usable_cells: Callable[[PageResult], bool],
        append_retry_reason: Callable[[PageResult, str], None],
    ) -> None:
        self.config = config
        self.normalize_text = normalize_text
        self.source_text_from_cell = source_text_from_cell
        self.retry_result_has_usable_cells = retry_result_has_usable_cells
        self.append_retry_reason = append_retry_reason

    @staticmethod
    def normalize_overlap_text(text: object) -> str:
        normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
        return "".join(character for character in normalized if character.isalnum())

    def ngram_hashes(self, text: str, size: int | None = None) -> set[int]:
        size = self.config.ngram_size if size is None else size
        if len(text) < size:
            return set()
        return {hash(text[index : index + size]) for index in range(len(text) - size + 1)}

    def page_result_primary_text(self, result: PageResult) -> str:
        cell_texts = []
        for cell in result.get("cells") or []:
            text = self.normalize_text(self.source_text_from_cell(cell))
            if text:
                cell_texts.append(text)
        if cell_texts:
            return "\n".join(cell_texts)
        fallback_text = self.normalize_text(result.get("fallback_text", ""))
        if fallback_text:
            return fallback_text
        return self.normalize_text(result.get("md_nohf_text", ""))

    def source_page_overlap_texts(self, pdf_path: Path) -> list[str]:
        with fitz.open(pdf_path) as doc:
            return [self.normalize_overlap_text(page.get_text("text")) for page in doc]

    def forward_matches(
        self,
        output_text: str,
        page_index: int,
        source_texts: list[str],
    ) -> list[dict[str, object]]:
        config = self.config
        if page_index < 0 or page_index >= len(source_texts):
            return []
        current_source = source_texts[page_index]
        if len(current_source) < config.min_source_chars:
            return []

        normalized_output = self.normalize_overlap_text(output_text)
        output_source_ratio = len(normalized_output) / max(1, len(current_source))
        if output_source_ratio <= config.min_output_source_ratio:
            return []
        output_ngrams = self.ngram_hashes(normalized_output)
        if not output_ngrams:
            return []

        matches = []
        stop = min(len(source_texts), page_index + config.lookahead + 1)
        for target_index in range(page_index + 1, stop):
            target_source = source_texts[target_index]
            if len(target_source) < config.min_neighbor_chars:
                continue
            target_ngrams = self.ngram_hashes(target_source)
            if not target_ngrams:
                continue
            coverage = len(target_ngrams & output_ngrams) / len(target_ngrams)
            if coverage < config.min_neighbor_coverage:
                continue
            matches.append(
                {
                    "target_page": target_index + 1,
                    "target_coverage": round(coverage, 6),
                    "output_source_length_ratio": round(output_source_ratio, 6),
                    "output_chars": len(normalized_output),
                    "source_chars": len(current_source),
                    "target_source_chars": len(target_source),
                }
            )
        return matches

    def detect(
        self,
        pdf_path: Path,
        page_results: dict[int, PageResult],
        source_texts: list[str] | None = None,
        *,
        mark: bool = True,
    ) -> dict[int, list[dict[str, object]]]:
        source_texts = source_texts or self.source_page_overlap_texts(pdf_path)
        detected = {}
        for page_index, result in sorted(page_results.items()):
            matches = self.forward_matches(
                self.page_result_primary_text(result),
                page_index,
                source_texts,
            )
            if not matches:
                continue
            detected[page_index] = matches
            if mark:
                result["forward_page_content_leak_detected"] = True
                result["forward_page_content_leak_targets"] = [
                    item["target_page"] for item in matches
                ]
                result["forward_page_content_leak_metrics"] = matches
                result["needs_retry"] = True
                self.append_retry_reason(result, self.config.retry_reason)
        return detected

    def retry_result_is_page_local(
        self,
        result: PageResult,
        page_index: int,
        source_texts: list[str],
    ) -> tuple[bool, dict[str, object]]:
        if not self.retry_result_has_usable_cells(result):
            return False, {"reason": "no usable isolated MinerU cells"}

        retry_text = self.normalize_overlap_text(self.page_result_primary_text(result))
        source_text = source_texts[page_index]
        source_ngrams = self.ngram_hashes(source_text)
        retry_ngrams = self.ngram_hashes(retry_text)
        own_coverage = (
            len(source_ngrams & retry_ngrams) / len(source_ngrams)
            if source_ngrams
            else 0.0
        )
        length_ratio = len(retry_text) / max(1, len(source_text))
        forward_matches = self.forward_matches(retry_text, page_index, source_texts)
        metrics = {
            "own_source_coverage": round(own_coverage, 6),
            "retry_source_length_ratio": round(length_ratio, 6),
            "forward_matches": forward_matches,
        }
        valid = (
            own_coverage >= self.config.min_retry_own_coverage
            and length_ratio <= self.config.max_retry_source_ratio
            and not forward_matches
        )
        if not valid:
            metrics["reason"] = "isolated MinerU result failed page-local text validation"
        return valid, metrics

    def source_pdf_text_page_result(
        self,
        source: fitz.Document,
        page_index: int,
    ) -> PageResult | None:
        page = source[page_index]
        page_size = [float(page.rect.width), float(page.rect.height)]
        cells = []
        for block in page.get_text("blocks", sort=True):
            if len(block) < 7 or int(block[6]) != 0:
                continue
            text = self.normalize_text(block[4])
            if not text:
                continue
            x0, y0, x1, y1 = [float(value) for value in block[:4]]
            x0 = max(0.0, min(x0, page_size[0]))
            y0 = max(0.0, min(y0, page_size[1]))
            x1 = max(x0 + 1.0, min(x1, page_size[0]))
            y1 = max(y0 + 1.0, min(y1, page_size[1]))
            cells.append(
                {
                    "bbox": [x0, y0, x1, y1],
                    "category": "Text",
                    "text": text,
                    "source": "source-pdf-text-fallback",
                    "__bbox_units": "pdf",
                    "__page_size": page_size,
                }
            )

        joined = "\n\n".join(self.source_text_from_cell(cell) for cell in cells)
        if len(self.normalize_overlap_text(joined)) < self.config.min_source_chars:
            return None
        return {
            "cells": cells,
            "fallback_text": "",
            "filtered": False,
            "needs_retry": False,
            "retry_reason": "",
            "image_size": None,
            "json_path": None,
            "image_path": None,
            "md_nohf_text": joined,
            "md_nohf_path": None,
            "source_pdf_text_fallback": True,
        }
