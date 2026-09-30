"""Standalone GUI for cold-start camera/projector thermal-drift capture."""

from __future__ import annotations

import ctypes
import json
import os
import queue
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import (
    BOTH,
    END,
    LEFT,
    RIGHT,
    X,
    Button,
    Entry,
    Frame,
    Label,
    StringVar,
    Text,
    Tk,
    filedialog,
    messagebox,
)
from typing import Any, Callable

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from camera_provider import CameraFrame, CameraInterface, CameraProvider  # noqa: E402
from thermal_drift_core import (  # noqa: E402
    PATTERN_ROLES,
    build_capture_schedule,
    circular_phase_change,
    image_statistics,
    phase_summary,
)


def local_now() -> datetime:
    return datetime.now().astimezone()


def iso_now() -> str:
    return local_now().isoformat(timespec="milliseconds")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def write_image(path: Path, image: np.ndarray) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    extension = path.suffix or ".png"
    ok, encoded = cv2.imencode(extension, np.asarray(image))
    if not ok:
        raise RuntimeError(f"Could not encode capture image: {path}")
    path.write_bytes(encoded.tobytes())
    return path.stat().st_size


@dataclass(frozen=True)
class Monitor:
    x: int
    y: int
    width: int
    height: int
    name: str
    primary: bool


