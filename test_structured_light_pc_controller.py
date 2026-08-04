from __future__ import annotations

import cv2
import numpy as np

from structured_light_pc_controller import (
    ExposureBracket,
    HdrConfig,
    QualityGateConfig,
    aruco_stage_geometry,
    aruco_marker_observations,
    assess_fpp_quality,
    merge_hdr_frames,
    select_structured_light_sequence_bracket,
    summarize_quality_issues,
)


def _quality_gate() -> QualityGateConfig:
    return QualityGateConfig(
        enabled=True,
        white_black_min_contrast_u8=20.0,
        gray_pair_min_valid_ratio=0.05,
        sine_min_modulation_u8=12.0,
        sine_min_valid_ratio=0.05,
        max_decoder_saturation_ratio=0.2,
        max_decoder_dark_ratio=0.8,
    )


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
