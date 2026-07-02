#!/usr/bin/env python3
"""PC master controller for XIMEA UV structured-light capture."""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from camera_provider import CameraError, CameraFrame, CameraInterface, CameraProvider, CameraSettings


IMAGE_SUFFIXES = {".bmp", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}


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


def pattern_sort_key(path: Path) -> tuple[int, str]:
    match = re.match(r"^(\d+)", path.name)
    index = int(match.group(1)) if match else 1_000_000
    return index, path.name.lower()


def load_patterns(pattern_dir: Path) -> list[Path]:
    if not pattern_dir.exists():
        raise SystemExit(f"Pattern directory does not exist: {pattern_dir}")
    patterns = sorted(
        [
            path
            for path in pattern_dir.iterdir()
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        ],
        key=pattern_sort_key,
    )
    if not patterns:
        raise SystemExit(f"No pattern images found in {pattern_dir}")
    return patterns


def read_image(cv2, path: Path):
    import numpy as np  # type: ignore

    data = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise SystemExit(f"Could not decode image: {path}")
    return image


def normalize_suffix(value: str) -> str:
    suffix = value.lower().strip()
    if not suffix:
        suffix = "png"
    if not suffix.startswith("."):
        suffix = "." + suffix
    if suffix not in {".png", ".tif", ".tiff", ".bmp", ".jpg", ".jpeg"}:
        raise argparse.ArgumentTypeError("save format must be png, tif, tiff, bmp, jpg, or jpeg")
    return suffix


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
        "angle_deg",
        "pattern_id",
        "capture_id",
        "attempt",
        "pattern_filename",
        "pattern_display_timestamp_pc_ms",
        "capture_command_timestamp_pc_ms",
        "camera_timestamp_ms",
        "camera_frame_index",
        "received_image_filename",
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


def run_scan(args: argparse.Namespace) -> int:
    cv2 = import_cv2()
    pattern_dir = args.patterns.resolve()
    patterns = load_patterns(pattern_dir)
    first_image = read_image(cv2, patterns[0])
    output_root = args.output.resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    scan_id = safe_scan_id(args.scan_id or datetime.now().strftime("scan_%Y%m%d_%H%M%S"))
    scan_dir = output_root / scan_id
    scan_dir.mkdir(parents=True, exist_ok=True)

    angles = parse_csv_ints(args.angles, "angles")
    scan_rows: list[dict[str, Any]] = []
    display: PatternDisplay | None = None
    camera: CameraInterface | None = None
    camera_settings: CameraSettings | None = None
    capture_id = 0
    aborted = False

    print(f"[scan] scan_id={scan_id} patterns={len(patterns)} angles={angles}", flush=True)

    try:
        if not args.no_camera:
            camera, camera_settings = open_camera(args)
        else:
            print("[camera] disabled by --no-camera", flush=True)

        if not args.no_display:
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

            for pattern_id, pattern_path in enumerate(patterns):
                image = read_image(cv2, pattern_path)
                success = False
                last_error = ""

                for attempt in range(1, args.retries + 2):
                    if display is not None:
                        display.show(cv2, image)
                    display_ts = now_ms()
                    time.sleep(args.settle_ms / 1000.0)

                    command_ts = now_ms()
                    row: dict[str, Any] = {
                        "scan_id": scan_id,
                        "angle_deg": angle,
                        "pattern_id": pattern_id,
                        "capture_id": capture_id,
                        "attempt": attempt,
                        "pattern_filename": pattern_path.name,
                        "pattern_display_timestamp_pc_ms": display_ts,
                        "capture_command_timestamp_pc_ms": command_ts,
                    }

                    try:
                        frame: CameraFrame | None = None
                        filename = ""
                        size_bytes = 0
                        if camera is not None:
                            frame = camera.capture_frame()
                            filename = capture_filename(
                                scan_id=scan_id,
                                angle_deg=angle,
                                pattern_id=pattern_id,
                                capture_id=capture_id,
                                suffix=args.save_format,
                            )
                            size_bytes = save_camera_frame(cv2, frame, scan_dir / filename)

                        row.update(
                            {
                                "camera_timestamp_ms": "" if frame is None else frame.timestamp_ms,
                                "camera_frame_index": "" if frame is None else frame.frame_index,
                                "received_image_filename": filename,
                                "size_bytes": size_bytes,
                                "status": "ok",
                                "error": "",
                            }
                        )
                        scan_rows.append(row)
                        success = True
                        print(
                            f"[capture] angle={angle:03d} pattern={pattern_id:03d} "
                            f"capture={capture_id:03d} saved={bool(camera)}",
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
                            f"[capture] failed angle={angle:03d} pattern={pattern_id:03d} "
                            f"capture={capture_id:03d}: {last_error}",
                            flush=True,
                        )
                        capture_id += 1
                        if attempt <= args.retries:
                            time.sleep(args.retry_delay_ms / 1000.0)

                if not success:
                    aborted = True
                    raise RuntimeError(
                        f"scan aborted at angle={angle} pattern={pattern_id}: {last_error}"
                    )

            previous_angle = angle

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
            "pattern_dir": str(pattern_dir),
            "patterns": [path.name for path in patterns],
            "angles_deg": angles,
            "settings": {
                "settle_ms": args.settle_ms,
                "capture_timeout_ms": args.camera_timeout_ms,
                "retries": args.retries,
                "camera": camera_settings.as_dict() if camera_settings else None,
                "save_format": args.save_format,
            },
            "rows": scan_rows,
        }
        (scan_dir / "scan_log.json").write_text(
            json.dumps(log, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        append_csv(scan_dir / "scan_log.csv", scan_rows)
        print(f"[scan] log saved: {scan_dir / 'scan_log.json'}", flush=True)
        print(f"[scan] csv saved: {scan_dir / 'scan_log.csv'}", flush=True)

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
    parser.add_argument("--retries", default=2, type=int)
    parser.add_argument("--retry-delay-ms", default=300, type=int)
    parser.add_argument("--angles", default="0")
    parser.add_argument("--pause-before-first-angle", action="store_true")
    parser.add_argument("--no-angle-prompt", action="store_true")
    parser.add_argument("--angle-advance-file", type=Path)
    parser.add_argument("--rotation-command")
    parser.add_argument("--rotate-first-angle", action="store_true")
    parser.add_argument("--scan-id")
    parser.add_argument("--no-display", action="store_true")
    parser.add_argument("--no-camera", action="store_true")

    parser.add_argument("--camera-config", default=Path("camera_config.json"), type=Path)
    parser.add_argument("--camera-provider", choices=("ximea", "mock"))
    parser.add_argument("--camera-device-index", type=int)
    parser.add_argument("--exposure-us", type=int)
    parser.add_argument("--gain-db", type=float)
    parser.add_argument("--fps", type=float)
    parser.add_argument(
        "--trigger-mode",
        choices=("off", "freerun", "free_run", "software", "edge_rising", "rising", "edge_falling", "falling"),
    )
    parser.add_argument("--image-format", choices=("mono8", "mono16", "rgb24"))
    parser.add_argument("--camera-timeout-ms", type=int)
    parser.add_argument("--save-format", default=".png", type=normalize_suffix)

    parser.add_argument("--preview", action="store_true")
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
    if args.preview:
        return run_preview(args)
    if args.single_capture:
        return run_single_capture(args)
    if args.continuous_capture is not None:
        return run_continuous_capture(args)
    return run_scan(args)


if __name__ == "__main__":
    raise SystemExit(main())
