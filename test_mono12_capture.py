from __future__ import annotations

from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from camera_provider import CameraError, CameraSettings, XimeaUvCamera
from structured_light_pc_controller import (
    ExposureBracket,
    FrameGuardConfig,
    HdrConfig,
    QualityGateConfig,
    assess_fpp_quality,
    load_capture_config,
    merge_hdr_frames,
    to_decoder_u8,
    validate_projected_frame,
)


def _hdr(sample_bit_depth: int | None) -> HdrConfig:
    return HdrConfig(
        False, 8, 250, 5, 0.0, 235, (ExposureBracket("single", 15000),), sample_bit_depth
    )


def _guard() -> FrameGuardConfig:
    return FrameGuardConfig(
        enabled=True,
        min_illuminated_ratio=0.05,
        min_pattern_change_ratio=0.05,
        min_signal_delta_u8=20.0,
    )


def _as_mono12(image_u8: np.ndarray) -> np.ndarray:
    """The right-aligned 12-bit frame a mono8 frame corresponds to."""
    return np.rint(image_u8.astype(np.float32) * (4095.0 / 255.0)).astype(np.uint16)


def test_mono12_frames_map_to_the_decoder_8bit_domain() -> None:
    frame = np.array([[0, 2048, 4095]], dtype=np.uint16)

    np.testing.assert_array_equal(to_decoder_u8(frame, 12), [[0, 128, 255]])
    # mono16 keeps its full-scale reading.
    np.testing.assert_array_equal(
        to_decoder_u8(np.array([[65535]], dtype=np.uint16)), [[255]]
    )


def test_frame_guard_accepts_mono12_black_after_white() -> None:
    white = np.full((8, 8), 3000, dtype=np.uint16)
    black = np.full((8, 8), 150, dtype=np.uint16)
    blank = np.zeros((8, 8), dtype=np.uint8)

    accepted = validate_projected_frame(
        cv2, frame=black, projected=blank, pattern_id=1, hdr=_hdr(12), guard=_guard(),
        white_reference=white, black_reference=None,
    )
    # Read as full-scale 16-bit, the same frames look identical: the failure
    # the lab capture hit before mono12 existed.
    rejected = validate_projected_frame(
        cv2, frame=black, projected=blank, pattern_id=1, hdr=_hdr(None), guard=_guard(),
        white_reference=white, black_reference=None,
    )

    assert accepted["passed"] is True
    assert rejected["reason"] == "black_frame_matches_white"


def test_quality_gate_scores_mono12_like_the_equivalent_mono8_scan() -> None:
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
    mono12 = {pattern_id: _as_mono12(image) for pattern_id, image in images.items()}

    mono8_report = assess_fpp_quality(cv2, images, _hdr(None), _quality_gate())
    mono12_report = assess_fpp_quality(cv2, mono12, _hdr(12), _quality_gate())

    assert mono12_report["passed"] is True
    assert mono12_report["failures"] == mono8_report["failures"]


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


def test_mono12_merge_saves_raw_samples_in_16bit() -> None:
    frame = np.array([[0, 100], [2048, 4095]], dtype=np.uint16)

    merged, saturated, dark, _selected, report = merge_hdr_frames(
        cv2, [frame], (ExposureBracket("single", 15000),), _hdr(12), [0.0], selected_bracket_index=0
    )

    assert merged.dtype == np.uint16
    np.testing.assert_array_equal(merged, frame)
    assert report["sample_bit_depth"] == 12
    assert report["output_bit_depth"] == 16
    # Thresholds scale to the 12-bit range: 250/255 and 5/255 of 4095.
    assert report["saturated_threshold"] == 4015
    assert report["dark_threshold"] == 80
    np.testing.assert_array_equal(saturated > 0, [[False, False], [False, True]])
    np.testing.assert_array_equal(dark > 0, [[True, False], [False, False]])


def test_mono8_merge_is_unchanged() -> None:
    frame = np.array([[0, 100], [200, 255]], dtype=np.uint8)

    merged, _s, _d, _m, report = merge_hdr_frames(
        cv2, [frame], (ExposureBracket("single", 15000),), _hdr(None), [0.0], selected_bracket_index=0
    )

    assert merged.dtype == np.uint8
    np.testing.assert_array_equal(merged, frame)
    assert report["sample_bit_depth"] is None
    assert report["output_bit_depth"] == 8


def _args(config_path, image_format=None):
    return SimpleNamespace(
        camera_config=config_path,
        image_format=image_format,
        exposure_us=None,
        gain_db=None,
        scan_type=None,
        focus_confirmed=None,
        scheimpflug_confirmed=None,
        keystone_predistortion=None,
        projector_tilt_deg=None,
        rig_id=None,
        calibration_id=None,
        projector_brightness=None,
        quality_gate=None,
    )


def test_capture_config_takes_sample_depth_from_image_format(tmp_path) -> None:
    config = tmp_path / "camera_config.json"
    config.write_text(
        '{"camera":{"ximea":{"image_format":"mono8"}},'
        '"capture":{"single_exposure":{"exposure_us":15000}}}',
        encoding="utf-8",
    )
    configured = tmp_path / "mono12_config.json"
    configured.write_text(
        '{"camera":{"ximea":{"image_format":"mono12"}},'
        '"capture":{"single_exposure":{"exposure_us":15000}}}',
        encoding="utf-8",
    )

    assert load_capture_config(_args(config)).hdr.sample_bit_depth is None
    assert load_capture_config(_args(config, "mono16")).hdr.sample_bit_depth is None
    assert load_capture_config(_args(config, "mono12")).hdr.sample_bit_depth == 12
    assert load_capture_config(_args(configured)).hdr.sample_bit_depth == 12


def _ximea_bytes(values: np.ndarray, image_format: str):
    camera = XimeaUvCamera(CameraSettings(provider="ximea", image_format=image_format))
    image = SimpleNamespace(width=values.shape[1], height=values.shape[0], padding_x=0)
    return camera._image_bytes_to_array(values.astype("<u2").tobytes(), image)


def test_ximea_mono12_frames_decode_as_uint16_and_reject_left_aligned_data() -> None:
    values = np.array([[0, 4095], [17, 2048]], dtype=np.uint16)

    np.testing.assert_array_equal(_ximea_bytes(values, "mono12"), values)
    with pytest.raises(CameraError, match="right-aligned 12-bit"):
        _ximea_bytes(values << 4, "mono12")
    np.testing.assert_array_equal(_ximea_bytes(values << 4, "mono16"), values << 4)
