#!/usr/bin/env python3
"""Camera abstraction for PRO4500 structured-light capture."""

from __future__ import annotations

import ctypes
import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class CameraError(RuntimeError):
    """Raised when a camera cannot be initialized or cannot deliver a frame."""


@dataclass
class CameraFrame:
    image: Any
    timestamp_ms: int
    frame_index: int
    pixel_format: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CameraSettings:
    provider: str = "mock"
    device_index: int = 0
    dll_path: str = ""
    exposure_us: int = 10000
    gain_db: float = 0.0
    fps: float = 15.0
    trigger_mode: str = "software"
    image_format: str = "mono8"
    timeout_ms: int = 5000
    width: int = 0
    height: int = 0
    sample_image: str = ""
    mock_width: int = 1280
    mock_height: int = 720

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "device_index": self.device_index,
            "dll_path": self.dll_path,
            "exposure_us": self.exposure_us,
            "gain_db": self.gain_db,
            "fps": self.fps,
            "trigger_mode": self.trigger_mode,
            "image_format": self.image_format,
            "timeout_ms": self.timeout_ms,
            "width": self.width,
            "height": self.height,
            "sample_image": self.sample_image,
            "mock_width": self.mock_width,
            "mock_height": self.mock_height,
        }


class CameraInterface(ABC):
    def __init__(self, settings: CameraSettings) -> None:
        self.settings = settings
        self.warnings: list[str] = []

    @abstractmethod
    def open(self) -> None:
        pass

    @abstractmethod
    def start(self) -> None:
        pass

    @abstractmethod
    def capture_frame(self) -> CameraFrame:
        pass

    @abstractmethod
    def stop(self) -> None:
        pass

    @abstractmethod
    def close(self) -> None:
        pass

    @abstractmethod
    def describe(self) -> str:
        pass

    def __enter__(self) -> "CameraInterface":
        self.open()
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        try:
            self.stop()
        finally:
            self.close()


class CameraProvider:
    @staticmethod
    def load_settings(config_path: Path | None, overrides: dict[str, Any] | None = None) -> CameraSettings:
        overrides = {key: value for key, value in (overrides or {}).items() if value is not None}
        config_path = config_path or Path("camera_config.json")
        config_dir = config_path.resolve().parent
        config: dict[str, Any] = {}

        if config_path.exists():
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise CameraError(f"Invalid camera config JSON: {config_path} ({exc})") from exc

        camera_config = config.get("camera", {})
        provider = str(overrides.get("provider") or camera_config.get("provider") or "mock").lower()
        section = camera_config.get(provider, {})
        ximea_section = camera_config.get("ximea", {})

        def value(name: str, default: Any, *, source: dict[str, Any] | None = None) -> Any:
            if name in overrides:
                return overrides[name]
            data = section if source is None else source
            return data.get(name, default)

        def resolved_path(raw: str) -> str:
            if not raw:
                return ""
            path = Path(raw)
            if not path.is_absolute():
                path = config_dir / path
            return str(path)

        settings = CameraSettings(
            provider=provider,
            device_index=int(value("device_index", ximea_section.get("device_index", 0), source=ximea_section)),
            dll_path=resolved_path(str(value("dll_path", ximea_section.get("dll_path", ""), source=ximea_section))),
            exposure_us=int(value("exposure_us", ximea_section.get("exposure_us", 10000), source=ximea_section)),
            gain_db=float(value("gain_db", ximea_section.get("gain_db", 0.0), source=ximea_section)),
            fps=float(value("fps", ximea_section.get("fps", 15.0), source=ximea_section)),
            trigger_mode=str(value("trigger_mode", ximea_section.get("trigger_mode", "software"), source=ximea_section)).lower(),
            image_format=str(value("image_format", ximea_section.get("image_format", "mono8"), source=ximea_section)).lower(),
            timeout_ms=int(value("timeout_ms", ximea_section.get("timeout_ms", 5000), source=ximea_section)),
            width=int(value("width", ximea_section.get("width", 0), source=ximea_section)),
            height=int(value("height", ximea_section.get("height", 0), source=ximea_section)),
            sample_image=resolved_path(str(value("sample_image", section.get("sample_image", "")))),
            mock_width=int(value("mock_width", section.get("width", 1280))),
            mock_height=int(value("mock_height", section.get("height", 720))),
        )
        return settings

    @staticmethod
    def create(settings: CameraSettings) -> CameraInterface:
        if settings.provider == "ximea":
            return XimeaUvCamera(settings)
        if settings.provider == "mock":
            return MockCamera(settings)
        raise CameraError(
            f"Unknown camera provider '{settings.provider}'. Use 'ximea' or 'mock'."
        )


