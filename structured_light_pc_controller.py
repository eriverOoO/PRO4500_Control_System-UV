#!/usr/bin/env python3
"""PC master controller for XIMEA UV structured-light capture."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from camera_provider import CameraError, CameraFrame, CameraInterface, CameraProvider, CameraSettings


IMAGE_SUFFIXES = {".bmp", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
FINAL_DECODE_SUFFIX = ".png"
ARUCO_DICTIONARIES = {
    "DICT_4X4_50": "DICT_4X4_50",
    "DICT_4X4_100": "DICT_4X4_100",
    "DICT_4X4_250": "DICT_4X4_250",
    "DICT_4X4_1000": "DICT_4X4_1000",
    "DICT_5X5_50": "DICT_5X5_50",
    "DICT_5X5_100": "DICT_5X5_100",
    "DICT_5X5_250": "DICT_5X5_250",
    "DICT_5X5_1000": "DICT_5X5_1000",
    "DICT_6X6_50": "DICT_6X6_50",
    "DICT_6X6_100": "DICT_6X6_100",
    "DICT_6X6_250": "DICT_6X6_250",
    "DICT_6X6_1000": "DICT_6X6_1000",
    "DICT_7X7_50": "DICT_7X7_50",
    "DICT_7X7_100": "DICT_7X7_100",
    "DICT_7X7_250": "DICT_7X7_250",
    "DICT_7X7_1000": "DICT_7X7_1000",
    "DICT_ARUCO_ORIGINAL": "DICT_ARUCO_ORIGINAL",
}

PATTERN_CONTRACT: tuple[tuple[int, str], ...] = (
    (0, "White"),
    (1, "Black"),
    (2, "Gray0"),
    (3, "Gray1"),
    (4, "Gray2"),
    (5, "Gray3"),
    (6, "Gray4"),
    (7, "Gray5"),
    (8, "Gray6"),
    (9, "Gray7"),
    (10, "Sine_000"),
    (11, "Sine_090"),
    (12, "Sine_180"),
    (13, "Sine_270"),
    (14, "Gray0_inv"),
    (15, "Gray1_inv"),
    (16, "Gray2_inv"),
    (17, "Gray3_inv"),
    (18, "Gray4_inv"),
    (19, "Gray5_inv"),
    (20, "Gray6_inv"),
    (21, "Gray7_inv"),
)
PATTERN_LABELS = dict(PATTERN_CONTRACT)
LEGACY_PATTERN_IDS = tuple(range(14))
FULL_PATTERN_IDS = tuple(pattern_id for pattern_id, _label in PATTERN_CONTRACT)
DEFAULT_CAPTURE_ORDER = (
    0,
    1,
    2,
    14,
    3,
    15,
    4,
    16,
    5,
    17,
    6,
    18,
    7,
    19,
    8,
    20,
    9,
    21,
    10,
    11,
    12,
    13,
)


@dataclass(frozen=True)
class PatternSpec:
    pattern_id: int
    label: str
    source_path: Path
    invert_source: bool = False


@dataclass(frozen=True)
class ExposureBracket:
    name: str
    exposure_us: int
    gain_db: float = 0.0

    @property
    def exposure_gain_scale(self) -> float:
        gain_linear = math.pow(10.0, float(self.gain_db) / 20.0)
        return max(1.0, float(self.exposure_us) * gain_linear)


@dataclass(frozen=True)
class HdrConfig:
    enabled: bool
    output_bit_depth: int
    saturated_threshold: int
    dark_threshold: int
    black_offset: float
    selection_headroom_threshold: int
    brackets: tuple[ExposureBracket, ...]


@dataclass(frozen=True)
class RigMetadata:
    scan_type: str
    projector_tilt_deg: float
    focus_confirmed: bool
    scheimpflug_confirmed: bool
    rig_id: str
    calibration_id: str
    projector_brightness: str
    keystone_predistortion: bool


@dataclass(frozen=True)
class CaptureConfig:
    hdr: HdrConfig
    rig: RigMetadata
    quality_gate: "QualityGateConfig"


@dataclass(frozen=True)
class QualityGateConfig:
    enabled: bool
    enforcement: str
    white_black_min_contrast_u8: float
    gray_pair_min_valid_ratio: float
    sine_min_modulation_u8: float
    sine_min_valid_ratio: float
    max_decoder_saturation_ratio: float
    max_decoder_dark_ratio: float


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def effective_pattern_settle_ms(args: argparse.Namespace) -> int:
    """Keep physical projector transitions out of the camera exposure window."""
    return max(1000, int(args.settle_ms))


def import_cv2():
    try:
        import cv2  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            "OpenCV is required for display and image saving. Run prepare_pc_python_env.ps1 "
            "or install opencv-python in the Python environment used for this script."
        ) from exc
    return cv2


def parse_csv_ints(value: str, label: str) -> list[int]:
    try:
        items = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{label} must be comma-separated integers") from exc
    if not items:
        raise argparse.ArgumentTypeError(f"{label} cannot be empty")
    return items


def safe_scan_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise ValueError("scan_id may contain only letters, numbers, '.', '_' and '-'")
    return value


def safe_filename_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return token or "bracket"


def pattern_id_from_filename(path: Path) -> int | None:
    for pattern in (r"^pattern[_-](\d{1,3})\b", r"^(\d{1,3})(?:\D|$)"):
        match = re.match(pattern, path.stem, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def pattern_sort_key(path: Path) -> tuple[int, str]:
    pattern_id = pattern_id_from_filename(path)
    index = pattern_id if pattern_id is not None else 1_000_000
    return index, path.name.lower()


def image_files(pattern_dir: Path) -> list[Path]:
    if not pattern_dir.exists():
        raise SystemExit(f"Pattern directory does not exist: {pattern_dir}")
    files = sorted(
        [
            path
            for path in pattern_dir.iterdir()
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        ],
        key=pattern_sort_key,
    )
    if not files:
        raise SystemExit(f"No pattern images found in {pattern_dir}")
    return files


def load_pattern_specs(pattern_dir: Path, *, legacy_14_patterns: bool) -> list[PatternSpec]:
    files_by_id: dict[int, Path] = {}
    for path in image_files(pattern_dir):
        pattern_id = pattern_id_from_filename(path)
        if pattern_id is None:
            continue
        files_by_id.setdefault(pattern_id, path)

    required_ids = LEGACY_PATTERN_IDS if legacy_14_patterns else FULL_PATTERN_IDS
    capture_order = LEGACY_PATTERN_IDS if legacy_14_patterns else DEFAULT_CAPTURE_ORDER
    specs: list[PatternSpec] = []
    missing: list[int] = []

    for pattern_id in capture_order:
        label = PATTERN_LABELS[pattern_id]
        if pattern_id >= 14:
            explicit_inverse_path = files_by_id.get(pattern_id)
            if explicit_inverse_path is not None:
                specs.append(
                    PatternSpec(
                        pattern_id=pattern_id,
                        label=label,
                        source_path=explicit_inverse_path,
                    )
                )
                continue

            normal_id = pattern_id - 12
            source_path = files_by_id.get(normal_id)
            if source_path is not None:
                specs.append(
                    PatternSpec(
                        pattern_id=pattern_id,
                        label=label,
                        source_path=source_path,
                        invert_source=True,
                    )
                )
                continue

        source_path = files_by_id.get(pattern_id)
        if source_path is None:
            missing.append(pattern_id)
            continue
        specs.append(PatternSpec(pattern_id=pattern_id, label=label, source_path=source_path))

    if missing:
        missing_text = ", ".join(f"{pattern_id:02d} {PATTERN_LABELS[pattern_id]}" for pattern_id in missing)
        raise SystemExit(f"Pattern directory is missing required pattern ids: {missing_text}")

    loaded_ids = {spec.pattern_id for spec in specs}
    missing_required = [pattern_id for pattern_id in required_ids if pattern_id not in loaded_ids]
    if missing_required:
        missing_text = ", ".join(f"{pattern_id:02d} {PATTERN_LABELS[pattern_id]}" for pattern_id in missing_required)
        raise SystemExit(f"Pattern contract could not be built. Missing ids: {missing_text}")

    return specs


def read_image(cv2, path: Path):
    import numpy as np  # type: ignore

    data = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise SystemExit(f"Could not decode image: {path}")
    return image


def invert_image(image: Any) -> Any:
    import numpy as np  # type: ignore

    max_value = np.iinfo(image.dtype).max if np.issubdtype(image.dtype, np.integer) else 1.0
    return (max_value - image).astype(image.dtype, copy=False)


def pattern_image(cv2, spec: PatternSpec) -> Any:
    image = read_image(cv2, spec.source_path)
    if spec.invert_source:
        image = invert_image(image)
    return image


def to_grayscale(cv2, image: Any) -> Any:
    if len(image.shape) == 2:
        return image
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def dtype_max(image: Any) -> int:
    import numpy as np  # type: ignore

    if np.issubdtype(image.dtype, np.integer):
        return int(np.iinfo(image.dtype).max)
    return 1


def scale_threshold(value: int, sensor_max: int) -> int:
    if sensor_max <= 255 or value > 255:
        return int(value)
    return int(round(value * (sensor_max / 255.0)))


def final_pattern_filename(pattern_id: int) -> str:
    return f"pattern_{pattern_id:03d}{FINAL_DECODE_SUFFIX}"


def mask_filename(pattern_id: int, name: str) -> str:
    return f"pattern_{pattern_id:03d}_{name}.png"


def normalize_suffix(value: str) -> str:
    suffix = value.lower().strip()
    if not suffix:
        suffix = "png"
    if not suffix.startswith("."):
        suffix = "." + suffix
    if suffix not in {".png", ".tif", ".tiff", ".bmp", ".jpg", ".jpeg"}:
        raise argparse.ArgumentTypeError("save format must be png, tif, tiff, bmp, jpg, or jpeg")
    return suffix


def read_json_file(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON file: {path} ({exc})") from exc


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def default_hdr_brackets(args: argparse.Namespace) -> tuple[ExposureBracket, ...]:
    gain_db = float(args.gain_db if args.gain_db is not None else 0.0)
    return (
        ExposureBracket("short", 2500, gain_db),
        ExposureBracket("mid", 14000, gain_db),
        ExposureBracket("long", 80000, gain_db),
    )


def bracket_overrides(args: argparse.Namespace) -> dict[str, tuple[int | None, float | None]]:
    return {
        "short": (args.short_exposure_us, args.short_gain_db),
        "mid": (args.mid_exposure_us, args.mid_gain_db),
        "long": (args.long_exposure_us, args.long_gain_db),
    }


def apply_bracket_overrides(
    brackets: list[ExposureBracket],
    overrides: dict[str, tuple[int | None, float | None]],
) -> list[ExposureBracket]:
    updated: list[ExposureBracket] = []
    seen: set[str] = set()
    for bracket in brackets:
        key = bracket.name.lower()
        exposure_override, gain_override = overrides.get(key, (None, None))
        seen.add(key)
        updated.append(
            ExposureBracket(
                name=bracket.name,
                exposure_us=max(1, int(exposure_override if exposure_override is not None else bracket.exposure_us)),
                gain_db=float(gain_override if gain_override is not None else bracket.gain_db),
            )
        )

    for name in ("short", "mid", "long"):
        exposure_override, gain_override = overrides[name]
        if name in seen or (exposure_override is None and gain_override is None):
            continue
        default_exposure = int(args_default_exposures()[name])
        updated.append(
            ExposureBracket(
                name=name,
                exposure_us=max(1, int(exposure_override if exposure_override is not None else default_exposure)),
                gain_db=float(gain_override if gain_override is not None else 0.0),
            )
        )
    return updated


def args_default_exposures() -> dict[str, int]:
    return {"short": 2500, "mid": 14000, "long": 80000}


def load_capture_config(args: argparse.Namespace) -> CaptureConfig:
    config = read_json_file(args.camera_config)
    capture_section = config.get("capture", {})
    hdr_section = capture_section.get("hdr", {})
    quality_section = capture_section.get("quality_gate", {})
    metadata_section = capture_section.get("metadata", {})
    quality_enforcement = str(quality_section.get("enforcement", "record_only")).strip().lower()
    if quality_enforcement not in ("record_only", "block"):
        raise ValueError("capture.quality_gate.enforcement must be 'record_only' or 'block'")

    bracket_items = hdr_section.get("brackets", [])
    brackets: list[ExposureBracket] = []
    for index, item in enumerate(bracket_items):
        if not isinstance(item, dict):
            continue
        name = safe_filename_token(str(item.get("name") or f"bracket_{index:02d}"))
        exposure_us = int(item.get("exposure_us", args.exposure_us or 20000))
        gain_db = float(item.get("gain_db", args.gain_db if args.gain_db is not None else 0.0))
        brackets.append(ExposureBracket(name=name, exposure_us=max(1, exposure_us), gain_db=gain_db))
    if not brackets:
        brackets = list(default_hdr_brackets(args))
    brackets = apply_bracket_overrides(brackets, bracket_overrides(args))

    output_bit_depth = int(hdr_section.get("output_bit_depth", 16))
    if output_bit_depth not in {8, 16}:
        raise SystemExit("capture.hdr.output_bit_depth must be 8 or 16")

    scan_type = args.scan_type or str(metadata_section.get("scan_type", "object"))
    if scan_type not in {"reference", "object"}:
        raise SystemExit("--scan-type must be 'reference' or 'object'")

    focus_confirmed = (
        args.focus_confirmed
        if args.focus_confirmed is not None
        else parse_bool(metadata_section.get("focus_confirmed"), False)
    )
    scheimpflug_confirmed = (
        args.scheimpflug_confirmed
        if args.scheimpflug_confirmed is not None
        else parse_bool(metadata_section.get("scheimpflug_confirmed"), False)
    )
    keystone_predistortion = (
        args.keystone_predistortion
        if args.keystone_predistortion is not None
        else parse_bool(metadata_section.get("keystone_predistortion"), False)
    )

    return CaptureConfig(
        hdr=HdrConfig(
            enabled=parse_bool(hdr_section.get("enabled"), True),
            output_bit_depth=output_bit_depth,
            saturated_threshold=int(hdr_section.get("saturated_threshold", 250)),
            dark_threshold=int(hdr_section.get("dark_threshold", 5)),
            black_offset=float(hdr_section.get("black_offset", 0.0)),
            selection_headroom_threshold=int(hdr_section.get("selection_headroom_threshold", 235)),
            brackets=tuple(brackets),
        ),
        rig=RigMetadata(
            scan_type=scan_type,
            projector_tilt_deg=float(args.projector_tilt_deg if args.projector_tilt_deg is not None else metadata_section.get("projector_tilt_deg", 30.0)),
            focus_confirmed=focus_confirmed,
            scheimpflug_confirmed=scheimpflug_confirmed,
            rig_id=str(args.rig_id if args.rig_id is not None else metadata_section.get("rig_id", "")),
            calibration_id=str(args.calibration_id if args.calibration_id is not None else metadata_section.get("calibration_id", "")),
            projector_brightness=str(args.projector_brightness if args.projector_brightness is not None else metadata_section.get("projector_brightness", "")),
            keystone_predistortion=keystone_predistortion,
        ),
        quality_gate=QualityGateConfig(
            enabled=(
                args.quality_gate
                if args.quality_gate is not None
                else parse_bool(quality_section.get("enabled"), True)
            ),
            enforcement=quality_enforcement,
            white_black_min_contrast_u8=float(quality_section.get("white_black_min_contrast_u8", 20.0)),
            gray_pair_min_valid_ratio=float(quality_section.get("gray_pair_min_valid_ratio", 0.05)),
            sine_min_modulation_u8=float(quality_section.get("sine_min_modulation_u8", 12.0)),
            sine_min_valid_ratio=float(quality_section.get("sine_min_valid_ratio", 0.05)),
            max_decoder_saturation_ratio=float(quality_section.get("max_decoder_saturation_ratio", 0.20)),
            max_decoder_dark_ratio=float(quality_section.get("max_decoder_dark_ratio", 0.80)),
        ),
    )


def write_image(cv2, path: Path, image: Any) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(path.suffix, image)
    if not ok:
        raise RuntimeError(f"cv2.imencode failed for {path.suffix}")
    payload = encoded.tobytes()
    path.write_bytes(payload)
    return len(payload)


def preview_image(cv2, image: Any) -> Any:
    import numpy as np  # type: ignore

    if getattr(image, "dtype", None) == np.uint16:
        max_value = int(image.max()) if image.size else 0
        scale = 255.0 / max(1, max_value)
        return cv2.convertScaleAbs(image, alpha=scale)
    return image


class GuiPreviewPublisher:
    """Publish a compact BMP frame that the native control panel can display.

    The file replacement is atomic, so the GUI will always either see the
    previous complete frame or the new complete frame.  This keeps camera I/O
    in this process: the GUI never needs to open a competing XIMEA handle.
    """

    def __init__(self, cv2, output_path: Path | None, max_width: int) -> None:
        self.cv2 = cv2
        self.output_path = output_path.resolve() if output_path is not None else None
        self.max_width = max(64, int(max_width))
        self.temp_path = (
            self.output_path.with_name(self.output_path.stem + ".writing.bmp")
            if self.output_path is not None
            else None
        )

    def publish(self, image: Any) -> None:
        if self.output_path is None or self.temp_path is None:
            return

        preview = preview_image(self.cv2, image)
        height, width = preview.shape[:2]
        if width > self.max_width:
            scaled_height = max(1, round(height * self.max_width / width))
            preview = self.cv2.resize(
                preview,
                (self.max_width, scaled_height),
                interpolation=self.cv2.INTER_AREA,
            )
        if len(preview.shape) == 3 and preview.shape[2] == 3:
            # xiAPI RGB data is RGB; OpenCV encoders expect BGR channel order.
            preview = self.cv2.cvtColor(preview, self.cv2.COLOR_RGB2BGR)

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        ok, encoded = self.cv2.imencode(".bmp", preview)
        if not ok:
            raise RuntimeError("cv2.imencode failed for GUI preview BMP")
        self.temp_path.write_bytes(encoded.tobytes())
        # A GUI image load can briefly hold the old bitmap open on Windows.
        # Preview delivery must never interrupt a real scan, so skip just this
        # display update if the replacement remains temporarily locked.
        for attempt in range(3):
            try:
                os.replace(self.temp_path, self.output_path)
                return
            except PermissionError:
                if attempt == 2:
                    return
                time.sleep(0.02)


def camera_overrides(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "provider": args.camera_provider,
        "device_index": args.camera_device_index,
        "exposure_us": args.exposure_us,
        "gain_db": args.gain_db,
        "fps": args.fps,
        "trigger_mode": args.trigger_mode,
        "image_format": args.image_format,
        "timeout_ms": args.camera_timeout_ms,
    }


def aruco_prescan_camera_profile(args: argparse.Namespace) -> dict[str, Any]:
    """Load the XIMEA-only settings that reproduce the validated CamTool view."""
    config = read_json_file(args.camera_config)
    camera = config.get("camera", {})
    ximea = camera.get("ximea", {}) if isinstance(camera, dict) else {}
    profile = ximea.get("aruco_prescan", {}) if isinstance(ximea, dict) else {}
    if not isinstance(profile, dict):
        raise ValueError("camera.ximea.aruco_prescan must be an object")
    return {
        name: profile[name]
        for name in ("exposure_us", "gain_db", "fps", "trigger_mode", "image_format", "timeout_ms")
        if name in profile
    }


def preview_camera_profile(args: argparse.Namespace) -> dict[str, Any]:
    config = read_json_file(args.camera_config)
    camera = config.get("camera", {})
    ximea = camera.get("ximea", {}) if isinstance(camera, dict) else {}
    profile = ximea.get("preview", {}) if isinstance(ximea, dict) else {}
    if not isinstance(profile, dict):
        raise ValueError("camera.ximea.preview must be an object")
    return {
        name: profile[name]
        for name in ("exposure_us", "gain_db", "fps", "trigger_mode", "image_format", "timeout_ms")
        if name in profile
    }


def open_camera(
    args: argparse.Namespace,
    *,
    exposure_us: int | None = None,
    profile_overrides: dict[str, Any] | None = None,
) -> tuple[CameraInterface, CameraSettings]:
    overrides = camera_overrides(args)
    # A mode-specific profile takes precedence over common GUI camera controls.
    # This keeps the main scan's software trigger and HDR operation independent.
    overrides.update(profile_overrides or {})
    if exposure_us is not None:
        if exposure_us < 1:
            raise ValueError("ArUco exposure must be at least 1 microsecond")
        overrides["exposure_us"] = exposure_us
    settings = CameraProvider.load_settings(args.camera_config, overrides)
    camera = CameraProvider.create(settings)
    camera.open()
    camera.start()
    print(f"[camera] opened {camera.describe()}", flush=True)
    for warning in camera.warnings:
        print(f"[camera] warning: {warning}", flush=True)
    return camera, settings


@dataclass
class MonitorBounds:
    x: int
    y: int
    width: int
    height: int
    device_name: str = ""
    primary: bool = False


def windows_monitors() -> list[MonitorBounds]:
    """Return physical monitor rectangles using the same Win32 coordinate space as OpenCV."""
    if sys.platform != "win32":
        return []

    import ctypes
    from ctypes import wintypes

    # HighGUI uses physical pixels on Windows.  Without DPI awareness Windows can
    # virtualize these coordinates, which makes a window miss a non-primary display.
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    monitors: list[MonitorBounds] = []
    monitor_enum_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
    )

    @monitor_enum_proc
    def collect(handle, _dc, _rect, _data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            rect = info.rcMonitor
            monitors.append(
                MonitorBounds(
                    x=rect.left,
                    y=rect.top,
                    width=rect.right - rect.left,
                    height=rect.bottom - rect.top,
                    device_name=info.szDevice,
                    primary=bool(info.dwFlags & 1),  # MONITORINFOF_PRIMARY
                )
            )
        return True

    if not ctypes.windll.user32.EnumDisplayMonitors(None, None, collect, 0):
        raise OSError("EnumDisplayMonitors failed")
    return monitors


class PatternDisplay:
    def __init__(self, args: argparse.Namespace, first_image: Any) -> None:
        self.window_name = args.window_name
        self.windowed = args.windowed
        self.monitor_index = args.monitor
        self.window_x = args.window_x
        self.window_y = args.window_y
        self.keep_aspect = not args.stretch
        self.bounds = self._detect_bounds(first_image)

    def _detect_bounds(self, first_image: Any) -> MonitorBounds:
        height, width = first_image.shape[:2]
        if self.windowed:
            return MonitorBounds(
                x=self.window_x or 80,
                y=self.window_y or 80,
                width=width,
                height=height,
            )

        try:
            monitors = windows_monitors()
            if not monitors:
                from screeninfo import get_monitors  # type: ignore

                monitors = [
                    MonitorBounds(monitor.x, monitor.y, monitor.width, monitor.height)
                    for monitor in get_monitors()
                ]
            if self.monitor_index < 0 or self.monitor_index >= len(monitors):
                primary = next((monitor for monitor in monitors if monitor.primary), monitors[0])
                print(
                    f"[display] monitor index {self.monitor_index} is unavailable; "
                    f"using {primary.device_name or 'the primary display'} instead.",
                    flush=True,
                )
                return primary
            return monitors[self.monitor_index]
        except Exception:
            from screeninfo import get_monitors  # type: ignore

            monitors = get_monitors()
            if self.monitor_index < 0 or self.monitor_index >= len(monitors):
                raise IndexError
            monitor = monitors[self.monitor_index]
            return MonitorBounds(monitor.x, monitor.y, monitor.width, monitor.height)
        except Exception:
            print(
                "[display] Could not read monitor geometry. "
                "Using image size; pass --window-x/--window-y or install screeninfo if needed.",
                flush=True,
            )
            return MonitorBounds(
                x=self.window_x or 0,
                y=self.window_y or 0,
                width=width,
                height=height,
            )

    def _pin_fullscreen_window_to_monitor(self) -> bool:
        """Correct HighGUI's Windows fullscreen placement after it defaults to primary."""
        if sys.platform != "win32" or self.windowed:
            return False
        try:
            import ctypes

            hwnd = ctypes.windll.user32.FindWindowW(None, self.window_name)
            if not hwnd:
                return False
            # HWND_TOP keeps the projection unobscured but does not make it a
            # permanent topmost window once the HighGUI window is destroyed.
            return bool(
                ctypes.windll.user32.SetWindowPos(
                    hwnd,
                    -1,  # HWND_TOPMOST
                    self.bounds.x,
                    self.bounds.y,
                    self.bounds.width,
                    self.bounds.height,
                    0x0040,  # SWP_SHOWWINDOW
                )
            )
        except Exception:
            return False

    def open(self, cv2) -> None:
        cv2.namedWindow(self.window_name, cv2.WINDOW_NORMAL)
        cv2.moveWindow(self.window_name, self.bounds.x, self.bounds.y)
        cv2.resizeWindow(self.window_name, self.bounds.width, self.bounds.height)
        if not self.windowed:
            cv2.setWindowProperty(
                self.window_name,
                cv2.WND_PROP_FULLSCREEN,
                cv2.WINDOW_FULLSCREEN,
            )
            # Process the fullscreen request before forcing the native window
            # back to the selected monitor.  Calling moveWindow only before
            # WINDOW_FULLSCREEN is not sufficient on the Windows HighGUI backend.
            cv2.waitKey(1)
            if not self._pin_fullscreen_window_to_monitor():
                cv2.moveWindow(self.window_name, self.bounds.x, self.bounds.y)
        print(
            "[display] window="
            f"{self.window_name!r} x={self.bounds.x} y={self.bounds.y} "
            f"w={self.bounds.width} h={self.bounds.height} "
            f"device={self.bounds.device_name or 'unknown'} "
            f"primary={self.bounds.primary}",
            flush=True,
        )

    def render(self, cv2, image: Any) -> Any:
        import numpy as np  # type: ignore

        if not self.keep_aspect:
            return cv2.resize(
                image,
                (self.bounds.width, self.bounds.height),
                interpolation=cv2.INTER_NEAREST,
            )

        image_h, image_w = image.shape[:2]
        scale = min(self.bounds.width / image_w, self.bounds.height / image_h)
        out_w = max(1, int(round(image_w * scale)))
        out_h = max(1, int(round(image_h * scale)))
        resized = cv2.resize(image, (out_w, out_h), interpolation=cv2.INTER_NEAREST)
        canvas = np.zeros((self.bounds.height, self.bounds.width, 3), dtype=np.uint8)
        x = (self.bounds.width - out_w) // 2
        y = (self.bounds.height - out_h) // 2
        canvas[y : y + out_h, x : x + out_w] = resized
        return canvas

    def show(self, cv2, image: Any) -> None:
        cv2.imshow(self.window_name, self.render(cv2, image))
        cv2.waitKey(1)

    def black(self, cv2) -> None:
        import numpy as np  # type: ignore

        image = np.zeros((self.bounds.height, self.bounds.width, 3), dtype=np.uint8)
        cv2.imshow(self.window_name, image)
        cv2.waitKey(1)

    def close(self, cv2) -> None:
        cv2.destroyWindow(self.window_name)


