from __future__ import annotations

import cv2
import numpy as np

from structured_light_pc_controller import (
    ExposureBracket,
    HdrConfig,
    merge_hdr_frames,
    select_structured_light_sequence_bracket,
    summarize_quality_issues,
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
    hdr = HdrConfig(True, 16, 250, 5, 0.0, brackets)
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
    hdr = HdrConfig(True, 16, 250, 5, 0.0, brackets)
    short = np.array([[10, 90], [40, 120]], dtype=np.uint8)
    long = np.array([[30, 255], [150, 255]], dtype=np.uint8)

    merged, _saturated, _dark, report = merge_hdr_frames(
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