class MockCamera(CameraInterface):
    def __init__(self, settings: CameraSettings) -> None:
        super().__init__(settings)
        self._opened = False
        self._started = False
        self._frame_index = 0
        self._sample: Any | None = None

    def open(self) -> None:
        if self.settings.sample_image:
            import cv2  # type: ignore

            sample = cv2.imread(self.settings.sample_image, cv2.IMREAD_GRAYSCALE)
            if sample is None:
                raise CameraError(f"Mock sample image could not be read: {self.settings.sample_image}")
            self._sample = sample
        self._opened = True

    def start(self) -> None:
        if not self._opened:
            raise CameraError("Mock camera was not opened")
        self._started = True

    def capture_frame(self) -> CameraFrame:
        if not self._started:
            raise CameraError("Mock camera acquisition is not running")

        import numpy as np  # type: ignore

        if self._sample is not None:
            image = self._sample.copy()
        else:
            width = max(1, int(self.settings.mock_width))
            height = max(1, int(self.settings.mock_height))
            x = np.linspace(0, 255, width, dtype=np.uint16)
            y = np.linspace(0, 255, height, dtype=np.uint16)[:, None]
            image = ((x + y + self._frame_index * 9) % 256).astype(np.uint8)

        frame = CameraFrame(
            image=image,
            timestamp_ms=int(time.time() * 1000),
            frame_index=self._frame_index,
            pixel_format="mono8",
            metadata={"provider": "mock"},
        )
        self._frame_index += 1
        if self.settings.fps > 0:
            time.sleep(min(0.2, 1.0 / self.settings.fps))
        return frame

    def stop(self) -> None:
        self._started = False

    def close(self) -> None:
        self._opened = False

    def describe(self) -> str:
        return f"mock camera {self.settings.mock_width}x{self.settings.mock_height}"


class XI_IMG(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint32),
        ("bp", ctypes.c_void_p),
        ("bp_size", ctypes.c_uint32),
        ("frm", ctypes.c_uint32),
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("nframe", ctypes.c_uint32),
        ("tsSec", ctypes.c_uint32),
        ("tsUSec", ctypes.c_uint32),
        ("GPI_level", ctypes.c_uint32),
        ("black_level", ctypes.c_uint32),
        ("padding_x", ctypes.c_uint32),
        ("AbsoluteOffsetX", ctypes.c_uint32),
        ("AbsoluteOffsetY", ctypes.c_uint32),
        ("transport_frm", ctypes.c_uint32),
    ]


