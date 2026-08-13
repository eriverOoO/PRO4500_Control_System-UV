"""One-window standalone camera/projector/checkerboard calibration application."""

from __future__ import annotations

import ctypes
import json
import queue
import shutil
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
    charuco_object_points,
    detect_charuco,
    draw_charuco_detection,
    estimate_image_motion_rms,
    decode_projector_axis_dense,
    estimate_projector_corners_from_local_homographies,
    estimate_stage_plane_from_aruco,
    generate_patterns,
    solve_geometry,
    strict_checkerboard_correspondence_mask,
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
                "target_type": "charuco",
                "squares": [int(value) for value in checkerboard["squares"]],
                "square_size_mm": float(checkerboard["square_size_mm"]),
                "marker_size_mm": float(checkerboard["marker_size_mm"]),
                "dictionary": str(checkerboard["dictionary"]),
                "minimum_visible_corner_count": int(
                    checkerboard.get("minimum_visible_corner_count", 9)
                ),
                "detection_exposure_us": int(checkerboard.get("detection_exposure_us", 200000)),
                "clahe_clip_limit": float(checkerboard.get("clahe_clip_limit", 3.0)),
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

    def _capture_exposure(self) -> tuple[int, float]:
        camera = self.config["camera"]["ximea"]
        return int(camera["exposure_us"]), float(camera.get("gain_db", 0.0))

    def _detection_exposure(self) -> tuple[int, float]:
        exposure_us, gain_db = self._capture_exposure()
        checkerboard = self.config["checkerboard"]
        return int(checkerboard.get("detection_exposure_us", exposure_us)), gain_db

    def _set_camera_exposure(
        self,
        camera: CameraInterface,
        exposure_us: int,
        gain_db: float,
        purpose: str,
    ) -> None:
        camera.configure_capture(exposure_us=exposure_us, gain_db=gain_db)
        self.log(f"{purpose}: exposure={exposure_us} us, gain={gain_db:g} dB")

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
        if manifest.get("rejected_poses"):
            manifest["rejected_poses"] = []
            write_json(manifest_path, manifest)
        existing_numbers: list[int] = []
        for parent in (session / "poses", session / "rejected"):
            for path in parent.glob("pose_*"):
                try:
                    existing_numbers.append(int(path.name.removeprefix("pose_")))
                except ValueError:
                    continue
        next_number = max(existing_numbers, default=0) + 1
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
            capture_exposure_us, capture_gain_db = self._capture_exposure()
            detection_exposure_us, detection_gain_db = self._detection_exposure()
            self._set_camera_exposure(
                camera,
                detection_exposure_us,
                detection_gain_db,
                f"{pose_id}: ChArUco detection",
            )
            detection_black = self._settle_and_capture(projector, camera, black_pattern)
            cv2.imwrite(
                str(temporary / "charuco_detection_exposure.png"), detection_black
            )
            self._set_camera_exposure(
                camera,
                capture_exposure_us,
                capture_gain_db,
                f"{pose_id}: structured light",
            )
            self.log(f"{pose_id}: black/white 기준 프레임 촬영")
            black = self._settle_and_capture(projector, camera, black_pattern)
            white = self._settle_and_capture(projector, camera, white_pattern)
            cv2.imwrite(str(temporary / "reference_black.png"), black)
            cv2.imwrite(str(temporary / "reference_white.png"), white)
            configured_board = self.config["checkerboard"]
            if manifest["checkerboard"].get("target_type") != "charuco":
                raise ValueError("기존 체커보드 세션은 ChArUco 촬영과 호환되지 않습니다. 새 세션을 생성하세요.")
            corners, charuco_ids, detection, report = detect_charuco(
                detection_black, detection_black, configured_board
            )
            report["detection_exposure_us"] = detection_exposure_us
            report["structured_light_exposure_us"] = capture_exposure_us
            cv2.imwrite(str(temporary / "charuco_response.png"), detection)
            cv2.imwrite(
                str(temporary / "charuco_detection.png"),
                draw_charuco_detection(detection, corners, charuco_ids),
            )
            minimum_area = float(self.config["capture"]["minimum_board_area_ratio"])
            minimum_corners = int(configured_board.get("minimum_visible_corner_count", 9))
            if (
                corners is None
                or charuco_ids is None
                or len(charuco_ids) < minimum_corners
                or float(report.get("board_image_area_ratio", 0.0)) < minimum_area
            ):
                raise ValueError(
                    "ChArUco 검출 실패 또는 ID 코너가 부족합니다. "
                    f"검출 코너={report.get('corner_count', 0)}개(최소 {minimum_corners}개), "
                    f"영역비={report.get('board_image_area_ratio', 0.0):.3%}."
                )
            np.save(temporary / "checkerboard_corners.npy", corners)
            np.save(temporary / "charuco_ids.npy", charuco_ids)
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
            self.log(f"{pose_id}: 촬영 중 보정판 움직임 확인")
            self._set_camera_exposure(
                camera,
                detection_exposure_us,
                detection_gain_db,
                f"{pose_id}: final ChArUco detection",
            )
            final_detection_black = self._settle_and_capture(
                projector, camera, black_pattern
            )
            cv2.imwrite(
                str(temporary / "charuco_detection_exposure_final.png"),
                final_detection_black,
            )
            final_corners, final_ids, final_detection, final_report = detect_charuco(
                final_detection_black, final_detection_black, configured_board
            )
            final_report["detection_exposure_us"] = detection_exposure_us
            cv2.imwrite(str(temporary / "charuco_response_final.png"), final_detection)
            cv2.imwrite(
                str(temporary / "charuco_detection_final.png"),
                draw_charuco_detection(final_detection, final_corners, final_ids),
            )
            motion_limit_px = float(
                self.config["capture"].get("maximum_pose_motion_rms_px", 2.0)
            )
            common_ids: list[int] = []
            if final_corners is not None and final_ids is not None:
                initial_by_id = {int(i): p for i, p in zip(charuco_ids, corners)}
                final_by_id = {int(i): p for i, p in zip(final_ids, final_corners)}
                common_ids = sorted(set(initial_by_id) & set(final_by_id))
            if len(common_ids) >= 6:
                displacement = np.array(
                    [initial_by_id[i] - final_by_id[i] for i in common_ids], dtype=np.float32
                )
                motion_rms_px = float(np.sqrt(np.mean(np.sum(displacement**2, axis=1))))
                motion_order = "charuco_id_matched"
                motion_method = "charuco_ids"
                motion_score: float | None = None
            else:
                motion_rms_px, motion_score = estimate_image_motion_rms(
                    detection_black, final_detection_black
                )
                motion_order = "not_applicable"
                motion_method = "black_frame_ecc" if motion_rms_px is not None else "unverified"
            report["capture_motion_validation"] = {
                "rms_px": motion_rms_px,
                "limit_px": motion_limit_px,
                "corner_order": motion_order,
                "method": motion_method,
                "ecc_score": motion_score,
                "common_charuco_ids": common_ids,
                "final_detection": final_report,
            }
            if motion_rms_px is not None and motion_rms_px > motion_limit_px:
                raise ValueError(
                    f"{pose_id} 패턴 촬영 중 보정판이 움직였습니다: "
                    f"코너 이동 RMS {motion_rms_px:.2f}px(허용 {motion_limit_px:.2f}px). "
                    "고정한 뒤 다시 촬영하세요. 저장하지 않습니다."
                )
            if motion_rms_px is None:
                self.log(f"{pose_id}: 종료 프레임 격자 검증 불가; 엄격 대응점 검사로 계속 확인합니다.")
            corner_config = manifest.get("projector_corner_estimation", {})
            decoded_axes: dict[str, tuple[np.ndarray, np.ndarray]] = {}
            for axis, projector_extent in (
                ("x", int(manifest["pattern"]["projector_size_px"][0])),
                ("y", int(manifest["pattern"]["projector_size_px"][1])),
            ):
                decoded_axes[axis] = decode_projector_axis_dense(
                    temporary / axis,
                    pattern_manifest["axes"][axis],
                    int(manifest["pattern"]["period_px"]),
                    projector_extent,
                    minimum_gray_contrast=float(
                        corner_config.get("minimum_gray_pair_contrast_u8", 5.0)
                    ),
                    minimum_modulation=float(
                        corner_config.get("minimum_sine_modulation_u8", 5.0)
                    ),
                )
            projector_corners, decoded_corner_mask, _corner_reports = (
                estimate_projector_corners_from_local_homographies(
                    corners,
                    decoded_axes["x"][0],
                    decoded_axes["y"][0],
                    decoded_axes["x"][1] & decoded_axes["y"][1],
                    patch_size_px=int(corner_config.get("local_patch_size_px", 47)),
                    minimum_valid_pixels=int(corner_config.get("minimum_valid_pixels", 24)),
                )
            )
            decoded_corner_count = int(np.count_nonzero(decoded_corner_mask))
            object_template = charuco_object_points(configured_board, charuco_ids)
            strict_mask, strict_report = strict_checkerboard_correspondence_mask(
                corners,
                projector_corners,
                decoded_corner_mask,
                None,
                tuple(int(v) for v in manifest["pattern"]["projector_size_px"]),
                planar_object_points=object_template,
                max_camera_grid_residual_px=float(
                    corner_config.get("strict_camera_grid_residual_px", 5.0)
                ),
                max_projector_grid_residual_px=float(
                    corner_config.get("strict_projector_grid_residual_px", 8.0)
                ),
            )
            strict_corner_count = int(np.count_nonzero(strict_mask))
            report["decoded_projector_corner_count"] = decoded_corner_count
            report["strict_validation"] = strict_report
            minimum_decoded_corners = int(
                configured_board.get("minimum_visible_corner_count", 9)
            )
            if strict_corner_count < minimum_decoded_corners:
                raise ValueError(
                    f"{pose_id} 촬영은 완료됐지만 엄격한 기하보정 검사에서 거부되었습니다: "
                    f"해독 {decoded_corner_count}개 중 정상 대응점 {strict_corner_count}개"
                    f"(최소 {minimum_decoded_corners}개). "
                    "판을 완전히 고정하고 위치나 각도를 바꿔 다시 촬영하세요."
                )
            np.save(temporary / "strict_correspondence_mask.npy", strict_mask)
            accepted = session / "poses" / pose_id
            temporary.replace(accepted)
            relative = accepted.relative_to(session).as_posix()
            manifest["captured_poses"].append(
                {
                    "pose_id": pose_id,
                    "captured_at": datetime.now().isoformat(timespec="seconds"),
                    "relative_dir": relative,
                    "checkerboard_detection": report,
                    "target_type": "charuco",
                    "operator_motion_known": False,
                }
            )
            write_json(manifest_path, manifest)
            self.log(
                f"{pose_id}: 기하보정용 pose 승인 및 저장 완료 "
                f"(엄격 검사 정상 대응점 {strict_corner_count}개). "
                "자세를 바꾼 뒤 다시 촬영하세요."
            )
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise
        finally:
            if camera is not None:
                try:
                    exposure_us, gain_db = self._capture_exposure()
                    camera.configure_capture(
                        exposure_us=exposure_us,
                        gain_db=gain_db,
                    )
                except Exception:
                    pass
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
        self.session_var = StringVar(
            value=str(WORKSPACE / "captures" / "charuco_geometry_calibration_session")
        )
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
        stage = cfg["stage_aruco"]
        info = (
            f"ChArUco 보드: {board['squares'][0]}×{board['squares'][1]} squares, "
            f"square {board['square_size_mm']} mm, marker {board['marker_size_mm']} mm  |  "
            f"프로젝터: {projector['width_px']}×{projector['height_px']}, monitor {projector['monitor_index']}\n"
            f"Stage ArUco 검은 외곽 한 변: {stage['marker_size_mm']} mm "
            "(흰색 여백 제외)\n"
            "프로젝터 OFF 프레임에서 고유 ID를 검출하고, 구조광은 주변 백색 영역에서 해독합니다. "
            "촬영 중 보드를 고정하고 완료 후 자세를 바꾸세요."
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
