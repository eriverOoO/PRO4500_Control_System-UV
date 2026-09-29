import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from scripts import phase_linear_spatial_experiment as experiment


def _write_view(root: Path, view: int, height_mm: float, slope: np.ndarray) -> None:
    base = root / "views" / f"deg_{view}"
    (base / "height").mkdir(parents=True)
    (base / "phase").mkdir()
    (base / "masks").mkdir()
    delta = (-height_mm * slope).astype(np.float32)
    absolute = (30.0 + delta + np.indices(slope.shape)[0] * 0.5).astype(np.float32)
    np.save(base / "height" / "delta_phase.npy", delta)
    np.save(base / "phase" / "absolute_phase.npy", absolute)
    cv2.imwrite(
        str(base / "masks" / "combined_mask.png"),
        np.full(slope.shape, 255, dtype=np.uint8),
    )


def _sample(name: str, height_mm: float, processed_dir: Path) -> dict:
    return {
        "name": name,
        "height_mm": height_mm,
        "processed_dir": str(processed_dir),
        "views": {
            "0": {"subdir": "views/deg_0", "roi_xyxy": [0, 0, 40, 30]},
            "180": {"subdir": "views/deg_180", "roi_xyxy": [0, 0, 40, 30]},
        },
    }


class PhaseLinearSpatialExperimentTests(unittest.TestCase):
    def test_spatial_model_improves_independent_holdout(self):
        with tempfile.TemporaryDirectory() as directory:
            self._run_spatial_model_test(Path(directory))

    def _run_spatial_model_test(self, tmp_path: Path):
        rows, cols = np.indices((30, 40))
        slope = (4.8 + 0.8 * cols / 39.0 + 0.3 * rows / 29.0).astype(np.float32)
        fit = []
        for height in (2.95, 5.03, 7.96):
            root = tmp_path / f"fit_{height}"
            _write_view(root, 0, height, slope)
            _write_view(root, 180, height, slope * 0.99)
            fit.append(_sample(f"fit_{height}", height, root))
        holdout_root = tmp_path / "holdout"
        _write_view(holdout_root, 0, 6.10, slope)
        _write_view(holdout_root, 180, 6.10, slope * 0.99)
        reference = (30.0 + rows * 0.5).astype(np.float32)
        reference_0 = tmp_path / "reference_0.npy"
        reference_180 = tmp_path / "reference_180.npy"
        np.save(reference_0, reference)
        np.save(reference_180, reference)
        # Exercise the legacy-data fallback: hold-out has absolute phase plus
        # a flat reference, but no precomputed delta_phase.npy.
        (holdout_root / "views" / "deg_0" / "height" / "delta_phase.npy").unlink()
        (holdout_root / "views" / "deg_180" / "height" / "delta_phase.npy").unlink()

        manifest = {
            "calibration_id": "synthetic_spatial",
            "phase_axis": "y",
            "reference_phase_by_physical_view": {
                "0": str(reference_0),
                "180": str(reference_180),
            },
            "settings": {"polynomial_degree": 2, "phase_fold_bins": 12},
            "fit": fit,
            "holdout": [_sample("holdout_6p10", 6.10, holdout_root)],
        }
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        output = tmp_path / "output"

        self.assertEqual(
            experiment.main(
                ["--manifest", str(manifest_path), "--output-dir", str(output)]
            ),
            0,
        )

        report = json.loads(
            (output / "phase_linear_spatial_experiment_report.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertGreater(
            report["holdout_summary"]["mean_of_holdout_mae_mm"][
                "improvement_percent"
            ],
            95.0,
        )
        self.assertEqual(report["fit"]["deg_0"]["direct_pixel_slope"]["count"], 1200)
        with np.load(output / "phase_linear_spatial_calibration.npz") as calibration:
            self.assertEqual(
                calibration["phase_linear_phase_per_mm_0"].shape, (30, 40)
            )
            self.assertEqual(float(calibration["phase_linear_height_sign_0"]), -1.0)
    def test_manifest_requires_explicit_top_surface_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            manifest = {
                "fit": [
                    {
                        "height_mm": 3.0,
                        "processed_dir": str(tmp_path),
                        "views": {
                            "0": {"subdir": "views/deg_0"},
                            "180": {"subdir": "views/deg_180"},
                        },
                    }
                ]
            }
            with self.assertRaisesRegex(
                ValueError, "roi_xyxy or surface_mask is required"
            ):
                experiment._view_inputs(manifest, tmp_path, "fit")


if __name__ == "__main__":
    unittest.main()