def run_rotation_command(
    command_template: str,
    *,
    angle: int,
    angle_index: int,
    previous_angle: int | None,
    scan_dir: Path,
) -> None:
    command = command_template.format(
        angle=angle,
        angle_index=angle_index,
        previous_angle="" if previous_angle is None else previous_angle,
        scan_dir=str(scan_dir),
    )
    print(f"[rotation] {command}", flush=True)
    completed = subprocess.run(command, shell=True)
    if completed.returncode != 0:
        raise RuntimeError(f"rotation command failed with exit code {completed.returncode}")


def read_angle_advance_token(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def wait_for_angle_advance(path: Path, *, angle: int, angle_index: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    wait_started_ms = now_ms()
    print(
        f"[angle] Waiting for rotation to angle={angle:03d} "
        f"(index={angle_index}). Click Next Angle in the PC controller.",
        flush=True,
    )
    while True:
        token = read_angle_advance_token(path)
        if token is not None and token >= wait_started_ms:
            print(f"[angle] Continue angle={angle:03d}", flush=True)
            return
        time.sleep(0.2)


def append_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    fieldnames = [
        "scan_id",
        "scan_type",
        "angle_deg",
        "pattern_id",
        "label",
        "capture_id",
        "attempt",
        "bracket_name",
        "exposure_us",
        "gain_db",
        "pattern_filename",
        "pattern_display_timestamp_pc_ms",
        "capture_command_timestamp_pc_ms",
        "camera_timestamp_ms",
        "camera_frame_index",
        "received_image_filename",
        "final_filename",
        "saturated_mask_filename",
        "dark_mask_filename",
        "size_bytes",
        "status",
        "error",
    ]
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def capture_filename(
    *,
    scan_id: str,
    angle_deg: int | None,
    pattern_id: int | None,
    capture_id: int,
    suffix: str,
    prefix: str = "",
) -> str:
    angle_text = "" if angle_deg is None else f"_angle_{angle_deg:03d}"
    if pattern_id is None:
        pattern_text = ""
    else:
        pattern_text = f"_pattern_{pattern_id:03d}"
    return f"{prefix}{scan_id}{angle_text}{pattern_text}_capture_{capture_id:03d}{suffix}"


def save_camera_frame(cv2, frame: CameraFrame, output_path: Path) -> int:
    return write_image(cv2, output_path, frame.image)


def optional_image_filename(path: Path | None, scan_dir: Path) -> str:
    return "" if path is None else relative_to_scan(path, scan_dir)


def synthesize_frame(cv2, pattern: Any, bracket: ExposureBracket, hdr: HdrConfig) -> Any:
    import numpy as np  # type: ignore

    gray = to_grayscale(cv2, pattern)
    max_scale = max(item.exposure_gain_scale for item in hdr.brackets)
    scale = bracket.exposure_gain_scale / max(1.0, max_scale)
    simulated = np.rint(np.clip(gray.astype(np.float32) * scale, 0, dtype_max(gray)))
    return simulated.astype(gray.dtype)


def to_decoder_u8(image: Any):
    """Use the same 0..255 domain the decoder uses for mono PNG inputs."""
    import numpy as np  # type: ignore

    array = np.asarray(image)
    if array.dtype == np.uint16:
        return np.rint(array.astype(np.float32) * (255.0 / 65535.0)).astype(np.uint8)
    if array.dtype != np.uint8:
        maximum = float(np.max(array)) if array.size else 1.0
        return np.clip(array.astype(np.float32) * (255.0 / max(1.0, maximum)), 0, 255).astype(np.uint8)
    return array


def merge_hdr_frames(
    cv2,
    frames: list[Any],
    brackets: tuple[ExposureBracket, ...],
    hdr: HdrConfig,
    black_offsets: list[float] | None = None,
    selected_bracket_index: int | None = None,
    sequence_selection: dict[str, Any] | None = None,
) -> tuple[Any, Any, Any, Any, dict[str, Any]]:
    import numpy as np  # type: ignore

    if not frames:
        raise RuntimeError("HDR merge requires at least one frame")

    gray_frames = [to_grayscale(cv2, frame) for frame in frames]
    first_shape = gray_frames[0].shape
    mismatched = [index for index, frame in enumerate(gray_frames) if frame.shape != first_shape]
    if mismatched:
        raise RuntimeError(f"HDR bracket frame shapes do not match; mismatched indices: {mismatched}")

    stack = np.stack(gray_frames, axis=0)
    sensor_max = dtype_max(gray_frames[0])
    saturated_threshold = min(sensor_max, scale_threshold(hdr.saturated_threshold, sensor_max))
    selection_threshold = min(
        saturated_threshold,
        scale_threshold(hdr.selection_headroom_threshold, sensor_max),
    )
    dark_threshold = min(sensor_max, scale_threshold(hdr.dark_threshold, sensor_max))
    if black_offsets is None:
        black_offsets = [hdr.black_offset] * len(frames)
    if len(black_offsets) != len(frames):
        raise RuntimeError("HDR black offset count does not match bracket frame count")
    offset_values = np.array(black_offsets, dtype=np.float32)[:, None, None]
    corrected_stack = np.maximum(stack.astype(np.float32) - offset_values, 0.0)

    scales = np.array([bracket.exposure_gain_scale for bracket in brackets], dtype=np.float32)
    priority = np.argsort(scales)
    if selected_bracket_index is None:
        chosen = np.full(first_shape, int(priority[0]), dtype=np.int32)
        any_valid = np.zeros(first_shape, dtype=bool)
        for index in priority:
            # Keep headroom below hard sensor clipping. Near-clip pixels use a
            # shorter bracket to preserve White/Black and inverse-Gray contrast.
            valid = (corrected_stack[index] > dark_threshold) & (stack[index] < selection_threshold)
            chosen[valid] = int(index)
            any_valid |= valid
        algorithm = "longest_headroom_radiance_normalized"
    else:
        if not 0 <= selected_bracket_index < len(frames):
            raise RuntimeError("selected HDR bracket index is out of range")
        chosen = np.full(first_shape, selected_bracket_index, dtype=np.int32)
        any_valid = (
            (corrected_stack[selected_bracket_index] > dark_threshold)
            & (stack[selected_bracket_index] < saturated_threshold)
        )
        algorithm = "structured_light_sequence_bracket"

    output_max = 65535 if hdr.output_bit_depth == 16 else 255
    output_dtype = np.uint16 if hdr.output_bit_depth == 16 else np.uint8
    if selected_bracket_index is None:
        selected = np.take_along_axis(corrected_stack, chosen[None, :, :], axis=0)[0]
        selected_scales = scales[chosen]
        max_scale = float(scales.max())
        output_signal = selected / np.maximum(selected_scales, 1.0) * max_scale
        output_signal[~any_valid] = 0.0
        output_signal = np.clip(output_signal, 0, sensor_max)
    else:
        # The decoder performs its own White/Black normalization.  Preserve the
        # selected bracket's native pattern contrast instead of applying an
        # HDR radiance scaling that changes its White/Black relationship.
        output_signal = stack[selected_bracket_index].astype(np.float32)
    merged = np.clip(
        output_signal * (output_max / max(1, sensor_max)), 0, output_max
    ).astype(output_dtype)

    selected_bracket_map = chosen.astype(np.uint8)
    merged_u8 = to_decoder_u8(merged)
    if selected_bracket_index is None:
        saturated_mask = np.all(stack >= saturated_threshold, axis=0).astype(np.uint8) * 255
        dark_mask = np.all(corrected_stack <= dark_threshold, axis=0).astype(np.uint8) * 255
    else:
        saturated_mask = (stack[selected_bracket_index] >= saturated_threshold).astype(np.uint8) * 255
        dark_mask = (corrected_stack[selected_bracket_index] <= dark_threshold).astype(np.uint8) * 255
    selected_bracket_map = chosen.astype(np.uint8)
    merged_u8 = to_decoder_u8(merged)

    report = {
        "algorithm": algorithm,
        "output_bit_depth": hdr.output_bit_depth,
        "saturated_threshold": int(saturated_threshold),
        "selection_headroom_threshold": int(selection_threshold),
        "dark_threshold": int(dark_threshold),
        "black_offsets": [float(value) for value in black_offsets],
        "saturated_pixel_count": int(np.count_nonzero(saturated_mask)),
        "decoder_near_white_pixel_count": int(np.count_nonzero(merged_u8 >= hdr.saturated_threshold)),
        "decoder_near_black_pixel_count": int(np.count_nonzero(merged_u8 <= hdr.dark_threshold)),
        "dark_pixel_count": int(np.count_nonzero(dark_mask)),
        "invalid_pixel_count": int(np.size(any_valid) - np.count_nonzero(any_valid)),
        "input_dtype": str(gray_frames[0].dtype),
        "input_shape": [int(first_shape[0]), int(first_shape[1])],
        "bracket_priority": [brackets[int(index)].name for index in priority],
        "selected_bracket_pixel_counts": {
            brackets[index].name: int(np.count_nonzero(selected_bracket_map == index))
            for index in range(len(brackets))
        },
    }
    if selected_bracket_index is not None:
        report["selected_sequence_bracket"] = brackets[selected_bracket_index].name
    if sequence_selection is not None:
        report["sequence_selection"] = sequence_selection
    return merged, saturated_mask, dark_mask, selected_bracket_map, report


def select_structured_light_sequence_bracket(
    cv2,
    captures: dict[int, tuple[list[Any], list[float]]],
    brackets: tuple[ExposureBracket, ...],
    hdr: HdrConfig,
) -> tuple[int | None, dict[str, Any]]:
    """Choose one bracket for an entire FPP sequence using decoder-facing metrics.

    Per-pattern HDR pixel selection can make a normal/inverse Gray pair or the
    four phase frames originate from different exposures.  The resulting image
    can look well exposed while its relative structured-light contrast is
    unusable.  This selector instead evaluates each complete bracket sequence
    with the same white/black, Gray-pair, and four-step sine tests used by the
    decoder, then uses one common bracket for all final patterns.
    """
    import numpy as np  # type: ignore

    required = set(range(22))
    if not required.issubset(captures):
        return None, {
            "mode": "fallback_per_pattern_hdr",
            "reason": "full 22-pattern Gray-pair FPP sequence was not captured",
        }
    if any(len(captures[pattern_id][0]) != len(brackets) for pattern_id in required):
        return None, {
            "mode": "fallback_per_pattern_hdr",
            "reason": "one or more patterns have incomplete HDR bracket captures",
        }

    candidate_reports: list[dict[str, Any]] = []
    best_index = 0
    best_score: tuple[int, int, int] | None = None

    for bracket_index, bracket in enumerate(brackets):
        normalized: dict[int, Any] = {}
        for pattern_id in required:
            frames, _offsets = captures[pattern_id]
            normalized[pattern_id] = to_grayscale(
                cv2, frames[bracket_index]
            ).astype(np.float32)

        white = normalized[0]
        black = normalized[1]
        signal = white - black
        valid_capture = (signal > 20.0) & (white < 250.0) & (white > 5.0)
        safe_signal = np.maximum(signal, 1e-6)
        gray_confidence = np.minimum.reduce(
            [
                np.abs(normalized[2 + bit] - normalized[14 + bit]) / safe_signal
                for bit in range(8)
            ]
        )
        gray_valid = gray_confidence >= 0.05
        corrected_sine = [
            np.clip((normalized[pattern_id] - black) / safe_signal, 0.0, 1.0)
            for pattern_id in (10, 11, 12, 13)
        ]
        modulation = 0.5 * np.hypot(
            corrected_sine[0] - corrected_sine[2],
            corrected_sine[3] - corrected_sine[1],
        )
        modulation_valid = modulation > 0.05
        combined = valid_capture & gray_valid & modulation_valid
        score = (
            int(np.count_nonzero(combined)),
            int(np.count_nonzero(valid_capture & gray_valid)),
            int(np.count_nonzero(valid_capture & modulation_valid)),
        )
        candidate_reports.append(
            {
                "bracket": bracket.name,
                "exposure_us": bracket.exposure_us,
                "gain_db": bracket.gain_db,
                "valid_capture_ratio": float(np.mean(valid_capture)),
                "gray_valid_ratio": float(np.mean(gray_valid)),
                "modulation_valid_ratio": float(np.mean(modulation_valid)),
                "combined_valid_ratio": float(np.mean(combined)),
            }
        )
        if best_score is None or score > best_score:
            best_index = bracket_index
            best_score = score

    return best_index, {
        "mode": "decoder_aware_common_bracket",
        "selected_bracket": brackets[best_index].name,
        "selection_metric": "maximize combined valid pixels, then Gray and sine overlaps",
        "candidates": candidate_reports,
    }


def prepare_single_exposure_frame(cv2, frame: Any) -> tuple[Any, Any, Any, Any, dict[str, Any]]:
    import numpy as np  # type: ignore

    output = to_grayscale(cv2, frame).copy()
    masks = np.zeros(output.shape, dtype=np.uint8)
    if np.issubdtype(output.dtype, np.integer):
        output_bit_depth = int(np.iinfo(output.dtype).bits)
    else:
        output_bit_depth = int(output.dtype.itemsize * 8)
    report = {
        "algorithm": "single_exposure_passthrough",
        "output_bit_depth": output_bit_depth,
        "saturated_threshold": None,
        "dark_threshold": None,
        "black_offsets": [],
        "saturated_pixel_count": 0,
        "dark_pixel_count": 0,
        "invalid_pixel_count": 0,
        "input_dtype": str(output.dtype),
        "input_shape": [int(output.shape[0]), int(output.shape[1])],
        "bracket_priority": ["single"],
    }
    return output, masks, masks.copy(), masks.copy(), report


def validate_decode_outputs(folder: Path, expected_ids: tuple[int, ...]) -> list[int]:
    return [
        pattern_id
        for pattern_id in expected_ids
        if not (folder / final_pattern_filename(pattern_id)).exists()
    ]


def relative_to_scan(path: Path, scan_dir: Path) -> str:
    try:
        return path.relative_to(scan_dir).as_posix()
    except ValueError:
        return path.as_posix()


def parse_aruco_ids(value: str) -> list[int]:
    try:
        marker_ids = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise ValueError("ArUco marker IDs must be comma-separated integers") from exc
    if not marker_ids:
        raise ValueError("At least one ArUco marker ID is required")
    return marker_ids


def aruco_dictionary(cv2, dictionary_name: str):
    aruco = getattr(cv2, "aruco", None)
    if aruco is None:
        raise RuntimeError(
            "cv2.aruco is unavailable. Install opencv-contrib-python in the PC controller environment."
        )
    dictionary_id = getattr(aruco, dictionary_name, None)
    if dictionary_id is None:
        raise ValueError(f"Unknown ArUco dictionary: {dictionary_name}")
    if hasattr(aruco, "getPredefinedDictionary"):
        return aruco.getPredefinedDictionary(dictionary_id)
    return aruco.Dictionary_get(dictionary_id)


def to_aruco_grayscale(cv2, image: Any):
    import numpy as np  # type: ignore

    grayscale = to_grayscale(cv2, image)
    if grayscale.dtype == np.uint8:
        return grayscale
    finite = grayscale[np.isfinite(grayscale)]
    if finite.size == 0:
        return np.zeros(grayscale.shape, dtype=np.uint8)
    low, high = np.percentile(finite, [1.0, 99.0])
    if high <= low:
        low, high = float(np.min(finite)), float(np.max(finite))
    if high <= low:
        return np.zeros(grayscale.shape, dtype=np.uint8)
    scaled = (grayscale.astype(np.float32) - float(low)) * (255.0 / (float(high) - float(low)))
    return np.clip(scaled, 0, 255).astype(np.uint8)


def detect_aruco_markers(cv2, image: Any, dictionary_name: str) -> dict[int, Any]:
    aruco = cv2.aruco
    dictionary = aruco_dictionary(cv2, dictionary_name)
    grayscale = to_aruco_grayscale(cv2, image)
    if hasattr(aruco, "ArucoDetector"):
        detector = aruco.ArucoDetector(dictionary, aruco.DetectorParameters())
        corners, ids, _rejected = detector.detectMarkers(grayscale)
    else:
        corners, ids, _rejected = aruco.detectMarkers(grayscale, dictionary)
    if ids is None:
        return {}
    return {
        int(marker_id): marker_corners.reshape(4, 2).astype(float)
        for marker_id, marker_corners in zip(ids.ravel().tolist(), corners)
    }


def aruco_marker_candidates(marker_ids: list[int]) -> list[list[int]]:
    """Return all markers first, then opposite stage pairs in configured order."""
    candidates = [marker_ids]
    if len(marker_ids) == 4:
        candidates.extend(([marker_ids[0], marker_ids[2]], [marker_ids[1], marker_ids[3]]))
    return candidates


def select_aruco_markers(
    detected: dict[int, Any],
    marker_ids: list[int],
) -> list[int]:
    for candidate in aruco_marker_candidates(marker_ids):
        if all(marker_id in detected for marker_id in candidate):
            return candidate
    found = sorted(detected)
    expected = (
        f"all IDs {marker_ids} or opposite pair {marker_ids[0]},{marker_ids[2]} "
        f"or {marker_ids[1]},{marker_ids[3]}"
        if len(marker_ids) == 4
        else f"all IDs {marker_ids}"
    )
    raise RuntimeError(
        "ArUco verification failed. Recapture this no-pattern image after checking "
        f"focus, exposure, and marker visibility. Expected {expected}; detected IDs={found}"
    )


def require_aruco_markers(
    cv2,
    image: Any,
    *,
    dictionary_name: str,
    marker_ids: list[int],
) -> tuple[dict[int, Any], list[int]]:
    detected = detect_aruco_markers(cv2, image, dictionary_name)
    return detected, select_aruco_markers(detected, marker_ids)


def aruco_prescan_dir(output_root: Path) -> Path:
    return output_root / "aruco_precalibration"


def aruco_prescan_image_path(output_root: Path, role: str) -> Path:
    if role not in {"zero", "rotated"}:
        raise ValueError("ArUco prescan role must be zero or rotated")
    name = "prescan_0.png" if role == "zero" else "prescan_nominal_180.png"
    return aruco_prescan_dir(output_root) / name


def aruco_stage_geometry(args: argparse.Namespace, marker_ids: list[int]) -> dict[str, Any]:
    """Return the physical stage coordinates associated with the configured marker order."""
    config = read_json_file(args.camera_config)
    capture = config.get("capture", {})
    stage = capture.get("aruco_stage", {}) if isinstance(capture, dict) else {}
    if not isinstance(stage, dict):
        stage = {}
    layout = str(stage.get("layout", "stage-cross"))
    if layout != "stage-cross":
        raise ValueError("capture.aruco_stage.layout must be stage-cross")
    radius_mm = float(stage.get("marker_center_radius_mm", 42.0))
    if not math.isfinite(radius_mm) or radius_mm <= 0:
        raise ValueError("capture.aruco_stage.marker_center_radius_mm must be positive")
    stage_diameter_mm = float(stage.get("stage_diameter_mm", 105.0))
    if not math.isfinite(stage_diameter_mm) or stage_diameter_mm < 2.0 * radius_mm:
        raise ValueError("capture.aruco_stage.stage_diameter_mm must contain the marker centers")
    directions = ((0.0, -radius_mm), (radius_mm, 0.0), (0.0, radius_mm), (-radius_mm, 0.0))
    return {
        "layout": layout,
        "marker_center_radius_mm": radius_mm,
        "stage_diameter_mm": stage_diameter_mm,
        "marker_centers_mm": {
            str(marker_id): [float(x), float(y)]
            for marker_id, (x, y) in zip(marker_ids, directions)
        },
        "marker_order": "top,right,bottom,left",
    }


def aruco_marker_observations(markers: dict[int, Any]) -> dict[str, dict[str, list[list[float]] | list[float]]]:
    """Serialize detected ArUco geometry for later stage-coordinate calibration."""
    import numpy as np  # type: ignore

    observations: dict[str, dict[str, list[list[float]] | list[float]]] = {}
    for marker_id, corners in sorted(markers.items()):
        polygon = np.asarray(corners, dtype=np.float64).reshape(4, 2)
        observations[str(marker_id)] = {
            "corners_px": polygon.tolist(),
            "center_px": polygon.mean(axis=0).tolist(),
        }
    return observations


def copy_aruco_prescan_artifacts(output_root: Path, scan_dir: Path) -> dict[str, Any]:
    """Keep per-scan ArUco evidence with calibration scans instead of a mutable global copy."""
    source_dir = aruco_prescan_dir(output_root)
    destination_dir = scan_dir / "aruco_prescan"
    filenames = (
        "prescan_0.png",
        "prescan_nominal_180.png",
        "zero_capture.json",
        "rotated_capture.json",
    )
    copied: list[str] = []
    missing: list[str] = []
    for filename in filenames:
        source = source_dir / filename
        if not source.exists():
            missing.append(filename)
            continue
        destination_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination_dir / filename)
        copied.append(filename)
    observations: dict[str, Any] = {}
    required_ids: set[str] = set()
    for role, filename in (("zero", "zero_capture.json"), ("rotated", "rotated_capture.json")):
        metadata = read_json_file(source_dir / filename)
        if not metadata:
            continue
        marker_data = metadata.get("marker_observations", {})
        requested = metadata.get("requested_marker_ids", [])
        if isinstance(marker_data, dict):
            observations[role] = sorted(marker_data)
        if isinstance(requested, list):
            required_ids.update(str(marker_id) for marker_id in requested)
    full_marker_coverage = bool(required_ids) and all(
        required_ids.issubset(set(observations.get(role, [])))
        for role in ("zero", "rotated")
    )
    return {
        "status": "copied" if copied else "not_found",
        "source": str(source_dir),
        "directory": str(destination_dir) if copied else "",
        "copied_files": copied,
        "missing_files": missing,
        "spatial_calibration": {
            "required_marker_ids": sorted(required_ids),
            "detected_marker_ids_by_role": observations,
            "full_marker_coverage": full_marker_coverage,
            "status": "ready" if full_marker_coverage else "insufficient_markers",
        },
    }


def run_aruco_prescan_capture(args: argparse.Namespace) -> int:
    """Capture and validate one no-pattern ArUco frame without replacing a good frame on failure."""
    cv2 = import_cv2()
    gui_preview = GuiPreviewPublisher(cv2, args.gui_preview_file, args.gui_preview_max_width)
    camera: CameraInterface | None = None
    try:
        marker_ids = parse_aruco_ids(args.aruco_ids)
        stage_geometry = aruco_stage_geometry(args, marker_ids)
        camera, settings = open_camera(
            args,
            exposure_us=args.aruco_exposure_us,
            profile_overrides=aruco_prescan_camera_profile(args),
        )
        print(
            f"[aruco] profile exposure={settings.exposure_us}us gain={settings.gain_db:g}dB "
            f"trigger={settings.trigger_mode} fps={settings.fps:g} format={settings.image_format}",
            flush=True,
        )
        frame = camera.capture_frame()
        gui_preview.publish(frame.image)
        markers, selected_ids = require_aruco_markers(
            cv2,
            frame.image,
            dictionary_name=args.aruco_dictionary,
            marker_ids=marker_ids,
        )
        output_path = aruco_prescan_image_path(args.output.resolve(), args.aruco_prescan_role)
        size_bytes = save_camera_frame(cv2, frame, output_path)
        metadata = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "role": args.aruco_prescan_role,
            "filename": output_path.name,
            "size_bytes": size_bytes,
            "dictionary": args.aruco_dictionary,
            "requested_marker_ids": marker_ids,
            "selected_marker_ids": selected_ids,
            "detected_ids": sorted(markers),
            "marker_observations": aruco_marker_observations(markers),
            "stage_geometry": stage_geometry,
            "exposure_us": settings.exposure_us,
            "gain_db": settings.gain_db,
            "camera_timestamp_ms": frame.timestamp_ms,
            "camera_frame_index": frame.frame_index,
        }
        (output_path.parent / f"{args.aruco_prescan_role}_capture.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(
            f"[aruco] verified role={args.aruco_prescan_role} selected_ids={selected_ids} saved={output_path}",
            flush=True,
        )
        return 0
    except (CameraError, RuntimeError, ValueError) as exc:
        print(f"[aruco] ERROR: {exc}", flush=True)
        return 1
    finally:
        if camera is not None:
            camera.stop()
            camera.close()


