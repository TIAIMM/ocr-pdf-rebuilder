"""PaddleOCR-VL page worker executed inside the dedicated Paddle environment.

The production entry process intentionally does not import PaddleOCR.  It starts
this worker once per document, so model initialization is paid once while each
page result is checkpointed independently as normalized JSON.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import re
import tempfile

import fitz


SCHEMA = 1
SHORT_REFERENCE_MARKER_RE = re.compile(r"\[(?:\d{1,4}|[A-Za-z]{1,4})\]")
LABEL_CATEGORIES = {
    "title": "Title",
    "doc_title": "Title",
    "paragraph_title": "Section-header",
    "section_title": "Section-header",
    "subtitle": "Section-header",
    "header": "Page-header",
    "footer": "Page-footer",
    "page_number": "Page-footer",
    "number": "Page-footer",
    "footnote": "Footnote",
    "formula": "Formula",
    "display_formula": "Formula",
    "equation": "Formula",
    "table": "Table",
    "table_caption": "Caption",
    "figure_caption": "Caption",
    "caption": "Caption",
    "list": "List-item",
    "list_item": "List-item",
    "image": "Picture",
    "figure": "Picture",
    "chart": "Picture",
    "seal": "Picture",
}


def atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def package_identity() -> dict[str, object]:
    packages = {}
    for name in ("paddleocr", "paddlepaddle-gpu", "paddlex", "PyMuPDF", "Pillow"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {
        "python": os.sys.version.split()[0],
        "python_executable": str(Path(os.sys.executable).resolve()),
        "packages": packages,
    }


def category_for_label(label: object) -> str:
    normalized = str(label or "text").strip().lower().replace("-", "_").replace(" ", "_")
    return LABEL_CATEGORIES.get(normalized, "Text")


def _normalized_block_bbox(block: object) -> list[float] | None:
    if not isinstance(block, dict):
        return None
    bbox = block.get("block_bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        normalized = [float(value) for value in bbox]
    except (TypeError, ValueError):
        return None
    if normalized[2] <= normalized[0] or normalized[3] <= normalized[1]:
        return None
    return normalized


def repair_marker_body_bbox_mismatches(
    raw_blocks: object,
    image_width: int,
    image_height: int,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Split marker/body text when Paddle assigns both to the marker's tiny bbox.

    PaddleOCR-VL can return a low-area reference marker block containing the
    marker plus a long quotation while leaving the adjacent, correctly sized
    text block empty.  Repair only that strongly constrained geometry; all
    ambiguous cases remain untouched for the renderer's safe fallback.
    """

    if not isinstance(raw_blocks, (list, tuple)):
        return [], []
    blocks = [dict(block) for block in raw_blocks if isinstance(block, dict)]
    repairs: list[dict[str, object]] = []
    used_empty_blocks: set[int] = set()

    max_marker_width = max(32.0, float(image_width) * 0.12)
    max_marker_height = max(24.0, float(image_height) * 0.04)
    max_horizontal_gap = max(36.0, float(image_width) * 0.15)
    min_body_width = max(120.0, float(image_width) * 0.25)
    min_body_height = max(30.0, float(image_height) * 0.04)

    for marker_index, marker_block in enumerate(blocks):
        content = str(marker_block.get("block_content") or "").strip()
        lines = content.splitlines()
        if len(lines) < 2:
            continue
        marker_text = lines[0].strip()
        body_text = "\n".join(lines[1:]).strip()
        if not SHORT_REFERENCE_MARKER_RE.fullmatch(marker_text) or len(body_text) < 20:
            continue

        marker_bbox = _normalized_block_bbox(marker_block)
        if marker_bbox is None:
            continue
        marker_width = marker_bbox[2] - marker_bbox[0]
        marker_height = marker_bbox[3] - marker_bbox[1]
        if marker_width > max_marker_width or marker_height > max_marker_height:
            continue

        marker_label = str(marker_block.get("block_label") or "text").strip().lower()
        candidates: list[tuple[tuple[float, ...], int, list[float]]] = []
        for body_index, body_block in enumerate(blocks):
            if body_index == marker_index or body_index in used_empty_blocks:
                continue
            if abs(body_index - marker_index) > 3:
                continue
            if str(body_block.get("block_content") or "").strip():
                continue
            body_label = str(body_block.get("block_label") or "text").strip().lower()
            if body_label != marker_label:
                continue
            body_bbox = _normalized_block_bbox(body_block)
            if body_bbox is None:
                continue
            body_width = body_bbox[2] - body_bbox[0]
            body_height = body_bbox[3] - body_bbox[1]
            if body_width < min_body_width or body_height < min_body_height:
                continue
            overlap = max(
                0.0,
                min(marker_bbox[3], body_bbox[3])
                - max(marker_bbox[1], body_bbox[1]),
            )
            if overlap / max(marker_height, 1.0) < 0.50:
                continue
            if body_bbox[0] < marker_bbox[0]:
                continue
            horizontal_gap = max(0.0, body_bbox[0] - marker_bbox[2])
            if horizontal_gap > max_horizontal_gap:
                continue
            body_area = body_width * body_height
            marker_area = marker_width * marker_height
            if body_area < marker_area * 8.0:
                continue
            score = (
                float(abs(body_index - marker_index)),
                horizontal_gap,
                -overlap,
                -body_area,
            )
            candidates.append((score, body_index, body_bbox))

        if not candidates:
            continue
        _score, body_index, body_bbox = min(candidates, key=lambda item: item[0])
        body_block = blocks[body_index]
        repair = {
            "kind": "marker_body_to_empty_sibling",
            "marker_block_id": marker_block.get("block_id"),
            "body_block_id": body_block.get("block_id"),
            "marker_bbox": marker_bbox,
            "body_bbox": body_bbox,
        }
        marker_block["block_content"] = marker_text
        marker_block["__paddle_bbox_content_repaired"] = True
        marker_block["__paddle_bbox_content_repair_role"] = "marker"
        body_block["block_content"] = body_text
        body_block["__paddle_bbox_content_repaired"] = True
        body_block["__paddle_bbox_content_repair_role"] = "body"
        used_empty_blocks.add(body_index)
        repairs.append(repair)

    return blocks, repairs


