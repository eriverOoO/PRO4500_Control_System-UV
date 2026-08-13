from __future__ import annotations

import cv2
import numpy as np
import pytest

from structured_light_pc_controller import (
    ExposureBracket,
    FrameGuardConfig,
    HdrConfig,
    QualityGateConfig,
    aruco_stage_geometry,
    aruco_marker_observations,
    assess_fpp_quality,
    compare_aruco_pose_to_prescan,
    create_aruco_alignment,
    merge_hdr_frames,
    load_capture_config,
    select_structured_light_sequence_bracket,
    summarize_quality_issues,
    validate_projected_frame,
)


def _quality_gate() -> QualityGateConfig:
    return QualityGateConfig(
        enabled=True,
        enforcement="record_only",
        white_black_min_contrast_u8=20.0,
        gray_pair_min_valid_ratio=0.05,
        sine_min_modulation_u8=12.0,
        sine_min_valid_ratio=0.05,
        max_decoder_saturation_ratio=0.2,
        max_decoder_dark_ratio=0.8,
    )


def _frame_guard() -> FrameGuardConfig:
    return FrameGuardConfig(
        enabled=True,
        min_illuminated_ratio=0.05,
        min_pattern_change_ratio=0.05,
        min_signal_delta_u8=20.0,
    )


def _single_hdr() -> HdrConfig:
    return HdrConfig(False, 8, 250, 5, 0.0, 235, (ExposureBracket("single", 15000),))


def test_frame_guard_discards_blank_white_and_stale_black() -> None:
    guard = _frame_guard()
    hdr = _single_hdr()
    blank = np.zeros((8, 8), dtype=np.uint8)
    white = np.full((8, 8), 120, dtype=np.uint8)

    rejected_white = validate_projected_frame(
        cv2, frame=blank, projected=white, pattern_id=0, hdr=hdr, guard=guard, white_reference=None, black_reference=None
    )
    rejected_black = validate_projected_frame(
        cv2, frame=white, projected=blank, pattern_id=1, hdr=hdr, guard=guard, white_reference=white, black_reference=None
    )

    assert rejected_white["passed"] is False
    assert rejected_white["reason"] == "white_frame_too_dark"
    assert rejected_black["passed"] is False
    assert rejected_black["reason"] == "black_frame_matches_white"


def test_frame_guard_discards_blank_pattern_after_white_black_references() -> None:
    guard = _frame_guard()
    hdr = _single_hdr()
    white = np.full((8, 8), 120, dtype=np.uint8)
    black = np.zeros((8, 8), dtype=np.uint8)
    pattern = np.zeros((8, 8), dtype=np.uint8)
    pattern[:, :4] = 100

    rejected = validate_projected_frame(
        cv2, frame=black, projected=pattern, pattern_id=2, hdr=hdr, guard=guard, white_reference=white, black_reference=black
    )
    accepted = validate_projected_frame(
        cv2, frame=pattern, projected=pattern, pattern_id=2, hdr=hdr, guard=guard, white_reference=white, black_reference=black
    )

    assert rejected["passed"] is False
    assert rejected["reason"] == "projector_blank_or_black_frame"
    assert accepted["passed"] is True


def _sequence_frames(white: int, black: int, on: int, off: int) -> dict[int, tuple[list[np.ndarray], list[float]]]:
    frames: dict[int, tuple[list[np.ndarray], list[float]]] = {}
    for pattern_id in range(22):
        if pattern_id == 0:
            value = white
        elif pattern_id == 1:
            value = black
        elif 2 <= pattern_id <= 9:
            value = on
        elif 14 <= pattern_id <= 21:
            value = off
        elif pattern_id in (10, 12):
            value = on if pattern_id == 10 else off
        else:
            value = (on + off) // 2
        frames[pattern_id] = ([np.full((6, 8), value, dtype=np.uint8)], [0.0])
    return frames


def test_structured_light_selector_prefers_unsaturated_complete_sequence() -> None:
    brackets = (
        ExposureBracket("short", 2500),
        ExposureBracket("long", 80000),
    )
    hdr = HdrConfig(True, 16, 250, 5, 0.0, 235, brackets)
    short = _sequence_frames(100, 10, 90, 20)
    long = _sequence_frames(255, 20, 255, 50)
    captures = {
        pattern_id: (
            [short[pattern_id][0][0], long[pattern_id][0][0]],
            [0.0, 0.0],
        )
        for pattern_id in range(22)
    }

    selected, report = select_structured_light_sequence_bracket(
        cv2, captures, brackets, hdr
    )

    assert selected == 0
    assert report["selected_bracket"] == "short"
    assert report["candidates"][0]["combined_valid_ratio"] == 1.0
    assert report["candidates"][1]["combined_valid_ratio"] == 0.0


