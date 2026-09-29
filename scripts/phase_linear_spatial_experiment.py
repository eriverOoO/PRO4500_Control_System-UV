"""Fit and validate a spatial phase-linear height calibration.

This script is intentionally independent of capture hardware.  Run it on the
PC that contains the *decoded* known-height and hold-out scans.  The input
manifest explicitly identifies the physical 0/180 degree view directory and
the top-surface ROI (or mask) for every scan, so folder-label swaps and stage
pixels cannot silently enter the fit.

Expected decoder files below each configured view directory are::

    height/delta_phase.npy
    phase/absolute_phase.npy          # required only for phase-fold diagnosis
    masks/combined_mask.png           # optional; finite delta phase is fallback

The output NPZ uses the keys already accepted by PCB_FPP_Decoder's
``phase_linear`` height mode.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np


DEFAULT_DELTA = Path("height/delta_phase.npy")
DEFAULT_ABSOLUTE = Path("phase/absolute_phase.npy")
DEFAULT_MASK = Path("masks/combined_mask.png")


@dataclass(frozen=True)
class ViewInput:
    sample_name: str
    height_mm: float
    physical_view: int
    base_dir: Path
    delta_path: Path
    absolute_path: Path
    reference_phase_path: Path | None
    combined_mask_path: Path
    surface_mask_path: Path | None
    roi_xyxy: tuple[int, int, int, int] | None


@dataclass
class LoadedView:
    spec: ViewInput
    delta: np.ndarray
    mask: np.ndarray
    absolute: np.ndarray | None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Validate paths, shapes, ROIs, masks, and physical-view mappings only",
    )
    return parser


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("manifest root must be a JSON object")
    return payload


def _resolve(path_value: str | Path, base: Path) -> Path:
    path = Path(path_value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _parse_roi(value: Any, context: str) -> tuple[int, int, int, int] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"{context}: roi_xyxy must be [x0, y0, x1, y1]")
    roi = tuple(int(item) for item in value)
    if roi[0] < 0 or roi[1] < 0 or roi[0] >= roi[2] or roi[1] >= roi[3]:
        raise ValueError(f"{context}: invalid roi_xyxy={roi}")
    return roi


def _view_inputs(
    manifest: dict[str, Any], manifest_dir: Path, section: str
) -> list[ViewInput]:
    items = manifest.get(section, [])
    if not isinstance(items, list):
        raise ValueError(f"manifest.{section} must be a list")
    parsed: list[ViewInput] = []
    references = manifest.get("reference_phase_by_physical_view", {})
    if not isinstance(references, dict):
        raise ValueError("manifest.reference_phase_by_physical_view must be an object")
    for sample_index, item in enumerate(items):
        context = f"{section}[{sample_index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{context} must be an object")
        name = str(item.get("name", f"{section}_{sample_index + 1}"))
        height = float(item["height_mm"])
        if not np.isfinite(height) or height < 0:
            raise ValueError(f"{context}: height_mm must be finite and non-negative")
        processed = _resolve(item["processed_dir"], manifest_dir)
        views = item.get("views")
        if not isinstance(views, dict):
            raise ValueError(f"{context}.views must explicitly define physical 0 and 180")
        for physical_view in (0, 180):
            raw = views.get(str(physical_view), views.get(physical_view))
            view_context = f"{context}.views.{physical_view}"
            if not isinstance(raw, dict):
                raise ValueError(f"{view_context} is required")
            subdir = Path(str(raw.get("subdir", f"views/deg_{physical_view}")))
            base_dir = _resolve(subdir, processed)
            surface_mask = raw.get("surface_mask")
            surface_mask_path = (
                _resolve(surface_mask, base_dir) if surface_mask is not None else None
            )
            roi = _parse_roi(raw.get("roi_xyxy"), view_context)
            if roi is None and surface_mask_path is None:
                raise ValueError(
                    f"{view_context}: roi_xyxy or surface_mask is required; "
                    "never fit the known-height block from the whole frame"
                )
            parsed.append(
                ViewInput(
                    sample_name=name,
                    height_mm=height,
                    physical_view=physical_view,
                    base_dir=base_dir,
                    delta_path=_resolve(raw.get("delta_phase", DEFAULT_DELTA), base_dir),
                    absolute_path=_resolve(
                        raw.get("absolute_phase", DEFAULT_ABSOLUTE), base_dir
                    ),
                    reference_phase_path=(
                        _resolve(
                            references.get(
                                str(physical_view), references.get(physical_view)
                            ),
                            manifest_dir,
                        )
                        if references.get(
                            str(physical_view), references.get(physical_view)
                        )
                        is not None
                        else None
                    ),
                    combined_mask_path=_resolve(
                        raw.get("combined_mask", DEFAULT_MASK), base_dir
                    ),
                    surface_mask_path=surface_mask_path,
                    roi_xyxy=roi,
                )
            )
    return parsed


def _read_mask(path: Path, shape: tuple[int, int], required: bool) -> np.ndarray:
    if not path.is_file():
        if required:
            raise FileNotFoundError(f"mask does not exist: {path}")
        return np.ones(shape, dtype=bool)
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError(f"mask could not be read: {path}")
    if image.shape != shape:
        raise ValueError(f"mask shape {image.shape} != phase shape {shape}: {path}")
    return image > 0


def _load_view(spec: ViewInput, need_absolute: bool) -> LoadedView:
    absolute: np.ndarray | None = None
    if spec.delta_path.is_file():
        delta = np.load(spec.delta_path, allow_pickle=False).astype(np.float32)
    else:
        if spec.reference_phase_path is None:
            raise FileNotFoundError(
                f"delta phase does not exist: {spec.delta_path}; set "
                "reference_phase_by_physical_view to derive it from absolute phase"
            )
        if not spec.absolute_path.is_file():
            raise FileNotFoundError(f"absolute phase does not exist: {spec.absolute_path}")
        if not spec.reference_phase_path.is_file():
            raise FileNotFoundError(
                f"reference phase does not exist: {spec.reference_phase_path}"
            )
        absolute = np.load(spec.absolute_path, allow_pickle=False).astype(np.float32)
        reference = np.load(
            spec.reference_phase_path, allow_pickle=False
        ).astype(np.float32)
        if absolute.shape != reference.shape:
            raise ValueError(
                f"absolute phase shape {absolute.shape} != reference shape "
                f"{reference.shape}: {spec.reference_phase_path}"
            )
        delta = (absolute - reference).astype(np.float32)
    if delta.ndim != 2:
        raise ValueError(f"delta phase must be 2-D: {spec.delta_path}")
    shape = delta.shape
    mask = np.isfinite(delta)
    mask &= _read_mask(spec.combined_mask_path, shape, required=False)
    if spec.surface_mask_path is not None:
        mask &= _read_mask(spec.surface_mask_path, shape, required=True)
    if spec.roi_xyxy is not None:
        x0, y0, x1, y1 = spec.roi_xyxy
        if x1 > shape[1] or y1 > shape[0]:
            raise ValueError(
                f"ROI {spec.roi_xyxy} exceeds image shape {shape}: {spec.sample_name} "
                f"physical view {spec.physical_view}"
            )
        roi_mask = np.zeros(shape, dtype=bool)
        roi_mask[y0:y1, x0:x1] = True
        mask &= roi_mask
    if int(mask.sum()) < 100:
        raise ValueError(
            f"fewer than 100 usable top-surface pixels: {spec.sample_name} "
            f"physical view {spec.physical_view}"
        )

    if need_absolute:
        if absolute is None:
            if not spec.absolute_path.is_file():
                raise FileNotFoundError(
                    f"absolute phase required for phase-fold diagnosis: {spec.absolute_path}"
                )
            absolute = np.load(spec.absolute_path, allow_pickle=False).astype(np.float32)
        if absolute.shape != shape:
            raise ValueError(
                f"absolute phase shape {absolute.shape} != delta shape {shape}: "
                f"{spec.absolute_path}"
            )
        mask &= np.isfinite(absolute)
    return LoadedView(spec=spec, delta=delta, mask=mask, absolute=absolute)


def _polynomial_design(
    rows: np.ndarray, cols: np.ndarray, shape: tuple[int, int], degree: int
) -> np.ndarray:
    height, width = shape
    x = (cols.astype(np.float64) - (width - 1) / 2.0) / max((width - 1) / 2.0, 1.0)
    y = (rows.astype(np.float64) - (height - 1) / 2.0) / max((height - 1) / 2.0, 1.0)
    terms = [np.ones_like(x), x, y]
    if degree >= 2:
        terms.extend((x * x, x * y, y * y))
    if degree >= 3:
        terms.extend((x**3, x * x * y, x * y * y, y**3))
    return np.column_stack(terms)


def _robust_polynomial_fit(
    rows: np.ndarray,
    cols: np.ndarray,
    values: np.ndarray,
    shape: tuple[int, int],
    degree: int,
    max_points: int,
) -> tuple[np.ndarray, dict[str, Any]]:
    if degree not in (1, 2, 3):
        raise ValueError("polynomial_degree must be 1, 2, or 3")
    finite = np.isfinite(values) & (values > 0)
    rows, cols, values = rows[finite], cols[finite], values[finite].astype(np.float64)
    if values.size < 100:
        raise ValueError("fewer than 100 positive local slope observations")

    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    robust_sigma = max(1.4826 * mad, median * 1e-4, 1e-8)
    inlier = np.abs(values - median) <= 6.0 * robust_sigma
    rows, cols, values = rows[inlier], cols[inlier], values[inlier]
    if values.size > max_points:
        rng = np.random.default_rng(20260929)
        selected = np.sort(rng.choice(values.size, size=max_points, replace=False))
        rows, cols, values = rows[selected], cols[selected], values[selected]

    design = _polynomial_design(rows, cols, shape, degree)
    weights = np.ones(values.size, dtype=np.float64)
    coefficients = np.linalg.lstsq(design, values, rcond=None)[0]
    for _ in range(5):
        residual = values - design @ coefficients
        scale = max(1.4826 * float(np.median(np.abs(residual))), 1e-8)
        cutoff = 1.5 * scale
        weights = np.minimum(1.0, cutoff / np.maximum(np.abs(residual), 1e-12))
        weighted_design = design * np.sqrt(weights)[:, None]
        weighted_values = values * np.sqrt(weights)
        coefficients = np.linalg.lstsq(weighted_design, weighted_values, rcond=None)[0]

    grid_rows, grid_cols = np.indices(shape)
    model = (
        _polynomial_design(grid_rows.ravel(), grid_cols.ravel(), shape, degree)
        @ coefficients
    ).reshape(shape)
    lower, upper = np.quantile(values, (0.01, 0.99))
    clipped_count = int(np.count_nonzero((model < lower) | (model > upper)))
    model = np.clip(model, lower, upper).astype(np.float32)
    fit_residual = values - design @ coefficients
    return model, {
        "degree": degree,
        "coefficients": [float(value) for value in coefficients],
        "coordinate_normalization": {
            "center_x_px": (shape[1] - 1) / 2.0,
            "center_y_px": (shape[0] - 1) / 2.0,
            "scale_x_px": max((shape[1] - 1) / 2.0, 1.0),
            "scale_y_px": max((shape[0] - 1) / 2.0, 1.0),
        },
        "input_observation_count": int(finite.sum()),
        "robust_inlier_and_sample_count": int(values.size),
        "fit_residual_p95_rad_per_mm": float(np.quantile(np.abs(fit_residual), 0.95)),
        "extrapolation_clipped_pixel_count": clipped_count,
        "clip_range_rad_per_mm": [float(lower), float(upper)],
    }


def _finite_stats(values: np.ndarray) -> dict[str, Any]:
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    if not values.size:
        return {"count": 0, "median": None, "p10": None, "p90": None}
    p10, p90 = np.quantile(values, (0.10, 0.90))
    median = float(np.median(values))
    return {
        "count": int(values.size),
        "median": median,
        "p10": float(p10),
        "p90": float(p90),
        "p10_p90_full_width_percent_of_median": float(
            100.0 * (p90 - p10) / median
        ),
    }


def _fit_view(
    loaded: list[LoadedView], degree: int, min_height_levels: int, max_points: int
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    if not loaded:
        raise ValueError("no fit inputs")
    shapes = {item.delta.shape for item in loaded}
    if len(shapes) != 1:
        raise ValueError(f"fit scans have different image shapes: {sorted(shapes)}")
    shape = loaded[0].delta.shape
    nonzero = [item for item in loaded if item.spec.height_mm > 0]
    if len({item.spec.height_mm for item in nonzero}) < 2:
        raise ValueError("fit requires at least two distinct non-zero known heights")

    normalized_medians = [
        float(np.median(item.delta[item.mask]) / item.spec.height_mm)
        for item in nonzero
    ]
    sign = -1.0 if float(np.median(normalized_medians)) < 0 else 1.0

    pooled_rows: list[np.ndarray] = []
    pooled_cols: list[np.ndarray] = []
    pooled_slopes: list[np.ndarray] = []
    for item in nonzero:
        rows, cols = np.nonzero(item.mask)
        slopes = sign * item.delta[item.mask] / item.spec.height_mm
        good = np.isfinite(slopes) & (slopes > 0)
        pooled_rows.append(rows[good])
        pooled_cols.append(cols[good])
        pooled_slopes.append(slopes[good])
    rows = np.concatenate(pooled_rows)
    cols = np.concatenate(pooled_cols)
    slopes = np.concatenate(pooled_slopes)
    scalar_slope = float(np.median(slopes))

    # Direct zero-anchored per-pixel fit.  Repeats at the same height are first
    # collapsed by a median so they do not masquerade as independent heights.
    height_groups: dict[float, list[LoadedView]] = {}
    for item in nonzero:
        height_groups.setdefault(item.spec.height_mm, []).append(item)
    numerator = np.zeros(shape, dtype=np.float64)
    denominator = np.zeros(shape, dtype=np.float64)
    height_level_count = np.zeros(shape, dtype=np.uint8)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        for height_mm, group in sorted(height_groups.items()):
            stack = np.stack(
                [np.where(item.mask, sign * item.delta, np.nan) for item in group]
            )
            group_delta = np.nanmedian(stack, axis=0)
            valid = np.isfinite(group_delta)
            numerator[valid] += height_mm * group_delta[valid]
            denominator[valid] += height_mm * height_mm
            height_level_count[valid] += 1
    observed_slope = np.full(shape, np.nan, dtype=np.float32)
    observed_valid = (height_level_count >= min_height_levels) & (denominator > 0)
    observed_slope[observed_valid] = (
        numerator[observed_valid] / denominator[observed_valid]
    ).astype(np.float32)
    observed_slope[observed_slope <= 0] = np.nan

    model, polynomial_report = _robust_polynomial_fit(
        rows, cols, slopes, shape, degree, max_points
    )
    return (
        {
            "model": model,
            "observed": observed_slope,
            "height_level_count": height_level_count,
            "height_sign": np.array(sign, dtype=np.float32),
            "offset": np.zeros(shape, dtype=np.float32),
        },
        {
            "height_sign": sign,
            "scalar_phase_per_mm": scalar_slope,
            "pooled_fit_scalar_phase_per_mm": scalar_slope,
            "pooled_local_slope": _finite_stats(slopes),
            "direct_pixel_slope": _finite_stats(observed_slope),
            "pixels_by_distinct_height_level_count": {
                str(level): int(np.count_nonzero(height_level_count == level))
                for level in range(1, int(height_level_count.max()) + 1)
            },
            "minimum_height_levels_for_direct_pixel_fit": min_height_levels,
            "polynomial_model": polynomial_report,
            "model_slope": _finite_stats(model),
        },
    )


def _error_metrics(error: np.ndarray) -> dict[str, Any]:
    error = np.asarray(error, dtype=np.float64)
    error = error[np.isfinite(error)]
    if not error.size:
        return {"valid_pixel_count": 0}
    return {
        "valid_pixel_count": int(error.size),
        "median_error_mm": float(np.median(error)),
        "mae_mm": float(np.mean(np.abs(error))),
        "rmse_mm": float(np.sqrt(np.mean(error * error))),
        "p95_absolute_error_mm": float(np.quantile(np.abs(error), 0.95)),
    }


def _phase_fold(
    error: np.ndarray, absolute: np.ndarray, bins: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    valid = np.isfinite(error) & np.isfinite(absolute)
    values = error[valid].astype(np.float64)
    phase = np.mod(absolute[valid].astype(np.float64), 2.0 * np.pi)
    if values.size < bins * 20:
        return {"available": False, "reason": "insufficient valid pixels"}, []
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    if mad > 0:
        keep = np.abs(values - median) <= 6.0 * 1.4826 * mad
        values, phase = values[keep], phase[keep]

    edges = np.linspace(0.0, 2.0 * np.pi, bins + 1)
    index = np.clip(np.digitize(phase, edges) - 1, 0, bins - 1)
    rows: list[dict[str, Any]] = []
    predicted = np.full(values.shape, np.nan, dtype=np.float64)
    bin_means: list[float] = []
    for bin_index in range(bins):
        selected = index == bin_index
        count = int(selected.sum())
        mean = float(np.mean(values[selected])) if count else math.nan
        bin_means.append(mean)
        if count:
            predicted[selected] = mean
        rows.append(
            {
                "bin": bin_index,
                "phase_center_rad": float((edges[bin_index] + edges[bin_index + 1]) / 2.0),
                "count": count,
                "mean_error_mm": None if not np.isfinite(mean) else mean,
            }
        )
    finite_means = np.asarray(bin_means)[np.isfinite(bin_means)]
    centered = values - float(np.mean(values))
    total_ss = float(np.sum(centered * centered))
    residual_ss = float(np.nansum((values - predicted) ** 2))
    binned_r2 = max(0.0, 1.0 - residual_ss / total_ss) if total_ss > 0 else 0.0

    harmonic_design = np.column_stack(
        (np.ones(phase.size), np.sin(phase), np.cos(phase))
    )
    harmonic_coefficients = np.linalg.lstsq(harmonic_design, values, rcond=None)[0]
    harmonic_prediction = harmonic_design @ harmonic_coefficients
    harmonic_residual_ss = float(np.sum((values - harmonic_prediction) ** 2))
    harmonic_r2 = (
        max(0.0, 1.0 - harmonic_residual_ss / total_ss) if total_ss > 0 else 0.0
    )
    amplitude = float(np.hypot(harmonic_coefficients[1], harmonic_coefficients[2]))
    return {
        "available": True,
        "used_pixel_count": int(values.size),
        "bin_count": bins,
        "binned_peak_to_peak_mm": float(np.max(finite_means) - np.min(finite_means)),
        "binned_explained_variance_r2": binned_r2,
        "first_harmonic_peak_to_peak_mm": 2.0 * amplitude,
        "first_harmonic_r2": harmonic_r2,
    }, rows


def _write_fold_csv(
    path: Path,
    scalar_rows: list[dict[str, Any]],
    spatial_rows: list[dict[str, Any]],
) -> None:
    spatial_by_bin = {int(row["bin"]): row for row in spatial_rows}
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "bin",
                "phase_center_rad",
                "count",
                "scalar_mean_error_mm",
                "spatial_mean_error_mm",
            ),
        )
        writer.writeheader()
        for row in scalar_rows:
            other = spatial_by_bin.get(int(row["bin"]), {})
            writer.writerow(
                {
                    "bin": row["bin"],
                    "phase_center_rad": row["phase_center_rad"],
                    "count": row["count"],
                    "scalar_mean_error_mm": row["mean_error_mm"],
                    "spatial_mean_error_mm": other.get("mean_error_mm"),
                }
            )


def _save_slope_preview(path: Path, slope: np.ndarray) -> None:
    finite = slope[np.isfinite(slope)]
    if not finite.size:
        return
    low, high = np.quantile(finite, (0.02, 0.98))
    scale = max(float(high - low), 1e-8)
    normalized = np.clip((slope - low) / scale, 0.0, 1.0)
    image = np.where(np.isfinite(normalized), normalized * 255.0, 0.0).astype(np.uint8)
    colored = cv2.applyColorMap(image, cv2.COLORMAP_TURBO)
    colored[~np.isfinite(slope)] = 0
    cv2.imwrite(str(path), colored)


def _validate_holdout(
    item: LoadedView,
    fitted: dict[str, np.ndarray],
    fit_report: dict[str, Any],
    bins: int,
    output_dir: Path,
) -> dict[str, Any]:
    sign = float(fitted["height_sign"])
    scalar_slope = float(fit_report["scalar_phase_per_mm"])
    model = fitted["model"]
    valid = item.mask & np.isfinite(model) & (model > 0)
    scalar_height = sign * item.delta[valid] / scalar_slope
    spatial_height = sign * item.delta[valid] / model[valid]
    scalar_error = scalar_height - item.spec.height_mm
    spatial_error = spatial_height - item.spec.height_mm
    scalar_metrics = _error_metrics(scalar_error)
    spatial_metrics = _error_metrics(spatial_error)
    mae_improvement = None
    p95_improvement = None
    if scalar_metrics.get("mae_mm", 0) > 0:
        mae_improvement = 100.0 * (
            1.0 - spatial_metrics["mae_mm"] / scalar_metrics["mae_mm"]
        )
    if scalar_metrics.get("p95_absolute_error_mm", 0) > 0:
        p95_improvement = 100.0 * (
            1.0
            - spatial_metrics["p95_absolute_error_mm"]
            / scalar_metrics["p95_absolute_error_mm"]
        )

    full_scalar_error = np.full(item.delta.shape, np.nan, dtype=np.float32)
    full_spatial_error = np.full(item.delta.shape, np.nan, dtype=np.float32)
    full_scalar_error[valid] = scalar_error
    full_spatial_error[valid] = spatial_error
    assert item.absolute is not None
    scalar_fold, scalar_rows = _phase_fold(full_scalar_error, item.absolute, bins)
    spatial_fold, spatial_rows = _phase_fold(full_spatial_error, item.absolute, bins)
    safe_name = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in item.spec.sample_name
    )
    _write_fold_csv(
        output_dir / f"phase_fold_{safe_name}_deg_{item.spec.physical_view}.csv",
        scalar_rows,
        spatial_rows,
    )
    return {
        "sample": item.spec.sample_name,
        "height_mm": item.spec.height_mm,
        "physical_view": item.spec.physical_view,
        "source_view_dir": str(item.spec.base_dir),
        "scalar": scalar_metrics,
        "spatial": spatial_metrics,
        "spatial_improvement_percent": {
            "mae": mae_improvement,
            "p95_absolute_error": p95_improvement,
        },
        "phase_fold_scalar_error": scalar_fold,
        "phase_fold_spatial_error": spatial_fold,
    }


def _aggregate_validation(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {"holdout_count": 0}
    scalar_mae = np.asarray([item["scalar"]["mae_mm"] for item in items])
    spatial_mae = np.asarray([item["spatial"]["mae_mm"] for item in items])
    scalar_p95 = np.asarray(
        [item["scalar"]["p95_absolute_error_mm"] for item in items]
    )
    spatial_p95 = np.asarray(
        [item["spatial"]["p95_absolute_error_mm"] for item in items]
    )
    return {
        "holdout_count": len(items),
        "mean_of_holdout_mae_mm": {
            "scalar": float(np.mean(scalar_mae)),
            "spatial": float(np.mean(spatial_mae)),
            "improvement_percent": float(
                100.0 * (1.0 - np.mean(spatial_mae) / np.mean(scalar_mae))
            ),
        },
        "mean_of_holdout_p95_absolute_error_mm": {
            "scalar": float(np.mean(scalar_p95)),
            "spatial": float(np.mean(spatial_p95)),
            "improvement_percent": float(
                100.0 * (1.0 - np.mean(spatial_p95) / np.mean(scalar_p95))
            ),
        },
    }


def _load_all(specs: Iterable[ViewInput], need_absolute: bool) -> list[LoadedView]:
    loaded: list[LoadedView] = []
    for spec in specs:
        loaded.append(_load_view(spec, need_absolute=need_absolute))
        print(
            f"[ok] {spec.sample_name} physical={spec.physical_view} "
            f"height={spec.height_mm:g}mm pixels={int(loaded[-1].mask.sum())} "
            f"dir={spec.base_dir}"
        )
    return loaded


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest_path = args.manifest.expanduser().resolve()
    manifest = _read_json(manifest_path)
    fit_specs = _view_inputs(manifest, manifest_path.parent, "fit")
    holdout_specs = _view_inputs(manifest, manifest_path.parent, "holdout")
    if not fit_specs:
        raise ValueError("manifest.fit must contain known-height scans")
    if not holdout_specs and not args.check_only:
        raise ValueError("manifest.holdout must contain independent validation scans")
    fit_loaded = _load_all(fit_specs, need_absolute=False)
    holdout_loaded = _load_all(holdout_specs, need_absolute=True)
    all_shapes = {item.delta.shape for item in fit_loaded + holdout_loaded}
    if len(all_shapes) != 1:
        raise ValueError(f"all scans must have one image shape, got {sorted(all_shapes)}")
    if args.check_only:
        print(
            f"Preflight OK: fit views={len(fit_loaded)}, "
            f"holdout views={len(holdout_loaded)}, image_shape={next(iter(all_shapes))}"
        )
        return 0

    settings = manifest.get("settings", {})
    if not isinstance(settings, dict):
        raise ValueError("manifest.settings must be an object")
    degree = int(settings.get("polynomial_degree", 2))
    min_height_levels = int(settings.get("minimum_height_levels_per_pixel", 2))
    max_points = int(settings.get("maximum_polynomial_fit_points", 300_000))
    fold_bins = int(settings.get("phase_fold_bins", 36))
    if min_height_levels < 1:
        raise ValueError("minimum_height_levels_per_pixel must be >= 1")
    if max_points < 100:
        raise ValueError("maximum_polynomial_fit_points must be >= 100")
    if fold_bins < 8:
        raise ValueError("phase_fold_bins must be >= 8")
    baseline_slopes = manifest.get("baseline_phase_per_mm_by_physical_view", {})
    if not isinstance(baseline_slopes, dict):
        raise ValueError(
            "manifest.baseline_phase_per_mm_by_physical_view must be an object"
        )

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    calibration_arrays: dict[str, np.ndarray] = {}
    fit_reports: dict[int, dict[str, Any]] = {}
    fitted_views: dict[int, dict[str, np.ndarray]] = {}
    for physical_view in (0, 180):
        selected = [
            item for item in fit_loaded if item.spec.physical_view == physical_view
        ]
        fitted, view_report = _fit_view(
            selected, degree, min_height_levels, max_points
        )
        configured_baseline = baseline_slopes.get(
            str(physical_view), baseline_slopes.get(physical_view)
        )
        if configured_baseline is not None:
            configured_baseline = float(configured_baseline)
            if not np.isfinite(configured_baseline) or configured_baseline <= 0:
                raise ValueError(
                    f"baseline slope for physical view {physical_view} must be positive"
                )
            view_report["scalar_phase_per_mm"] = configured_baseline
            view_report["scalar_baseline_source"] = "manifest_current_calibration"
        else:
            view_report["scalar_baseline_source"] = "pooled_fit_observations"
        fitted_views[physical_view] = fitted
        fit_reports[physical_view] = view_report
        calibration_arrays[f"phase_linear_phase_per_mm_{physical_view}"] = fitted[
            "model"
        ]
        calibration_arrays[f"phase_linear_offset_phase_{physical_view}"] = fitted[
            "offset"
        ]
        calibration_arrays[f"phase_linear_height_sign_{physical_view}"] = fitted[
            "height_sign"
        ]
        np.save(output_dir / f"slope_observed_deg_{physical_view}.npy", fitted["observed"])
        np.save(
            output_dir / f"slope_height_level_count_deg_{physical_view}.npy",
            fitted["height_level_count"],
        )
        _save_slope_preview(
            output_dir / f"slope_model_deg_{physical_view}.png", fitted["model"]
        )

    calibration_id = str(
        manifest.get("calibration_id", "phase_linear_spatial_experiment")
    )
    phase_axis = str(manifest.get("phase_axis", "y")).strip().lower()
    if phase_axis not in ("x", "y"):
        raise ValueError("manifest.phase_axis must be x or y")
    calibration_arrays["phase_axis"] = np.array(phase_axis)
    calibration_arrays["calibration_id"] = np.array(calibration_id)
    calibration_path = output_dir / "phase_linear_spatial_calibration.npz"
    np.savez_compressed(calibration_path, **calibration_arrays)

    validation: list[dict[str, Any]] = []
    for item in holdout_loaded:
        physical_view = item.spec.physical_view
        validation.append(
            _validate_holdout(
                item,
                fitted_views[physical_view],
                fit_reports[physical_view],
                fold_bins,
                output_dir,
            )
        )

    report = {
        "calibration_id": calibration_id,
        "method": "zero_anchored_spatial_polynomial_phase_linear",
        "phase_axis": phase_axis,
        "image_shape": list(next(iter(all_shapes))),
        "calibration_npz": str(calibration_path),
        "fit": {f"deg_{view}": fit_reports[view] for view in (0, 180)},
        "holdout": validation,
        "holdout_summary": _aggregate_validation(validation),
        "interpretation_rules": {
            "spatial_model_support": (
                "Require improvement on independent hold-outs, not in-sample fit. "
                "A wide slope interval alone is not proof because block flatness, "
                "noise, and height nonlinearity also contribute."
            ),
            "gamma_support": (
                "A repeatable wrapped-phase first-harmonic amplitude that remains "
                "after spatial calibration supports a periodic phase/gamma term."
            ),
            "production_warning": (
                "Do not replace the production calibration unless both physical "
                "views and every hold-out height improve without new regional bias."
            ),
        },
    }
    report_path = output_dir / "phase_linear_spatial_experiment_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Saved calibration: {calibration_path}")
    print(f"Saved report: {report_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, KeyError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
