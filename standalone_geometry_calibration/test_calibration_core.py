from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from standalone_geometry_calibration.app import CalibrationCapture
from standalone_geometry_calibration.calibration_core import (
    PatternProfile,
    checkerboard_grid_candidates,
    checkerboard_motion_rms,
    charuco_object_points,
    create_charuco_board,
    detect_charuco,
    estimate_image_motion_rms,
    decode_projector_axis,
    estimate_projector_corners_from_local_homographies,
    generate_patterns,
    gray_to_binary,
    strict_checkerboard_correspondence_mask,
)


def test_calibration_capture_uses_separate_detection_and_pattern_exposures() -> None:
    capture = CalibrationCapture.__new__(CalibrationCapture)
    capture.config = {
        "camera": {"ximea": {"exposure_us": 200000, "gain_db": 0.0}},
        "checkerboard": {"detection_exposure_us": 2000000},
    }

    assert capture._capture_exposure() == (200000, 0.0)
    assert capture._detection_exposure() == (2000000, 0.0)


def test_stage_aruco_size_is_separate_from_charuco_board_marker() -> None:
    config_path = Path(__file__).with_name("calibration_config.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert config["checkerboard"]["marker_size_mm"] == 9.0
    assert config["stage_aruco"]["marker_size_mm"] == 12.0


def test_partial_grid_candidates_include_3x4_but_exclude_3x3() -> None:
    candidates = checkerboard_grid_candidates((3, 3), (7, 7), minimum_corner_count=12)
    assert (3, 4) in candidates
    assert (4, 3) in candidates
    assert (3, 3) not in candidates
    assert all(cols * rows >= 12 for cols, rows in candidates)


def test_strict_checkerboard_correspondence_rejects_grid_outliers() -> None:
    ideal = np.array([(x, y) for y in range(4) for x in range(4)], dtype=np.float32)
    camera = ideal * 100.0 + np.array([200.0, 300.0], dtype=np.float32)
    projector = ideal * 40.0 + np.array([50.0, 60.0], dtype=np.float32)
    camera[0] += [60.0, -40.0]
    projector[1] += [30.0, 30.0]
    mask, report = strict_checkerboard_correspondence_mask(
        camera, projector, np.ones(16, dtype=bool), (4, 4), (1280, 800)
    )
    assert not mask[0]
    assert not mask[1]
    assert np.count_nonzero(mask) == 14
    assert report["strict_corner_count"] == 14


def test_checkerboard_motion_rms_accepts_reversed_corner_order() -> None:
    corners = np.array([(x, y) for y in range(4) for x in range(4)], dtype=np.float32)
    rms, order = checkerboard_motion_rms(corners, corners[::-1])
    assert rms == 0.0
    assert order == "reversed"


def test_image_motion_estimation_detects_translation() -> None:
    image = np.zeros((160, 200), dtype=np.uint8)
    cv2.rectangle(image, (40, 30), (160, 130), 255, -1)
    shifted = cv2.warpAffine(image, np.float32([[1, 0, 3], [0, 1, -4]]), (200, 160))
    rms, score = estimate_image_motion_rms(image, shifted)
    assert rms is not None and score is not None
    assert 4.0 <= rms <= 6.0


def test_charuco_detection_preserves_global_corner_ids() -> None:
    config = {
        "squares": [8, 6],
        "square_size_mm": 12.0,
        "marker_size_mm": 9.0,
        "dictionary": "DICT_5X5_250",
        "clahe_clip_limit": 3.0,
    }
    image = create_charuco_board(config).generateImage((800, 600), marginSize=20)
    corners, ids, _detection, report = detect_charuco(image, image, config)
    assert corners is not None and ids is not None
    assert len(ids) == (8 - 1) * (6 - 1)
    object_points = charuco_object_points(config, ids)
    assert np.array_equal(ids, np.arange(len(ids), dtype=np.int32))
    assert np.allclose(object_points[1] - object_points[0], [12.0, 0.0, 0.0])
    assert report["target_type"] == "charuco"


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


def test_current_charuco_session_global_outlier_is_excluded_if_available() -> None:
    session = Path(
        r"C:\Users\LEELAB\Desktop\PRO4500_CONTROL_ximea\captures\charuco_geometry_calibration_session"
    )
    if not (session / "session_manifest.json").is_file():
        return
    from standalone_geometry_calibration.calibration_core import solve_geometry

    generated_paths = (
        session / "geometry_calibration.json",
        session / "geometry_calibration_diagnostics.json",
    )
    original_outputs = {
        path: path.read_bytes() if path.is_file() else None for path in generated_paths
    }
    try:
        result = solve_geometry(session)
    finally:
        for path, original in original_outputs.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)
    assert result["quality"]["valid"]
    assert result["quality"]["accepted_pose_count"] >= 8
    assert "pose_003" in result["quality"]["excluded_global_pose_ids"]


def test_local_homography_estimates_corner_when_center_is_uv_invalid() -> None:
    yy, xx = np.indices((80, 100))
    projector_x = (1.2 * xx + 0.03 * yy + 7.0).astype(np.float32)
    projector_y = (-0.02 * xx + 1.1 * yy + 4.0).astype(np.float32)
    valid = np.ones((80, 100), dtype=bool)
    valid[38:43, 48:53] = False
    corner = np.array([[50.0, 40.0]], dtype=np.float32)
    result, accepted, _reports = estimate_projector_corners_from_local_homographies(
        corner, projector_x, projector_y, valid, patch_size_px=31, minimum_valid_pixels=24
    )
    assert accepted[0]
    assert np.allclose(result[0], [68.2, 47.0], atol=0.1)
