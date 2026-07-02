#!/usr/bin/env python3
"""Print basic XIMEA camera information through xiAPI."""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path


XI_OK = 0
XI_RESOURCE_OR_FUNCTION_LOCKED = 57

DLL_CANDIDATES = [
    r"C:\XIMEA\API\xiAPI\xiapi64.dll",
    r"C:\XIMEA\XIMEACamTool64\xiapi64.dll",
    r"C:\XIMEA\xiCOP\xiapi64.dll",
    r"C:\XIMEA\API\Python\v3\ximea\libs\windows\64bit\xiapi64.dll",
    r"C:\XIMEA\API\Python\v2\ximea\libs\windows\64bit\xiapi64.dll",
    "xiapi64.dll",
]

STRING_INFO = [
    "device_name",
    "device_type",
    "device_sn",
    "device_sens_sn",
    "device_inst_path",
    "device_loc_path",
    "device_user_id",
]

INT_INFO = [
    "device_model_id",
    "sensor_model_id",
]

OPEN_STRING_PARAMS = [
    "device_name",
    "device_type",
    "device_sn",
    "api_version",
    "drv_version",
    "hw_revision",
    "lens_model_name",
    "lens_serial_number",
]

OPEN_INT_PARAMS = [
    "width",
    "height",
    "width:max",
    "height:max",
    "width_total",
    "height_total",
    "sensor_bit_depth",
    "sensor_model_id",
    "device_model_id",
]

OPEN_FLOAT_PARAMS = [
    "exposure:min",
    "exposure:max",
    "gain:min",
    "gain:max",
    "framerate:min",
    "framerate:max",
]


def load_xiapi() -> ctypes.CDLL:
    explicit = os.environ.get("XIMEA_XIAPI_DLL", "")
    candidates = [explicit] if explicit else []
    candidates.extend(DLL_CANDIDATES)
    errors: list[str] = []

    for candidate in candidates:
        if not candidate:
            continue
        try:
            return ctypes.CDLL(candidate)
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")

    raise RuntimeError("Could not load xiAPI DLL:\n" + "\n".join(errors))


def bind(lib: ctypes.CDLL) -> None:
    lib.xiGetNumberDevices.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
    lib.xiGetNumberDevices.restype = ctypes.c_int
    lib.xiGetDeviceInfoInt.argtypes = [ctypes.c_uint32, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
    lib.xiGetDeviceInfoInt.restype = ctypes.c_int
    lib.xiGetDeviceInfoString.argtypes = [ctypes.c_uint32, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    lib.xiGetDeviceInfoString.restype = ctypes.c_int
    lib.xiOpenDevice.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    lib.xiOpenDevice.restype = ctypes.c_int
    lib.xiCloseDevice.argtypes = [ctypes.c_void_p]
    lib.xiCloseDevice.restype = ctypes.c_int
    lib.xiGetParamInt.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
    lib.xiGetParamInt.restype = ctypes.c_int
    lib.xiGetParamFloat.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_float)]
    lib.xiGetParamFloat.restype = ctypes.c_int
    lib.xiGetParamString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    lib.xiGetParamString.restype = ctypes.c_int


def device_info_string(lib: ctypes.CDLL, index: int, param: str) -> str | None:
    buffer = ctypes.create_string_buffer(1024)
    code = lib.xiGetDeviceInfoString(index, param.encode("ascii"), buffer, ctypes.sizeof(buffer))
    if code != XI_OK:
        return None
    return buffer.value.decode("utf-8", errors="replace")


def device_info_int(lib: ctypes.CDLL, index: int, param: str) -> int | None:
    value = ctypes.c_int()
    code = lib.xiGetDeviceInfoInt(index, param.encode("ascii"), ctypes.byref(value))
    if code != XI_OK:
        return None
    return int(value.value)


def param_string(lib: ctypes.CDLL, handle: ctypes.c_void_p, param: str) -> str | None:
    buffer = ctypes.create_string_buffer(1024)
    code = lib.xiGetParamString(handle, param.encode("ascii"), buffer, ctypes.sizeof(buffer))
    if code != XI_OK:
        return None
    return buffer.value.decode("utf-8", errors="replace")


def param_int(lib: ctypes.CDLL, handle: ctypes.c_void_p, param: str) -> int | None:
    value = ctypes.c_int()
    code = lib.xiGetParamInt(handle, param.encode("ascii"), ctypes.byref(value))
    if code != XI_OK:
        return None
    return int(value.value)


def param_float(lib: ctypes.CDLL, handle: ctypes.c_void_p, param: str) -> float | None:
    value = ctypes.c_float()
    code = lib.xiGetParamFloat(handle, param.encode("ascii"), ctypes.byref(value))
    if code != XI_OK:
        return None
    return float(value.value)


def main() -> int:
    lib = load_xiapi()
    bind(lib)
    count = ctypes.c_uint32()
    code = lib.xiGetNumberDevices(ctypes.byref(count))
    if code != XI_OK:
        print(json.dumps({"error": f"xiGetNumberDevices failed: {code}"}, indent=2))
        return 1

    result: dict[str, object] = {
        "xiapi_dll": str(Path(getattr(lib, "_name", "")).resolve()),
        "device_count": int(count.value),
        "devices": [],
    }

    devices: list[dict[str, object]] = []
    for index in range(int(count.value)):
        info: dict[str, object] = {"index": index, "device_info": {}, "open_params": {}}
        for param in STRING_INFO:
            value = device_info_string(lib, index, param)
            if value:
                info["device_info"][param] = value
        for param in INT_INFO:
            value = device_info_int(lib, index, param)
            if value is not None:
                info["device_info"][param] = value

        handle = ctypes.c_void_p()
        open_code = lib.xiOpenDevice(index, ctypes.byref(handle))
        info["open_code"] = int(open_code)
        if open_code == XI_OK:
            for param in OPEN_STRING_PARAMS:
                value = param_string(lib, handle, param)
                if value:
                    info["open_params"][param] = value
            for param in OPEN_INT_PARAMS:
                value = param_int(lib, handle, param)
                if value is not None:
                    info["open_params"][param] = value
            for param in OPEN_FLOAT_PARAMS:
                value = param_float(lib, handle, param)
                if value is not None:
                    info["open_params"][param] = value
            lib.xiCloseDevice(handle)
        elif open_code == XI_RESOURCE_OR_FUNCTION_LOCKED:
            info["open_error"] = "XI_RESOURCE_OR_FUNCTION_LOCKED: close XIMEA CamTool/xiCOP and retry for full parameters."
        else:
            info["open_error"] = f"xiOpenDevice failed: {open_code}"

        devices.append(info)
    result["devices"] = devices
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