def aruco_reprojection_stats(cv2, source: Any, target: Any, matrix: Any, inliers: Any) -> dict[str, float | int]:
    import numpy as np  # type: ignore

    projected = cv2.perspectiveTransform(source.reshape(-1, 1, 2).astype(np.float32), matrix).reshape(-1, 2)
    distances = np.linalg.norm(projected - target, axis=1)
    inlier_mask = np.ones(len(distances), dtype=bool) if inliers is None else np.asarray(inliers).reshape(-1).astype(bool)
    if len(inlier_mask) != len(distances):
        inlier_mask = np.ones(len(distances), dtype=bool)
    inlier_distances = distances[inlier_mask]
    return {
        "reprojection_rmse_px": float(np.sqrt(np.mean(distances * distances))),
        "inlier_reprojection_rmse_px": float(np.sqrt(np.mean(inlier_distances * inlier_distances))),
        "max_reprojection_error_px": float(np.max(distances)),
        "point_count": int(len(distances)),
        "inlier_count": int(np.count_nonzero(inlier_mask)),
    }


def aruco_similarity_summary(source: Any, target: Any) -> tuple[float, float, list[float] | None]:
    """Summarize actual rotation and fixed point; the homography remains the fusion warp."""
    import numpy as np  # type: ignore

    src = np.asarray(source, dtype=np.float64)
    dst = np.asarray(target, dtype=np.float64)
    src_mean, dst_mean = src.mean(axis=0), dst.mean(axis=0)
    src_centered, dst_centered = src - src_mean, dst - dst_mean
    energy = float(np.sum(src_centered * src_centered))
    if energy <= np.finfo(float).eps:
        raise RuntimeError("ArUco points are degenerate; recapture both prescan images")
    u, _singular_values, vt = np.linalg.svd(src_centered.T @ dst_centered)
    rotation = vt.T @ u.T
    if np.linalg.det(rotation) < 0:
        vt[-1, :] *= -1.0
        rotation = vt.T @ u.T
    scale = float(np.sum((src_centered @ rotation.T) * dst_centered) / energy)
    translation = dst_mean - scale * (rotation @ src_mean)
    angle_deg = math.degrees(math.atan2(float(rotation[1, 0]), float(rotation[0, 0])))
    angle_deg = (angle_deg + 180.0) % 360.0 - 180.0
    fixed_point_matrix = np.eye(2) - scale * rotation
    center = None
    if abs(float(np.linalg.det(fixed_point_matrix))) >= 1e-8:
        value = np.linalg.solve(fixed_point_matrix, translation)
        center = [float(value[0]), float(value[1])]
    return float(angle_deg), scale, center


