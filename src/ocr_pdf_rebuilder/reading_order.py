"""Conservative page-local reading-order reconstruction.

OCR engines do not always return layout blocks in the order in which a reader
would consume them.  In particular, a two-column page can arrive as a single
top-to-bottom stream, while headers, footers and marginal markers can be
interleaved with body blocks.  This module keeps the heuristic deliberately
conservative: it switches to column-major ordering only when the block
geometry provides clear, repeated column evidence.
"""

from __future__ import annotations

import math
import re
from typing import Any


_HEADER_CATEGORIES = frozenset({"Page-header"})
_FOOTER_CATEGORIES = frozenset({"Page-footer"})
_COLUMN_TEXT_CATEGORIES = frozenset(
    {"Text", "List-item", "Footnote", "Formula", "Caption"}
)
_COLUMN_BARRIER_CATEGORIES = frozenset(
    {"Title", "Section-header", "Table", "Picture", "ImageFallback"}
)
_MARGIN_MARKER_RE = re.compile(r"^[\[\](){}0-9.,:;+/\\\-–—]+$")


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _geometry(item: dict[str, Any]) -> tuple[float, float, float, float] | None:
    if all(key in item for key in ("left", "top", "width", "height")):
        values = [_finite_number(item.get(key)) for key in ("left", "top", "width", "height")]
        if all(value is not None for value in values):
            left, top, width, height = values
            if width >= 0 and height >= 0:
                return left, top, left + width, top + height

    bbox = item.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        values = [_finite_number(value) for value in bbox]
        if all(value is not None for value in values):
            x0, y0, x1, y1 = values
            return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
    return None


def _category(item: dict[str, Any]) -> str:
    return str(item.get("category") or "Text")


def _text(item: dict[str, Any]) -> str:
    value = item.get("text")
    if value is None:
        value = item.get("content")
    return str(value or "").strip()


