"""Pure calculation helpers for the standalone thermal-drift capture app."""

from __future__ import annotations

import math
from typing import Any

import numpy as np


PATTERN_ROLES = ("white", "sine_000", "sine_090", "sine_180", "sine_270")


def build_capture_schedule(
    *,
    total_duration_minutes: float,
    initial_duration_minutes: float,
    initial_interval_seconds: float,
    regular_interval_seconds: float,
) -> list[float]:
    """Return elapsed-second targets, including t=0 and the exact final time."""

    total = float(total_duration_minutes) * 60.0
    initial_duration = min(total, float(initial_duration_minutes) * 60.0)
    initial_interval = float(initial_interval_seconds)
    regular_interval = float(regular_interval_seconds)
    if total <= 0:
        raise ValueError("total_duration_minutes must be positive")
    if initial_duration < 0:
        raise ValueError("initial_duration_minutes cannot be negative")
    if initial_interval <= 0 or regular_interval <= 0:
        raise ValueError("capture intervals must be positive")

    result = [0.0]
    value = initial_interval
    while value <= initial_duration + 1e-9:
        result.append(value)
        value += initial_interval

    value = initial_duration + regular_interval
    while value <= total + 1e-9:
        result.append(value)
        value += regular_interval

    if not math.isclose(result[-1], total, abs_tol=1e-9):
        result.append(total)
    return result


def image_statistics(image: np.ndarray) -> dict[str, float | int | str]:
    values = np.asarray(image)
    return {
        "dtype": str(values.dtype),
        "minimum": int(values.min()),
        "maximum": int(values.max()),
        "mean": float(values.mean()),
        "median": float(np.median(values)),
    }


def phase_summary(
    images: dict[str, np.ndarray],
    *,
    minimum_modulation: float,
    saturation_value: float,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Calculate wrapped four-step phase and a conservative validity mask."""

    missing = [role for role in PATTERN_ROLES if role not in images]
    if missing:
        raise ValueError(f"missing thermal-drift images: {', '.join(missing)}")
    shapes = {np.asarray(images[role]).shape for role in PATTERN_ROLES}
    if len(shapes) != 1:
        raise ValueError("all thermal-drift images must have the same shape")

    white = np.asarray(images["white"], dtype=np.float32)
    i0 = np.asarray(images["sine_000"], dtype=np.float32)
    i90 = np.asarray(images["sine_090"], dtype=np.float32)
    i180 = np.asarray(images["sine_180"], dtype=np.float32)
    i270 = np.asarray(images["sine_270"], dtype=np.float32)
    cosine = i0 - i180
    sine = i270 - i90
    modulation = 0.5 * np.hypot(cosine, sine)
    phase = np.arctan2(sine, cosine).astype(np.float32)
    maximum = np.maximum.reduce((white, i0, i90, i180, i270))
    valid = (modulation >= float(minimum_modulation)) & (maximum < float(saturation_value))
    valid_count = int(np.count_nonzero(valid))
    summary: dict[str, Any] = {
        "valid_pixel_count": valid_count,
        "valid_pixel_ratio": float(valid.mean()),
        "minimum_modulation": float(minimum_modulation),
        "saturation_value": float(saturation_value),
        "white_mean": float(white.mean()),
        "modulation_mean_valid": float(modulation[valid].mean()) if valid_count else None,
        "modulation_median_valid": float(np.median(modulation[valid])) if valid_count else None,
    }
    return phase, valid, summary


def circular_phase_change(
    reference_phase: np.ndarray,
    reference_valid: np.ndarray,
    current_phase: np.ndarray,
    current_valid: np.ndarray,
) -> dict[str, float | int | None]:
    """Summarize wrapped phase change without assuming it stays away from +/-pi."""

    if reference_phase.shape != current_phase.shape:
        raise ValueError("reference and current phase shapes differ")
    valid = np.asarray(reference_valid, dtype=bool) & np.asarray(current_valid, dtype=bool)
    count = int(np.count_nonzero(valid))
    if count == 0:
        return {
            "common_valid_pixel_count": 0,
            "circular_mean_delta_rad": None,
            "circular_std_delta_rad": None,
        }
    delta = np.asarray(current_phase[valid] - reference_phase[valid], dtype=np.float64)
    vector = np.mean(np.exp(1j * delta))
    magnitude = max(float(abs(vector)), np.finfo(float).tiny)
    return {
        "common_valid_pixel_count": count,
        "circular_mean_delta_rad": float(np.angle(vector)),
        "circular_std_delta_rad": float(np.sqrt(max(0.0, -2.0 * np.log(magnitude)))),
    }
