"""Regression checks: reject corner outliers without erasing real drift."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent / "standalone_thermal_drift"))
from analyze_refined import refine_markers, robust_alignment, refined_pair, trajectory_summary, fixed_roi_audit
from test_thermal_drift_live import signal


class RefinedAnalysisTests(unittest.TestCase):
    def test_outlier_does_not_become_coherent_camera_motion(self):
        ref, cur = signal(), signal()
        cur["markers"]["9"]["corners_px"][1] = [276, 183]
        matrix, metrics = robust_alignment(ref, cur)
        self.assertIsNotNone(matrix)
        self.assertEqual(metrics["inlier_count"], 7)
        self.assertGreater(metrics["all_corner_reprojection_rms_px"], 1)
        self.assertLess(metrics["inlier_reprojection_rms_px"], 1e-5)
        self.assertLess(metrics["transform_grid_motion_p95_px"], 1e-5)

    def test_coherent_subpixel_motion_is_preserved(self):
        ref, cur = signal(), signal()
        for marker in cur["markers"].values():
            marker["corners_px"] = (np.array(marker["corners_px"]) + [0.4, 0.3]).tolist()
        _, metrics = robust_alignment(ref, cur)
        self.assertAlmostEqual(metrics["inlier_corner_motion_rms_px"], 0.5, places=4)
        self.assertAlmostEqual(metrics["transform_grid_motion_p95_px"], 0.5, places=4)

    def test_one_marker_cannot_certify_registration(self):
        ref, cur = signal(), signal()
        del cur["markers"]["9"]
        matrix, metrics = robust_alignment(ref, cur)
        self.assertIsNone(matrix)
        self.assertEqual(metrics["status"], "insufficient_markers")

    def test_phase_drift_survives_refinement(self):
        metrics = refined_pair(signal(), signal(offset=.15))
        for name in ("perspective", "similarity"):
            self.assertAlmostEqual(metrics[name]["phase"]["plane_coefficients_rad_per_1000px"][0], .15, places=4)

    def test_rigid_camera_shift_is_removed_from_phase(self):
        ref, cur = signal(), signal()
        for key in ("phase", "valid", "cosine", "sine", "white"):
            cur[key] = np.roll(cur[key], 1, axis=0)
        for marker in cur["markers"].values():
            marker["corners_px"] = (np.array(marker["corners_px"]) + [0, 1]).tolist()
        metrics = refined_pair(ref, cur)
        self.assertGreater(abs(metrics["raw_phase"]["plane_coefficients_rad_per_1000px"][0]), .1)
        for name in ("perspective", "similarity"):
            self.assertLess(abs(metrics[name]["phase"]["plane_coefficients_rad_per_1000px"][0]), 1e-5)

    def test_image_marker_refinement_uses_only_saved_ids(self):
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        white = np.full((240, 320), 255, np.uint8)
        white[50:150, 50:150] = cv2.aruco.generateImageMarker(dictionary, 3, 100)
        markers = refine_markers(white, {"aruco": {"markers": {"3": {}}}})
        self.assertIn("3", markers)
        self.assertEqual(refine_markers(white, {"aruco": {"markers": {"9": {}}}}), {})

    def test_summary_never_claims_formal_stability(self):
        records = []
        for t in (0, 180, 360):
            records.append(dict(elapsed_seconds=t, image_shape=[240, 320],
                                cold=refined_pair(signal(), signal(offset=t * .0001))))
        summary = trajectory_summary(records)
        self.assertFalse(summary["formal_stability_confirmed"])
        self.assertAlmostEqual(summary["recent_plane_slope_rad_per_minute_per_1000px"][0], .006, places=4)

    def test_fixed_roi_preserves_known_linear_phase_change(self):
        points = [dict(index=i, actual_set_start_elapsed_since_projection_seconds=i * 180)
                  for i in range(6)]
        def load(_session, point):
            data = signal(offset=point["index"] * .01)
            if point["index"] == 3:
                data["valid"][:100, :100] = False
            return data
        with patch("analyze_refined.refined_data", side_effect=load):
            result = fixed_roi_audit(Path("unused"), points)
        self.assertEqual(result["status"], "ok")
        for model in result["models"].values():
            self.assertAlmostEqual(model["last_pair"]["plane_coefficients_rad_per_1000px"][0], .05, places=5)
            self.assertLess(model["median_pixel_temporal_detrended_rms_rad"], 1e-5)


if __name__ == "__main__":
    unittest.main()
