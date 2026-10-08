"""Checks for drift sensitivity, incomplete-data handling and stop decisions."""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent / "standalone_thermal_drift"))
from analyze_live import alignment, fit_delta, phase_data, stable_window, complete_points


def signal(offset=0.0, slope=0.0):
    h, w = 240, 320
    yy, xx = np.mgrid[:h, :w]
    phase = yy * 0.2 + offset + slope * (xx - w / 2) / 1000
    images = {"white": np.full((h, w), 180, np.float32)}
    for name, angle in zip(("sine_000", "sine_090", "sine_180", "sine_270"), (0, np.pi/2, np.pi, 3*np.pi/2)):
        images[name] = (100 + 40 * np.cos(phase + angle)).astype(np.float32)
    markers = {"3": {"corners_px": [[30, 20], [50, 20], [50, 40], [30, 40]]},
               "9": {"corners_px": [[260, 180], [280, 180], [280, 200], [260, 200]]}}
    return phase_data(images, {"aruco": {"markers": markers}})


class LiveAnalysisTests(unittest.TestCase):
    def test_plane_detects_known_offset_and_slope_through_wraps(self):
        fit = fit_delta(signal(), signal(offset=0.15, slope=0.3))
        self.assertEqual(fit["status"], "ok")
        np.testing.assert_allclose(fit["plane_coefficients_rad_per_1000px"], [0.15, 0.3, 0], atol=1e-5)

    def test_two_marker_alignment_detects_camera_motion(self):
        reference, current = signal(), signal()
        for marker in current["markers"].values():
            marker["corners_px"] = (np.array(marker["corners_px"]) + [0.4, 0]).tolist()
        matrix, metrics = alignment(reference, current)
        self.assertIsNotNone(matrix)
        self.assertAlmostEqual(metrics["corner_motion_rms_px"], 0.4, places=4)

    def _records(self):
        phase = dict(status="ok", common_valid_ratio=1, rejected_pixel_ratio=0,
                     circular_alias_risk=False, residual_rms_rad=0.001,
                     spatial_p95_abs_delta_rad=0.001, plane_coefficients_rad_per_1000px=[0.001, 0, 0])
        metric = dict(camera=dict(status="ok", corner_motion_rms_px=0.01),
                      aligned_phase=phase, raw_phase=phase, white_relative_change=0.001)
        return [dict(elapsed_seconds=t, recent_comparison=metric, image_shape=[240, 320])
                for t in range(0, 901, 180)]

    def test_stable_window_requires_fifteen_minutes(self):
        records = self._records()
        self.assertTrue(stable_window(records)["recommend_stop"])
        self.assertFalse(stable_window(records[:-1])["recommend_stop"])

    def test_phase_drift_and_missing_markers_prevent_stop(self):
        records = self._records()
        records[-1]["recent_comparison"]["raw_phase"]["spatial_p95_abs_delta_rad"] = 0.1
        self.assertFalse(stable_window(records)["recommend_stop"])
        records = self._records()
        records[-1]["recent_comparison"]["aligned_phase"] = None
        self.assertFalse(stable_window(records)["recommend_stop"])

    def test_unfinished_timepoint_is_not_analyzed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = dict(timepoints=[dict(index=0, frames=[dict(role="white", filename="t0/white.png")])])
            points, pending = complete_points(root, manifest)
            self.assertEqual(points, [])
            self.assertEqual(len(pending), 1)


if __name__ == "__main__":
    unittest.main()