def test_selected_sequence_bracket_preserves_native_decoder_signal() -> None:
    brackets = (ExposureBracket("short", 2500), ExposureBracket("long", 80000))
    hdr = HdrConfig(True, 16, 250, 5, 0.0, 235, brackets)
    short = np.array([[10, 90], [40, 120]], dtype=np.uint8)
    long = np.array([[30, 255], [150, 255]], dtype=np.uint8)

    merged, _saturated, _dark, _selected_map, report = merge_hdr_frames(
        cv2,
        [short, long],
        brackets,
        hdr,
        [0.0, 0.0],
        selected_bracket_index=0,
        sequence_selection={"mode": "decoder_aware_common_bracket"},
    )

    recovered = merged.astype(np.float32) * (255.0 / 65535.0)
    np.testing.assert_allclose(recovered, short, atol=1.0)
    assert report["algorithm"] == "structured_light_sequence_bracket"
    assert report["selected_sequence_bracket"] == "short"


def test_quality_issue_summary_records_failures_without_stopping_scan() -> None:
    summary = summarize_quality_issues(
        {
            "status": "failed_continued",
            "output_dir": "C:/captures/quality_gate/preflight",
            "failures": ["White/Black contrast=8.0 < 20.0"],
        },
        {
            "0": {"failures": ["Sine modulation valid=0.010"]},
            "180": {"failures": []},
        },
    )

    assert summary["enforcement"] == "record_only"
    assert summary["main_scan_continued_after_preflight_failure"] is True
    assert summary["preflight"]["failure_count"] == 1
    assert summary["final_scan_failures_by_angle"] == {"0": ["Sine modulation valid=0.010"]}


def test_quality_gate_measures_gray_and_sine_inside_projected_stage() -> None:
    """A black camera border must not turn a valid small stage into a warning."""
    images = {pattern_id: np.zeros((10, 10), dtype=np.uint8) for pattern_id in range(22)}
    stage = np.s_[2:8, 2:8]
    images[0][stage] = 120
    for normal_id, inverse_id in zip(range(2, 10), range(14, 22)):
        images[normal_id][stage] = 100
        images[inverse_id][stage] = 20
    images[10][stage] = 100
    images[11][stage] = 60
    images[12][stage] = 20
    images[13][stage] = 60

    report = assess_fpp_quality(
        cv2,
        images,
        HdrConfig(False, 16, 250, 5, 0.0, 235, (ExposureBracket("single", 6000),)),
        _quality_gate(),
    )

    assert report["passed"] is True
    assert report["gray_pairs"]["009_021"]["contrast_valid_ratio"] == 1.0
    assert report["gray_pairs"]["009_021"]["full_frame_contrast_valid_ratio"] == 0.36
    assert report["sine"]["valid_ratio"] == 1.0
    assert report["sine"]["full_frame_valid_ratio"] == 0.36


def test_capture_config_uses_one_fixed_exposure(tmp_path) -> None:
    config = tmp_path / "camera_config.json"
    config.write_text(
        '{"capture":{"single_exposure":{"exposure_us":15000,"gain_db":0.0}}}',
        encoding="utf-8",
    )
    args = type(
        "Args",
        (),
        {
            "camera_config": config,
            "exposure_us": None,
            "gain_db": None,
            "scan_type": None,
            "focus_confirmed": None,
            "scheimpflug_confirmed": None,
            "keystone_predistortion": None,
            "projector_tilt_deg": None,
            "rig_id": None,
            "calibration_id": None,
            "projector_brightness": None,
            "quality_gate": None,
        },
    )()

    capture = load_capture_config(args)

    assert capture.hdr.enabled is False
    assert capture.hdr.brackets == (ExposureBracket("single", 15000, 0.0),)


def test_aruco_marker_observations_save_corners_and_centers() -> None:
    observations = aruco_marker_observations(
        {2: np.array([[1, 2], [5, 2], [5, 6], [1, 6]], dtype=np.float32)}
    )

    assert observations == {
        "2": {
            "corners_px": [[1.0, 2.0], [5.0, 2.0], [5.0, 6.0], [1.0, 6.0]],
            "center_px": [3.0, 4.0],
        }
    }


