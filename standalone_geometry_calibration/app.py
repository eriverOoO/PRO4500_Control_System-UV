"""One-window standalone camera/projector/checkerboard calibration application."""

from __future__ import annotations

import ctypes
import json
import queue
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, Button, Entry, Frame, Label, StringVar, Text, Tk, filedialog, messagebox
from typing import Any, Callable

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from camera_provider import CameraInterface, CameraProvider  # noqa: E402
from calibration_core import (  # noqa: E402
    PatternProfile,
    detect_checkerboard,
    draw_checkerboard_detection,
    estimate_stage_plane_from_aruco,
    generate_patterns,
    solve_geometry,
)


@dataclass(frozen=True)
class Monitor:
    x: int
    y: int
    width: int
    height: int
    primary: bool


def windows_monitors() -> list[Monitor]:
    if sys.platform != "win32":
        return []
    from ctypes import wintypes

    ctypes.windll.user32.SetProcessDPIAware()

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
        ]

    result: list[Monitor] = []
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
    )

    @callback_type
    def collect(handle, _dc, _rect, _data):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            rect = info.rcMonitor
            result.append(Monitor(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top, bool(info.dwFlags & 1)))
        return True

    ctypes.windll.user32.EnumDisplayMonitors(None, None, collect, 0)
    return result