def windows_monitors() -> list[Monitor]:
    if sys.platform != "win32":
        return []
    from ctypes import wintypes

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

    result: list[Monitor] = []
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )

    @callback_type
    def collect(handle, _dc, _rect, _data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            rect = info.rcMonitor
            result.append(
                Monitor(
                    rect.left,
                    rect.top,
                    rect.right - rect.left,
                    rect.bottom - rect.top,
                    info.szDevice,
                    bool(info.dwFlags & 1),
                )
            )
        return True

    if not ctypes.windll.user32.EnumDisplayMonitors(None, None, collect, 0):
        raise OSError("EnumDisplayMonitors failed")
    return result


class ProjectorWindow:
    def __init__(self, monitor_index: int, first_pattern: np.ndarray) -> None:
        monitors = windows_monitors()
        if not monitors:
            raise RuntimeError("Windows monitor geometry could not be read")
        if not 0 <= monitor_index < len(monitors):
            raise ValueError(
                f"Projector monitor index {monitor_index} is invalid; detected {len(monitors)} monitors"
            )
        self.monitor = monitors[monitor_index]
        self.image_shape = first_pattern.shape[:2]
        self.name = "Standalone Thermal Drift Projection"

    def render(self, image: np.ndarray) -> np.ndarray:
        image_h, image_w = image.shape[:2]
        scale = min(self.monitor.width / image_w, self.monitor.height / image_h)
        out_w = max(1, int(round(image_w * scale)))
        out_h = max(1, int(round(image_h * scale)))
        resized = cv2.resize(image, (out_w, out_h), interpolation=cv2.INTER_NEAREST)
        canvas = np.zeros((self.monitor.height, self.monitor.width), dtype=resized.dtype)
        x = (self.monitor.width - out_w) // 2
        y = (self.monitor.height - out_h) // 2
        canvas[y : y + out_h, x : x + out_w] = resized
        return canvas

    def open(self) -> None:
        cv2.namedWindow(self.name, cv2.WINDOW_NORMAL)
        cv2.moveWindow(self.name, self.monitor.x, self.monitor.y)
        cv2.resizeWindow(self.name, self.monitor.width, self.monitor.height)
        cv2.setWindowProperty(self.name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        cv2.waitKey(1)
        hwnd = ctypes.windll.user32.FindWindowW(None, self.name)
        if hwnd:
            ctypes.windll.user32.SetWindowPos(
                hwnd,
                -1,
                self.monitor.x,
                self.monitor.y,
                self.monitor.width,
                self.monitor.height,
                0x0040,
            )

    def show(self, image: np.ndarray) -> None:
        cv2.imshow(self.name, self.render(image))
        cv2.waitKey(1)

    def black(self) -> None:
        self.show(np.zeros(self.image_shape, dtype=np.uint8))

    def close(self) -> None:
        try:
            cv2.destroyWindow(self.name)
        except cv2.error:
            pass


def load_patterns(config: dict[str, Any]) -> tuple[dict[str, np.ndarray], dict[str, Path]]:
    pattern_config = config["patterns"]
    directory = Path(str(pattern_config["directory"]))
    if not directory.is_absolute():
        directory = (ROOT / directory).resolve()
    filenames = pattern_config["files"]
    images: dict[str, np.ndarray] = {}
    paths: dict[str, Path] = {}
    shapes: set[tuple[int, ...]] = set()
    for role in PATTERN_ROLES:
        path = directory / str(filenames[role])
        image = None
        if path.exists():
            # cv2.imread can fail on Korean Windows paths.  Reading the bytes
            # through pathlib and decoding them keeps the standalone app usable
            # from the actual workspace location.
            encoded = np.frombuffer(path.read_bytes(), dtype=np.uint8)
            image = cv2.imdecode(encoded, cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise FileNotFoundError(f"Required thermal-drift pattern is missing: {path}")
        images[role] = image
        paths[role] = path
        shapes.add(image.shape)
    if len(shapes) != 1:
        raise ValueError("Thermal-drift patterns do not share one image size")
    return images, paths


def detect_aruco(image: np.ndarray, config: dict[str, Any]) -> dict[str, Any]:
    aruco_config = config.get("aruco", {})
    if not bool(aruco_config.get("enabled", True)):
        return {"status": "disabled", "markers": {}}
    dictionary_name = str(aruco_config.get("dictionary", "DICT_4X4_50"))
    if not hasattr(cv2, "aruco") or not hasattr(cv2.aruco, dictionary_name):
        return {"status": "unsupported_dictionary", "dictionary": dictionary_name, "markers": {}}
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, dictionary_name))
    detector = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())
    corners, ids, _rejected = detector.detectMarkers(np.asarray(image))
    allowed = {int(value) for value in aruco_config.get("marker_ids", [])}
    markers: dict[str, Any] = {}
    if ids is not None:
        for marker_corners, marker_id in zip(corners, ids.flatten()):
            marker_id = int(marker_id)
            if allowed and marker_id not in allowed:
                continue
            points = np.asarray(marker_corners, dtype=np.float64).reshape(4, 2)
            markers[str(marker_id)] = {
                "corners_px": points.tolist(),
                "center_px": points.mean(axis=0).tolist(),
            }
    return {
        "status": "detected" if markers else "not_detected",
        "dictionary": dictionary_name,
        "markers": markers,
    }


