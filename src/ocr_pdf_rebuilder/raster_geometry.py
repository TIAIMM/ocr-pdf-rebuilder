"""Bound page rasterization without changing page-to-image coordinates."""

from __future__ import annotations

import math


DEFAULT_MAX_RASTER_PIXELS = 24_000_000
LOW_RESOLUTION_DPI = 72.0


def bounded_render_dpi(
    width_points: float,
    height_points: float,
    requested_dpi: float,
    max_pixels: int = DEFAULT_MAX_RASTER_PIXELS,
) -> dict[str, float | int | bool]:
    """Plan a render using a conservative pixel budget.

    The actual pixmap dimensions are recorded separately by the caller.  A
    small safety margin accounts for raster rounding at non-integral scales.
    """

    width = float(width_points)
    height = float(height_points)
    dpi = float(requested_dpi)
    if not all(math.isfinite(value) and value > 0 for value in (width, height, dpi)):
        raise ValueError("page size and requested DPI must be finite positive numbers")
    if not isinstance(max_pixels, int) or max_pixels <= 0:
        raise ValueError("max_pixels must be a positive integer")

    requested_pixels = (width * dpi / 72.0) * (height * dpi / 72.0)
    if not math.isfinite(requested_pixels):
        raise ValueError("page raster dimensions are too large")
    safe_budget = max_pixels * 0.98
    effective_dpi = dpi
    if requested_pixels > safe_budget:
        effective_dpi = dpi * math.sqrt(safe_budget / requested_pixels)

    estimated_pixels = math.ceil(width * effective_dpi / 72.0) * math.ceil(
        height * effective_dpi / 72.0
    )
    for _ in range(100):
        if estimated_pixels <= max_pixels:
            break
        effective_dpi *= 0.90 * math.sqrt(max_pixels / estimated_pixels)
        estimated_pixels = math.ceil(width * effective_dpi / 72.0) * math.ceil(
            height * effective_dpi / 72.0
        )
    else:
        raise ValueError("page geometry cannot fit within the pixel budget")

    return {
        "requested_dpi": dpi,
        "effective_dpi": effective_dpi,
        "max_pixels": max_pixels,
        "estimated_pixels": estimated_pixels,
        "downscaled": effective_dpi < dpi - 1e-6,
        "low_resolution_risk": effective_dpi < LOW_RESOLUTION_DPI,
    }