class ProjectorWindow:
    def __init__(self, monitor_index: int, profile: PatternProfile) -> None:
        monitors = windows_monitors()
        if not monitors:
            raise RuntimeError("Windows monitor geometry could not be read")
        if not 0 <= monitor_index < len(monitors):
            raise ValueError(f"Projector monitor index {monitor_index} is invalid; detected {len(monitors)} monitors")
        self.monitor = monitors[monitor_index]
        if (self.monitor.width, self.monitor.height) != (profile.width, profile.height):
            raise ValueError(
                "Projector monitor resolution does not match the calibration pattern: "
                f"monitor={self.monitor.width}x{self.monitor.height}, pattern={profile.width}x{profile.height}. "
                "Set Windows scaling to 100% and update calibration_config.json."
            )
        self.name = "Standalone Geometry Calibration Projection"

    def open(self) -> None:
        cv2.namedWindow(self.name, cv2.WINDOW_NORMAL)
        cv2.moveWindow(self.name, self.monitor.x, self.monitor.y)
        cv2.resizeWindow(self.name, self.monitor.width, self.monitor.height)
        cv2.setWindowProperty(self.name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        cv2.waitKey(1)
        hwnd = ctypes.windll.user32.FindWindowW(None, self.name)
        if hwnd:
            ctypes.windll.user32.SetWindowPos(
                hwnd, -1, self.monitor.x, self.monitor.y, self.monitor.width, self.monitor.height, 0x0040
            )

    def show(self, image: np.ndarray) -> None:
        cv2.imshow(self.name, image)
        cv2.waitKey(1)

    def close(self) -> None:
        try:
            cv2.destroyWindow(self.name)
        except cv2.error:
            pass


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class CalibrationCapture:
    def __init__(self, config_path: Path, logger: Callable[[str], None]) -> None:
        self.config_path = config_path
        self.config = read_json(config_path)
        self.log = logger
        projector = self.config["projector"]
        self.profile = PatternProfile(
            width=int(projector["width_px"]),
            height=int(projector["height_px"]),
            period_px=int(projector["fringe_period_px"]),
        )

    def create_session(self, session: Path) -> None:
        if session.exists() and any(session.iterdir()):
            raise ValueError(f"Session folder is not empty: {session}")
        session.mkdir(parents=True, exist_ok=True)
        pattern_manifest = generate_patterns(session / "patterns", self.profile)
        checkerboard = self.config["checkerboard"]
        manifest = {
            "schema_version": 1,
            "session_id": session.name,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "standalone": True,
            "production_controller_modified_or_invoked": False,
            "checkerboard": {
                "inner_corners": [int(value) for value in checkerboard["inner_corners"]],
                "square_size_mm": float(checkerboard["square_size_mm"]),
                "outer_shape_required": False,
            },
            "pattern": {
                "projector_size_px": [self.profile.width, self.profile.height],
                "period_px": self.profile.period_px,
                "manifest": "patterns/pattern_manifest.json",
                "axis_gray_bits": {
                    axis: pattern_manifest["axes"][axis]["gray_bits"] for axis in ("x", "y")
                },
            },
            "camera_settings": CameraProvider.load_settings(self.config_path).as_dict(),
            "calibration_quality": self.config["calibration_quality"],
            "projector_corner_estimation": self.config["projector_corner_estimation"],
            "captured_poses": [],
            "rejected_poses": [],
            "geometry_calibration": "not_run",
            "stage_plane": "not_run",
        }
        write_json(session / "session_manifest.json", manifest)
        (session / "poses").mkdir()
        (session / "rejected").mkdir()
        self.log(f"세션 준비 완료: {session}")
        self.log("패턴을 모두 사전 생성했습니다. 체커보드를 자유로운 위치/거리/기울기로 놓으세요.")

    def _open_camera(self) -> CameraInterface:
        settings = CameraProvider.load_settings(self.config_path)
        camera = CameraProvider.create(settings)
        camera.open()
        camera.start()
        return camera

    def _settle_and_capture(
        self,
        projector: ProjectorWindow,
        camera: CameraInterface,
        image: np.ndarray,
    ) -> np.ndarray:
        projector.show(image)
        time.sleep(max(100, int(self.config["projector"]["settle_ms"])) / 1000.0)
        flush = max(0, int(self.config["capture"]["flush_frames_after_pattern"]))
        for _ in range(flush):
            camera.capture_frame()
        return np.asarray(camera.capture_frame().image)

    def capture_next_pose(self, session: Path) -> None:
        manifest_path = session / "session_manifest.json"
        manifest = read_json(manifest_path)
        next_number = len(manifest["captured_poses"]) + len(manifest["rejected_poses"]) + 1
        pose_id = f"pose_{next_number:03d}"
        temporary = session / "rejected" / pose_id
        temporary.mkdir(parents=True, exist_ok=False)
        camera: CameraInterface | None = None
        projector: ProjectorWindow | None = None
        try:
            camera = self._open_camera()
            projector = ProjectorWindow(int(self.config["projector"]["monitor_index"]), self.profile)
            projector.open()
            patterns = session / "patterns"
            black_pattern = cv2.imread(str(patterns / "reference_black.png"), cv2.IMREAD_GRAYSCALE)
            white_pattern = cv2.imread(str(patterns / "reference_white.png"), cv2.IMREAD_GRAYSCALE)
            if black_pattern is None or white_pattern is None:
                raise ValueError("Generated black/white patterns are missing")
            self.log(f"{pose_id}: black/white 기준 프레임 촬영")
            black = self._settle_and_capture(projector, camera, black_pattern)
            white = self._settle_and_capture(projector, camera, white_pattern)
            cv2.imwrite(str(temporary / "reference_black.png"), black)
            cv2.imwrite(str(temporary / "reference_white.png"), white)
            inner = tuple(int(v) for v in manifest["checkerboard"]["inner_corners"])
            corners, detection, report = detect_checkerboard(black, white, inner)
            cv2.imwrite(str(temporary / "checkerboard_response.png"), detection)
            cv2.imwrite(str(temporary / "checkerboard_detection.png"), draw_checkerboard_detection(white, inner, corners))
            minimum_area = float(self.config["capture"]["minimum_board_area_ratio"])
            if corners is None or float(report.get("board_image_area_ratio", 0.0)) < minimum_area:
                manifest["rejected_poses"].append(
                    {"pose_id": pose_id, "captured_at": datetime.now().isoformat(timespec="seconds"), "report": report}
                )
                write_json(manifest_path, manifest)
                raise ValueError(
                    "체커보드 검출 실패 또는 화면에서 너무 작습니다. "
                    f"검출={report['found']}, 영역비={report.get('board_image_area_ratio', 0.0):.3%}. 자세를 바꿔 재촬영하세요."
                )
            np.save(temporary / "checkerboard_corners.npy", corners)
            pattern_manifest = read_json(patterns / "pattern_manifest.json")
            for axis in ("x", "y"):
                axis_output = temporary / axis
                axis_output.mkdir()
                sequence = pattern_manifest["axes"][axis]["sequence"]
                self.log(f"{pose_id}: {axis.upper()}축 {len(sequence)}개 패턴 자동 촬영")
                for index, entry in enumerate(sequence, start=1):
                    source = patterns / axis / entry["file"]
                    projected = cv2.imread(str(source), cv2.IMREAD_GRAYSCALE)
                    if projected is None:
                        raise ValueError(f"Pattern is missing: {source}")
                    frame = self._settle_and_capture(projector, camera, projected)
                    if not cv2.imwrite(str(axis_output / entry["file"]), frame):
                        raise RuntimeError(f"Could not save captured pattern: {entry['file']}")
                    self.log(f"{pose_id}: {axis.upper()} {index}/{len(sequence)}")
            accepted = session / "poses" / pose_id
            temporary.replace(accepted)
            relative = accepted.relative_to(session).as_posix()
            manifest["captured_poses"].append(
                {
                    "pose_id": pose_id,
                    "captured_at": datetime.now().isoformat(timespec="seconds"),
                    "relative_dir": relative,
                    "checkerboard_detection": report,
                    "operator_motion_known": False,
                }
            )
            write_json(manifest_path, manifest)
            self.log(f"{pose_id}: 저장 완료. 이제 체커보드 자세를 바꾼 뒤 다시 촬영하세요.")
        finally:
            if projector is not None:
                projector.show(np.zeros((self.profile.height, self.profile.width), dtype=np.uint8))
                projector.close()
            if camera is not None:
                try:
                    camera.stop()
                finally:
                    camera.close()

    def solve(self, session: Path) -> None:
        result = solve_geometry(session)
        manifest_path = session / "session_manifest.json"
        manifest = read_json(manifest_path)
        manifest["geometry_calibration"] = "geometry_calibration.json"
        write_json(manifest_path, manifest)
        stereo = result["camera_to_projector"]
        self.log(
            "기하 보정 완료: "
            f"판정={'통과' if result['quality']['valid'] else '실패'}, "
            f"stereo RMS={stereo['stereo_rms_px']:.3f}px, baseline={stereo['baseline_mm']:.3f}mm"
        )

    def capture_stage_z0(self, session: Path) -> None:
        calibration_path = session / "geometry_calibration.json"
        if not calibration_path.is_file():
            raise ValueError("먼저 체커보드 기하 보정을 계산하세요")
        calibration = read_json(calibration_path)
        if not calibration.get("quality", {}).get("valid", False):
            raise ValueError("기하 보정 품질 판정이 실패했습니다. pose를 보강하고 다시 계산하세요")
        output = session / "stage_z0"
        output.mkdir(exist_ok=True)
        camera: CameraInterface | None = None
        projector: ProjectorWindow | None = None
        try:
            camera = self._open_camera()
            projector = ProjectorWindow(int(self.config["projector"]["monitor_index"]), self.profile)
            projector.open()
            patterns = session / "patterns"
            black_pattern = cv2.imread(str(patterns / "reference_black.png"), cv2.IMREAD_GRAYSCALE)
            white_pattern = cv2.imread(str(patterns / "reference_white.png"), cv2.IMREAD_GRAYSCALE)
            black = self._settle_and_capture(projector, camera, black_pattern)
            white = self._settle_and_capture(projector, camera, white_pattern)
            cv2.imwrite(str(output / "projector_black.png"), black)
            cv2.imwrite(str(output / "projector_white.png"), white)
            difference = cv2.normalize(cv2.subtract(white, black), None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
            stage = self.config["stage_aruco"]
            last_error: Exception | None = None
            for name, candidate in (("projector_white", white), ("white_minus_black", difference), ("projector_black", black)):
                try:
                    plane, preview = estimate_stage_plane_from_aruco(
                        candidate,
                        calibration,
                        str(stage["dictionary"]),
                        float(stage["marker_size_mm"]),
                        [int(value) for value in stage["marker_ids"]],
                    )
                    plane["source_image"] = name
                    cv2.imwrite(str(output / "aruco_detection.png"), preview)
                    calibration["stage_plane"] = plane
                    write_json(calibration_path, calibration)
                    write_json(output / "stage_plane.json", plane)
                    manifest_path = session / "session_manifest.json"
                    manifest = read_json(manifest_path)
                    manifest["stage_plane"] = "stage_z0/stage_plane.json"
                    write_json(manifest_path, manifest)
                    self.log(f"스테이지 z=0 등록 완료: ArUco {plane['marker_ids']}")
                    return
                except Exception as exc:
                    last_error = exc
            raise ValueError(f"black/white 어느 영상에서도 ArUco 평면을 계산하지 못했습니다: {last_error}")
        finally:
            if projector is not None:
                projector.show(np.zeros((self.profile.height, self.profile.width), dtype=np.uint8))
                projector.close()
            if camera is not None:
                try:
                    camera.stop()
                finally:
                    camera.close()


class CalibrationApp:
    def __init__(self) -> None:
        self.root = Tk()
        self.root.title("Standalone Camera-Projector Geometry Calibration")
        self.root.geometry("900x650")
        self.messages: queue.Queue[tuple[str, str]] = queue.Queue()
        self.running = False
        self.session_var = StringVar(value=str(WORKSPACE / "captures" / "geometry_calibration_session"))
        self.config_path = ROOT / "calibration_config.json"
        self.worker = CalibrationCapture(self.config_path, self._queue_log)
        self._build()
        self.root.after(100, self._poll)

    def _build(self) -> None:
        row = Frame(self.root)
        row.pack(fill=X, padx=12, pady=(12, 4))
        Label(row, text="세션 폴더").pack(side=LEFT)
        Entry(row, textvariable=self.session_var).pack(side=LEFT, fill=X, expand=True, padx=8)
        Button(row, text="선택", command=self._choose).pack(side=RIGHT)
        cfg = read_json(self.config_path)
        board = cfg["checkerboard"]
        projector = cfg["projector"]
        info = (
            f"체커보드: 내부 코너 {board['inner_corners'][0]}×{board['inner_corners'][1]}, "
            f"실측 칸 {board['square_size_mm']} mm  |  "
            f"프로젝터: {projector['width_px']}×{projector['height_px']}, monitor {projector['monitor_index']}\n"
            "각 pose 동안 보드를 고정하세요. 완료 후 위치·거리·yaw/pitch/roll을 임의로 바꿔 반복합니다. 이동량 입력은 없습니다."
        )
        Label(self.root, text=info, justify=LEFT, anchor="w").pack(fill=X, padx=12, pady=8)
        buttons = Frame(self.root)
        buttons.pack(fill=X, padx=12, pady=4)
        self.controls = [
            Button(buttons, text="1. 새 세션 + 패턴 준비", command=lambda: self._run(self._prepare)),
            Button(buttons, text="2. 다음 pose 자동 촬영", command=lambda: self._run(self._capture)),
            Button(buttons, text="3. 기하 보정 계산", command=lambda: self._run(self._solve)),
            Button(buttons, text="4. ArUco 스테이지 z=0 촬영", command=lambda: self._run(self._z0)),
        ]
        for button in self.controls:
            button.pack(side=LEFT, padx=(0, 8))
        Button(buttons, text="세션 열기", command=self._open_session).pack(side=RIGHT)
        self.log_text = Text(self.root, wrap="word")
        self.log_text.pack(fill=BOTH, expand=True, padx=12, pady=12)
        self._append("설정 변경은 standalone_geometry_calibration/calibration_config.json에서 합니다.\n")

    def _session(self) -> Path:
        return Path(self.session_var.get().strip()).resolve()

    def _choose(self) -> None:
        selected = filedialog.askdirectory()
        if selected:
            self.session_var.set(selected)

    def _prepare(self) -> None:
        self.worker.create_session(self._session())

    def _capture(self) -> None:
        self.worker.capture_next_pose(self._session())

    def _solve(self) -> None:
        self.worker.solve(self._session())

    def _z0(self) -> None:
        self.worker.capture_stage_z0(self._session())

    def _run(self, action: Callable[[], None]) -> None:
        if self.running:
            return
        self.running = True
        for control in self.controls:
            control.config(state="disabled")

        def execute() -> None:
            try:
                action()
            except Exception as exc:
                self.messages.put(("error", f"{type(exc).__name__}: {exc}"))
            finally:
                self.messages.put(("done", ""))

        threading.Thread(target=execute, daemon=True).start()

    def _queue_log(self, message: str) -> None:
        self.messages.put(("log", message))

    def _poll(self) -> None:
        try:
            while True:
                kind, message = self.messages.get_nowait()
                if kind == "log":
                    self._append(message + "\n")
                elif kind == "error":
                    self._append("오류: " + message + "\n")
                    messagebox.showerror("칼리브레이션 오류", message)
                elif kind == "done":
                    self.running = False
                    for control in self.controls:
                        control.config(state="normal")
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _append(self, text: str) -> None:
        self.log_text.insert(END, text)
        self.log_text.see(END)

    def _open_session(self) -> None:
        session = self._session()
        if not session.exists():
            messagebox.showwarning("세션 없음", "먼저 세션을 준비하세요")
            return
        import os

        os.startfile(session)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    CalibrationApp().run()
