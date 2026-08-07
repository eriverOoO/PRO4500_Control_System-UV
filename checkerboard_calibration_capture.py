"""Set up and capture checkerboard camera-projector calibration sessions.

This tool deliberately stops at data acquisition.  It creates horizontal and
vertical structured-light pattern sets, records every locked checkerboard pose,
and verifies that each pose has complete fixed-exposure captures.  It does not
fit camera/projector intrinsics, stereo extrinsics, or a 3D reconstruction.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np


PATTERN_NAMES = (
    "00_White.bmp",
    "01_Black.bmp",
    "02_Gray0.bmp",
    "03_Gray1.bmp",
    "04_Gray2.bmp",
    "05_Gray3.bmp",
    "06_Gray4.bmp",
    "07_Gray5.bmp",
    "08_Gray6.bmp",
    "09_Gray7.bmp",
    "10_Sine_000.bmp",
    "11_Sine_090.bmp",
    "12_Sine_180.bmp",
    "13_Sine_270.bmp",
    "14_Gray0_inv.bmp",
    "15_Gray1_inv.bmp",
    "16_Gray2_inv.bmp",
    "17_Gray3_inv.bmp",
    "18_Gray4_inv.bmp",
    "19_Gray5_inv.bmp",
    "20_Gray6_inv.bmp",
    "21_Gray7_inv.bmp",
)
POSE_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]+$")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _require_pattern_source(pattern_dir: Path) -> None:
    missing = [name for name in PATTERN_NAMES if not (pattern_dir / name).is_file()]
    if missing:
        raise ValueError("Pattern source is missing: " + ", ".join(missing))


def _vertical_pattern(image: np.ndarray, *, interpolation: int) -> np.ndarray:
    """Turn an x-varying pattern into the matching y-varying pattern."""
    if image.ndim not in (2, 3):
        raise ValueError(f"Unsupported pattern shape: {image.shape}")
    height, width = image.shape[:2]
    middle_row = image[height // 2]
    if image.ndim == 2:
        source_column = middle_row.reshape(width, 1)
    else:
        source_column = middle_row.reshape(width, 1, image.shape[2])
    vertical_line = cv2.resize(source_column, (1, height), interpolation=interpolation)
    return np.repeat(vertical_line, width, axis=1)


def _copy_axis_patterns(source: Path, destination: Path, axis: str) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name in PATTERN_NAMES:
        input_path = source / name
        output_path = destination / name
        if axis == "x":
            shutil.copy2(input_path, output_path)
            continue
        image = cv2.imread(str(input_path), cv2.IMREAD_UNCHANGED)
        if image is None:
            raise ValueError(f"Could not read pattern: {input_path}")
        interpolation = cv2.INTER_LINEAR if "Sine" in name else cv2.INTER_NEAREST
        vertical = _vertical_pattern(image, interpolation=interpolation)
        if not cv2.imwrite(str(output_path), vertical):
            raise ValueError(f"Could not write pattern: {output_path}")


def _fixed_exposure(camera_config: Path) -> dict[str, float | int]:
    config = _read_json(camera_config)
    capture = config.get("capture", {})
    single = capture.get("single_exposure", {}) if isinstance(capture, dict) else {}
    if not isinstance(single, dict):
        raise ValueError("capture.single_exposure must be an object")
    if "hdr" in capture:
        raise ValueError("camera config must use capture.single_exposure, not capture.hdr")
    exposure = int(single.get("exposure_us", 0))
    gain = float(single.get("gain_db", 0.0))
    if exposure < 1:
        raise ValueError("capture.single_exposure.exposure_us must be at least 1")
    return {"exposure_us": exposure, "gain_db": gain}


def suggested_poses() -> list[dict[str, Any]]:
    return [
        {"pose_id": "p01_center_z00", "description": "center, flat, 0 mm"},
        {"pose_id": "p02_center_z05", "description": "center, flat, 5 mm spacer"},
        {"pose_id": "p03_center_z10", "description": "center, flat, 10 mm spacer"},
        {"pose_id": "p04_center_z15", "description": "center, flat, 15 mm spacer"},
        {"pose_id": "p05_pitch_pos10", "description": "center, +10 deg pitch wedge"},
        {"pose_id": "p06_pitch_neg10", "description": "center, -10 deg pitch wedge"},
        {"pose_id": "p07_roll_pos10", "description": "center, +10 deg roll wedge"},
        {"pose_id": "p08_roll_neg10", "description": "center, -10 deg roll wedge"},
        {"pose_id": "p09_yaw_45", "description": "flat, stage rotated 45 deg"},
        {"pose_id": "p10_yaw_90", "description": "flat, stage rotated 90 deg"},
        {"pose_id": "p11_pitch_pos10_z10", "description": "+10 deg pitch, 10 mm spacer"},
        {"pose_id": "p12_roll_pos10_z10", "description": "+10 deg roll, 10 mm spacer"},
    ]


def create_session(session: Path, pattern_source: Path, camera_config: Path) -> Path:
    session = session.resolve()
    pattern_source = pattern_source.resolve()
    camera_config = camera_config.resolve()
    if session.exists():
        raise ValueError(f"Session directory already exists: {session}")
    _require_pattern_source(pattern_source)
    fixed = _fixed_exposure(camera_config)
    _copy_axis_patterns(pattern_source, session / "patterns_x", "x")
    _copy_axis_patterns(pattern_source, session / "patterns_y", "y")
    session.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "session_id": session.name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "purpose": "checkerboard camera-projector calibration capture only; no geometry fit has run",
        "checkerboard": {
            "inner_corners": [9, 9],
            "square_size_mm": 7.0,
            "checker_area_mm": [70.0, 70.0],
            "stage_diameter_mm": 100.0,
        },
        "capture": {
            "board_locked_during_each_axis_sequence": True,
            "fixed_camera_settings": fixed,
            "patterns_per_axis": 22,
            "axes": ["x", "y"],
            "frames_per_pose": 44,
            "pattern_source": str(pattern_source),
            "camera_config": str(camera_config),
        },
        "suggested_poses": suggested_poses(),
        "captured_poses": [],
        "calibration_fit_status": "not_run",
    }
    _write_json(session / "session_manifest.json", manifest)
    (session / "captures").mkdir()
    (session / "README_CAPTURE.txt").write_text(
        "Checkerboard capture session\n\n"
        "For each pose, lock the board so it cannot move between the X and Y sequences.\n"
        "Run: python checkerboard_calibration_capture.py capture --session <session> --pose-id <pose_id>\n"
        "Each pose stores 22 X-axis frames and 22 Y-axis frames at the fixed camera exposure.\n"
        "This session is capture evidence only. It does not calculate calibration parameters.\n",
        encoding="utf-8",
    )
    return session


def _verify_scan(scan_dir: Path, expected_exposure_us: int) -> dict[str, Any]:
    angle_dir = scan_dir / "angle_000"
    missing = [pattern_id for pattern_id in range(22) if not (angle_dir / f"pattern_{pattern_id:03d}.png").is_file()]
    if missing:
        raise RuntimeError(f"Incomplete capture {scan_dir.name}; missing patterns {missing}")
    log_path = scan_dir / "scan_log.json"
    log = _read_json(log_path)
    rows = [row for row in log.get("rows", []) if row.get("status") == "ok"]
    exposures = {int(row["exposure_us"]) for row in rows if row.get("exposure_us") is not None}
    if exposures != {expected_exposure_us}:
        raise RuntimeError(f"Capture {scan_dir.name} used exposures {sorted(exposures)}, expected {expected_exposure_us}")
    return {
        "scan_directory": str(scan_dir),
        "pattern_count": 22,
        "exposure_us": expected_exposure_us,
        "quality_report": str(angle_dir / "quality_report.json"),
    }


def capture_pose(session: Path, pose_id: str, controller: Path) -> None:
    if not POSE_ID_PATTERN.fullmatch(pose_id):
        raise ValueError("pose_id may use only letters, numbers, '.', '_' and '-'")
    session = session.resolve()
    manifest_path = session / "session_manifest.json"
    manifest = _read_json(manifest_path)
    if any(item["pose_id"] == pose_id for item in manifest.get("captured_poses", [])):
        raise ValueError(f"Pose already captured: {pose_id}")
    fixed = manifest["capture"]["fixed_camera_settings"]
    captures = session / "captures"
    camera_config = Path(manifest["capture"]["camera_config"])
    for axis in ("x", "y"):
        scan_id = f"{pose_id}_{axis}"
        command = [
            sys.executable,
            str(controller),
            "--patterns",
            str(session / f"patterns_{axis}"),
            "--output",
            str(captures),
            "--scan-id",
            scan_id,
            "--angles",
            "0",
            "--no-angle-prompt",
            "--camera-config",
            str(camera_config),
            "--scan-type",
            "reference",
            "--calibration-id",
            manifest["session_id"],
            "--save-all-images",
        ]
        print("[checkerboard] capture", axis.upper(), flush=True)
        subprocess.run(command, check=True)
    x_summary = _verify_scan(captures / f"{pose_id}_x", int(fixed["exposure_us"]))
    y_summary = _verify_scan(captures / f"{pose_id}_y", int(fixed["exposure_us"]))
    manifest["captured_poses"].append(
        {
            "pose_id": pose_id,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "board_locked_during_x_and_y": True,
            "x_axis": x_summary,
            "y_axis": y_summary,
        }
    )
    _write_json(manifest_path, manifest)
    print(f"[checkerboard] complete pose={pose_id} frames=44", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Set up or capture a checkerboard calibration session.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    setup = subparsers.add_parser("setup", help="Create a checkerboard capture session and X/Y pattern sets.")
    setup.add_argument("--session", required=True, type=Path)
    setup.add_argument("--patterns", required=True, type=Path, help="Current 22-frame scan pattern folder.")
    setup.add_argument("--camera-config", default=Path("camera_config.json"), type=Path)
    capture = subparsers.add_parser("capture", help="Capture one locked checkerboard pose as X then Y sequences.")
    capture.add_argument("--session", required=True, type=Path)
    capture.add_argument("--pose-id", required=True)
    capture.add_argument("--controller", default=Path("structured_light_pc_controller.py"), type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "setup":
        session = create_session(args.session, args.patterns, args.camera_config)
        print(f"[checkerboard] session ready: {session}")
        return 0
    capture_pose(args.session, args.pose_id, args.controller.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