def test_main_scan_aruco_pose_accepts_matching_prescan_position() -> None:
    reference = {
        0: np.array([[10, 10], [20, 10], [20, 20], [10, 20]], dtype=np.float32),
        2: np.array([[10, 90], [20, 90], [20, 100], [10, 100]], dtype=np.float32),
    }
    current = {marker_id: corners + np.array([2.0, -1.0]) for marker_id, corners in reference.items()}

    report = compare_aruco_pose_to_prescan(
        reference,
        current,
        [0, 1, 2, 3],
        {
            "max_center_shift_px": 5.0,
            "max_rotation_deg": 2.0,
            "max_scale_deviation": 0.03,
        },
    )

    assert report["passed"] is True
    assert report["marker_ids"] == [0, 2]
    assert report["mean_corner_shift_px"] == pytest.approx(np.sqrt(5.0))


def test_main_scan_aruco_pose_rejects_position_different_from_prescan() -> None:
    reference = {
        0: np.array([[10, 10], [20, 10], [20, 20], [10, 20]], dtype=np.float32),
        2: np.array([[10, 90], [20, 90], [20, 100], [10, 100]], dtype=np.float32),
    }
    current = {marker_id: corners + np.array([12.0, 0.0]) for marker_id, corners in reference.items()}

    report = compare_aruco_pose_to_prescan(
        reference,
        current,
        [0, 1, 2, 3],
        {
            "max_center_shift_px": 5.0,
            "max_rotation_deg": 2.0,
            "max_scale_deviation": 0.03,
        },
    )

    assert report["passed"] is False
    assert report["mean_corner_shift_px"] == pytest.approx(12.0)


def test_current_scan_aruco_images_create_per_scan_alignment(tmp_path, monkeypatch) -> None:
    zero = {
        0: np.array([[10, 10], [20, 10], [20, 20], [10, 20]], dtype=np.float32),
        2: np.array([[80, 80], [90, 80], [90, 90], [80, 90]], dtype=np.float32),
    }
    rotated = {
        marker_id: np.array([100.0, 100.0], dtype=np.float32) - corners
        for marker_id, corners in zero.items()
    }
    zero_path = tmp_path / "angle_000" / "accepted.png"
    rotated_path = tmp_path / "angle_180" / "accepted.png"
    output_path = tmp_path / "stage_precalibration.json"
    camera_config = tmp_path / "camera_config.json"
    camera_config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "structured_light_pc_controller.read_image",
        lambda _cv2, path: "zero" if path == zero_path else "rotated",
    )
    monkeypatch.setattr(
        "structured_light_pc_controller.require_aruco_markers",
        lambda _cv2, image, **_kwargs: (
            (zero, [0, 2]) if image == "zero" else (rotated, [0, 2])
        ),
    )
    args = type(
        "Args",
        (),
        {
            "aruco_ids": "0,1,2,3",
            "aruco_dictionary": "DICT_4X4_50",
            "aruco_ransac_threshold_px": 3.0,
            "aruco_intended_rotation_deg": 180.0,
            "aruco_stage_command_value": 250.0,
            "camera_config": camera_config,
        },
    )()

    payload = create_aruco_alignment(
        cv2,
        args=args,
        zero_path=zero_path,
        rotated_path=rotated_path,
        output_path=output_path,
    )

    assert output_path.is_file()
    assert payload["transform_direction"] == "180_to_0"
    assert payload["aruco"]["marker_ids"] == [0, 2]
    assert payload["stage_precalibration"]["actual_rotation_magnitude_deg"] == pytest.approx(180.0)


def test_aruco_stage_geometry_uses_configured_stage_cross_coordinates(tmp_path) -> None:
    config = tmp_path / "camera_config.json"
    config.write_text(
        '{"capture":{"aruco_stage":{"layout":"stage-cross","marker_center_radius_mm":42,"stage_diameter_mm":105}}}',
        encoding="utf-8",
    )
    geometry = aruco_stage_geometry(type("Args", (), {"camera_config": config})(), [0, 1, 2, 3])

    assert geometry["marker_centers_mm"] == {
        "0": [0.0, -42.0], "1": [42.0, 0.0], "2": [0.0, 42.0], "3": [-42.0, 0.0]
    }
