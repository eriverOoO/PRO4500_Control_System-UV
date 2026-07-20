"""Automated structural and photometric dataset checks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .dataset import read_png, write_png


def _normalized(image: np.ndarray) -> np.ndarray:
    return image.astype(np.float32) / float(np.iinfo(image.dtype).max)


def validate_dataset(output_dir: Path) -> dict[str, Any]:
    """Validate the complete 44-frame dataset and write useful diagnostics."""

    checks: dict[str, dict[str, Any]] = {}

    def check(name: str, passed: bool, detail: Any = "") -> None:
        checks[name] = {"passed": bool(passed), "detail": detail}

    manifest_path = output_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Missing manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = {f"angle_{angle:03d}/pattern_{index:03d}.png" for angle in (0, 180) for index in range(22)}
    actual = {item["filename"] for item in manifest["frames"]}
    check("manifest_has_44_exact_frames", actual == expected, {"expected": 44, "actual": len(actual)})

    paths = [output_dir / name for name in sorted(expected)]
    check("all_files_exist", all(path.is_file() for path in paths))
    images = [read_png(path) for path in paths if path.is_file()]
    signatures = {(image.shape, str(image.dtype)) for image in images}
    check("consistent_shape_and_dtype", len(images) == 44 and len(signatures) == 1, list(map(str, signatures)))
    check("finite_and_valid_png", all(np.isfinite(image).all() for image in images))

    if len(images) == 44:
        by_angle = {angle: [_normalized(read_png(output_dir / f"angle_{angle:03d}" / f"pattern_{i:03d}.png")) for i in range(22)] for angle in (0, 180)}
        valid_path = output_dir / "ground_truth" / "valid_mask.png"
        valid_0 = read_png(valid_path) > 0 if valid_path.is_file() else np.ones_like(by_angle[0][0], dtype=bool)
        valid_masks = {0: valid_0, 180: np.rot90(valid_0, 2)}
        white_black = all(float(frames[0].mean()) > float(frames[1].mean()) + 0.02 for frames in by_angle.values())
        check("white_brighter_than_black", white_black)

        inverse_correlations = []
        complement_errors = []
        for angle, frames in by_angle.items():
            roi = valid_masks[angle]
            contrast = np.maximum(frames[0] - frames[1], 0.02)
            for index in range(8):
                normal = (frames[index + 2] - frames[1]) / contrast
                inverse = (frames[index + 14] - frames[1]) / contrast
                inverse_correlations.append(float(np.corrcoef(normal[roi], inverse[roi])[0, 1]))
                complement_errors.append(float(np.mean(np.abs(normal[roi] + inverse[roi] - 1.0))))
        check(
            "gray_inverse_complementary",
            max(inverse_correlations) < -0.85 and max(complement_errors) < 0.08,
            {"correlations": inverse_correlations, "mean_sum_errors": complement_errors},
        )

        sine_correlations = []
        for angle, frames in by_angle.items():
            roi = valid_masks[angle]
            contrast = np.maximum(frames[0] - frames[1], 0.02)
            normalized_sines = [(frames[index] - frames[1]) / contrast for index in range(10, 14)]
            sine_correlations.extend([
                float(np.corrcoef(normalized_sines[0][roi], normalized_sines[2][roi])[0, 1]),
                float(np.corrcoef(normalized_sines[1][roi], normalized_sines[3][roi])[0, 1]),
            ])
        check("sine_opposite_phases", max(sine_correlations) < -0.35, sine_correlations)

        diagnostics = output_dir / "diagnostics"
        diagnostics.mkdir(parents=True, exist_ok=True)
        for angle, frames in by_angle.items():
            contrast = np.clip(frames[0] - frames[1], 0, 1)
            modulation = 0.5 * np.sqrt((frames[10] - frames[12]) ** 2 + (frames[13] - frames[11]) ** 2)
            wrapped = np.arctan2(frames[13] - frames[11], frames[10] - frames[12])
            write_png(diagnostics / f"angle_{angle:03d}_contrast.png", np.rint(contrast * 65535).astype(np.uint16))
            write_png(diagnostics / f"angle_{angle:03d}_modulation.png", np.rint(np.clip(modulation, 0, 1) * 65535).astype(np.uint16))
            write_png(diagnostics / f"angle_{angle:03d}_wrapped_phase.png", np.rint((wrapped + np.pi) / (2 * np.pi) * 65535).astype(np.uint16))

    height_0_path = output_dir / "ground_truth" / "angle_000_height_mm.npy"
    height_180_path = output_dir / "ground_truth" / "angle_180_height_mm.npy"
    if height_0_path.is_file() and height_180_path.is_file():
        height_0 = np.load(height_0_path)
        height_180 = np.load(height_180_path)
        check("angle_180_is_rotated_geometry", np.array_equal(height_180, np.rot90(height_0, 2)))
        check("height_is_finite_mm", np.isfinite(height_0).all() and float(height_0.min()) >= 0)
    else:
        check("angle_180_is_rotated_geometry", False, "missing height ground truth")
        check("height_is_finite_mm", False, "missing height ground truth")

    report = {"passed": all(item["passed"] for item in checks.values()), "checks": checks}
    (output_dir / "validation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report
