#!/usr/bin/env python3
"""PC master controller for XIMEA UV structured-light capture."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
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


def now_ms() -> int:
    return time.time_ns() // 1_000_000


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
    mid_exposure = int(args.exposure_us or 10000)
    gain_db = float(args.gain_db if args.gain_db is not None else 0.0)
    return (
        ExposureBracket("short", max(1, mid_exposure // 4), gain_db),
        ExposureBracket("mid", max(1, mid_exposure), gain_db),
        ExposureBracket("long", max(1, mid_exposure * 4), gain_db),
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
    return {"short": 2500, "mid": 10000, "long": 40000}


def load_capture_config(args: argparse.Namespace) -> CaptureConfig:
    config = read_json_file(args.camera_config)
    capture_section = config.get("capture", {})
    hdr_section = capture_section.get("hdr", {})
    metadata_section = capture_section.get("metadata", {})

    bracket_items = hdr_section.get("brackets", [])
    brackets: list[ExposureBracket] = []
    for index, item in enumerate(bracket_items):
        if not isinstance(item, dict):
            continue
        name = safe_filename_token(str(item.get("name") or f"bracket_{index:02d}"))
        exposure_us = int(item.get("exposure_us", args.exposure_us or 10000))
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


def open_camera(args: argparse.Namespace) -> tuple[CameraInterface, CameraSettings]:
    settings = CameraProvider.load_settings(args.camera_config, camera_overrides(args))
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
        print(
            "[display] window="
            f"{self.window_name!r} x={self.bounds.x} y={self.bounds.y} "
            f"w={self.bounds.width} h={self.bounds.height}",
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


def merge_hdr_frames(
    cv2,
    frames: list[Any],
    brackets: tuple[ExposureBracket, ...],
    hdr: HdrConfig,
    black_offsets: list[float] | None = None,
) -> tuple[Any, Any, Any, dict[str, Any]]:
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
    dark_threshold = min(sensor_max, scale_threshold(hdr.dark_threshold, sensor_max))
    if black_offsets is None:
        black_offsets = [hdr.black_offset] * len(frames)
    if len(black_offsets) != len(frames):
        raise RuntimeError("HDR black offset count does not match bracket frame count")
    offset_values = np.array(black_offsets, dtype=np.float32)[:, None, None]
    corrected_stack = np.maximum(stack.astype(np.float32) - offset_values, 0.0)

    scales = np.array([bracket.exposure_gain_scale for bracket in brackets], dtype=np.float32)
    priority = np.argsort(scales)
    chosen = np.full(first_shape, int(priority[0]), dtype=np.int32)
    any_valid = np.zeros(first_shape, dtype=bool)
    for index in priority:
        valid = (corrected_stack[index] > dark_threshold) & (stack[index] < saturated_threshold)
        chosen[valid] = int(index)
        any_valid |= valid

    selected = np.take_along_axis(corrected_stack, chosen[None, :, :], axis=0)[0]
    selected_scales = scales[chosen]
    max_scale = float(scales.max())
    normalized = selected / np.maximum(selected_scales, 1.0) * max_scale
    normalized[~any_valid] = 0.0
    normalized = np.clip(normalized, 0, sensor_max)

    output_max = 65535 if hdr.output_bit_depth == 16 else 255
    output_dtype = np.uint16 if hdr.output_bit_depth == 16 else np.uint8
    merged = np.clip(normalized * (output_max / max(1, sensor_max)), 0, output_max).astype(output_dtype)

    saturated_mask = np.all(stack >= saturated_threshold, axis=0).astype(np.uint8) * 255
    dark_mask = np.all(corrected_stack <= dark_threshold, axis=0).astype(np.uint8) * 255

    report = {
        "algorithm": "longest_unsaturated_radiance_normalized",
        "output_bit_depth": hdr.output_bit_depth,
        "saturated_threshold": int(saturated_threshold),
        "dark_threshold": int(dark_threshold),
        "black_offsets": [float(value) for value in black_offsets],
        "saturated_pixel_count": int(np.count_nonzero(saturated_mask)),
        "dark_pixel_count": int(np.count_nonzero(dark_mask)),
        "invalid_pixel_count": int(np.size(any_valid) - np.count_nonzero(any_valid)),
        "input_dtype": str(gray_frames[0].dtype),
        "input_shape": [int(first_shape[0]), int(first_shape[1])],
        "bracket_priority": [brackets[int(index)].name for index in priority],
    }
    return merged, saturated_mask, dark_mask, report


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


def run_scan(args: argparse.Namespace) -> int:
    cv2 = import_cv2()
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
            brackets=(ExposureBracket("single", int(args.exposure_us or 10000), float(args.gain_db or 0.0)),),
        )

    output_root = args.output.resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    scan_id = safe_scan_id(args.scan_id or datetime.now().strftime("scan_%Y%m%d_%H%M%S"))
    scan_dir = output_root / scan_id
    scan_dir.mkdir(parents=True, exist_ok=True)

    angles = parse_csv_ints(args.angles, "angles")
    expected_pattern_ids = LEGACY_PATTERN_IDS if args.legacy_14_patterns else FULL_PATTERN_IDS
    scan_rows: list[dict[str, Any]] = []
    final_pattern_rows: list[dict[str, Any]] = []
    hdr_reports: list[dict[str, Any]] = []
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

            for spec in patterns:
                projected = pattern_image(cv2, spec)
                if display is not None:
                    display.show(cv2, projected)
                display_ts = now_ms()
                time.sleep(args.settle_ms / 1000.0)

                bracket_frames: list[Any] = []
                bracket_black_offsets: list[float] = []
                bracket_entries: list[dict[str, Any]] = []
                last_error = ""

                for bracket in hdr.brackets:
                    success = False
                    bracket_token = safe_filename_token(bracket.name)
                    exposure_path = None
                    if args.save_all_images:
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

                merged, saturated_mask, dark_mask, merge_report = merge_hdr_frames(
                    cv2,
                    bracket_frames,
                    hdr.brackets,
                    hdr,
                    bracket_black_offsets,
                )
                final_path = angle_dir / final_pattern_filename(spec.pattern_id)
                final_size = write_image(cv2, final_path, merged)

                final_filename = relative_to_scan(final_path, scan_dir)
                saturated_filename = ""
                dark_filename = ""
                saturated_size = 0
                dark_size = 0
                if args.save_all_images:
                    saturated_path = angle_dir / "hdr_masks" / mask_filename(spec.pattern_id, "saturated")
                    dark_path = angle_dir / "hdr_masks" / mask_filename(spec.pattern_id, "dark")
                    saturated_size = write_image(cv2, saturated_path, saturated_mask)
                    dark_size = write_image(cv2, dark_path, dark_mask)
                    saturated_filename = relative_to_scan(saturated_path, scan_dir)
                    dark_filename = relative_to_scan(dark_path, scan_dir)
                merge_report.update(
                    {
                        "filename": final_filename,
                        "size_bytes": final_size,
                        "saturated_mask_filename": saturated_filename,
                        "saturated_mask_size_bytes": saturated_size,
                        "dark_mask_filename": dark_filename,
                        "dark_mask_size_bytes": dark_size,
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
                hdr_reports.append(
                    {
                        "angle_deg": angle,
                        "pattern_id": spec.pattern_id,
                        "label": spec.label,
                        **merge_report,
                    }
                )
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

            previous_angle = angle

        for angle in angles:
            angle_dir = scan_dir if len(angles) == 1 else scan_dir / f"angle_{angle:03d}"
            missing = validate_decode_outputs(angle_dir, expected_pattern_ids)
            if missing:
                missing_text = ", ".join(f"{pattern_id:02d} {PATTERN_LABELS[pattern_id]}" for pattern_id in missing)
                raise RuntimeError(f"decode output validation failed for {angle_dir}: missing {missing_text}")
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
            "metadata": asdict(capture_config.rig),
            "settings": {
                "settle_ms": args.settle_ms,
                "bracket_settle_ms": args.bracket_settle_ms,
                "capture_timeout_ms": args.camera_timeout_ms,
                "retries": args.retries,
                "camera": camera_settings.as_dict() if camera_settings else None,
                "synthetic_capture": bool(args.dry_run or args.no_camera),
                "save_format": args.save_format,
                "final_decode_format": FINAL_DECODE_SUFFIX,
                "save_all_images": bool(args.save_all_images),
                "hdr": asdict(hdr),
                "legacy_14_patterns": args.legacy_14_patterns,
            },
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
                time.sleep(args.settle_ms / 1000.0)

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
    camera: CameraInterface | None = None
    try:
        camera, _settings = open_camera(args)
        cv2.namedWindow(args.preview_window_name, cv2.WINDOW_NORMAL)
        print("[preview] running. Press ESC or q in the preview window to stop.", flush=True)
        while True:
            frame = camera.capture_frame()
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
        cv2.destroyAllWindows()
    return 0


def run_single_capture(args: argparse.Namespace) -> int:
    cv2 = import_cv2()
    camera: CameraInterface | None = None
    try:
        camera, _settings = open_camera(args)
        scan_id = safe_scan_id(args.scan_id or datetime.now().strftime("single_%Y%m%d_%H%M%S"))
        output_dir = args.output.resolve() / scan_id
        frame = camera.capture_frame()
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
    parser.add_argument("--patterns", default="generated_patterns", type=Path)
    parser.add_argument("--output", default="captures", type=Path)
    parser.add_argument("--monitor", default=1, type=int)
    parser.add_argument("--window-name", default="StructuredLight Projection")
    parser.add_argument("--preview-window-name", default="XIMEA UV Preview")
    parser.add_argument("--windowed", action="store_true")
    parser.add_argument("--window-x", type=int)
    parser.add_argument("--window-y", type=int)
    parser.add_argument("--stretch", action="store_true", help="Stretch pattern to screen.")
    parser.add_argument("--settle-ms", default=300, type=int)
    parser.add_argument("--pre-black-ms", default=300, type=int)
    parser.add_argument("--finish-black-ms", default=300, type=int)
    parser.add_argument("--bracket-settle-ms", default=50, type=int)
    parser.add_argument("--retries", default=2, type=int)
    parser.add_argument("--retry-delay-ms", default=300, type=int)
    parser.add_argument("--angles", default="0")
    parser.add_argument("--pause-before-first-angle", action="store_true")
    parser.add_argument("--no-angle-prompt", action="store_true")
    parser.add_argument("--angle-advance-file", type=Path)
    parser.add_argument("--rotation-command")
    parser.add_argument("--rotate-first-angle", action="store_true")
    parser.add_argument("--scan-id")
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
        action="store_true",
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
    parser.add_argument("--save-format", default=".png", type=normalize_suffix)

    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--project-only", action="store_true")
    parser.add_argument("--project-repeat", default=1, type=int)
    parser.add_argument("--single-capture", action="store_true")
    parser.add_argument("--continuous-capture", nargs="?", const=0, type=int)
    parser.add_argument("--check-camera", action="store_true")
    parser.add_argument("--capture-interval-ms", default=0, type=int)
    return parser.parse_args()


def main() -> int:
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10 or newer is required.")
    args = parse_args()
    if args.check_camera:
        return run_check_camera(args)
    if args.project_only:
        return run_project_only(args)
    if args.preview:
        return run_preview(args)
    if args.single_capture:
        return run_single_capture(args)
    if args.continuous_capture is not None:
        return run_continuous_capture(args)
    return run_scan(args)


if __name__ == "__main__":
    raise SystemExit(main())
