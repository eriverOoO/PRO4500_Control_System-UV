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


def _visible_reference_settings(camera_config: Path) -> dict[str, float | int]:
    """Return the separately configurable exposure for the white-light reference.

    This image is deliberately not captured under the UV structured-light
    illumination.  It is the camera image used to find the checkerboard
    corners, so it commonly needs a longer exposure than the UV pattern burst.
    """
    config = _read_json(camera_config)
    capture = config.get("capture", {})
    reference = capture.get("checkerboard_visible_reference", {}) if isinstance(capture, dict) else {}
    if not isinstance(reference, dict):
        raise ValueError("capture.checkerboard_visible_reference must be an object")
    fallback = _fixed_exposure(camera_config)
    exposure = int(reference.get("exposure_us", fallback["exposure_us"]))
    gain = float(reference.get("gain_db", fallback["gain_db"]))
    if exposure < 1:
        raise ValueError("capture.checkerboard_visible_reference.exposure_us must be at least 1")
    return {"exposure_us": exposure, "gain_db": gain}


def suggested_poses() -> list[dict[str, Any]]:
    return [
        {"pose_id": "p01_center_flat", "description": "centered and flat on the stage"},
        {"pose_id": "p02_center_spacer_5", "description": "centered, flat, 5 mm spacer"},
        {"pose_id": "p03_center_spacer_10", "description": "centered, flat, 10 mm spacer"},
        {"pose_id": "p04_left_flat", "description": "shifted left, flat"},
        {"pose_id": "p05_right_flat", "description": "shifted right, flat"},
        {"pose_id": "p06_top_flat", "description": "shifted away from camera, flat"},
        {"pose_id": "p07_bottom_flat", "description": "shifted toward camera, flat"},
        {"pose_id": "p08_pitch_forward", "description": "forward tilt using any stable wedge"},
        {"pose_id": "p09_pitch_back", "description": "backward tilt using any stable wedge"},
        {"pose_id": "p10_roll_left", "description": "left tilt using any stable wedge"},
        {"pose_id": "p11_roll_right", "description": "right tilt using any stable wedge"},
        {"pose_id": "p12_diagonal_tilt", "description": "combined pitch and roll using a stable wedge"},
    ]


