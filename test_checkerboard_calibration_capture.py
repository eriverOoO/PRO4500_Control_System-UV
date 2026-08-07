from __future__ import annotations

import json

import cv2
import numpy as np

from checkerboard_calibration_capture import PATTERN_NAMES, create_session


def test_setup_creates_fixed_exposure_x_and_y_pattern_sets(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for index, name in enumerate(PATTERN_NAMES):
        image = np.tile(np.arange(12, dtype=np.uint8), (8, 1))
        image = (image + index).astype(np.uint8)
        assert cv2.imwrite(str(source / name), image)
    config = tmp_path / "camera_config.json"
    config.write_text(
        '{"capture":{"single_exposure":{"exposure_us":15000,"gain_db":0.0}}}',
        encoding="utf-8",
    )

    session = create_session(tmp_path / "session", source, config)

    manifest = json.loads((session / "session_manifest.json").read_text(encoding="utf-8"))
    assert manifest["capture"]["frames_per_pose"] == 44
    assert manifest["capture"]["fixed_camera_settings"]["exposure_us"] == 15000
    assert manifest["checkerboard"]["inner_corners"] == [9, 9]
    assert len(list((session / "patterns_x").glob("*.bmp"))) == 22
    assert len(list((session / "patterns_y").glob("*.bmp"))) == 22
    vertical = cv2.imread(str(session / "patterns_y" / "02_Gray0.bmp"), cv2.IMREAD_GRAYSCALE)
    assert np.all(vertical[:, 0] == vertical[:, -1])
    assert np.any(vertical[0, :] != vertical[-1, :])