class ThermalDriftCapture:
    def __init__(self, config_path: Path, logger: Callable[[str], None]) -> None:
        self.config_path = config_path
        self.config = read_json(config_path)
        self.log = logger

    def _new_session(self, output_root: Path) -> Path:
        output_root.mkdir(parents=True, exist_ok=True)
        stamp = local_now().strftime("thermal_drift_%Y%m%d_%H%M%S")
        session = output_root / stamp
        suffix = 2
        while session.exists():
            session = output_root / f"{stamp}_{suffix}"
            suffix += 1
        session.mkdir()
        return session

    def _open_camera(self) -> tuple[CameraInterface, dict[str, Any]]:
        settings = CameraProvider.load_settings(self.config_path)
        camera = CameraProvider.create(settings)
        camera.open()
        camera.start()
        return camera, settings.as_dict()

    def _settle_and_capture(
        self,
        *,
        display: ProjectorWindow,
        camera: CameraInterface,
        pattern: np.ndarray,
        stop_event: threading.Event,
    ) -> tuple[CameraFrame, dict[str, Any]]:
        display.show(pattern)
        displayed_at = iso_now()
        displayed_monotonic_ns = time.monotonic_ns()
        settle_seconds = max(0.1, float(self.config["capture"]["settle_ms"]) / 1000.0)
        if stop_event.wait(settle_seconds):
            raise InterruptedError("measurement stopped")
        discarded: list[int] = []
        for _ in range(max(0, int(self.config["capture"].get("flush_frames_after_pattern", 0)))):
            discarded.append(camera.capture_frame().frame_index)
        command_at = iso_now()
        command_monotonic_ns = time.monotonic_ns()
        frame = camera.capture_frame()
        received_at = iso_now()
        return frame, {
            "pattern_displayed_at": displayed_at,
            "pattern_displayed_monotonic_ns": displayed_monotonic_ns,
            "capture_command_at": command_at,
            "capture_command_monotonic_ns": command_monotonic_ns,
            "frame_received_at": received_at,
            "discarded_frame_indices": discarded,
        }

    def run(
        self,
        *,
        output_root: Path,
        total_duration_minutes: float,
        initial_elapsed_seconds: float,
        room_temperature_c: float | None,
        note: str,
        operator_start_requested_at: datetime,
        operator_start_requested_monotonic_ns: int,
        stop_event: threading.Event,
        session_callback: Callable[[Path], None],
    ) -> Path:
        if initial_elapsed_seconds < 0:
            raise ValueError("전원 ON 후 경과 초는 0 이상이어야 합니다")
        images, source_paths = load_patterns(self.config)
        schedule_config = self.config["schedule"]
        schedule = build_capture_schedule(
            total_duration_minutes=total_duration_minutes,
            initial_duration_minutes=float(schedule_config["initial_duration_minutes"]),
            initial_interval_seconds=float(schedule_config["initial_interval_seconds"]),
            regular_interval_seconds=float(schedule_config["regular_interval_seconds"]),
        )
        session = self._new_session(output_root.resolve())
        session_callback(session)
        shutil.copy2(self.config_path, session / "thermal_drift_config.json")
        copied_patterns: dict[str, str] = {}
        for role, source in source_paths.items():
            destination = session / "patterns" / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied_patterns[role] = str(destination.relative_to(session))

        camera: CameraInterface | None = None
        display: ProjectorWindow | None = None
        manifest_path = session / "session_manifest.json"
        started_at = local_now()
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "session_id": session.name,
            "mode": "standalone_thermal_drift",
            "status": "initializing",
            "application_started_at": started_at.isoformat(timespec="milliseconds"),
            "operator_start_requested_at": operator_start_requested_at.isoformat(timespec="milliseconds"),
            "timezone": str(started_at.tzinfo),
            "initial_elapsed_since_projector_power_on_seconds": float(initial_elapsed_seconds),
            "estimated_projector_power_on_at": None,
            "projector_pattern_started_at": None,
            "camera_opened_at": None,
            "camera_temperature_c": None,
            "camera_temperature_note": "not available from the current camera provider",
            "projector_temperature_c": None,
            "projector_temperature_note": "not measured; elapsed time is the proxy",
            "room_temperature_c": room_temperature_c,
            "note": note,
            "schedule": {
                "total_duration_minutes": float(total_duration_minutes),
                "initial_duration_minutes": float(schedule_config["initial_duration_minutes"]),
                "initial_interval_seconds": float(schedule_config["initial_interval_seconds"]),
                "regular_interval_seconds": float(schedule_config["regular_interval_seconds"]),
                "target_elapsed_seconds": schedule,
            },
            "capture_contract": {
                "patterns_per_timepoint": list(PATTERN_ROLES),
                "frames_per_timepoint": len(PATTERN_ROLES),
                "gray_code_frames": 0,
                "stage_rotation": False,
                "standby_pattern": "sine_000",
            },
            "patterns": copied_patterns,
            "config_snapshot": "thermal_drift_config.json",
            "camera_settings": None,
            "timepoints": [],
        }
        atomic_write_json(manifest_path, manifest)
        reference_phase: np.ndarray | None = None
        reference_valid: np.ndarray | None = None

        try:
            self.log("카메라를 연결하는 중입니다.")
            camera, camera_settings = self._open_camera()
            manifest["camera_opened_at"] = iso_now()
            manifest["camera_settings"] = camera_settings
            self.log(f"카메라 연결 완료: {camera.describe()}")
            for warning in camera.warnings:
                self.log(f"카메라 경고: {warning}")

            projector = self.config["projector"]
            display = ProjectorWindow(int(projector["monitor_index"]), images["sine_000"])
            display.open()
            display.show(images["sine_000"])
            projection_start_monotonic_ns = time.monotonic_ns()
            projection_start = local_now()
            elapsed_at_projection_start = (
                float(initial_elapsed_seconds)
                + (projection_start_monotonic_ns - operator_start_requested_monotonic_ns) / 1e9
            )
            manifest["projector_pattern_started_at"] = projection_start.isoformat(timespec="milliseconds")
            manifest["estimated_elapsed_at_projection_start_seconds"] = elapsed_at_projection_start
            estimated_power_on = operator_start_requested_at - timedelta(
                seconds=float(initial_elapsed_seconds)
            )
            manifest["estimated_projector_power_on_at"] = estimated_power_on.isoformat(timespec="milliseconds")
            manifest["status"] = "running"
            atomic_write_json(manifest_path, manifest)
            self.log(
                f"투사 시작: {manifest['projector_pattern_started_at']} / "
                f"전원 ON 추정 경과 {elapsed_at_projection_start:.1f}초"
            )
            self.log(
                f"총 {total_duration_minutes:g}분, {len(schedule)}회 시점, "
                f"시점당 원본 5장을 촬영합니다."
            )

            for index, target_elapsed in enumerate(schedule):
                due_ns = projection_start_monotonic_ns + int(target_elapsed * 1_000_000_000)
                remaining = (due_ns - time.monotonic_ns()) / 1_000_000_000.0
                if remaining > 0:
                    self.log(
                        f"다음 촬영 #{index + 1}: 투사 시작 후 "
                        f"{target_elapsed / 60.0:.1f}분 (약 {remaining:.0f}초 후)"
                    )
                    if stop_event.wait(remaining):
                        break
                if stop_event.is_set():
                    break

                set_started_at = iso_now()
                actual_start_elapsed = (time.monotonic_ns() - projection_start_monotonic_ns) / 1e9
                actual_power_on_elapsed = (
                    float(initial_elapsed_seconds)
                    + (time.monotonic_ns() - operator_start_requested_monotonic_ns) / 1e9
                )
                point_dir = session / "timepoints" / f"t{index:03d}_{round(target_elapsed):06d}s"
                captured: dict[str, np.ndarray] = {}
                frame_rows: list[dict[str, Any]] = []
                self.log(
                    f"촬영 #{index + 1}/{len(schedule)} 시작: "
                    f"예정 {target_elapsed:.1f}초, 실제 {actual_start_elapsed:.1f}초"
                )
                for role in PATTERN_ROLES:
                    frame, timing = self._settle_and_capture(
                        display=display,
                        camera=camera,
                        pattern=images[role],
                        stop_event=stop_event,
                    )
                    filename = f"{role}.png"
                    size_bytes = write_image(point_dir / filename, np.asarray(frame.image))
                    captured[role] = np.asarray(frame.image).copy()
                    frame_rows.append(
                        {
                            "role": role,
                            "filename": str((point_dir / filename).relative_to(session)),
                            "size_bytes": size_bytes,
                            "camera_timestamp_ms": frame.timestamp_ms,
                            "camera_frame_index": frame.frame_index,
                            "pixel_format": frame.pixel_format,
                            "camera_metadata": frame.metadata,
                            "statistics": image_statistics(np.asarray(frame.image)),
                            **timing,
                        }
                    )

                display.show(images["sine_000"])
                phase, valid, phase_metrics = phase_summary(
                    captured,
                    minimum_modulation=float(self.config["analysis"]["minimum_modulation"]),
                    saturation_value=float(self.config["analysis"]["saturation_value"]),
                )
                if reference_phase is None or reference_valid is None:
                    reference_phase = phase.copy()
                    reference_valid = valid.copy()
                    phase_change = {
                        "common_valid_pixel_count": int(np.count_nonzero(valid)),
                        "circular_mean_delta_rad": 0.0,
                        "circular_std_delta_rad": 0.0,
                    }
                else:
                    phase_change = circular_phase_change(
                        reference_phase,
                        reference_valid,
                        phase,
                        valid,
                    )
                completed_elapsed = (time.monotonic_ns() - projection_start_monotonic_ns) / 1e9
                point = {
                    "index": index,
                    "target_elapsed_since_projection_seconds": target_elapsed,
                    "actual_set_start_elapsed_since_projection_seconds": actual_start_elapsed,
                    "estimated_set_start_elapsed_since_power_on_seconds": actual_power_on_elapsed,
                    "schedule_lateness_seconds": actual_start_elapsed - target_elapsed,
                    "set_started_at": set_started_at,
                    "set_completed_at": iso_now(),
                    "set_completed_elapsed_since_projection_seconds": completed_elapsed,
                    "room_temperature_c": room_temperature_c,
                    "camera_temperature_c": None,
                    "projector_temperature_c": None,
                    "aruco": detect_aruco(captured["white"], self.config),
                    "phase_summary": phase_metrics,
                    "phase_change_from_first": phase_change,
                    "frames": frame_rows,
                }
                manifest["timepoints"].append(point)
                atomic_write_json(point_dir / "timepoint.json", point)
                atomic_write_json(manifest_path, manifest)
                self.log(
                    f"촬영 #{index + 1} 완료: 유효 위상 픽셀 "
                    f"{phase_metrics['valid_pixel_ratio'] * 100.0:.1f}%"
                )

            manifest["status"] = "stopped" if stop_event.is_set() else "completed"
            manifest["ended_at"] = iso_now()
            manifest["completed_timepoint_count"] = len(manifest["timepoints"])
            atomic_write_json(manifest_path, manifest)
            self.log(
                "사용자가 중지했습니다. 이미 촬영한 데이터는 보존됩니다."
                if stop_event.is_set()
                else "열 드리프트 촬영이 완료되었습니다."
            )
            return session
        except InterruptedError:
            manifest["status"] = "stopped"
            manifest["ended_at"] = iso_now()
            manifest["completed_timepoint_count"] = len(manifest["timepoints"])
            atomic_write_json(manifest_path, manifest)
            self.log("촬영을 중지했습니다. 완료된 시점의 데이터는 보존됩니다.")
            return session
        except Exception as exc:
            manifest["status"] = "failed"
            manifest["ended_at"] = iso_now()
            manifest["error"] = f"{type(exc).__name__}: {exc}"
            manifest["completed_timepoint_count"] = len(manifest["timepoints"])
            atomic_write_json(manifest_path, manifest)
            raise
        finally:
            if display is not None:
                try:
                    display.black()
                    time.sleep(0.2)
                finally:
                    display.close()
            if camera is not None:
                try:
                    camera.stop()
                finally:
                    camera.close()