def run_aruco_precalibration(args: argparse.Namespace) -> int:
    """Create the standard decoder-compatible fusion JSON from two verified prescans."""
    import numpy as np  # type: ignore

    cv2 = import_cv2()
    try:
        marker_ids = parse_aruco_ids(args.aruco_ids)
        output_root = args.output.resolve()
        zero_path = aruco_prescan_image_path(output_root, "zero")
        rotated_path = aruco_prescan_image_path(output_root, "rotated")
        if not zero_path.exists() or not rotated_path.exists():
            missing = [str(path) for path in (zero_path, rotated_path) if not path.exists()]
            raise RuntimeError("Capture the missing ArUco prescan image(s) first: " + ", ".join(missing))
        zero = read_image(cv2, zero_path)
        rotated = read_image(cv2, rotated_path)
        target_by_id, _target_selected = require_aruco_markers(
            cv2, zero, dictionary_name=args.aruco_dictionary, marker_ids=marker_ids
        )
        source_by_id, _source_selected = require_aruco_markers(
            cv2, rotated, dictionary_name=args.aruco_dictionary, marker_ids=marker_ids
        )
        shared_by_candidate = [
            candidate
            for candidate in aruco_marker_candidates(marker_ids)
            if all(marker_id in target_by_id and marker_id in source_by_id for marker_id in candidate)
        ]
        if not shared_by_candidate:
            raise RuntimeError(
                "The 0 and nominal-180 prescans do not share the same full marker set or opposite pair. "
                "Recapture the view with the missing matching pair."
            )
        selected_ids = shared_by_candidate[0]
        source = np.asarray([point for marker_id in selected_ids for point in source_by_id[marker_id]], dtype=np.float32)
        target = np.asarray([point for marker_id in selected_ids for point in target_by_id[marker_id]], dtype=np.float32)
        matrix, inliers = cv2.findHomography(
            source,
            target,
            method=cv2.RANSAC,
            ransacReprojThreshold=float(args.aruco_ransac_threshold_px),
        )
        if matrix is None:
            raise RuntimeError("Could not estimate ArUco homography. Recapture both prescan images")
        stats = aruco_reprojection_stats(cv2, source, target, matrix, inliers)
        angle_deg, scale, center = aruco_similarity_summary(source, target)
        payload = {
            "homography": np.asarray(matrix, dtype=float).tolist(),
            "matrix": np.asarray(matrix, dtype=float).tolist(),
            "transform_kind": "homography",
            "transform_direction": "180_to_0",
            "source": {"role": "stage-rotated", "image": str(rotated_path)},
            "target": {"role": "stage-0", "image": str(zero_path)},
            "aruco": {
                "dictionary": args.aruco_dictionary,
                "requested_marker_ids": marker_ids,
                "marker_ids": selected_ids,
                "method": "homography",
                "ransac_threshold_px": float(args.aruco_ransac_threshold_px),
                **stats,
                "rotation_source_to_target_deg": angle_deg,
                "deviation_from_180_deg": abs(abs(angle_deg) - float(args.aruco_intended_rotation_deg)),
                "similarity_scale": scale,
                "rotation_center_target_xy": center,
            },
            "stage_precalibration": {
                "commanded_stage_value": float(args.aruco_stage_command_value),
                "intended_rotation_deg": float(args.aruco_intended_rotation_deg),
                "actual_rotation_magnitude_deg": abs(angle_deg),
                "source_to_target_rotation_deg": angle_deg,
                "rotation_center_target_xy": center,
                "similarity_scale": scale,
                "usage": "Maps the nominal-180-degree structured-light scan into the 0-degree scan.",
                "transform_direction": "180_to_0",
            },
        }
        output_path = aruco_prescan_dir(output_root) / "stage_precalibration.json"
        output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(
            f"[aruco] calibration ready selected_ids={selected_ids} actual_rotation={abs(angle_deg):.4f}deg "
            f"rmse={stats['reprojection_rmse_px']:.3f}px saved={output_path}",
            flush=True,
        )
        return 0
    except (RuntimeError, ValueError) as exc:
        print(f"[aruco] ERROR: {exc}", flush=True)
        return 1


