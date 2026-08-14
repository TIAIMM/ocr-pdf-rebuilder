"""Read-only validation of generated text and image PDF artifacts."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
import re

import fitz


SUSPECTED_MARKDOWN_RE = re.compile(
    r"("
    r"\*\*[^*\n]{1,120}\*\*"
    r"|__[^_\n]{1,120}__"
    r"|(?<!\*)\*[^*\n]{1,120}\*(?!\*)"
    r"|^\s{0,3}#{1,6}\s+\S+"
    r"|`[^`\n]{1,120}`"
    r")",
    re.MULTILINE,
)


class PdfArtifactValidator:
    def __init__(
        self,
        *,
        radical_pattern: re.Pattern[str],
        control_pattern: re.Pattern[str],
        latex_pattern: re.Pattern[str],
        debug_markdown: bool = False,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.radical_pattern = radical_pattern
        self.control_pattern = control_pattern
        self.latex_pattern = latex_pattern
        self.debug_markdown = debug_markdown
        self.logger = logger or (lambda _message: None)

    def scan(
        self,
        pdf_path: Path,
        include_markdown: bool | None = None,
        checks: Iterable[str] | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict[str, object]:
        if include_markdown is None:
            include_markdown = self.debug_markdown
        if checks is None:
            selected_checks = {"radicals", "control_chars", "latex_residue"}
        else:
            selected_checks = set(checks)
        if include_markdown:
            selected_checks.add("suspected_markdown")

        scan = {
            "page_count": 0,
            "image_pages": [],
            "text_char_counts": [],
            "radical_offenders": {},
            "control_char_offenders": {},
            "latex_residue_offenders": {},
            "suspected_markdown_offenders": {},
        }
        with fitz.open(pdf_path) as doc:
            scan["page_count"] = doc.page_count
            if not selected_checks:
                return scan
            for page_index, page in enumerate(doc):
                page_no = page_index + 1
                text = page.get_text()
                scan["text_char_counts"].append(len(text.strip()))
                if page.get_images(full=True):
                    scan["image_pages"].append(page_no)

                if "radicals" in selected_checks:
                    radical_chars = sorted(set(self.radical_pattern.findall(text)))
                    if radical_chars:
                        scan["radical_offenders"][page_no] = radical_chars

                if "control_chars" in selected_checks:
                    control_chars = sorted(set(self.control_pattern.findall(text)))
                    if control_chars:
                        scan["control_char_offenders"][page_no] = control_chars

                if "latex_residue" in selected_checks:
                    latex_matches = []
                    for match in self.latex_pattern.finditer(text):
                        snippet = text[max(0, match.start() - 45) : match.end() + 45]
                        latex_matches.append(re.sub(r"\s+", " ", snippet).strip())
                        if len(latex_matches) >= 4:
                            break
                    if latex_matches:
                        scan["latex_residue_offenders"][page_no] = latex_matches

                if "suspected_markdown" in selected_checks:
                    markdown_matches = sorted(set(SUSPECTED_MARKDOWN_RE.findall(text)))
                    if markdown_matches:
                        scan["suspected_markdown_offenders"][page_no] = markdown_matches[:5]
                if progress_callback is not None:
                    progress_callback(page_no, doc.page_count)
        return scan

    def validate_no_radicals(self, pdf_path: Path, validation_scan=None) -> None:
        validation_scan = validation_scan or self.scan(pdf_path, checks={"radicals"})
        offenders = validation_scan["radical_offenders"]
        if offenders:
            details = "; ".join(
                f"page {page_no}: {''.join(chars)}" for page_no, chars in offenders.items()
            )
            raise RuntimeError(
                "Output PDF still contains Kangxi/CJK radical characters after normalization: "
                f"{details}. Delete the output PDF and regenerate after checking normalize_text()."
            )

    def validate_page_count(
        self,
        pdf_path: Path,
        expected_page_count: int,
        validation_scan=None,
    ) -> None:
        validation_scan = validation_scan or self.scan(
            pdf_path,
            include_markdown=False,
            checks=set(),
        )
        actual_page_count = validation_scan["page_count"]
        if actual_page_count != expected_page_count:
            raise RuntimeError(
                "Output PDF page count does not match the source PDF: "
                f"expected={expected_page_count}, actual={actual_page_count}, output={pdf_path}"
            )

    def validate_no_raster_images(self, pdf_path: Path, validation_scan=None) -> None:
        validation_scan = validation_scan or self.scan(pdf_path)
        image_pages = validation_scan.get("image_pages", [])
        if image_pages:
            raise RuntimeError(
                "Text-only output PDF contains raster images on pages: "
                + ", ".join(str(page_no) for page_no in image_pages[:40])
            )

    def validate_pages_are_blank(
        self,
        pdf_path: Path,
        page_indices: Iterable[int],
        validation_scan=None,
    ) -> None:
        validation_scan = validation_scan or self.scan(pdf_path)
        text_counts = validation_scan.get("text_char_counts", [])
        image_pages = set(validation_scan.get("image_pages", []))
        offenders = []
        for page_index in page_indices:
            page_no = int(page_index) + 1
            text_count = text_counts[page_index] if 0 <= page_index < len(text_counts) else None
            if text_count not in (0, None) or page_no in image_pages:
                offenders.append(page_no)
        if offenders:
            raise RuntimeError(
                "Text-only output did not keep fallback pages blank: "
                + ", ".join(str(page_no) for page_no in offenders)
            )

    def validate_images_on_pages(
        self,
        pdf_path: Path,
        page_indices: Iterable[int],
        validation_scan=None,
    ) -> None:
        validation_scan = validation_scan or self.scan(pdf_path)
        image_pages = set(validation_scan.get("image_pages", []))
        missing = [
            int(page_index) + 1
            for page_index in page_indices
            if int(page_index) + 1 not in image_pages
        ]
        if missing:
            raise RuntimeError(
                "Image-variant PDF is missing required source-page images on pages: "
                + ", ".join(str(page_no) for page_no in missing)
            )

    def validate_no_control_chars(self, pdf_path: Path, validation_scan=None) -> None:
        validation_scan = validation_scan or self.scan(pdf_path, checks={"control_chars"})
        offenders = validation_scan["control_char_offenders"]
        if offenders:
            details = "; ".join(
                f"page {page_no}: "
                + " ".join(f"U+{ord(ch):04X}" for ch in chars)
                for page_no, chars in offenders.items()
            )
            raise RuntimeError(
                "Output PDF still contains control characters after normalization: "
                f"{details}. Delete the output PDF and regenerate after checking line splitting."
            )

    def validate_no_latex_residue(self, pdf_path: Path, validation_scan=None) -> None:
        validation_scan = validation_scan or self.scan(pdf_path, checks={"latex_residue"})
        offenders = validation_scan["latex_residue_offenders"]
        if offenders:
            details = "; ".join(
                f"page {page_no}: {matches}" for page_no, matches in offenders.items()
            )
            raise RuntimeError(
                "Output PDF still contains LaTeX/formula markup after normalization: "
                f"{details}. Delete the output PDF and regenerate after checking "
                "normalize_safe_latex_markup()."
            )

    def warn_suspected_markdown(self, pdf_path: Path, validation_scan=None) -> None:
        if not self.debug_markdown:
            return
        validation_scan = validation_scan or self.scan(
            pdf_path,
            include_markdown=True,
            checks={"suspected_markdown"},
        )
        offenders = validation_scan["suspected_markdown_offenders"]
        if offenders:
            details = "; ".join(
                f"page {page_no}: {matches}" for page_no, matches in offenders.items()
            )
            self.logger(f"    Debug suspected markdown warnings: {details}")
