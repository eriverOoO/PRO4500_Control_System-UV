"""Standalone camera-projector geometry calibration primitives.

This module intentionally does not import the production capture controller or
the PCB decoder.  The only shared component is the low-level XIMEA provider.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np


PHASES = (0, 90, 180, 270)


@dataclass(frozen=True)
class PatternProfile:
    width: int = 1280
    height: int = 800
    period_px: int = 12

    def axis_length(self, axis: str) -> int:
        return self.width if axis == "x" else self.height

    def gray_bits(self, axis: str) -> int:
        cycles = math.ceil(self.axis_length(axis) / self.period_px)
        return max(1, math.ceil(math.log2(cycles)))


def _write_png(path: Path, image: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), image):
        raise RuntimeError(f"Could not write image: {path}")


def generate_patterns(root: Path, profile: PatternProfile) -> dict[str, Any]:
    """Generate and persist all patterns before the first hardware capture."""
    root.mkdir(parents=True, exist_ok=True)
    shape = (profile.height, profile.width)
    _write_png(root / "reference_black.png", np.zeros(shape, dtype=np.uint8))
    _write_png(root / "reference_white.png", np.full(shape, 255, dtype=np.uint8))
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "projector_size_px": [profile.width, profile.height],
        "period_px": profile.period_px,
        "axes": {},
    }
    yy, xx = np.indices(shape)
    for axis, coordinate in (("x", xx), ("y", yy)):
        axis_dir = root / axis
        bit_count = profile.gray_bits(axis)
        cycle = coordinate // profile.period_px
        gray = np.bitwise_xor(cycle, cycle >> 1)
        entries: list[dict[str, Any]] = []
        for bit_index in range(bit_count):
            shift = bit_count - 1 - bit_index
            normal = (((gray >> shift) & 1) * 255).astype(np.uint8)
            inverse = 255 - normal
            normal_name = f"gray_{bit_index:02d}.png"
            inverse_name = f"gray_{bit_index:02d}_inv.png"
            _write_png(axis_dir / normal_name, normal)
            _write_png(axis_dir / inverse_name, inverse)
            entries.extend(
                (
                    {"kind": "gray", "bit": bit_index, "inverse": False, "file": normal_name},
                    {"kind": "gray", "bit": bit_index, "inverse": True, "file": inverse_name},
                )
            )
        phase = 2.0 * np.pi * coordinate.astype(np.float64) / profile.period_px
        for phase_deg in PHASES:
            image = np.rint(127.5 + 127.5 * np.cos(phase + np.deg2rad(phase_deg)))
            name = f"sine_{phase_deg:03d}.png"
            _write_png(axis_dir / name, np.clip(image, 0, 255).astype(np.uint8))
            entries.append({"kind": "sine", "phase_deg": phase_deg, "file": name})
        manifest["axes"][axis] = {"gray_bits": bit_count, "sequence": entries}
    (root / "pattern_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def detect_checkerboard(
    black: np.ndarray,
    white: np.ndarray,
    inner_corners: tuple[int, int] | list[tuple[int, int]],
) -> tuple[np.ndarray | None, np.ndarray, dict[str, Any]]:
    """Detect the largest configured local grid visible in the camera FOV."""
    if isinstance(inner_corners, tuple) and len(inner_corners) == 2 and isinstance(inner_corners[0], int):
        candidates = [inner_corners]
    else:
        candidates = [tuple(int(v) for v in item) for item in inner_corners]
    candidates.sort(key=lambda size: size[0] * size[1], reverse=True)
    black_gray = _gray_u8(black)
    white_gray = _gray_u8(white)
    response = cv2.subtract(white_gray, black_gray)
    response = cv2.normalize(response, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    sources = (
        ("projector_black", black_gray),
        ("projector_white", white_gray),
        ("white_minus_black", response),
    )
    flags = cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY | cv2.CALIB_CB_NORMALIZE_IMAGE
    report: dict[str, Any] = {
        "found": False,
        "visible_grid_candidates": [list(size) for size in candidates],
        "contrast_p95_minus_p05": float(np.percentile(response, 95) - np.percentile(response, 5)),
        "laplacian_variance": float(cv2.Laplacian(black_gray, cv2.CV_64F).var()),
    }
    selected_image = response
    corners = None
    selected_size: tuple[int, int] | None = None
    selected_source = "none"
    for size in candidates:
        for source_name, source_image in sources:
            for polarity_name, candidate_image in (("normal", source_image), ("inverted", 255 - source_image)):
                found, detected = cv2.findChessboardCornersSB(candidate_image, size, flags=flags)
                if found and detected is not None:
                    corners = detected
                    selected_size = size
                    selected_source = f"{source_name}:{polarity_name}"
                    selected_image = candidate_image
                    break
            if corners is not None:
                break
        if corners is not None:
            break
    if corners is None or selected_size is None:
        return None, selected_image, report
    points = corners.reshape(-1, 2).astype(np.float32)
    hull_area = float(cv2.contourArea(cv2.convexHull(points)))
    report["found"] = True
    report["detected_inner_corners"] = list(selected_size)
    report["detection_source"] = selected_source
    report["board_image_area_ratio"] = hull_area / float(selected_image.size)
    report["corner_count"] = int(points.shape[0])
    return points, selected_image, report


def checkerboard_grid_candidates(
    minimum: tuple[int, int],
    maximum: tuple[int, int],
    minimum_corner_count: int = 12,
) -> list[tuple[int, int]]:
    """Return supported partial grids, largest first, excluding underconstrained grids."""
    candidates = [
        (cols, rows)
        for cols in range(int(minimum[0]), int(maximum[0]) + 1)
        for rows in range(int(minimum[1]), int(maximum[1]) + 1)
        if cols * rows >= int(minimum_corner_count)
    ]
    return sorted(candidates, key=lambda size: size[0] * size[1], reverse=True)


def draw_checkerboard_detection(
    image: np.ndarray,
    inner_corners: tuple[int, int],
    corners: np.ndarray | None,
) -> np.ndarray:
    preview = cv2.cvtColor(_gray_u8(image), cv2.COLOR_GRAY2BGR)
    if corners is not None:
        cv2.drawChessboardCorners(preview, inner_corners, corners.reshape(-1, 1, 2), True)
    return preview


def _gray_u8(image: np.ndarray) -> np.ndarray:
    array = np.asarray(image)
    if array.ndim == 3:
        array = cv2.cvtColor(array, cv2.COLOR_BGR2GRAY)
    if array.dtype == np.uint8:
        return array
    maximum = float(np.iinfo(array.dtype).max) if np.issubdtype(array.dtype, np.integer) else 1.0
    return np.clip(array.astype(np.float32) * (255.0 / max(maximum, 1.0)), 0, 255).astype(np.uint8)


def bilinear_sample(image: np.ndarray, points: np.ndarray) -> np.ndarray:
    map_x = points[:, 0].astype(np.float32).reshape(-1, 1)
    map_y = points[:, 1].astype(np.float32).reshape(-1, 1)
    return cv2.remap(
        np.asarray(image, dtype=np.float32), map_x, map_y, cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT, borderValue=np.nan,
    ).reshape(-1)


def gray_to_binary(gray: np.ndarray, bits: int) -> np.ndarray:
    binary = gray.astype(np.int32).copy()
    shifted = binary.copy()
    for _ in range(bits):
        shifted >>= 1
        binary ^= shifted
    return binary


def decode_projector_axis(
    capture_dir: Path,
    pattern_axis_manifest: dict[str, Any],
    corners: np.ndarray,
    period_px: int,
    axis_length: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Decode continuous projector coordinates only at checkerboard corners."""
    bit_count = int(pattern_axis_manifest["gray_bits"])
    gray_value = np.zeros(corners.shape[0], dtype=np.int32)
    confidence = np.full(corners.shape[0], np.inf, dtype=np.float32)
    for bit_index in range(bit_count):
        normal = cv2.imread(str(capture_dir / f"gray_{bit_index:02d}.png"), cv2.IMREAD_GRAYSCALE)
        inverse = cv2.imread(str(capture_dir / f"gray_{bit_index:02d}_inv.png"), cv2.IMREAD_GRAYSCALE)
        if normal is None or inverse is None:
            raise ValueError(f"Missing Gray pair {bit_index} in {capture_dir}")
        delta = bilinear_sample(normal, corners) - bilinear_sample(inverse, corners)
        gray_value = (gray_value << 1) | (delta >= 0).astype(np.int32)
        confidence = np.minimum(confidence, np.abs(delta))
    cycle = gray_to_binary(gray_value, bit_count)
    sine = []
    for phase_deg in PHASES:
        image = cv2.imread(str(capture_dir / f"sine_{phase_deg:03d}.png"), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ValueError(f"Missing sine {phase_deg} in {capture_dir}")
        sine.append(bilinear_sample(image, corners))
    wrapped = np.mod(np.arctan2(sine[3] - sine[1], sine[0] - sine[2]), 2.0 * np.pi)
    coordinate = cycle.astype(np.float64) * period_px + wrapped * period_px / (2.0 * np.pi)
    valid = (
        np.isfinite(coordinate)
        & (coordinate >= 0)
        & (coordinate < axis_length)
        & (confidence >= 5.0)
    )
    return coordinate.astype(np.float32), valid


def decode_projector_axis_dense(
    capture_dir: Path,
    pattern_axis_manifest: dict[str, Any],
    period_px: int,
    axis_length: int,
    *,
    minimum_gray_contrast: float,
    minimum_modulation: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Decode a dense axis map while rejecting ink-dark/low-modulation pixels."""
    bit_count = int(pattern_axis_manifest["gray_bits"])
    gray_value: np.ndarray | None = None
    confidence: np.ndarray | None = None
    for bit_index in range(bit_count):
        normal = cv2.imread(str(capture_dir / f"gray_{bit_index:02d}.png"), cv2.IMREAD_GRAYSCALE)
        inverse = cv2.imread(str(capture_dir / f"gray_{bit_index:02d}_inv.png"), cv2.IMREAD_GRAYSCALE)
        if normal is None or inverse is None:
            raise ValueError(f"Missing Gray pair {bit_index} in {capture_dir}")
        delta = normal.astype(np.float32) - inverse.astype(np.float32)
        if gray_value is None:
            gray_value = np.zeros(normal.shape, dtype=np.int32)
            confidence = np.full(normal.shape, np.inf, dtype=np.float32)
        gray_value = (gray_value << 1) | (delta >= 0).astype(np.int32)
        confidence = np.minimum(confidence, np.abs(delta))
    assert gray_value is not None and confidence is not None
    sine_images: list[np.ndarray] = []
    for phase_deg in PHASES:
        image = cv2.imread(str(capture_dir / f"sine_{phase_deg:03d}.png"), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ValueError(f"Missing sine {phase_deg} in {capture_dir}")
        sine_images.append(image.astype(np.float32))
    in_phase = sine_images[0] - sine_images[2]
    quadrature = sine_images[3] - sine_images[1]
    modulation = 0.5 * np.hypot(in_phase, quadrature)
    wrapped = np.mod(np.arctan2(quadrature, in_phase), 2.0 * np.pi)
    cycle = gray_to_binary(gray_value, bit_count)
    coordinate = cycle.astype(np.float32) * period_px + wrapped.astype(np.float32) * period_px / (2.0 * np.pi)
    valid = (
        np.isfinite(coordinate)
        & (coordinate >= 0)
        & (coordinate < axis_length)
        & (confidence >= float(minimum_gray_contrast))
        & (modulation >= float(minimum_modulation))
    )
    return coordinate.astype(np.float32), valid


def estimate_projector_corners_from_local_homographies(
    camera_corners: np.ndarray,
    projector_x: np.ndarray,
    projector_y: np.ndarray,
    valid: np.ndarray,
    *,
    patch_size_px: int,
    minimum_valid_pixels: int,
) -> tuple[np.ndarray, np.ndarray, list[dict[str, Any]]]:
    """Evaluate projector coordinates at ink boundaries from nearby valid pixels.

    Printed black squares may suppress all UV modulation at the mathematical
    checkerboard corner.  A small surrounding patch still contains illuminated
    white regions.  Fit a robust local camera-to-projector homography on those
    pixels and evaluate it at the sub-pixel checkerboard corner.
    """
    if patch_size_px < 5 or patch_size_px % 2 == 0:
        raise ValueError("local patch size must be an odd integer >= 5")
    radius = patch_size_px // 2
    height, width = valid.shape
    estimates = np.full((camera_corners.shape[0], 2), np.nan, dtype=np.float32)
    accepted = np.zeros(camera_corners.shape[0], dtype=bool)
    reports: list[dict[str, Any]] = []
    for index, corner in enumerate(camera_corners):
        cx, cy = (float(corner[0]), float(corner[1]))
        x0 = max(0, int(math.floor(cx)) - radius)
        x1 = min(width, int(math.floor(cx)) + radius + 1)
        y0 = max(0, int(math.floor(cy)) - radius)
        y1 = min(height, int(math.floor(cy)) + radius + 1)
        patch_valid = valid[y0:y1, x0:x1]
        yy, xx = np.nonzero(patch_valid)
        count = int(xx.size)
        report: dict[str, Any] = {"corner_index": index, "valid_patch_pixels": count, "accepted": False}
        if count < minimum_valid_pixels:
            reports.append(report)
            continue
        camera_points = np.column_stack((xx + x0, yy + y0)).astype(np.float32)
        projector_points = np.column_stack(
            (projector_x[y0:y1, x0:x1][patch_valid], projector_y[y0:y1, x0:x1][patch_valid])
        ).astype(np.float32)
        homography, inliers = cv2.findHomography(
            camera_points, projector_points, method=cv2.RANSAC, ransacReprojThreshold=1.5
        )
        if homography is None or inliers is None or int(np.count_nonzero(inliers)) < minimum_valid_pixels:
            reports.append(report)
            continue
        value = cv2.perspectiveTransform(
            np.array([[[cx, cy]]], dtype=np.float32), homography
        ).reshape(2)
        if not np.all(np.isfinite(value)):
            reports.append(report)
            continue
        estimates[index] = value
        accepted[index] = True
        report.update(
            {
                "accepted": True,
                "ransac_inliers": int(np.count_nonzero(inliers)),
                "projector_xy": value.tolist(),
            }
        )
        reports.append(report)
    return estimates, accepted, reports


def board_object_points(inner_corners: tuple[int, int], square_size_mm: float) -> np.ndarray:
    cols, rows = inner_corners
    points = np.zeros((cols * rows, 3), dtype=np.float32)
    grid = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2)
    points[:, :2] = grid.astype(np.float32) * float(square_size_mm)
    return points


def strict_checkerboard_correspondence_mask(
    camera_corners: np.ndarray,
    projector_corners: np.ndarray,
    decoded_mask: np.ndarray,
    inner_corners: tuple[int, int],
    projector_size: tuple[int, int],
    *,
    max_camera_grid_residual_px: float = 5.0,
    max_projector_grid_residual_px: float = 8.0,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Reject decoded points that do not form the same planar checkerboard grid."""
    cols, rows = (int(v) for v in inner_corners)
    camera = np.asarray(camera_corners, dtype=np.float32).reshape(-1, 2)
    projector = np.asarray(projector_corners, dtype=np.float32).reshape(-1, 2)
    decoded = np.asarray(decoded_mask, dtype=bool).reshape(-1)
    expected = cols * rows
    if camera.shape[0] != expected or projector.shape[0] != expected or decoded.size != expected:
        raise ValueError("Checkerboard correspondence count does not match detected grid")
    projector_width, projector_height = (int(v) for v in projector_size)
    finite = np.all(np.isfinite(camera), axis=1) & np.all(np.isfinite(projector), axis=1)
    in_bounds = (
        (projector[:, 0] >= 0)
        & (projector[:, 0] < projector_width)
        & (projector[:, 1] >= 0)
        & (projector[:, 1] < projector_height)
    )
    initial = decoded & finite & in_bounds
    accepted = np.zeros(expected, dtype=bool)
    report: dict[str, Any] = {
        "decoded_corner_count": int(np.count_nonzero(decoded)),
        "projector_in_bounds_count": int(np.count_nonzero(initial)),
        "strict_corner_count": 0,
        "camera_grid_residual_limit_px": float(max_camera_grid_residual_px),
        "projector_grid_residual_limit_px": float(max_projector_grid_residual_px),
    }
    if np.count_nonzero(initial) < 4:
        return accepted, report
    ideal = np.array([(x, y) for y in range(rows) for x in range(cols)], dtype=np.float32)
    camera_h, _ = cv2.findHomography(
        ideal[initial], camera[initial], cv2.RANSAC, float(max_camera_grid_residual_px)
    )
    projector_h, _ = cv2.findHomography(
        ideal[initial], projector[initial], cv2.RANSAC, float(max_projector_grid_residual_px)
    )
    if camera_h is None or projector_h is None:
        return accepted, report
    camera_fit = cv2.perspectiveTransform(ideal[:, None, :], camera_h).reshape(-1, 2)
    projector_fit = cv2.perspectiveTransform(ideal[:, None, :], projector_h).reshape(-1, 2)
    camera_residual = np.linalg.norm(camera - camera_fit, axis=1)
    projector_residual = np.linalg.norm(projector - projector_fit, axis=1)
    accepted = (
        initial
        & (camera_residual <= float(max_camera_grid_residual_px))
        & (projector_residual <= float(max_projector_grid_residual_px))
    )
    report.update(
        {
            "strict_corner_count": int(np.count_nonzero(accepted)),
            "rejected_grid_outlier_count": int(np.count_nonzero(initial & ~accepted)),
            "camera_grid_residual_max_accepted_px": (
                float(np.max(camera_residual[accepted])) if np.any(accepted) else None
            ),
            "projector_grid_residual_max_accepted_px": (
                float(np.max(projector_residual[accepted])) if np.any(accepted) else None
            ),
        }
    )
    return accepted, report


def checkerboard_motion_rms(
    initial_corners: np.ndarray, final_corners: np.ndarray
) -> tuple[float, str]:
    """Measure pose motion while allowing OpenCV's possible 180-degree ordering flip."""
    initial = np.asarray(initial_corners, dtype=np.float32).reshape(-1, 2)
    final = np.asarray(final_corners, dtype=np.float32).reshape(-1, 2)
    if initial.shape != final.shape or initial.size == 0:
        raise ValueError("Initial/final checkerboard corner counts do not match")
    direct = float(np.sqrt(np.mean(np.sum((initial - final) ** 2, axis=1))))
    reversed_order = float(
        np.sqrt(np.mean(np.sum((initial - final[::-1]) ** 2, axis=1)))
    )
    if direct <= reversed_order:
        return direct, "direct"
    return reversed_order, "reversed"


def estimate_image_motion_rms(
    initial_image: np.ndarray, final_image: np.ndarray
) -> tuple[float | None, float | None]:
    """Estimate board motion from equal white-pattern frames when grid size changes."""
    initial = _gray_u8(initial_image).astype(np.float32) / 255.0
    final = _gray_u8(final_image).astype(np.float32) / 255.0
    if initial.shape != final.shape:
        return None, None
    initial = cv2.GaussianBlur(initial, (5, 5), 0)
    final = cv2.GaussianBlur(final, (5, 5), 0)
    warp = np.eye(2, 3, dtype=np.float32)
    try:
        score, warp = cv2.findTransformECC(
            initial,
            final,
            warp,
            cv2.MOTION_EUCLIDEAN,
            (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 100, 1e-6),
            None,
            5,
        )
    except cv2.error:
        return None, None
    height, width = initial.shape
    sample = np.array(
        [[0.0, 0.0], [width - 1.0, 0.0], [0.0, height - 1.0], [width - 1.0, height - 1.0]],
        dtype=np.float32,
    )
    transformed = cv2.transform(sample.reshape(-1, 1, 2), warp).reshape(-1, 2)
    rms = float(np.sqrt(np.mean(np.sum((sample - transformed) ** 2, axis=1))))
    return rms, float(score)


def solve_geometry(session: Path) -> dict[str, Any]:
    manifest_path = session / "session_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    square_mm = float(manifest["checkerboard"]["square_size_mm"])
    pattern = manifest["pattern"]
    projector_width, projector_height = (int(v) for v in pattern["projector_size_px"])
    pattern_manifest = json.loads((session / "patterns" / "pattern_manifest.json").read_text(encoding="utf-8"))
    object_points: list[np.ndarray] = []
    camera_points: list[np.ndarray] = []
    projector_points: list[np.ndarray] = []
    pose_reports: list[dict[str, Any]] = []
    camera_size: tuple[int, int] | None = None
    for pose in manifest.get("captured_poses", []):
        pose_dir = session / pose["relative_dir"]
        corners = np.load(pose_dir / "checkerboard_corners.npy", allow_pickle=False).astype(np.float32)
        detected_grid = tuple(
            int(v) for v in pose["checkerboard_detection"]["detected_inner_corners"]
        )
        object_template = board_object_points(detected_grid, square_mm)
        corner_config = manifest.get("projector_corner_estimation", {})
        x_map, valid_x = decode_projector_axis_dense(
            pose_dir / "x",
            pattern_manifest["axes"]["x"],
            int(pattern["period_px"]),
            projector_width,
            minimum_gray_contrast=float(corner_config.get("minimum_gray_pair_contrast_u8", 5.0)),
            minimum_modulation=float(corner_config.get("minimum_sine_modulation_u8", 5.0)),
        )
        y_map, valid_y = decode_projector_axis_dense(
            pose_dir / "y",
            pattern_manifest["axes"]["y"],
            int(pattern["period_px"]),
            projector_height,
            minimum_gray_contrast=float(corner_config.get("minimum_gray_pair_contrast_u8", 5.0)),
            minimum_modulation=float(corner_config.get("minimum_sine_modulation_u8", 5.0)),
        )
        projector_corners, valid, corner_reports = estimate_projector_corners_from_local_homographies(
            corners,
            x_map,
            y_map,
            valid_x & valid_y,
            patch_size_px=int(corner_config.get("local_patch_size_px", 47)),
            minimum_valid_pixels=int(corner_config.get("minimum_valid_pixels", 24)),
        )
        minimum_decoded_corners = int(
            manifest["checkerboard"].get("minimum_visible_corner_count", 12)
        )
        valid, strict_report = strict_checkerboard_correspondence_mask(
            corners,
            projector_corners,
            valid,
            detected_grid,
            (projector_width, projector_height),
            max_camera_grid_residual_px=float(
                corner_config.get("strict_camera_grid_residual_px", 5.0)
            ),
            max_projector_grid_residual_px=float(
                corner_config.get("strict_projector_grid_residual_px", 8.0)
            ),
        )
        if np.count_nonzero(valid) < minimum_decoded_corners:
            pose_reports.append(
                {
                    "pose_id": pose["pose_id"],
                    "used": False,
                    "reason": "too few strict checkerboard/projector correspondences",
                    "strict_validation": strict_report,
                }
            )
            continue
        camera_image = cv2.imread(str(pose_dir / "reference_white.png"), cv2.IMREAD_GRAYSCALE)
        if camera_image is None:
            raise ValueError(f"Missing reference_white.png in {pose_dir}")
        camera_size = (camera_image.shape[1], camera_image.shape[0])
        object_points.append(object_template[valid])
        camera_points.append(corners[valid].reshape(-1, 1, 2))
        projector_points.append(projector_corners[valid].astype(np.float32).reshape(-1, 1, 2))
        pose_reports.append(
            {
                "pose_id": pose["pose_id"],
                "used": True,
                "decoded_corner_count": int(np.count_nonzero(valid)),
                "strict_validation": strict_report,
                "projector_corner_method": (
                    f"{int(corner_config.get('local_patch_size_px', 47))}px_"
                    "local_homography_on_uv_valid_pixels"
                ),
                "corner_reports": corner_reports,
            }
        )
    quality_limits = manifest.get("calibration_quality", {})
    minimum_poses = int(quality_limits.get("minimum_accepted_poses", 8))
    diagnostic = {
        "captured_pose_count": len(manifest.get("captured_poses", [])),
        "accepted_pose_count": len(object_points),
        "minimum_accepted_poses": minimum_poses,
        "pose_reports": pose_reports,
    }
    (session / "geometry_calibration_diagnostics.json").write_text(
        json.dumps(diagnostic, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if camera_size is None or len(object_points) < minimum_poses:
        raise ValueError(
            f"Only {len(object_points)} of {len(manifest.get('captured_poses', []))} captured poses "
            f"passed the strict checkerboard/projector correspondence test. "
            f"At least {minimum_poses} accepted "
            "checkerboard poses are required; see geometry_calibration_diagnostics.json. "
            "10-16 diverse poses are recommended"
        )
    camera_rms, camera_k, camera_dist, _rvecs, _tvecs = cv2.calibrateCamera(
        object_points, camera_points, camera_size, None, None
    )
    projector_rms, projector_k, projector_dist, _prvecs, _ptvecs = cv2.calibrateCamera(
        object_points, projector_points, (projector_width, projector_height), None, None
    )
    stereo_flags = cv2.CALIB_FIX_INTRINSIC
    stereo_rms, camera_k, camera_dist, projector_k, projector_dist, rotation, translation, essential, fundamental = cv2.stereoCalibrate(
        object_points,
        camera_points,
        projector_points,
        camera_k,
        camera_dist,
        projector_k,
        projector_dist,
        camera_size,
        flags=stereo_flags,
        criteria=(cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 200, 1e-9),
    )
    quality_checks = {
        "accepted_pose_count": len(object_points),
        "minimum_accepted_poses": minimum_poses,
        "camera_rms_pass": float(camera_rms) <= float(quality_limits.get("max_camera_rms_px", 1.0)),
        "projector_rms_pass": float(projector_rms) <= float(quality_limits.get("max_projector_rms_px", 1.5)),
        "stereo_rms_pass": float(stereo_rms) <= float(quality_limits.get("max_stereo_rms_px", 2.0)),
        "limits": quality_limits,
    }
    quality_checks["valid"] = bool(
        quality_checks["camera_rms_pass"]
        and quality_checks["projector_rms_pass"]
        and quality_checks["stereo_rms_pass"]
    )
    result = {
        "schema_version": 1,
        "method": "gray_phase_camera_projector_stereo_calibration",
        "units": "mm",
        "camera": {
            "image_size_px": list(camera_size),
            "K": camera_k.tolist(),
            "distortion": camera_dist.reshape(-1).tolist(),
            "rms_reprojection_px": float(camera_rms),
        },
        "projector": {
            "image_size_px": [projector_width, projector_height],
            "K": projector_k.tolist(),
            "distortion": projector_dist.reshape(-1).tolist(),
            "rms_reprojection_px": float(projector_rms),
        },
        "camera_to_projector": {
            "R": rotation.tolist(),
            "t_mm": translation.reshape(-1).tolist(),
            "baseline_mm": float(np.linalg.norm(translation)),
            "stereo_rms_px": float(stereo_rms),
            "essential": essential.tolist(),
            "fundamental": fundamental.tolist(),
        },
        "checkerboard": manifest["checkerboard"],
        "pose_reports": pose_reports,
        "quality": quality_checks,
        "stage_plane": None,
    }
    output = session / "geometry_calibration.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def estimate_stage_plane_from_aruco(
    image: np.ndarray,
    calibration: dict[str, Any],
    dictionary_name: str,
    marker_size_mm: float,
    requested_ids: list[int],
) -> tuple[dict[str, Any], np.ndarray]:
    gray = _gray_u8(image)
    dictionary_id = getattr(cv2.aruco, dictionary_name)
    dictionary = cv2.aruco.getPredefinedDictionary(dictionary_id)
    detector = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())
    corners, ids, _rejected = detector.detectMarkers(gray)
    if ids is None:
        raise ValueError("No ArUco marker was detected in the stage image")
    camera_k = np.asarray(calibration["camera"]["K"], dtype=np.float64)
    distortion = np.asarray(calibration["camera"]["distortion"], dtype=np.float64)
    half = marker_size_mm / 2.0
    object_corners = np.array(
        [[-half, half, 0], [half, half, 0], [half, -half, 0], [-half, -half, 0]], dtype=np.float32
    )
    normals: list[np.ndarray] = []
    offsets: list[float] = []
    used_ids: list[int] = []
    for marker_corners, marker_id in zip(corners, ids.reshape(-1), strict=True):
        marker_id = int(marker_id)
        if requested_ids and marker_id not in requested_ids:
            continue
        ok, rvec, tvec = cv2.solvePnP(
            object_corners, marker_corners.reshape(4, 2), camera_k, distortion,
            flags=cv2.SOLVEPNP_IPPE_SQUARE,
        )
        if not ok:
            continue
        rotation, _ = cv2.Rodrigues(rvec)
        normal = rotation[:, 2]
        if normal[2] > 0:
            normal = -normal
        normal /= np.linalg.norm(normal)
        normals.append(normal)
        offsets.append(float(-normal @ tvec.reshape(3)))
        used_ids.append(marker_id)
    if not normals:
        raise ValueError("Requested ArUco markers were not detected or pose estimation failed")
    normal = np.mean(np.stack(normals), axis=0)
    normal /= np.linalg.norm(normal)
    offset = float(np.median(offsets))
    preview = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    cv2.aruco.drawDetectedMarkers(preview, corners, ids)
    return {
        "equation_camera_coordinates": "normal dot X + offset_mm = 0",
        "normal": normal.tolist(),
        "offset_mm": offset,
        "marker_size_mm": float(marker_size_mm),
        "marker_ids": used_ids,
        "z0_is_marker_print_surface": True,
    }, preview