def _stable_order(item: dict[str, Any], fallback: int) -> int:
    for key in ("order", "__paddle_order"):
        value = item.get(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return fallback


def _is_margin_marker(item: dict[str, Any]) -> bool:
    if _category(item) not in {"Text", "List-item", "Footnote"}:
        return False
    lines = [line.strip() for line in _text(item).splitlines() if line.strip()]
    if not lines:
        return False
    compact = re.sub(r"\s+", "", lines[0])
    return bool(compact) and len(compact) <= 14 and bool(_MARGIN_MARKER_RE.fullmatch(compact))


def _promote_margin_markers(
    records: list[tuple[int, dict[str, Any], tuple[float, float, float, float]]],
    page_width: float,
) -> list[tuple[int, dict[str, Any], tuple[float, float, float, float]]]:
    """Keep a narrow left-margin marker immediately before its body block."""

    ordered = list(records)
    for index in range(1, len(ordered)):
        marker = ordered[index]
        previous = ordered[index - 1]
        if not _is_margin_marker(marker[1]):
            continue
        _marker_left, marker_top, marker_right, marker_bottom = marker[2]
        previous_left, previous_top, _previous_right, previous_bottom = previous[2]
        close_vertically = (
            marker_top <= previous_bottom + max(6.0, (marker_bottom - marker_top) * 1.5)
            and marker_bottom >= previous_top - max(6.0, (previous_bottom - previous_top) * 0.25)
        )
        in_left_margin = marker_right <= previous_left + page_width * 0.08
        if close_vertically and in_left_margin:
            ordered[index - 1], ordered[index] = ordered[index], ordered[index - 1]
    return ordered


def _ordinary_key(record: tuple[int, dict[str, Any], tuple[float, float, float, float]]) -> tuple[Any, ...]:
    index, item, (left, top, _right, _bottom) = record
    category = _category(item)
    if category in _HEADER_CATEGORIES:
        rank = 0
    elif category in _FOOTER_CATEGORIES:
        rank = 2
    else:
        rank = 1
    return rank, top, left, _stable_order(item, index), index


def _column_candidates(
    records: list[tuple[int, dict[str, Any], tuple[float, float, float, float]]],
    page_width: float,
) -> list[tuple[int, dict[str, Any], tuple[float, float, float, float]]]:
    candidates = []
    for record in records:
        _index, item, (left, _top, right, _bottom) = record
        category = _category(item)
        if category not in _COLUMN_TEXT_CATEGORIES or _is_margin_marker(item):
            continue
        if not _text(item) or right <= left:
            continue
        width_ratio = (right - left) / max(page_width, 1.0)
        if width_ratio < 0.08 or width_ratio > 0.72:
            continue
        candidates.append(record)
    return candidates


def _candidate_groups(
    candidates: list[tuple[int, dict[str, Any], tuple[float, float, float, float]]],
    page_width: float,
) -> list[list[tuple[int, dict[str, Any], tuple[float, float, float, float]]]]:
    if len(candidates) < 4:
        return []
    x_sorted = sorted(
        candidates,
        key=lambda record: (
            record[2][0],
            record[2][2],
            record[2][1],
            _stable_order(record[1], record[0]),
            record[0],
        ),
    )
    split_gap = max(page_width * 0.12, 24.0)
    groups: list[list[tuple[int, dict[str, Any], tuple[float, float, float, float]]]] = []
    current = [x_sorted[0]]
    for record in x_sorted[1:]:
        previous_left = current[-1][2][0]
        if record[2][0] - previous_left >= split_gap:
            groups.append(current)
            current = [record]
        else:
            current.append(record)
    groups.append(current)
    return groups


def _validated_column_groups(
    groups: list[list[tuple[int, dict[str, Any], tuple[float, float, float, float]]]],
    page_width: float,
) -> list[list[tuple[int, dict[str, Any], tuple[float, float, float, float]]]]:
    if len(groups) < 2 or len(groups) > 4 or any(len(group) < 2 for group in groups):
        return []

    groups = sorted(groups, key=lambda group: min(record[2][0] for record in group))
    extents = []
    for group in groups:
        left = min(record[2][0] for record in group)
        right = max(record[2][2] for record in group)
        if right <= left or (right - left) / max(page_width, 1.0) > 0.72:
            return []
        extents.append((left, right))

    minimum_gap = max(page_width * 0.02, 6.0)
    for previous, following in zip(extents, extents[1:]):
        if following[0] - previous[1] < minimum_gap:
            return []
    return groups


def _attach_small_items_to_columns(
    body_records: list[tuple[int, dict[str, Any], tuple[float, float, float, float]]],
    groups: list[list[tuple[int, dict[str, Any], tuple[float, float, float, float]]]],
    candidate_indexes: set[int],
    page_width: float,
) -> tuple[list[list[tuple[int, dict[str, Any], tuple[float, float, float, float]]]], list[tuple[int, dict[str, Any], tuple[float, float, float, float]]]]:
    extents = [
        (min(record[2][0] for record in group), max(record[2][2] for record in group))
        for group in groups
    ]
    attached = [list(group) for group in groups]
    barriers = []
    for record in body_records:
        index, item, (left, _top, right, _bottom) = record
        if index in candidate_indexes:
            continue
        category = _category(item)
        if category in _COLUMN_BARRIER_CATEGORIES:
            barriers.append(record)
            continue
        width_ratio = (right - left) / max(page_width, 1.0)
        center = (left + right) / 2.0
        assigned = None
        for group_index, (group_left, group_right) in enumerate(extents):
            if group_left - page_width * 0.04 <= center <= group_right + page_width * 0.04:
                assigned = group_index
                break
        if assigned is None and _is_margin_marker(item) and right <= extents[0][0] + page_width * 0.08:
            assigned = 0
        if assigned is not None and width_ratio <= 0.72:
            attached[assigned].append(record)
        else:
            barriers.append(record)
    return attached, barriers


def _column_major_body(
    body_records: list[tuple[int, dict[str, Any], tuple[float, float, float, float]]],
    groups: list[list[tuple[int, dict[str, Any], tuple[float, float, float, float]]]],
    candidates: list[tuple[int, dict[str, Any], tuple[float, float, float, float]]],
    page_width: float,
) -> list[dict[str, Any]]:
    candidate_indexes = {record[0] for record in candidates}
    groups, barriers = _attach_small_items_to_columns(
        body_records, groups, candidate_indexes, page_width
    )
    for group in groups:
        group.sort(
            key=lambda record: (
                record[2][1],
                record[2][0],
                _stable_order(record[1], record[0]),
                record[0],
            )
        )
        group[:] = _promote_margin_markers(group, page_width)
    barriers.sort(key=lambda record: _ordinary_key(record))

    emitted: set[int] = set()
    ordered: list[dict[str, Any]] = []

    def emit_before(top: float) -> None:
        for group in groups:
            for index, item, (_left, item_top, _right, _bottom) in group:
                if index not in emitted and item_top < top:
                    ordered.append(item)
                    emitted.add(index)

    for index, item, (_left, top, _right, _bottom) in barriers:
        emit_before(top)
        if index not in emitted:
            ordered.append(item)
            emitted.add(index)
    for group in groups:
        for index, item, _geometry_value in group:
            if index not in emitted:
                ordered.append(item)
                emitted.add(index)

    # The branch above accounts for every body record, but keep this guard for
    # malformed/unknown records so ordering never drops an OCR block.
    for index, item, _geometry_value in sorted(body_records, key=_ordinary_key):
        if index not in emitted:
            ordered.append(item)
            emitted.add(index)
    return ordered


def _records_for_items(
    items: list[dict[str, Any]],
) -> list[tuple[int, dict[str, Any], tuple[float, float, float, float]]]:
    return [
        (index, item, _geometry(item))
        for index, item in enumerate(items)
        if isinstance(item, dict) and _geometry(item) is not None
    ]


def _effective_page_width(
    records: list[tuple[int, dict[str, Any], tuple[float, float, float, float]]],
    page_width: float | None,
) -> float:
    if page_width is None or _finite_number(page_width) is None or float(page_width) <= 0:
        return max(record[2][2] for record in records)
    return float(page_width)


def has_clear_column_layout(
    items: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    page_width: float | None = None,
) -> bool:
    """Report whether geometry is strong enough to justify column-major text."""

    original = list(items or [])
    if len(original) < 2:
        return False
    records = _records_for_items(original)
    if len(records) != len(original):
        return False
    effective_width = _effective_page_width(records, page_width)
    header_indexes = {
        record[0] for record in records if _category(record[1]) in _HEADER_CATEGORIES
    }
    footer_indexes = {
        record[0] for record in records if _category(record[1]) in _FOOTER_CATEGORIES
    }
    body = [
        record
        for record in records
        if record[0] not in header_indexes and record[0] not in footer_indexes
    ]
    candidates = _column_candidates(body, effective_width)
    groups = _candidate_groups(candidates, effective_width)
    return bool(_validated_column_groups(groups, effective_width))


def order_items_for_reading(
    items: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    page_width: float | None = None,
    page_height: float | None = None,
) -> list[dict[str, Any]]:
    """Return items in a conservative reader-facing order.

    ``page_height`` is accepted to keep the call site explicit and to allow
    future page-shape checks; the current decision is based on horizontal
    separation and repeated vertical blocks.  The input objects are returned
    unchanged, including their original ``order`` fields.
    """

    del page_height
    original = list(items or [])
    if len(original) < 2:
        return original
    records = _records_for_items(original)
    if len(records) != len(original):
        return original

    page_width = _effective_page_width(records, page_width)

    headers = [record for record in records if _category(record[1]) in _HEADER_CATEGORIES]
    footers = [record for record in records if _category(record[1]) in _FOOTER_CATEGORIES]
    # Use the original indexes for filtering so equal-looking OCR dictionaries
    # are still kept as distinct blocks.
    header_indexes = {record[0] for record in headers}
    footer_indexes = {record[0] for record in footers}
    body = [
        record
        for record in records
        if record[0] not in header_indexes and record[0] not in footer_indexes
    ]

    candidates = _column_candidates(body, page_width)
    groups = _validated_column_groups(_candidate_groups(candidates, page_width), page_width)
    if groups:
        ordered_body = _column_major_body(body, groups, candidates, page_width)
    else:
        ordinary_body = sorted(body, key=_ordinary_key)
        ordinary_body = _promote_margin_markers(ordinary_body, page_width)
        ordered_body = [record[1] for record in ordinary_body]

    ordered = (
        [record[1] for record in sorted(headers, key=_ordinary_key)]
        + ordered_body
        + [record[1] for record in sorted(footers, key=_ordinary_key)]
    )
    return ordered


__all__ = ["has_clear_column_layout", "order_items_for_reading"]
