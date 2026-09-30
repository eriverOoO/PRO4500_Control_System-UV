from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from threading import Event
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parent
MODULE = ROOT / "standalone_thermal_drift"
if str(MODULE) not in sys.path:
    sys.path.insert(0, str(MODULE))

from thermal_drift_core import (  # noqa: E402
    build_capture_schedule,
    circular_phase_change,
    phase_summary,
)
import app as thermal_app  # noqa: E402


def _phase_images(phase: float, shape: tuple[int, int] = (8, 10)) -> dict[str, np.ndarray]:
    level = 100.0
    amplitude = 30.0
    return {
        "white": np.full(shape, 180, dtype=np.uint8),
        "sine_000": np.full(shape, level + amplitude * np.cos(phase), dtype=np.uint8),
        "sine_090": np.full(shape, level + amplitude * np.cos(phase + np.pi / 2), dtype=np.uint8),
        "sine_180": np.full(shape, level + amplitude * np.cos(phase + np.pi), dtype=np.uint8),
        "sine_270": np.full(shape, level + amplitude * np.cos(phase + 3 * np.pi / 2), dtype=np.uint8),
    }


class ThermalDriftCoreTests(unittest.TestCase):
    def test_default_schedule_samples_early_and_includes_exact_end(self) -> None:
        schedule = build_capture_schedule(
            total_duration_minutes=90,
            initial_duration_minutes=10,
            initial_interval_seconds=60,
            regular_interval_seconds=180,
        )
        self.assertEqual(schedule[:11], [float(value) for value in range(0, 601, 60)])
        self.assertEqual(schedule[11], 780.0)
        self.assertEqual(schedule[-1], 5400.0)
        self.assertEqual(len(schedule), len(set(schedule)))

    def test_schedule_adds_a_non_aligned_final_time(self) -> None:
        schedule = build_capture_schedule(
            total_duration_minutes=12,
            initial_duration_minutes=10,
            initial_interval_seconds=60,
            regular_interval_seconds=180,
        )
        self.assertEqual(schedule[-2:], [600.0, 720.0])

    def test_schedule_rejects_invalid_values(self) -> None:
        with self.assertRaises(ValueError):
            build_capture_schedule(
                total_duration_minutes=0,
                initial_duration_minutes=10,
                initial_interval_seconds=60,
                regular_interval_seconds=180,
            )

    def test_four_step_phase_reports_known_change(self) -> None:
        reference_phase, reference_valid, reference = phase_summary(
            _phase_images(0.0), minimum_modulation=5.0, saturation_value=250.0
        )
        current_phase, current_valid, current = phase_summary(
            _phase_images(0.4), minimum_modulation=5.0, saturation_value=250.0
        )
        change = circular_phase_change(
            reference_phase, reference_valid, current_phase, current_valid
        )
        self.assertEqual(reference["valid_pixel_ratio"], 1.0)
        self.assertEqual(current["valid_pixel_ratio"], 1.0)
        self.assertAlmostEqual(float(change["circular_mean_delta_rad"]), 0.4, delta=0.03)

    def test_saturated_pixels_are_excluded(self) -> None:
        images = _phase_images(0.0)
        images["white"][0, 0] = 255
        _phase, valid, summary = phase_summary(
            images, minimum_modulation=5.0, saturation_value=250.0
        )
        self.assertFalse(valid[0, 0])
        self.assertEqual(summary["valid_pixel_count"], valid.size - 1)

    def test_mock_session_writes_two_complete_timepoints(self) -> None:
        class FakeProjectorWindow:
            def __init__(self, _monitor_index: int, _first_pattern: np.ndarray) -> None:
                self.shown = 0

            def open(self) -> None:
                pass

            def show(self, _image: np.ndarray) -> None:
                self.shown += 1

            def black(self) -> None:
                pass

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = json.loads(
                (MODULE / "thermal_drift_config.json").read_text(encoding="utf-8")
            )
            config["patterns"]["directory"] = str(ROOT / "generated_patterns1")
            config["camera"]["provider"] = "mock"
            config["camera"]["mock"]["fps"] = 1000
            config["capture"]["settle_ms"] = 1
            config["schedule"]["initial_duration_minutes"] = 0
            config["schedule"]["initial_interval_seconds"] = 60
            config["schedule"]["regular_interval_seconds"] = 60
            config_path = root / "thermal_drift_config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            worker = thermal_app.ThermalDriftCapture(config_path, lambda _message: None)
            sessions: list[Path] = []
            requested_at = datetime.now().astimezone()
            requested_monotonic_ns = thermal_app.time.monotonic_ns()
            with patch.object(thermal_app, "ProjectorWindow", FakeProjectorWindow):
                session = worker.run(
                    output_root=root / "output",
                    total_duration_minutes=0.0001,
                    initial_elapsed_seconds=0,
                    room_temperature_c=23.5,
                    note="mock integration test",
                    operator_start_requested_at=requested_at,
                    operator_start_requested_monotonic_ns=requested_monotonic_ns,
                    stop_event=Event(),
                    session_callback=sessions.append,
                )

            manifest = json.loads(
                (session / "session_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["completed_timepoint_count"], 2)
            self.assertEqual(sessions, [session])
            self.assertTrue((session / manifest["config_snapshot"]).is_file())
            self.assertEqual(len(list((session / "timepoints").rglob("*.png"))), 10)
            for point in manifest["timepoints"]:
                self.assertEqual(len(point["frames"]), 5)
                self.assertEqual(point["room_temperature_c"], 23.5)


if __name__ == "__main__":
    unittest.main()