def verify_aruco_prescan_stability(args: argparse.Namespace) -> dict[str, Any]:
    """Reject a stale 0-degree prescan before main capture if the rig moved."""
    import numpy as np  # type: ignore

    if args.dry_run or args.no_camera:
        return {"status": "skipped", "reason": "synthetic capture"}
    config = read_json_file(args.camera_config)
    ximea = config.get("camera", {}).get("ximea", {})
    limits = ximea.get("aruco_stability_check", {}) if isinstance(ximea, dict) else {}
    if not parse_bool(limits.get("enabled") if isinstance(limits, dict) else None, True):
        return {"status": "skipped", "reason": "disabled"}
    cv2 = import_cv2()
    zero_path = aruco_prescan_image_path(args.output.resolve(), "zero")
    if not zero_path.exists():
        raise RuntimeError("ArUco stability check requires the verified 0-degree prescan")
    zero = read_image(cv2, zero_path)
    marker_ids = parse_aruco_ids(args.aruco_ids)
    target_by_id, _selected = require_aruco_markers(
        cv2, zero, dictionary_name=args.aruco_dictionary, marker_ids=marker_ids
    )
    camera: CameraInterface | None = None
    try:
        camera, _settings = open_camera(args, profile_overrides=aruco_prescan_camera_profile(args))
        current_frame = camera.capture_frame()
    finally:
        if camera is not None:
            camera.stop()
            camera.close()
    try:
        source_by_id, _selected = require_aruco_markers(
            cv2, current_frame.image, dictionary_name=args.aruco_dictionary, marker_ids=marker_ids
        )
    except RuntimeError as exc:
        # Main scan lighting is intentionally different from the no-pattern
        # ArUco exposure.  Do not discard a verified precalibration merely
        # because a fresh verification frame cannot see the printed markers.
        result = {
            "status": "unverified_marker_not_visible",
            "reason": str(exc),
            "action": "using_existing_stage_precalibration",
        }
        print("[aruco] stability check skipped: markers are not visible; using existing precalibration", flush=True)
        return result
    candidates = [
        candidate for candidate in aruco_marker_candidates(marker_ids)
        if all(marker_id in target_by_id and marker_id in source_by_id for marker_id in candidate)
    ]
    if not candidates:
        raise RuntimeError("ArUco stability check cannot find the same marker pair; recapture prescan")
    selected_ids = candidates[0]
    source = np.asarray([point for marker_id in selected_ids for point in source_by_id[marker_id]], dtype=np.float32)
    target = np.asarray([point for marker_id in selected_ids for point in target_by_id[marker_id]], dtype=np.float32)
    center_shift = float(np.mean(np.linalg.norm(source - target, axis=1)))
    rotation_deg, scale, _center = aruco_similarity_summary(source, target)
    max_shift = float(limits.get("max_center_shift_px", 10.0))
    max_rotation = float(limits.get("max_rotation_deg", 2.0))
    max_scale_deviation = float(limits.get("max_scale_deviation", 0.03))
    result = {
        "status": "passed",
        "marker_ids": selected_ids,
        "mean_corner_shift_px": center_shift,
        "rotation_deg": rotation_deg,
        "scale": scale,
        "limits": {
            "max_center_shift_px": max_shift,
            "max_rotation_deg": max_rotation,
            "max_scale_deviation": max_scale_deviation,
        },
    }
    if center_shift > max_shift or abs(rotation_deg) > max_rotation or abs(scale - 1.0) > max_scale_deviation:
        raise RuntimeError(
            "ArUco prescan is stale: rig/board/stage moved "
            f"(shift={center_shift:.2f}px rotation={rotation_deg:.2f}deg scale={scale:.4f}); recapture prescan"
        )
    output_dir = aruco_prescan_dir(args.output.resolve())
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    write_image(cv2, output_dir / f"stability_before_scan_{stamp}.png", current_frame.image)
    (output_dir / "stability_before_scan.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"[aruco] stability check passed marker_ids={selected_ids} shift={center_shift:.2f}px", flush=True)
    return result


QUALITY_GATE_PATTERN_IDS = (0, 1, 2, 14, 10, 11, 12, 13)


def decoder_channel_stats(cv2, image: Any, hdr: HdrConfig) -> dict[str, float | int | str]:
    """Summarize the exact mono/blue-equivalent values consumed by the decoder."""
    import numpy as np  # type: ignore

    decoder_u8 = to_decoder_u8(to_grayscale(cv2, image))
    return {
        "channel": "blue_equivalent_mono" if len(image.shape) == 2 else "blue",
        "dtype": str(image.dtype),
        "decoder_dtype": str(decoder_u8.dtype),
        "min": int(np.min(decoder_u8)),
        "max": int(np.max(decoder_u8)),
        "mean": float(np.mean(decoder_u8)),
        "median": float(np.median(decoder_u8)),
        "decoder_saturation_ratio": float(np.mean(decoder_u8 >= hdr.saturated_threshold)),
        "decoder_dark_ratio": float(np.mean(decoder_u8 <= hdr.dark_threshold)),
    }


def assess_fpp_quality(cv2, images: dict[int, Any], hdr: HdrConfig, gate: QualityGateConfig) -> dict[str, Any]:
    import numpy as np  # type: ignore

    stats = {str(pattern_id): decoder_channel_stats(cv2, image, hdr) for pattern_id, image in images.items()}
    result: dict[str, Any] = {"channel": "blue_equivalent", "patterns": stats, "checks": []}
    failures: list[str] = []
    active: Any | None = None
    if 0 in images and 1 in images:
        white = to_decoder_u8(to_grayscale(cv2, images[0])).astype(np.float32)
        black = to_decoder_u8(to_grayscale(cv2, images[1])).astype(np.float32)
        signal = np.maximum(white - black, 0.0)
        # The camera deliberately includes a large black border around the
        # projected stage.  A whole-frame median is therefore dominated by
        # that border (and can report 1--3 DN even when the illuminated stage
        # has excellent contrast).  Measure the portion that actually reaches
        # the decoder's minimum contrast instead, and require a meaningful
        # amount of that illuminated area.
        active = signal >= gate.white_black_min_contrast_u8
        active_ratio = float(np.mean(active))
        contrast = float(np.median(signal[active])) if np.any(active) else 0.0
        min_active_ratio = gate.gray_pair_min_valid_ratio
        passed = active_ratio >= min_active_ratio
        result["white_black"] = {
            "metric": "median_active_signal_u8",
            "median_contrast_u8": contrast,
            "active_contrast_ratio": active_ratio,
            "min_active_contrast_ratio": min_active_ratio,
            "passed": passed,
        }
        if not passed:
            failures.append(
                "White/Black active contrast coverage="
                f"{active_ratio:.3f} < {min_active_ratio:.3f}"
            )
    for normal_id, inverse_id in zip(range(2, 10), range(14, 22)):
        if normal_id not in images or inverse_id not in images:
            continue
        difference = np.abs(
            to_decoder_u8(to_grayscale(cv2, images[normal_id])).astype(np.float32)
            - to_decoder_u8(to_grayscale(cv2, images[inverse_id])).astype(np.float32)
        )
        valid = difference >= gate.white_black_min_contrast_u8
        full_frame_valid_ratio = float(np.mean(valid))
        # Evaluate Gray complements inside the illuminated stage only.  The
        # full camera frame has a deliberately black border, so a full-frame
        # ratio can flag a valid dark object merely because most pixels never
        # receive a projected pattern.
        valid_ratio = float(np.mean(valid[active])) if active is not None and np.any(active) else 0.0
        passed = valid_ratio >= gate.gray_pair_min_valid_ratio
        result.setdefault("gray_pairs", {})[f"{normal_id:03d}_{inverse_id:03d}"] = {
            "contrast_valid_ratio": valid_ratio,
            "full_frame_contrast_valid_ratio": full_frame_valid_ratio,
            "evaluation_region": "white_black_active_stage",
            "passed": passed,
        }
        if not passed:
            failures.append(f"Gray pair {normal_id:03d}/{inverse_id:03d} valid={valid_ratio:.3f}")
    sine_ids = [pattern_id for pattern_id in range(10, 14) if pattern_id in images]
    if len(sine_ids) == 4:
        sine_stack = np.stack([to_decoder_u8(to_grayscale(cv2, images[pattern_id])) for pattern_id in sine_ids], axis=0)
        modulation = sine_stack.max(axis=0).astype(np.float32) - sine_stack.min(axis=0).astype(np.float32)
        valid = modulation >= gate.sine_min_modulation_u8
        full_frame_valid_ratio = float(np.mean(valid))
        valid_ratio = float(np.mean(valid[active])) if active is not None and np.any(active) else 0.0
        result["sine"] = {
            "median_modulation_u8": float(np.median(modulation)),
            "valid_ratio": valid_ratio,
            "full_frame_valid_ratio": full_frame_valid_ratio,
            "evaluation_region": "white_black_active_stage",
            "passed": valid_ratio >= gate.sine_min_valid_ratio,
        }
        if valid_ratio < gate.sine_min_valid_ratio:
            failures.append(f"Sine modulation valid={valid_ratio:.3f}")
    # Final 16-bit HDR values are radiance-normalized to the longest bracket.
    # Their near-white ratio is retained above as a decoder-facing diagnostic,
    # but it is not a sensor-clipping gate. The preflight adds raw all-bracket
    # saturation/dark checks, which distinguish real clipping from valid White.
    result["passed"] = not failures
    result["failures"] = failures
    result["recommendations"] = (
        [] if not failures else [
            "Reduce the long HDR exposure or projector brightness when decoder saturation is high.",
            "Block ambient light and verify projector focus when White/Black or Gray contrast is low.",
            "Check projector pattern timing and camera focus when sine modulation is low.",
        ]
    )
    return result


def write_quality_histogram(cv2, images: dict[int, Any], output_path: Path) -> None:
    import numpy as np  # type: ignore

    canvas = np.full((300, 512, 3), 255, dtype=np.uint8)
    colors = ((0, 0, 255), (0, 128, 0), (255, 0, 0), (0, 128, 128))
    for index, pattern_id in enumerate(sorted(images)[:4]):
        histogram = np.bincount(to_decoder_u8(to_grayscale(cv2, images[pattern_id])).ravel(), minlength=256)
        peak = max(1, int(histogram.max()))
        points = np.array([[value * 2, 280 - int(histogram[value] * 240 / peak)] for value in range(256)], dtype=np.int32)
        cv2.polylines(canvas, [points], False, colors[index], 1)
        cv2.putText(canvas, f"{pattern_id:03d}", (12 + 80 * index, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.5, colors[index], 1)
    write_image(cv2, output_path, canvas)


def record_pre_capture_display_witness(
    cv2,
    *,
    camera: CameraInterface | None,
    projected: Any,
    spec: PatternSpec,
    hdr: HdrConfig,
    args: argparse.Namespace,
    output_dir: Path,
) -> dict[str, Any]:
    """Persist the requested pattern and the settled camera view before HDR capture.

    The projector framebuffer itself cannot be read back through OpenCV.  Saving the
    rendered pattern alongside a camera frame acquired *after* the settle interval
    makes a timing/display failure visible without confusing it with HDR merging.
    """
    witness_dir = output_dir / "display_witness"
    projected_path = witness_dir / f"pattern_{spec.pattern_id:03d}_projected.png"
    write_image(cv2, projected_path, projected)
    result: dict[str, Any] = {
        "pattern_id": spec.pattern_id,
        "label": spec.label,
        "requested_pattern": projected_path.name,
        "settle_ms": effective_pattern_settle_ms(args),
        "flush_frame_count": max(0, args.settle_flush_frames),
    }
    if camera is None:
        result["camera_witness"] = None
        return result

    # A software-triggered XIMEA capture has no stale free-run queue, but taking
    # disposable frames here makes the timing explicit and covers providers that do.
    bracket = hdr.brackets[0]
    camera.configure_capture(exposure_us=bracket.exposure_us, gain_db=bracket.gain_db)
    if args.bracket_settle_ms > 0:
        time.sleep(args.bracket_settle_ms / 1000.0)
    discarded_indices: list[int] = []
    for _ in range(max(0, args.settle_flush_frames)):
        discarded_indices.append(camera.capture_frame().frame_index)
    frame = camera.capture_frame()
    camera_path = witness_dir / f"pattern_{spec.pattern_id:03d}_camera_settled.png"
    save_camera_frame(cv2, frame, camera_path)
    result.update(
        {
            "camera_witness": camera_path.name,
            "witness_exposure_us": bracket.exposure_us,
            "witness_gain_db": bracket.gain_db,
            "camera_timestamp_ms": frame.timestamp_ms,
            "camera_frame_index": frame.frame_index,
            "discarded_frame_indices": discarded_indices,
        }
    )
    return result


def run_capture_quality_gate(args: argparse.Namespace, patterns: list[PatternSpec], capture_config: CaptureConfig) -> dict[str, Any]:
    """Capture a preflight diagnostic set before optionally blocking the main scan."""
    cv2 = import_cv2()
    hdr = capture_config.hdr
    requested = {spec.pattern_id: spec for spec in patterns}
    selected = [requested[pattern_id] for pattern_id in QUALITY_GATE_PATTERN_IDS if pattern_id in requested]
    output_dir = args.output.resolve() / "quality_gate" / datetime.now().strftime("preflight_%Y%m%d_%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)
    camera: CameraInterface | None = None
    display: PatternDisplay | None = None
    merged_images: dict[int, Any] = {}
    merge_reports: dict[int, dict[str, Any]] = {}
    display_witnesses: list[dict[str, Any]] = []
    try:
        if not args.no_camera and not args.dry_run:
            camera, _settings = open_camera(args)
        if not args.no_display and not args.dry_run:
            display = PatternDisplay(args, pattern_image(cv2, selected[0]))
            display.open(cv2)
        for spec in selected:
            projected = pattern_image(cv2, spec)
            if display is not None:
                display.show(cv2, projected)
            time.sleep(effective_pattern_settle_ms(args) / 1000.0)
            display_witnesses.append(
                record_pre_capture_display_witness(
                    cv2,
                    camera=camera,
                    projected=projected,
                    spec=spec,
                    hdr=hdr,
                    args=args,
                    output_dir=output_dir,
                )
            )
            frames: list[Any] = []
            offsets: list[float] = []
            for bracket in hdr.brackets:
                if camera is not None:
                    camera.configure_capture(exposure_us=bracket.exposure_us, gain_db=bracket.gain_db)
                    time.sleep(args.bracket_settle_ms / 1000.0)
                    frame = camera.capture_frame()
                    image = frame.image
                    offsets.append(float(frame.metadata.get("black_level", hdr.black_offset)))
                else:
                    image = synthesize_frame(cv2, projected, bracket, hdr)
                    offsets.append(hdr.black_offset)
                frames.append(image)
                write_image(cv2, output_dir / "raw" / f"pattern_{spec.pattern_id:03d}_{bracket.name}.png", image)
            merged, saturated, dark, selected_map, merge = merge_hdr_frames(cv2, frames, hdr.brackets, hdr, offsets)
            merged_images[spec.pattern_id] = merged
            merge_reports[spec.pattern_id] = merge
            write_image(cv2, output_dir / final_pattern_filename(spec.pattern_id), merged)
            write_image(cv2, output_dir / "masks" / mask_filename(spec.pattern_id, "saturated"), saturated)
            write_image(cv2, output_dir / "masks" / mask_filename(spec.pattern_id, "dark"), dark)
            write_image(cv2, output_dir / "masks" / mask_filename(spec.pattern_id, "selected_bracket"), selected_map)
        report = assess_fpp_quality(cv2, merged_images, hdr, capture_config.quality_gate)
        raw_hdr_checks: dict[str, Any] = {}
        for pattern_id, merge in merge_reports.items():
            total = max(1, int(merged_images[pattern_id].size))
            saturated_ratio = float(merge["saturated_pixel_count"] / total)
            dark_ratio = float(merge["dark_pixel_count"] / total)
            raw_hdr_checks[str(pattern_id)] = {
                "all_brackets_saturated_ratio": saturated_ratio,
                "all_brackets_dark_ratio": dark_ratio,
            }
            if saturated_ratio > capture_config.quality_gate.max_decoder_saturation_ratio:
                report["failures"].append(f"pattern {pattern_id:03d} raw sensor saturation={saturated_ratio:.3f}")
            if pattern_id != 1 and dark_ratio > capture_config.quality_gate.max_decoder_dark_ratio:
                report["failures"].append(f"pattern {pattern_id:03d} raw sensor dark={dark_ratio:.3f}")
        report["raw_hdr_checks"] = raw_hdr_checks
        report["display_witnesses"] = display_witnesses
        report["passed"] = not report["failures"]
        report.update(
            {
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "output_dir": str(output_dir),
                "mode": "preflight",
                "enforcement": capture_config.quality_gate.enforcement,
                "status": "passed" if report["passed"] else "failed",
            }
        )
        write_quality_histogram(cv2, merged_images, output_dir / "blue_channel_histograms.png")
        (output_dir / "quality_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        if report["passed"]:
            print(f"[quality] preflight passed: {output_dir}", flush=True)
        else:
            print(
                "[quality] WARNING: preflight criteria not met; continuing main scan and recording failures: "
                + "; ".join(report["failures"]),
                flush=True,
            )
        return report
    finally:
        if display is not None:
            display.close(cv2)
        if camera is not None:
            camera.stop()
            camera.close()


def summarize_quality_issues(
    preflight_report: dict[str, Any], final_reports: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Create a compact, machine-readable index of all quality criteria failures."""
    preflight_failures = list(preflight_report.get("failures", []))
    final_failures = {
        str(angle): list(report.get("failures", []))
        for angle, report in final_reports.items()
        if report.get("failures")
    }
    return {
        "schema_version": 1,
        "enforcement": "record_only",
        "main_scan_continued_after_preflight_failure": bool(preflight_failures),
        "preflight": {
            "status": preflight_report.get("status", "not_run"),
            "failure_count": len(preflight_failures),
            "failures": preflight_failures,
            "report_directory": preflight_report.get("output_dir"),
        },
        "final_scan_failures_by_angle": final_failures,
        "final_scan_failure_count": sum(len(failures) for failures in final_failures.values()),
    }


def run_scan(args: argparse.Namespace) -> int:
    cv2 = import_cv2()
    gui_preview = GuiPreviewPublisher(cv2, args.gui_preview_file, args.gui_preview_max_width)
    pattern_dir = args.patterns.resolve()
    patterns = load_pattern_specs(pattern_dir, legacy_14_patterns=args.legacy_14_patterns)
    first_image = pattern_image(cv2, patterns[0])
    capture_config = load_capture_config(args)
    hdr = capture_config.hdr
    if not hdr.enabled:
        hdr = HdrConfig(
            enabled=False,
            output_bit_depth=hdr.output_bit_depth,
            saturated_threshold=hdr.saturated_threshold,
            dark_threshold=hdr.dark_threshold,
            black_offset=hdr.black_offset,
            selection_headroom_threshold=hdr.selection_headroom_threshold,
            brackets=(ExposureBracket("single", int(args.exposure_us or 20000), float(args.gain_db or 0.0)),),
        )

    configured_preflight_calibration = args.stage_precalibration or (
        aruco_prescan_dir(args.output.resolve()) / "stage_precalibration.json"
    )
    aruco_stability: dict[str, Any] = {"status": "not_checked"}
    if configured_preflight_calibration.exists():
        try:
            aruco_stability = verify_aruco_prescan_stability(args)
        except (CameraError, RuntimeError, ValueError) as exc:
            print(f"[aruco] ERROR: main scan blocked: {exc}", flush=True)
            return 1

    quality_gate_report: dict[str, Any] = {"status": "disabled"}
    if capture_config.quality_gate.enabled:
        quality_config = CaptureConfig(hdr=hdr, rig=capture_config.rig, quality_gate=capture_config.quality_gate)
        try:
            quality_gate_report = run_capture_quality_gate(args, patterns, quality_config)
        except (CameraError, RuntimeError, ValueError) as exc:
            print(f"[quality] ERROR: preflight could not run: {exc}", flush=True)
            return 1
        if (
            not quality_gate_report.get("passed", False)
            and capture_config.quality_gate.enforcement == "block"
        ):
            print(
                "[quality] ERROR: preflight failed; main scan blocked. "
                f"Inspect {quality_gate_report.get('output_dir', 'the preflight report')}.",
                flush=True,
            )
            return 1

    output_root = args.output.resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    scan_id = safe_scan_id(args.scan_id or datetime.now().strftime("scan_%Y%m%d_%H%M%S"))
    scan_dir = output_root / scan_id
    scan_dir.mkdir(parents=True, exist_ok=True)
    stage_precalibration: dict[str, str] = {"status": "not_found"}
    aruco_prescan_artifacts = copy_aruco_prescan_artifacts(output_root, scan_dir)
    if aruco_prescan_artifacts["status"] == "copied":
        print(
            "[aruco] copied per-scan prescan evidence: "
            + ", ".join(aruco_prescan_artifacts["copied_files"]),
            flush=True,
        )
        if aruco_prescan_artifacts["spatial_calibration"]["status"] != "ready":
            print(
                "[aruco] WARNING: spatial height calibration needs all requested ArUco IDs "
                "in both zero and rotated prescans; this scan remains usable for 0/180 alignment.",
                flush=True,
            )
    configured_precalibration = args.stage_precalibration or (
        aruco_prescan_dir(output_root) / "stage_precalibration.json"
    )
    if configured_precalibration.exists():
        copied_precalibration = scan_dir / "stage_precalibration.json"
        shutil.copy2(configured_precalibration, copied_precalibration)
        stage_precalibration = {
            "status": "copied",
            "source": str(configured_precalibration),
            "filename": copied_precalibration.name,
        }
        print(f"[aruco] using persisted precalibration: {copied_precalibration}", flush=True)
    else:
        print(
            "[aruco] WARNING: no persisted precalibration JSON was found; "
            "capture and calculate the 0/nominal-180 ArUco prescan before decoding this scan.",
            flush=True,
        )

    angles = parse_csv_ints(args.angles, "angles")
    expected_pattern_ids = LEGACY_PATTERN_IDS if args.legacy_14_patterns else FULL_PATTERN_IDS
    # A quality-gated scan is auditable only when every bracket and its selection
    # masks are retained, even if the legacy Save All checkbox is off.
    save_diagnostics = bool(args.save_all_images or capture_config.quality_gate.enabled)
    scan_rows: list[dict[str, Any]] = []
    final_pattern_rows: list[dict[str, Any]] = []
    hdr_reports: list[dict[str, Any]] = []
    final_quality_reports: dict[str, Any] = {}
    main_scan_marker_visibility: dict[str, Any] = {}
    display: PatternDisplay | None = None
    camera: CameraInterface | None = None
    camera_settings: CameraSettings | None = None
    capture_id = 0
    aborted = False

    print(
        f"[scan] scan_id={scan_id} scan_type={capture_config.rig.scan_type} "
        f"patterns={len(patterns)} angles={angles} brackets={len(hdr.brackets)}",
        flush=True,
    )
    print(
        "[scan] hdr brackets="
        + ", ".join(
            f"{bracket.name}:{bracket.exposure_us}us/{bracket.gain_db:g}dB"
            for bracket in hdr.brackets
        ),
        flush=True,
    )

    try:
        synthetic_capture = bool(args.dry_run or args.no_camera)
        if not synthetic_capture:
            camera, camera_settings = open_camera(args)
            if not hdr.enabled:
                print(
                    f"[camera] fixed settings for entire scan: "
                    f"exposure={camera_settings.exposure_us}us "
                    f"gain={camera_settings.gain_db:g}dB",
                    flush=True,
                )
        else:
            mode = "dry-run synthetic" if args.dry_run else "synthetic because --no-camera was set"
            print(f"[camera] {mode}", flush=True)

        if not args.no_display and not args.dry_run:
            display = PatternDisplay(args, first_image)
            display.open(cv2)
            display.black(cv2)
            time.sleep(args.pre_black_ms / 1000.0)

        previous_angle: int | None = None
        for angle_index, angle in enumerate(angles):
            if args.rotation_command and (angle_index > 0 or args.rotate_first_angle):
                if display is not None:
                    display.black(cv2)
                run_rotation_command(
                    args.rotation_command,
                    angle=angle,
                    angle_index=angle_index,
                    previous_angle=previous_angle,
                    scan_dir=scan_dir,
                )
            elif angle_index > 0 or args.pause_before_first_angle:
                if display is not None:
                    display.black(cv2)
                if args.angle_advance_file:
                    wait_for_angle_advance(
                        args.angle_advance_file,
                        angle=angle,
                        angle_index=angle_index,
                    )
                elif not args.no_angle_prompt:
                    input(f"Set rotation stage to {angle} degrees, then press Enter...")

            angle_dir = scan_dir if len(angles) == 1 else scan_dir / f"angle_{angle:03d}"
            angle_dir.mkdir(parents=True, exist_ok=True)
            angle_hdr_captures: list[dict[str, Any]] = []

            for spec in patterns:
                projected = pattern_image(cv2, spec)
                if display is not None:
                    display.show(cv2, projected)
                display_ts = now_ms()
                time.sleep(effective_pattern_settle_ms(args) / 1000.0)

                bracket_frames: list[Any] = []
                bracket_black_offsets: list[float] = []
                bracket_entries: list[dict[str, Any]] = []
                last_error = ""

                for bracket in hdr.brackets:
                    success = False
                    bracket_token = safe_filename_token(bracket.name)
                    exposure_path = None
                    if save_diagnostics:
                        exposure_path = (
                            angle_dir
                            / "exposures"
                            / f"pattern_{spec.pattern_id:03d}"
                            / f"{bracket_token}{args.save_format}"
                        )

                    for attempt in range(1, args.retries + 2):
                        command_ts = now_ms()
                        row: dict[str, Any] = {
                            "scan_id": scan_id,
                            "scan_type": capture_config.rig.scan_type,
                            "angle_deg": angle,
                            "pattern_id": spec.pattern_id,
                            "label": spec.label,
                            "capture_id": capture_id,
                            "attempt": attempt,
                            "bracket_name": bracket.name,
                            "exposure_us": bracket.exposure_us,
                            "gain_db": bracket.gain_db,
                            "pattern_filename": spec.source_path.name,
                            "pattern_display_timestamp_pc_ms": display_ts,
                            "capture_command_timestamp_pc_ms": command_ts,
                        }

                        try:
                            if camera is not None:
                                if hdr.enabled:
                                    camera.configure_capture(
                                        exposure_us=bracket.exposure_us,
                                        gain_db=bracket.gain_db,
                                    )
                                    if args.bracket_settle_ms > 0:
                                        time.sleep(args.bracket_settle_ms / 1000.0)
                                frame = camera.capture_frame()
                            else:
                                synthetic = synthesize_frame(cv2, projected, bracket, hdr)
                                frame = CameraFrame(
                                    image=synthetic,
                                    timestamp_ms=now_ms(),
                                    frame_index=capture_id,
                                    pixel_format=str(synthetic.dtype),
                                    metadata={
                                        "provider": "synthetic",
                                        "exposure_us": bracket.exposure_us,
                                        "gain_db": bracket.gain_db,
                                    },
                                )

                            gui_preview.publish(frame.image)

                            size_bytes = 0
                            if exposure_path is not None:
                                size_bytes = save_camera_frame(cv2, frame, exposure_path)
                            filename = optional_image_filename(exposure_path, scan_dir)
                            row.update(
                                {
                                    "camera_timestamp_ms": frame.timestamp_ms,
                                    "camera_frame_index": frame.frame_index,
                                    "received_image_filename": filename,
                                    "size_bytes": size_bytes,
                                    "status": "ok",
                                    "error": "",
                                }
                            )
                            scan_rows.append(row)
                            bracket_frames.append(frame.image)
                            black_offset = float(frame.metadata.get("black_level", hdr.black_offset))
                            bracket_black_offsets.append(black_offset)
                            bracket_entries.append(
                                {
                                    "name": bracket.name,
                                    "filename": filename,
                                    "exposure_us": bracket.exposure_us,
                                    "gain_db": bracket.gain_db,
                                    "black_offset": black_offset,
                                    "capture_timestamp_pc_ms": command_ts,
                                    "camera_timestamp_ms": frame.timestamp_ms,
                                    "camera_frame_index": frame.frame_index,
                                    "pixel_format": frame.pixel_format,
                                    "camera_metadata": frame.metadata,
                                }
                            )
                            success = True
                            print(
                                f"[capture] angle={angle:03d} pattern={spec.pattern_id:03d} "
                                f"{spec.label} bracket={bracket.name} capture={capture_id:03d}",
                                flush=True,
                            )
                            capture_id += 1
                            break
                        except Exception as exc:
                            last_error = str(exc)
                            row.update(
                                {
                                    "status": "retry" if attempt <= args.retries else "failed",
                                    "error": last_error,
                                }
                            )
                            scan_rows.append(row)
                            print(
                                f"[capture] failed angle={angle:03d} pattern={spec.pattern_id:03d} "
                                f"bracket={bracket.name} capture={capture_id:03d}: {last_error}",
                                flush=True,
                            )
                            capture_id += 1
                            if attempt <= args.retries:
                                time.sleep(args.retry_delay_ms / 1000.0)

                    if not success:
                        aborted = True
                        raise RuntimeError(
                            f"scan aborted at angle={angle} pattern={spec.pattern_id} "
                            f"bracket={bracket.name}: {last_error}"
                        )

                if hdr.enabled:
                    merged, saturated_mask, dark_mask, selected_bracket_map, merge_report = merge_hdr_frames(
                        cv2,
                        bracket_frames,
                        hdr.brackets,
                        hdr,
                        bracket_black_offsets,
                    )
                else:
                    merged, saturated_mask, dark_mask, selected_bracket_map, merge_report = prepare_single_exposure_frame(
                        cv2,
                        bracket_frames[0],
                    )
                final_path = angle_dir / final_pattern_filename(spec.pattern_id)
                final_size = write_image(cv2, final_path, merged)

                final_filename = relative_to_scan(final_path, scan_dir)
                saturated_filename = ""
                dark_filename = ""
                selected_bracket_filename = ""
                saturated_size = 0
                dark_size = 0
                selected_bracket_size = 0
                saturated_path: Path | None = None
                dark_path: Path | None = None
                selected_bracket_path: Path | None = None
                if save_diagnostics and hdr.enabled:
                    saturated_path = angle_dir / "hdr_masks" / mask_filename(spec.pattern_id, "saturated")
                    dark_path = angle_dir / "hdr_masks" / mask_filename(spec.pattern_id, "dark")
                    selected_bracket_path = angle_dir / "hdr_masks" / mask_filename(spec.pattern_id, "selected_bracket")
                    saturated_size = write_image(cv2, saturated_path, saturated_mask)
                    dark_size = write_image(cv2, dark_path, dark_mask)
                    selected_bracket_size = write_image(cv2, selected_bracket_path, selected_bracket_map)
                    saturated_filename = relative_to_scan(saturated_path, scan_dir)
                    dark_filename = relative_to_scan(dark_path, scan_dir)
                    selected_bracket_filename = relative_to_scan(selected_bracket_path, scan_dir)
                merge_report.update(
                    {
                        "filename": final_filename,
                        "size_bytes": final_size,
                        "saturated_mask_filename": saturated_filename,
                        "saturated_mask_size_bytes": saturated_size,
                        "dark_mask_filename": dark_filename,
                        "dark_mask_size_bytes": dark_size,
                        "selected_bracket_map_filename": selected_bracket_filename,
                        "selected_bracket_map_size_bytes": selected_bracket_size,
                    }
                )
                pattern_entry = {
                    "pattern_id": spec.pattern_id,
                    "label": spec.label,
                    "filename": final_filename,
                    "angle_deg": angle,
                    "source_pattern_filename": spec.source_path.name,
                    "source_inverted": spec.invert_source,
                    "brackets": bracket_entries,
                    "merge": merge_report,
                }
                final_pattern_rows.append(pattern_entry)
                hdr_report = {
                    "angle_deg": angle,
                    "pattern_id": spec.pattern_id,
                    "label": spec.label,
                    **merge_report,
                }
                hdr_reports.append(hdr_report)
                for row in scan_rows[-len(bracket_entries) :]:
                    if row.get("pattern_id") == spec.pattern_id and row.get("angle_deg") == angle:
                        row["final_filename"] = final_filename
                        row["saturated_mask_filename"] = saturated_filename
                        row["dark_mask_filename"] = dark_filename
                print(
                    f"[merge] angle={angle:03d} pattern={spec.pattern_id:03d} "
                    f"saved={final_filename}",
                    flush=True,
                )
                if hdr.enabled:
                    angle_hdr_captures.append(
                        {
                            "spec": spec,
                            "frames": bracket_frames,
                            "black_offsets": bracket_black_offsets,
                            "final_path": final_path,
                            "saturated_path": saturated_path,
                            "dark_path": dark_path,
                            "merge_report": merge_report,
                            "hdr_report": hdr_report,
                        }
                    )

            if hdr.enabled:
                captures_by_pattern = {
                    int(record["spec"].pattern_id): (
                        record["frames"],
                        record["black_offsets"],
                    )
                    for record in angle_hdr_captures
                }
                selected_index, sequence_selection = select_structured_light_sequence_bracket(
                    cv2,
                    captures_by_pattern,
                    hdr.brackets,
                    hdr,
                )
                if selected_index is None:
                    print(
                        f"[merge] angle={angle:03d} using legacy per-pattern HDR: "
                        f"{sequence_selection['reason']}",
                        flush=True,
                    )
                else:
                    selected_name = hdr.brackets[selected_index].name
                    print(
                        f"[merge] angle={angle:03d} selected common structured-light bracket="
                        f"{selected_name}",
                        flush=True,
                    )
                    for record in angle_hdr_captures:
                        merged, saturated_mask, dark_mask, _selected_bracket_map, refreshed_report = merge_hdr_frames(
                            cv2,
                            record["frames"],
                            hdr.brackets,
                            hdr,
                            record["black_offsets"],
                            selected_bracket_index=selected_index,
                            sequence_selection=sequence_selection,
                        )
                        final_size = write_image(cv2, record["final_path"], merged)
                        saturated_path = record["saturated_path"]
                        dark_path = record["dark_path"]
                        saturated_size = (
                            write_image(cv2, saturated_path, saturated_mask)
                            if saturated_path is not None
                            else 0
                        )
                        dark_size = (
                            write_image(cv2, dark_path, dark_mask)
                            if dark_path is not None
                            else 0
                        )
                        previous_report = record["merge_report"]
                        output_metadata = {
                            key: previous_report[key]
                            for key in (
                                "filename",
                                "saturated_mask_filename",
                                "dark_mask_filename",
                            )
                        }
                        output_metadata.update(
                            {
                                "size_bytes": final_size,
                                "saturated_mask_size_bytes": saturated_size,
                                "dark_mask_size_bytes": dark_size,
                            }
                        )
                        refreshed_report.update(output_metadata)
                        previous_report.clear()
                        previous_report.update(refreshed_report)
                        refreshed_hdr_report = record["hdr_report"]
                        refreshed_hdr_report.clear()
                        refreshed_hdr_report.update(
                            {
                                "angle_deg": angle,
                                "pattern_id": record["spec"].pattern_id,
                                "label": record["spec"].label,
                                **previous_report,
                            }
                        )

            previous_angle = angle

        for angle in angles:
            angle_dir = scan_dir if len(angles) == 1 else scan_dir / f"angle_{angle:03d}"
            missing = validate_decode_outputs(angle_dir, expected_pattern_ids)
            if missing:
                missing_text = ", ".join(f"{pattern_id:02d} {PATTERN_LABELS[pattern_id]}" for pattern_id in missing)
                raise RuntimeError(f"decode output validation failed for {angle_dir}: missing {missing_text}")
            final_images = {
                pattern_id: read_image(cv2, angle_dir / final_pattern_filename(pattern_id))
                for pattern_id in expected_pattern_ids
            }
            visible_markers = detect_aruco_markers(cv2, final_images[0], args.aruco_dictionary)
            main_scan_marker_visibility[str(angle)] = {
                "pattern": "pattern_000.png",
                "detected_ids": sorted(visible_markers),
                "roi_status": "available" if len(visible_markers) == 4 else "disabled_insufficient_markers",
                "note": "Precomputed stage_precalibration remains valid even when this pattern has no markers.",
            }
            quality = assess_fpp_quality(cv2, final_images, hdr, capture_config.quality_gate)
            quality.update({"mode": "final_scan", "angle_deg": angle, "created_at": datetime.now().isoformat(timespec="seconds")})
            write_quality_histogram(cv2, final_images, angle_dir / "blue_channel_histograms.png")
            (angle_dir / "quality_report.json").write_text(json.dumps(quality, indent=2), encoding="utf-8")
            final_quality_reports[str(angle)] = quality
            print(f"[quality] final angle={angle:03d} passed={quality['passed']}", flush=True)
        print("[scan] decode output validation ok", flush=True)

    except KeyboardInterrupt:
        aborted = True
        print("[scan] Interrupted by user", flush=True)
    except Exception as exc:
        aborted = True
        print(f"[scan] ERROR: {exc}", flush=True)
    finally:
        if display is not None:
            display.black(cv2)
            time.sleep(args.finish_black_ms / 1000.0)
            display.close(cv2)

        if camera is not None:
            try:
                camera.stop()
            finally:
                camera.close()
            for warning in camera.warnings:
                print(f"[camera] warning: {warning}", flush=True)

        log = {
            "scan_id": scan_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "status": "aborted" if aborted else "ok",
            "scan_type": capture_config.rig.scan_type,
            "pattern_dir": str(pattern_dir),
            "pattern_contract": [
                {"pattern_id": pattern_id, "label": label}
                for pattern_id, label in PATTERN_CONTRACT
                if pattern_id in expected_pattern_ids
            ],
            "capture_order": [
                {"pattern_id": spec.pattern_id, "label": spec.label, "source": spec.source_path.name, "inverted": spec.invert_source}
                for spec in patterns
            ],
            "angles_deg": angles,
            "stage_precalibration": stage_precalibration,
            "aruco_prescan_artifacts": aruco_prescan_artifacts,
            "metadata": asdict(capture_config.rig),
            "settings": {
                "settle_ms": effective_pattern_settle_ms(args),
                "bracket_settle_ms": args.bracket_settle_ms,
                "capture_timeout_ms": args.camera_timeout_ms,
                "retries": args.retries,
                "camera": camera_settings.as_dict() if camera_settings else None,
                "synthetic_capture": bool(args.dry_run or args.no_camera),
                "save_format": args.save_format,
                "final_decode_format": FINAL_DECODE_SUFFIX,
                "save_all_images": bool(args.save_all_images),
                "save_diagnostics": save_diagnostics,
                "hdr": asdict(hdr),
                "legacy_14_patterns": args.legacy_14_patterns,
            },
            "quality_gate": quality_gate_report,
            "quality_issue_summary": summarize_quality_issues(quality_gate_report, final_quality_reports),
            "aruco_stability": aruco_stability,
            "final_quality": final_quality_reports,
            "main_scan_marker_visibility": main_scan_marker_visibility,
            "final_patterns": final_pattern_rows,
            "hdr_merge_report": hdr_reports,
            "rows": scan_rows,
        }
        (scan_dir / "scan_log.json").write_text(
            json.dumps(log, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (scan_dir / "hdr_merge_report.json").write_text(
            json.dumps(
                {
                    "scan_id": scan_id,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "scan_type": capture_config.rig.scan_type,
                    "patterns": hdr_reports,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        append_csv(scan_dir / "scan_log.csv", scan_rows)
        print(f"[scan] log saved: {scan_dir / 'scan_log.json'}", flush=True)
        print(f"[scan] hdr report saved: {scan_dir / 'hdr_merge_report.json'}", flush=True)
        print(f"[scan] csv saved: {scan_dir / 'scan_log.csv'}", flush=True)

    return 1 if aborted else 0


def run_project_only(args: argparse.Namespace) -> int:
    cv2 = import_cv2()
    pattern_dir = args.patterns.resolve()
    patterns = load_pattern_specs(pattern_dir, legacy_14_patterns=args.legacy_14_patterns)
    first_image = pattern_image(cv2, patterns[0])
    repeat = max(1, int(args.project_repeat))
    display: PatternDisplay | None = None
    aborted = False

    print(
        f"[project] pattern_dir={pattern_dir} patterns={len(patterns)} repeat={repeat}",
        flush=True,
    )

    try:
        if args.no_display or args.dry_run:
            print("[project] display disabled; validating pattern load only", flush=True)
            return 0

        display = PatternDisplay(args, first_image)
        display.open(cv2)
        display.black(cv2)
        time.sleep(args.pre_black_ms / 1000.0)

        for repeat_index in range(repeat):
            print(f"[project] repeat {repeat_index + 1}/{repeat}", flush=True)
            for spec in patterns:
                projected = pattern_image(cv2, spec)
                display.show(cv2, projected)
                print(
                    f"[project] pattern={spec.pattern_id:03d} {spec.label} "
                    f"source={spec.source_path.name}",
                    flush=True,
                )
                time.sleep(effective_pattern_settle_ms(args) / 1000.0)

        print("[project] complete", flush=True)
    except KeyboardInterrupt:
        aborted = True
        print("[project] Interrupted by user", flush=True)
    except Exception as exc:
        aborted = True
        print(f"[project] ERROR: {exc}", flush=True)
    finally:
        if display is not None:
            display.black(cv2)
            time.sleep(args.finish_black_ms / 1000.0)
            display.close(cv2)

    return 1 if aborted else 0


def run_preview(args: argparse.Namespace) -> int:
    cv2 = import_cv2()
    gui_preview = GuiPreviewPublisher(cv2, args.gui_preview_file, args.gui_preview_max_width)
    camera: CameraInterface | None = None
    try:
        camera, _settings = open_camera(args, profile_overrides=preview_camera_profile(args))
        show_opencv_window = args.gui_preview_file is None
        if show_opencv_window:
            cv2.namedWindow(args.preview_window_name, cv2.WINDOW_NORMAL)
            print("[preview] running. Press ESC or q in the preview window to stop.", flush=True)
        else:
            print("[preview] publishing frames to the control panel.", flush=True)
        while True:
            frame = camera.capture_frame()
            gui_preview.publish(frame.image)
            if show_opencv_window:
                cv2.imshow(args.preview_window_name, preview_image(cv2, frame.image))
                key = cv2.waitKey(1) & 0xFF
                if key in {27, ord("q")}:
                    break
    except CameraError as exc:
        print(f"[camera] ERROR: {exc}", flush=True)
        return 1
    finally:
        if camera is not None:
            camera.stop()
            camera.close()
        if args.gui_preview_file is None:
            cv2.destroyAllWindows()
    return 0


def run_preview_capture(args: argparse.Namespace) -> int:
    """Capture one current camera frame for the GUI without saving it to a scan folder."""
    cv2 = import_cv2()
    gui_preview = GuiPreviewPublisher(cv2, args.gui_preview_file, args.gui_preview_max_width)
    camera: CameraInterface | None = None
    try:
        camera, _settings = open_camera(args, profile_overrides=preview_camera_profile(args))
        frame = camera.capture_frame()
        gui_preview.publish(frame.image)
        if args.gui_preview_file is None:
            cv2.namedWindow(args.preview_window_name, cv2.WINDOW_NORMAL)
            cv2.imshow(args.preview_window_name, preview_image(cv2, frame.image))
            cv2.waitKey(0)
        print("[preview] captured one frame for display only; no image was saved.", flush=True)
        return 0
    except CameraError as exc:
        print(f"[camera] ERROR: {exc}", flush=True)
        return 1
    finally:
        if camera is not None:
            camera.stop()
            camera.close()
        if args.gui_preview_file is None:
            cv2.destroyAllWindows()


def run_single_capture(args: argparse.Namespace) -> int:
    cv2 = import_cv2()
    gui_preview = GuiPreviewPublisher(cv2, args.gui_preview_file, args.gui_preview_max_width)
    camera: CameraInterface | None = None
    try:
        camera, _settings = open_camera(args)
        scan_id = safe_scan_id(args.scan_id or datetime.now().strftime("single_%Y%m%d_%H%M%S"))
        output_dir = args.output.resolve() / scan_id
        frame = camera.capture_frame()
        gui_preview.publish(frame.image)
        filename = capture_filename(
            scan_id=scan_id,
            angle_deg=None,
            pattern_id=None,
            capture_id=0,
            suffix=args.save_format,
        )
        size_bytes = save_camera_frame(cv2, frame, output_dir / filename)
        metadata = {
            "scan_id": scan_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "single_capture",
            "filename": filename,
            "size_bytes": size_bytes,
            "camera_timestamp_ms": frame.timestamp_ms,
            "camera_frame_index": frame.frame_index,
            "pixel_format": frame.pixel_format,
            "camera_metadata": frame.metadata,
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "capture_log.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"[capture] saved {output_dir / filename} ({size_bytes} bytes)", flush=True)
        return 0
    except CameraError as exc:
        print(f"[camera] ERROR: {exc}", flush=True)
        return 1
    finally:
        if camera is not None:
            camera.stop()
            camera.close()


def run_continuous_capture(args: argparse.Namespace) -> int:
    cv2 = import_cv2()
    gui_preview = GuiPreviewPublisher(cv2, args.gui_preview_file, args.gui_preview_max_width)
    camera: CameraInterface | None = None
    count = max(0, int(args.continuous_capture))
    try:
        camera, _settings = open_camera(args)
        scan_id = safe_scan_id(args.scan_id or datetime.now().strftime("continuous_%Y%m%d_%H%M%S"))
        output_dir = args.output.resolve() / scan_id
        output_dir.mkdir(parents=True, exist_ok=True)
        rows: list[dict[str, Any]] = []
        index = 0
        print(
            "[capture] continuous capture started. Stop the process to end."
            if count == 0
            else f"[capture] continuous capture started for {count} frames.",
            flush=True,
        )
        while count == 0 or index < count:
            frame = camera.capture_frame()
            gui_preview.publish(frame.image)
            filename = capture_filename(
                scan_id=scan_id,
                angle_deg=None,
                pattern_id=None,
                capture_id=index,
                suffix=args.save_format,
            )
            size_bytes = save_camera_frame(cv2, frame, output_dir / filename)
            rows.append(
                {
                    "capture_id": index,
                    "filename": filename,
                    "size_bytes": size_bytes,
                    "camera_timestamp_ms": frame.timestamp_ms,
                    "camera_frame_index": frame.frame_index,
                    "pixel_format": frame.pixel_format,
                }
            )
            print(f"[capture] saved {filename} ({size_bytes} bytes)", flush=True)
            index += 1
            if args.capture_interval_ms > 0:
                time.sleep(args.capture_interval_ms / 1000.0)

        (output_dir / "capture_log.json").write_text(
            json.dumps(
                {
                    "scan_id": scan_id,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "mode": "continuous_capture",
                    "rows": rows,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return 0
    except KeyboardInterrupt:
        print("[capture] interrupted", flush=True)
        return 130
    except CameraError as exc:
        print(f"[camera] ERROR: {exc}", flush=True)
        return 1
    finally:
        if camera is not None:
            camera.stop()
            camera.close()


def run_check_camera(args: argparse.Namespace) -> int:
    camera: CameraInterface | None = None
    try:
        camera, settings = open_camera(args)
        print(f"[camera] check ok: {settings.as_dict()}", flush=True)
        return 0
    except CameraError as exc:
        print(f"[camera] ERROR: {exc}", flush=True)
        return 1
    finally:
        if camera is not None:
            camera.stop()
            camera.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Display structured-light patterns and capture XIMEA UV camera frames."
    )
    parser.add_argument("--patterns", default="generated_patterns_centered", type=Path)
    parser.add_argument("--output", default="captures", type=Path)
    parser.add_argument("--monitor", default=1, type=int)
    parser.add_argument("--window-name", default="StructuredLight Projection")
    parser.add_argument("--preview-window-name", default="XIMEA UV Preview")
    parser.add_argument("--windowed", action="store_true")
    parser.add_argument("--window-x", type=int)
    parser.add_argument("--window-y", type=int)
    parser.add_argument("--stretch", action="store_true", help="Stretch pattern to screen.")
    parser.add_argument(
        "--settle-ms",
        default=1000,
        type=int,
        help="Requested milliseconds to wait after each projector pattern update (minimum applied: 1000 ms).",
    )
    parser.add_argument(
        "--settle-flush-frames",
        default=2,
        type=int,
        help="Disposable camera frames acquired after settling for preflight display-timing diagnostics.",
    )
    parser.add_argument("--pre-black-ms", default=300, type=int)
    parser.add_argument("--finish-black-ms", default=300, type=int)
    parser.add_argument("--bracket-settle-ms", default=150, type=int)
    parser.add_argument("--retries", default=2, type=int)
    parser.add_argument("--retry-delay-ms", default=300, type=int)
    parser.add_argument("--angles", default="0")
    parser.add_argument("--pause-before-first-angle", action="store_true")
    parser.add_argument("--no-angle-prompt", action="store_true")
    parser.add_argument("--angle-advance-file", type=Path)
    parser.add_argument("--rotation-command")
    parser.add_argument("--rotate-first-angle", action="store_true")
    parser.add_argument("--scan-id")
    parser.add_argument(
        "--stage-precalibration",
        type=Path,
        help="Optional decoder-compatible ArUco precalibration JSON to copy into each scan folder.",
    )
    parser.add_argument("--scan-type", choices=("reference", "object"))
    parser.add_argument("--projector-tilt-deg", type=float)
    parser.add_argument("--focus-confirmed", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--scheimpflug-confirmed", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--rig-id")
    parser.add_argument("--calibration-id")
    parser.add_argument("--projector-brightness")
    parser.add_argument("--keystone-predistortion", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--no-display", action="store_true")
    parser.add_argument("--no-camera", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Generate synthetic captures without camera or projector display.")
    parser.add_argument("--legacy-14-patterns", action="store_true", help="Capture only ids 0..13 for older decoders.")
    parser.add_argument(
        "--save-all-images",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Save raw exposure brackets and HDR masks in addition to final decoder images.",
    )

    parser.add_argument("--camera-config", default=Path("camera_config.json"), type=Path)
    parser.add_argument("--camera-provider", choices=("ximea", "mock"))
    parser.add_argument("--camera-device-index", type=int)
    parser.add_argument("--exposure-us", type=int)
    parser.add_argument("--gain-db", type=float)
    parser.add_argument("--short-exposure-us", type=int)
    parser.add_argument("--short-gain-db", type=float)
    parser.add_argument("--mid-exposure-us", type=int)
    parser.add_argument("--mid-gain-db", type=float)
    parser.add_argument("--long-exposure-us", type=int)
    parser.add_argument("--long-gain-db", type=float)
    parser.add_argument("--fps", type=float)
    parser.add_argument(
        "--trigger-mode",
        choices=("off", "freerun", "free_run", "software", "edge_rising", "rising", "edge_falling", "falling"),
    )
    parser.add_argument("--image-format", choices=("mono8", "mono16", "rgb24"))
    parser.add_argument("--camera-timeout-ms", type=int)
    parser.add_argument("--quality-gate", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--save-format", default=".png", type=normalize_suffix)

    parser.add_argument("--preview", action="store_true")
    parser.add_argument(
        "--preview-once",
        action="store_true",
        help="Capture one camera frame for preview only; do not save it.",
    )
    parser.add_argument(
        "--gui-preview-file",
        type=Path,
        help="Write the latest camera frame as a BMP for the native control panel.",
    )
    parser.add_argument(
        "--gui-preview-max-width",
        default=360,
        type=int,
        help="Maximum width of the control-panel preview bitmap.",
    )
    parser.add_argument("--project-only", action="store_true")
    parser.add_argument("--project-repeat", default=1, type=int)
    parser.add_argument("--single-capture", action="store_true")
    parser.add_argument("--continuous-capture", nargs="?", const=0, type=int)
    parser.add_argument("--check-camera", action="store_true")
    parser.add_argument("--aruco-prescan-capture", action="store_true")
    parser.add_argument("--aruco-prescan-role", choices=("zero", "rotated"))
    parser.add_argument(
        "--aruco-exposure-us",
        type=int,
        help="Exposure used only for no-pattern ArUco prescan captures.",
    )
    parser.add_argument("--aruco-precalibration", action="store_true")
    parser.add_argument("--aruco-dictionary", choices=sorted(ARUCO_DICTIONARIES), default="DICT_4X4_50")
    parser.add_argument("--aruco-ids", default="0,1,2,3")
    parser.add_argument("--aruco-ransac-threshold-px", default=3.0, type=float)
    parser.add_argument(
        "--aruco-stage-command-value",
        default=250.0,
        type=float,
        help="Stage program value for the nominal-180 view; it is not degrees.",
    )
    parser.add_argument("--aruco-intended-rotation-deg", default=180.0, type=float)
    parser.add_argument("--capture-interval-ms", default=0, type=int)
    return parser.parse_args()


def main() -> int:
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10 or newer is required.")
    args = parse_args()
    if args.check_camera:
        return run_check_camera(args)
    if args.aruco_prescan_capture:
        if args.aruco_prescan_role is None:
            raise SystemExit("--aruco-prescan-capture requires --aruco-prescan-role zero or rotated")
        return run_aruco_prescan_capture(args)
    if args.aruco_precalibration:
        return run_aruco_precalibration(args)
    if args.project_only:
        return run_project_only(args)
    if args.preview:
        return run_preview(args)
    if args.preview_once:
        return run_preview_capture(args)
    if args.single_capture:
        return run_single_capture(args)
    if args.continuous_capture is not None:
        return run_continuous_capture(args)
    return run_scan(args)


if __name__ == "__main__":
    raise SystemExit(main())