class ThermalDriftApp:
    def __init__(self) -> None:
        self.root = Tk()
        self.root.title("독립 열 드리프트 촬영")
        self.root.geometry("900x680")
        self.config_path = ROOT / "thermal_drift_config.json"
        self.config = read_json(self.config_path)
        self.messages: queue.Queue[tuple[str, str]] = queue.Queue()
        self.stop_event = threading.Event()
        self.running = False
        self.failed = False
        self.close_after_stop = False
        self.latest_session: Path | None = None
        self.output_var = StringVar(value=str(WORKSPACE / "captures" / "thermal_drift"))
        self.duration_var = StringVar(value=str(self.config["schedule"]["total_duration_minutes"]))
        self.initial_elapsed_var = StringVar(value="0")
        self.room_temperature_var = StringVar(value="")
        self.note_var = StringVar(value="")
        self.status_var = StringVar(value="대기 중")
        self.worker = ThermalDriftCapture(self.config_path, self._queue_log)
        self._build()
        self.root.after(100, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _labeled_entry(self, parent: Frame, label: str, variable: StringVar, width: int = 14) -> None:
        Label(parent, text=label).pack(side=LEFT, padx=(0, 4))
        Entry(parent, textvariable=variable, width=width).pack(side=LEFT, padx=(0, 14))

    def _build(self) -> None:
        output_row = Frame(self.root)
        output_row.pack(fill=X, padx=12, pady=(12, 4))
        Label(output_row, text="저장 폴더").pack(side=LEFT)
        Entry(output_row, textvariable=self.output_var).pack(side=LEFT, fill=X, expand=True, padx=8)
        self.choose_button = Button(output_row, text="선택", command=self._choose_output)
        self.choose_button.pack(side=RIGHT)

        input_row = Frame(self.root)
        input_row.pack(fill=X, padx=12, pady=4)
        self._labeled_entry(input_row, "총 촬영(분)", self.duration_var)
        self._labeled_entry(input_row, "투사전원 ON 후 이미 지난 시간(초)", self.initial_elapsed_var)
        self._labeled_entry(input_row, "실내온도(°C, 선택)", self.room_temperature_var)

        note_row = Frame(self.root)
        note_row.pack(fill=X, padx=12, pady=4)
        Label(note_row, text="메모").pack(side=LEFT)
        Entry(note_row, textvariable=self.note_var).pack(side=LEFT, fill=X, expand=True, padx=8)

        schedule = self.config["schedule"]
        camera = self.config["camera"][self.config["camera"]["provider"]]
        info = (
            "촬영 순서: White 1장 + Sine 0/90/180/270° 4장 (Gray code 없음, 회전 없음)\n"
            f"기본 일정: 처음 {schedule['initial_duration_minutes']} 분까지 "
            f"{schedule['initial_interval_seconds']} 초 간격, 이후 "
            f"{schedule['regular_interval_seconds']} 초 간격  |  "
            f"카메라: {camera['exposure_us']} us, {camera.get('gain_db', 0)} dB, "
            f"{camera['image_format']}\n"
            "대기 중에도 Sine 0° 패턴을 계속 투사합니다. "
            "전원을 켠 직후라면 '이미 지난 시간'을 0으로 두세요."
        )
        Label(self.root, text=info, justify=LEFT, anchor="w").pack(fill=X, padx=12, pady=10)

        button_row = Frame(self.root)
        button_row.pack(fill=X, padx=12, pady=4)
        self.start_button = Button(button_row, text="열 드리프트 촬영 시작", command=self._start)
        self.start_button.pack(side=LEFT, padx=(0, 8))
        self.stop_button = Button(button_row, text="중지", command=self._stop, state="disabled")
        self.stop_button.pack(side=LEFT, padx=(0, 8))
        Button(button_row, text="최근 세션 열기", command=self._open_session).pack(side=RIGHT)
        Label(button_row, textvariable=self.status_var).pack(side=RIGHT, padx=12)

        self.log_text = Text(self.root, wrap="word")
        self.log_text.pack(fill=BOTH, expand=True, padx=12, pady=12)
        self._append(
            "내일 실행 순서: 빈 스테이지를 고정 -> 카메라/프로젝터 ON -> "
            "즉시 '촬영 시작' 클릭. 촬영 중 장비를 건드리지 마세요.\n"
        )

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory()
        if selected:
            self.output_var.set(selected)

    def _optional_float(self, value: str, label: str) -> float | None:
        value = value.strip()
        if not value:
            return None
        try:
            return float(value)
        except ValueError as exc:
            raise ValueError(f"{label}에 숫자를 입력하세요") from exc

    def _start(self) -> None:
        if self.running:
            return
        try:
            duration = float(self.duration_var.get().strip())
            initial_elapsed = float(self.initial_elapsed_var.get().strip())
            room_temperature = self._optional_float(self.room_temperature_var.get(), "실내온도")
            if duration <= 0:
                raise ValueError("총 촬영 시간은 0분보다 길어야 합니다")
            if initial_elapsed < 0:
                raise ValueError("전원 ON 후 경과 시간은 0초 이상이어야 합니다")
        except ValueError as exc:
            messagebox.showerror("입력 오류", str(exc))
            return

        self.running = True
        self.failed = False
        self.stop_event.clear()
        self.start_button.config(state="disabled")
        self.choose_button.config(state="disabled")
        self.stop_button.config(state="normal")
        self.status_var.set("카메라 연결 중")
        self._append("실험을 시작합니다...\n")
        operator_start_requested_at = local_now()
        operator_start_requested_monotonic_ns = time.monotonic_ns()

        def execute() -> None:
            try:
                session = self.worker.run(
                    output_root=Path(self.output_var.get().strip()),
                    total_duration_minutes=duration,
                    initial_elapsed_seconds=initial_elapsed,
                    room_temperature_c=room_temperature,
                    note=self.note_var.get().strip(),
                    operator_start_requested_at=operator_start_requested_at,
                    operator_start_requested_monotonic_ns=operator_start_requested_monotonic_ns,
                    stop_event=self.stop_event,
                    session_callback=lambda path: self.messages.put(("session", str(path))),
                )
                self.messages.put(("finished", str(session)))
            except Exception as exc:
                self.messages.put(("error", f"{type(exc).__name__}: {exc}"))
            finally:
                self.messages.put(("done", ""))

        threading.Thread(target=execute, daemon=True).start()

    def _stop(self) -> None:
        if self.running:
            self.stop_event.set()
            self.stop_button.config(state="disabled")
            self.status_var.set("중지 중")
            self._append("중지를 요청했습니다. 현재 프레임을 안전하게 정리합니다.\n")

    def _queue_log(self, message: str) -> None:
        self.messages.put(("log", message))

    def _poll(self) -> None:
        try:
            while True:
                kind, message = self.messages.get_nowait()
                if kind == "log":
                    self._append(message + "\n")
                elif kind == "session":
                    self.latest_session = Path(message)
                    self.status_var.set("촬영 중")
                    self._append(f"세션: {message}\n")
                elif kind == "finished":
                    self.latest_session = Path(message)
                elif kind == "error":
                    self.failed = True
                    self._append("오류: " + message + "\n")
                    messagebox.showerror("열 드리프트 촬영 오류", message)
                elif kind == "done":
                    self.running = False
                    self.start_button.config(state="normal")
                    self.choose_button.config(state="normal")
                    self.stop_button.config(state="disabled")
                    if self.failed:
                        self.status_var.set("오류")
                    else:
                        self.status_var.set("중지됨" if self.stop_event.is_set() else "완료")
                    if self.close_after_stop:
                        self.root.destroy()
                        return
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _append(self, text: str) -> None:
        self.log_text.insert(END, text)
        self.log_text.see(END)

    def _open_session(self) -> None:
        if self.latest_session is None or not self.latest_session.exists():
            messagebox.showwarning("세션 없음", "아직 생성된 열 드리프트 세션이 없습니다")
            return
        os.startfile(self.latest_session)

    def _on_close(self) -> None:
        if not self.running:
            self.root.destroy()
            return
        if messagebox.askyesno("촬영 중", "촬영을 중지하고 창을 닫을까요? 완료된 데이터는 보존됩니다."):
            self.close_after_stop = True
            self._stop()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    ThermalDriftApp().run()
