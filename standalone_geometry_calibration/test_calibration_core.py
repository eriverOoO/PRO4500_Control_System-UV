from __future__ import annotations

import json

import cv2
import numpy as np

from standalone_geometry_calibration.calibration_core import (
    PatternProfile,
    decode_projector_axis,
    generate_patterns,
    gray_to_binary,
)


def test_gray_to_binary_round_trip() -> None:
    values = np.arange(128, dtype=np.int32)
    gray = values ^ (values >> 1)
    assert np.array_equal(gray_to_binary(gray, 7), values)


def test_generated_patterns_decode_projector_coordinates(tmp_path) -> None:
    profile = PatternProfile(width=128, height=80, period_px=8)
    manifest = generate_patterns(tmp_path, profile)
    points = np.array([[4.0, 5.0], [27.0, 22.0], [67.0, 50.0], [111.0, 72.0]], dtype=np.float32)
    coordinates, valid = decode_projector_axis(
        tmp_path / "x",
        manifest["axes"]["x"],
        points,
        profile.period_px,
        profile.width,
    )
    assert np.all(valid)
    assert np.allclose(coordinates, points[:, 0], atol=0.15)
    saved = json.loads((tmp_path / "pattern_manifest.json").read_text(encoding="utf-8"))
    assert saved["axes"]["x"]["gray_bits"] == 4


def test_checkerboard_pattern_files_are_lossless_png(tmp_path) -> None:
    generate_patterns(tmp_path, PatternProfile(width=96, height=64, period_px=8))
    image = cv2.imread(str(tmp_path / "x" / "gray_00.png"), cv2.IMREAD_GRAYSCALE)
    assert image is not None
    assert set(np.unique(image)) <= {0, 255}