def normalized_page_result(
    data: dict[str, object],
    *,
    page_index: int,
    image_width: int,
    image_height: int,
    raw_json_path: Path,
) -> dict[str, object]:
    result = data.get("res", data)
    if not isinstance(result, dict):
        result = {}
    raw_blocks, bbox_content_repairs = repair_marker_body_bbox_mismatches(
        result.get("parsing_res_list") or [],
        image_width,
        image_height,
    )
    cells = []
    for source_order, block in enumerate(raw_blocks):
        if not isinstance(block, dict):
            continue
        bbox = block.get("block_bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        try:
            normalized_bbox = [float(value) for value in bbox]
        except (TypeError, ValueError):
            continue
        content = str(block.get("block_content") or "").strip()
        category = category_for_label(block.get("block_label"))
        if not content and category != "Picture":
            continue
        order_value = block.get("block_order")
        try:
            order = int(order_value) if order_value is not None else source_order
        except (TypeError, ValueError):
            order = source_order
        cell = {
            "bbox": normalized_bbox,
            "category": category,
            "text": content,
            "content": content,
            "__bbox_units": "image",
            "__paddle_label": str(block.get("block_label") or "text"),
            "__paddle_order": order,
        }
        if block.get("__paddle_bbox_content_repaired"):
            cell["__paddle_bbox_content_repaired"] = True
            cell["__paddle_bbox_content_repair_role"] = block.get(
                "__paddle_bbox_content_repair_role"
            )
        cells.append(cell)
    cells.sort(
        key=lambda cell: (
            int(cell.get("__paddle_order", 0)),
            float(cell["bbox"][1]),
            float(cell["bbox"][0]),
        )
    )
    markdown = "\n\n".join(str(cell.get("text") or "") for cell in cells if cell.get("text"))
    return {
        "schema": SCHEMA,
        "engine": "PaddleOCR-VL",
        "page_index": page_index,
        "cells": cells,
        "fallback_text": "",
        "filtered": False,
        "needs_retry": False,
        "retry_reason": "",
        "paddle_bbox_content_repaired": bool(bbox_content_repairs),
        "paddle_bbox_content_repairs": bbox_content_repairs,
        "image_size": [image_width, image_height],
        "json_path": str(raw_json_path),
        "image_path": None,
        "md_nohf_text": markdown,
        "md_nohf_path": None,
    }


def result_json_from_prediction(prediction: object, raw_dir: Path) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    before = set(raw_dir.glob("*.json"))
    prediction.save_to_json(save_path=str(raw_dir))
    after = set(raw_dir.glob("*.json"))
    candidates = sorted(after - before, key=lambda path: path.stat().st_mtime_ns)
    if not candidates:
        candidates = sorted(raw_dir.glob("*.json"), key=lambda path: path.stat().st_mtime_ns)
    if not candidates:
        raise RuntimeError("PaddleOCR-VL did not write a JSON result")
    return candidates[-1]


def parse_pages(value: str | None, page_count: int) -> list[int]:
    if not value:
        return list(range(page_count))
    pages = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        page_number = int(token)
        if page_number < 1 or page_number > page_count:
            raise ValueError(f"page outside document: {page_number}")
        pages.append(page_number - 1)
    return sorted(set(pages))


def create_pipeline(args: argparse.Namespace):
    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", args.model_source)
    from paddleocr import PaddleOCRVL

    return PaddleOCRVL(
        pipeline_version=args.pipeline_version,
        layout_detection_model_name=args.layout_model,
        vl_rec_model_name=args.recognition_model,
        vl_rec_backend=args.backend,
        vl_rec_server_url=args.server_url,
        device=args.device,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_layout_detection=True,
        format_block_content=False,
        merge_layout_blocks=True,
        use_queues=False,
    )


def run(args: argparse.Namespace) -> int:
    output_dir = args.output_dir.resolve()
    page_result_dir = output_dir / "page_results"
    page_image_dir = output_dir / "pages"
    raw_dir = output_dir / "raw"
    with fitz.open(args.pdf) as document:
        page_indices = parse_pages(args.pages, document.page_count)
        pending = [
            page_index
            for page_index in page_indices
            if args.force
            or not (page_result_dir / f"page_{page_index + 1:04d}.json").is_file()
        ]
        for page_index in page_indices:
            if page_index not in pending:
                print(
                    f"PaddleOCR page {page_index + 1}/{document.page_count}: checkpoint reused",
                    flush=True,
                )
        if not pending:
            return 0

        print(
            "Initializing PaddleOCR-VL "
            f"({args.layout_model} + {args.recognition_model}, {args.device})",
            flush=True,
        )
        pipeline = create_pipeline(args)
        try:
            matrix = fitz.Matrix(args.dpi / 72.0, args.dpi / 72.0)
            for page_index in pending:
                page = document[page_index]
                pixmap = page.get_pixmap(matrix=matrix, alpha=False)
                image_path = page_image_dir / f"page_{page_index + 1:04d}.png"
                image_path.parent.mkdir(parents=True, exist_ok=True)
                pixmap.save(str(image_path))

                predictions = pipeline.predict(
                    input=str(image_path),
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_layout_detection=True,
                    format_block_content=False,
                    merge_layout_blocks=True,
                    use_queues=False,
                    max_new_tokens=args.max_new_tokens,
                )
                prediction = next(iter(predictions), None)
                if prediction is None:
                    raise RuntimeError(f"PaddleOCR-VL returned no result for page {page_index + 1}")
                page_raw_dir = raw_dir / f"page_{page_index + 1:04d}"
                raw_json = result_json_from_prediction(prediction, page_raw_dir)
                raw_data = json.loads(raw_json.read_text(encoding="utf-8"))
                normalized = normalized_page_result(
                    raw_data,
                    page_index=page_index,
                    image_width=pixmap.width,
                    image_height=pixmap.height,
                    raw_json_path=raw_json,
                )
                atomic_write_json(
                    page_result_dir / f"page_{page_index + 1:04d}.json",
                    normalized,
                )
                print(
                    f"PaddleOCR page {page_index + 1}/{document.page_count}: "
                    f"blocks={len(normalized['cells'])}",
                    flush=True,
                )
        finally:
            pipeline.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--identity", action="store_true")
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--pages")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--device", default="gpu:0")
    parser.add_argument("--pipeline-version", default="v1.6")
    parser.add_argument("--layout-model", default="PP-DocLayoutV3")
    parser.add_argument("--recognition-model", default="PaddleOCR-VL-1.6-0.9B")
    parser.add_argument(
        "--backend",
        choices=("native", "vllm-server"),
        default="vllm-server",
    )
    parser.add_argument("--server-url")
    parser.add_argument("--max-new-tokens", type=int, default=2048)
    parser.add_argument("--model-source", default="ModelScope")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.identity:
        print(json.dumps(package_identity(), ensure_ascii=False, sort_keys=True))
        return
    if args.pdf is None or args.output_dir is None:
        parser.error("--pdf and --output-dir are required")
    if args.backend.endswith("-server") and not args.server_url:
        parser.error("--server-url is required for a server backend")
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