class XimeaUvCamera(CameraInterface):
    XI_OK = 0
    XI_MONO8 = 0
    XI_MONO16 = 1
    XI_RGB24 = 2
    XI_TRG_OFF = 0
    XI_TRG_EDGE_RISING = 1
    XI_TRG_EDGE_FALLING = 2
    XI_TRG_SOFTWARE = 3
    XI_ACQ_TIMING_MODE_FREE_RUN = 0
    XI_ACQ_TIMING_MODE_FRAME_RATE = 1
    XI_ACQ_TIMING_MODE_FRAME_RATE_LIMIT = 2

    IMAGE_FORMATS = {
        "mono8": XI_MONO8,
        "mono16": XI_MONO16,
        "rgb24": XI_RGB24,
    }

    TRIGGER_MODES = {
        "off": XI_TRG_OFF,
        "freerun": XI_TRG_OFF,
        "free_run": XI_TRG_OFF,
        "software": XI_TRG_SOFTWARE,
        "edge_rising": XI_TRG_EDGE_RISING,
        "rising": XI_TRG_EDGE_RISING,
        "edge_falling": XI_TRG_EDGE_FALLING,
        "falling": XI_TRG_EDGE_FALLING,
    }

    def __init__(self, settings: CameraSettings) -> None:
        super().__init__(settings)
        self._lib: Any | None = None
        self._handle = ctypes.c_void_p()
        self._started = False
        self._opened = False
        self._device_count = 0

    def open(self) -> None:
        self._lib = self._load_library()
        self._bind_functions(self._lib)

        count = ctypes.c_uint32(0)
        self._check(self._lib.xiGetNumberDevices(ctypes.byref(count)), "xiGetNumberDevices")
        self._device_count = int(count.value)
        if self._device_count <= 0:
            raise CameraError(
                "No XIMEA camera was detected. Check USB/PCIe connection, power, driver installation, and xiCOP/CamTool visibility."
            )
        if self.settings.device_index < 0 or self.settings.device_index >= self._device_count:
            raise CameraError(
                f"XIMEA device_index {self.settings.device_index} is out of range. Detected devices: {self._device_count}."
            )

        self._check(
            self._lib.xiOpenDevice(ctypes.c_uint32(self.settings.device_index), ctypes.byref(self._handle)),
            "xiOpenDevice",
        )
        self._opened = True
        try:
            self._configure_device()
        except Exception:
            self.close()
            raise

    def start(self) -> None:
        self._require_open()
        self._check(self._lib.xiStartAcquisition(self._handle), "xiStartAcquisition")
        self._started = True

    def capture_frame(self) -> CameraFrame:
        self._require_started()
        if self.settings.trigger_mode in {"software"}:
            self._check(
                self._lib.xiSetParamInt(self._handle, b"trigger_software", 1),
                "xiSetParamInt(trigger_software)",
            )

        image = XI_IMG()
        image.size = ctypes.sizeof(XI_IMG)
        self._check(
            self._lib.xiGetImage(self._handle, ctypes.c_uint32(max(1, self.settings.timeout_ms)), ctypes.byref(image)),
            "xiGetImage",
        )
        if not image.bp:
            raise CameraError("XIMEA xiGetImage returned an empty image buffer")

        frame_bytes = ctypes.string_at(image.bp, max(0, int(image.bp_size)))
        array = self._image_bytes_to_array(frame_bytes, image)
        timestamp_ms = int(image.tsSec) * 1000 + int(image.tsUSec) // 1000
        if timestamp_ms <= 0:
            timestamp_ms = int(time.time() * 1000)

        return CameraFrame(
            image=array,
            timestamp_ms=timestamp_ms,
            frame_index=int(image.nframe),
            pixel_format=self.settings.image_format,
            metadata={
                "provider": "ximea",
                "width": int(image.width),
                "height": int(image.height),
                "padding_x": int(image.padding_x),
                "black_level": int(image.black_level),
                "transport_format": int(image.transport_frm),
            },
        )

    def stop(self) -> None:
        if self._lib is not None and self._opened and self._started:
            code = self._lib.xiStopAcquisition(self._handle)
            if code != self.XI_OK:
                self.warnings.append(f"xiStopAcquisition failed with code {code}")
        self._started = False

    def close(self) -> None:
        if self._lib is not None and self._opened:
            code = self._lib.xiCloseDevice(self._handle)
            if code != self.XI_OK:
                self.warnings.append(f"xiCloseDevice failed with code {code}")
        self._handle = ctypes.c_void_p()
        self._opened = False

    def describe(self) -> str:
        return (
            f"XIMEA xiAPI device_index={self.settings.device_index} "
            f"devices={self._device_count} format={self.settings.image_format}"
        )

    def _configure_device(self) -> None:
        fmt = self.IMAGE_FORMATS.get(self.settings.image_format)
        if fmt is None:
            raise CameraError(
                f"Unsupported XIMEA image_format '{self.settings.image_format}'. Use mono8, mono16, or rgb24."
            )

        self._check(
            self._lib.xiSetParamInt(self._handle, b"imgdataformat", int(fmt)),
            "xiSetParamInt(imgdataformat)",
        )
        if self.settings.width > 0:
            self._try_set_int("width", self.settings.width)
        if self.settings.height > 0:
            self._try_set_int("height", self.settings.height)

        self._try_set_int("exposure", self.settings.exposure_us)
        self._try_set_float("gain", self.settings.gain_db)

        trigger = self.TRIGGER_MODES.get(self.settings.trigger_mode)
        if trigger is None:
            raise CameraError(
                f"Unsupported XIMEA trigger_mode '{self.settings.trigger_mode}'. Use off, software, edge_rising, or edge_falling."
            )
        self._try_set_int("trigger_source", int(trigger))

        if self.settings.fps > 0:
            if not self._try_set_int("acq_timing_mode", self.XI_ACQ_TIMING_MODE_FRAME_RATE):
                self._try_set_int("acq_timing_mode", self.XI_ACQ_TIMING_MODE_FRAME_RATE_LIMIT)
            self._try_set_float("framerate", self.settings.fps)

    def _image_bytes_to_array(self, frame_bytes: bytes, image: XI_IMG) -> Any:
        import numpy as np  # type: ignore

        width = max(1, int(image.width))
        height = max(1, int(image.height))
        padding = max(0, int(image.padding_x))
        fmt = self.settings.image_format

        if fmt == "mono16":
            dtype = np.uint16
            channels = 1
            bytes_per_pixel = 2
        elif fmt == "rgb24":
            dtype = np.uint8
            channels = 3
            bytes_per_pixel = 3
        else:
            dtype = np.uint8
            channels = 1
            bytes_per_pixel = 1

        row_bytes = width * channels * bytes_per_pixel + padding
        expected = row_bytes * height
        if len(frame_bytes) < expected:
            expected = len(frame_bytes)
        raw = np.frombuffer(frame_bytes[:expected], dtype=np.uint8)
        if raw.size < row_bytes * height:
            raise CameraError(
                f"XIMEA frame buffer is smaller than expected ({raw.size} bytes for {width}x{height})."
            )

        rows = raw.reshape(height, row_bytes)
        active = rows[:, : width * channels * bytes_per_pixel]
        if dtype == np.uint16:
            array = active.reshape(height, width, bytes_per_pixel).copy().view(np.uint16).reshape(height, width)
        elif channels == 3:
            rgb = active.reshape(height, width, 3).copy()
            array = rgb[:, :, ::-1]
        else:
            array = active.reshape(height, width).copy()
        return array

    def _try_set_int(self, name: str, value: int) -> bool:
        code = self._lib.xiSetParamInt(self._handle, name.encode("ascii"), int(value))
        if code != self.XI_OK:
            self.warnings.append(f"xiSetParamInt({name}={value}) failed with code {code}")
            return False
        return True

    def _try_set_float(self, name: str, value: float) -> bool:
        code = self._lib.xiSetParamFloat(self._handle, name.encode("ascii"), ctypes.c_float(float(value)))
        if code != self.XI_OK:
            self.warnings.append(f"xiSetParamFloat({name}={value}) failed with code {code}")
            return False
        return True

    def _require_open(self) -> None:
        if self._lib is None or not self._opened:
            raise CameraError("XIMEA camera is not opened")

    def _require_started(self) -> None:
        self._require_open()
        if not self._started:
            raise CameraError("XIMEA camera acquisition is not running")

    def _check(self, code: int, operation: str) -> None:
        if int(code) != self.XI_OK:
            raise CameraError(f"XIMEA xiAPI {operation} failed with code {int(code)}")

    def _bind_functions(self, lib: Any) -> None:
        required = [
            "xiGetNumberDevices",
            "xiOpenDevice",
            "xiCloseDevice",
            "xiStartAcquisition",
            "xiStopAcquisition",
            "xiGetImage",
            "xiSetParamInt",
            "xiSetParamFloat",
        ]
        missing = [name for name in required if not hasattr(lib, name)]
        if missing:
            raise CameraError(f"XIMEA xiAPI runtime is missing required functions: {', '.join(missing)}")

        lib.xiGetNumberDevices.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
        lib.xiGetNumberDevices.restype = ctypes.c_int
        lib.xiOpenDevice.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        lib.xiOpenDevice.restype = ctypes.c_int
        lib.xiCloseDevice.argtypes = [ctypes.c_void_p]
        lib.xiCloseDevice.restype = ctypes.c_int
        lib.xiStartAcquisition.argtypes = [ctypes.c_void_p]
        lib.xiStartAcquisition.restype = ctypes.c_int
        lib.xiStopAcquisition.argtypes = [ctypes.c_void_p]
        lib.xiStopAcquisition.restype = ctypes.c_int
        lib.xiGetImage.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(XI_IMG)]
        lib.xiGetImage.restype = ctypes.c_int
        lib.xiSetParamInt.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        lib.xiSetParamInt.restype = ctypes.c_int
        lib.xiSetParamFloat.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_float]
        lib.xiSetParamFloat.restype = ctypes.c_int

    def _load_library(self) -> Any:
        errors: list[str] = []
        for candidate in self._library_candidates():
            try:
                return ctypes.CDLL(str(candidate))
            except OSError as exc:
                errors.append(f"{candidate}: {exc}")

        detail = "\n".join(errors[:8])
        raise CameraError(
            "XIMEA xiAPI runtime was not found. Install the XIMEA Windows Software Package, "
            "confirm XIMEA CamTool/xiCOP can see the camera, and either add xiapi64.dll to PATH "
            "or set camera.ximea.dll_path in camera_config.json."
            + (f"\nTried:\n{detail}" if detail else "")
        )

    def _library_candidates(self) -> list[Path | str]:
        names = ["xiapi64.dll", "xiapi.dll", "xiapi32.dll"] if os.name == "nt" else ["libm3api.so"]
        candidates: list[Path | str] = []

        explicit = self.settings.dll_path or os.environ.get("XIMEA_XIAPI_DLL", "")
        if explicit:
            path = Path(explicit)
            if path.is_dir():
                candidates.extend(path / name for name in names)
            else:
                candidates.append(path)

        if os.name == "nt":
            for root in [
                Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
                Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
                Path(r"C:\XIMEA"),
            ]:
                candidates.extend(
                    [
                        root / "API" / "xiAPI" / "xiapi64.dll",
                        root / "XIMEA" / "API" / "x64" / "xiapi64.dll",
                        root / "XIMEA" / "API" / "x86" / "xiapi32.dll",
                        root / "API" / "x64" / "xiapi64.dll",
                        root / "API" / "x86" / "xiapi32.dll",
                    ]
                )

        candidates.extend(names)
        return candidates