def create_session(session: Path, pattern_source: Path, camera_config: Path) -> Path:
    session = session.resolve()
    pattern_source = pattern_source.resolve()
    camera_config = camera_config.resolve()
    if session.exists():
        raise ValueError(f"Session directory already exists: {session}")
    _require_pattern_source(pattern_source)
    fixed = _fixed_exposure(camera_config)
    visible_reference = _visible_reference_settings(camera_config)
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
            "visible_reference": {
                "required": True,
                "illumination": "external visible white light; UV projector LED off",
                "projector_uv": "off",
                "checkerboard_detection": "9x9 inner corners must be detected before UV capture",
                "camera_settings": visible_reference,
            },
            "pattern_source": str(pattern_source),
            "camera_config": str(camera_config),
        },
        "suggested_poses": suggested_poses(),
        "visible_reference_captures": [],
        "captured_poses": [],
        "calibration_fit_status": "not_run",
    }
    _write_json(session / "session_manifest.json", manifest)
    (session / "captures").mkdir()
    (session / "README_CAPTURE.txt").write_text(
        "Checkerboard capture session\n\n"
        "For each pose, lock the board so it cannot move between the visible reference and X/Y sequences.\n"
        "1. Turn the UV projector LED off, turn on external visible white light, then run capture-visible.\n"
        "2. Turn visible light off, restore the UV projector LED, then run capture-uv.\n"
        "The visible reference must detect all 9x9 checkerboard inner corners before UV capture can start.\n"
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


def _visible_reference_entry(manifest: dict[str, Any], pose_id: str) -> dict[str, Any] | None:
    for item in manifest.get("visible_reference_captures", []):
        if item.get("pose_id") == pose_id:
            return item
    return None


def _require_visible_reference_workflow(manifest: dict[str, Any]) -> None:
    if not isinstance(manifest.get("capture", {}).get("visible_reference"), dict):
        raise RuntimeError(
            "This session was created by the older UV-only workflow. "
            "Create a new checkerboard session so every pose has a verified visible-light reference."
        )


def _detect_checkerboard(reference_path: Path, inner_corners: tuple[int, int]) -> int:
    image = cv2.imread(str(reference_path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise RuntimeError(f"Could not read visible reference image: {reference_path}")
    found, corners = cv2.findChessboardCornersSB(image, inner_corners)
    if not found or corners is None:
        raise RuntimeError(
            "Visible reference does not contain a detectable 9x9 checkerboard. "
            "Keep the UV projector LED off, increase visible-light contrast/exposure, and recapture."
        )
    return int(len(corners))


def capture_visible_reference(session: Path, pose_id: str, controller: Path) -> None:
    if not POSE_ID_PATTERN.fullmatch(pose_id):
        raise ValueError("pose_id may use only letters, numbers, '.', '_' and '-'")
    session = session.resolve()
    manifest_path = session / "session_manifest.json"
    manifest = _read_json(manifest_path)
    _require_visible_reference_workflow(manifest)
    if any(item["pose_id"] == pose_id for item in manifest.get("captured_poses", [])):
        raise ValueError(f"Pose is already complete: {pose_id}")
    camera_config = Path(manifest["capture"]["camera_config"])
    settings = manifest["capture"]["visible_reference"]["camera_settings"]
    captures = session / "captures"
    scan_id = f"{pose_id}_visible_reference"
    command = [
        sys.executable, str(controller), "--single-capture", "--output", str(captures),
        "--scan-id", scan_id, "--camera-config", str(camera_config),
        "--exposure-us", str(settings["exposure_us"]), "--gain-db", str(settings["gain_db"]),
    ]
    print("[checkerboard] capture visible-light reference (UV projector LED must be off)", flush=True)
    subprocess.run(command, check=True)
    reference_dir = captures / scan_id
    capture_log = _read_json(reference_dir / "capture_log.json")
    reference_path = reference_dir / str(capture_log["filename"])
    corner_count = _detect_checkerboard(reference_path, tuple(manifest["checkerboard"]["inner_corners"]))
    entry = {
        "pose_id": pose_id,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "illumination": manifest["capture"]["visible_reference"]["illumination"],
        "image": str(reference_path),
        "camera_settings": settings,
        "checkerboard_detected": True,
        "corner_count": corner_count,
    }
    references = [item for item in manifest.get("visible_reference_captures", []) if item.get("pose_id") != pose_id]
    references.append(entry)
    manifest["visible_reference_captures"] = references
    _write_json(manifest_path, manifest)
    print(f"[checkerboard] visible reference verified pose={pose_id} corners={corner_count}", flush=True)


def capture_uv_pose(session: Path, pose_id: str, controller: Path) -> None:
    if not POSE_ID_PATTERN.fullmatch(pose_id):
        raise ValueError("pose_id may use only letters, numbers, '.', '_' and '-'")
    session = session.resolve()
    manifest_path = session / "session_manifest.json"
    manifest = _read_json(manifest_path)
    _require_visible_reference_workflow(manifest)
    if any(item["pose_id"] == pose_id for item in manifest.get("captured_poses", [])):
        raise ValueError(f"Pose already captured: {pose_id}")
    reference = _visible_reference_entry(manifest, pose_id)
    if reference is None:
        raise RuntimeError(
            f"No verified visible-light reference exists for {pose_id}. "
            "Capture it with the UV projector LED off before the UV X/Y sequence."
        )
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
            "board_locked_from_visible_reference_through_x_and_y": True,
            "visible_reference": reference,
            "x_axis": x_summary,
            "y_axis": y_summary,
        }
    )
    _write_json(manifest_path, manifest)
    print(f"[checkerboard] complete pose={pose_id} visible_reference=1 uv_frames=44", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Set up or capture a checkerboard calibration session.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    setup = subparsers.add_parser("setup", help="Create a checkerboard capture session and X/Y pattern sets.")
    setup.add_argument("--session", required=True, type=Path)
    setup.add_argument("--patterns", required=True, type=Path, help="Current 22-frame scan pattern folder.")
    setup.add_argument("--camera-config", default=Path("camera_config.json"), type=Path)
    for name, help_text in (
        ("capture-visible", "Capture and verify a visible-light checkerboard reference with UV projector LED off."),
        ("capture-uv", "Capture UV X then Y sequences after a verified visible-light reference."),
    ):
        capture = subparsers.add_parser(name, help=help_text)
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
    if args.command == "capture-visible":
        capture_visible_reference(args.session, args.pose_id, args.controller.resolve())
    else:
        capture_uv_pose(args.session, args.pose_id, args.controller.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
