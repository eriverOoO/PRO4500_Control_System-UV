# PRO4500 XIMEA UV Capture Control

This workspace contains a Windows PRO4500 / LightCrafter 4500 control app and
a structured-light capture controller that uses a XIMEA UV camera through
xiAPI. The previous network/mobile-camera capture path is not used here.

## Main Features

- LightCrafter 4500 Blue LED brightness control over USB HID.
- Pattern projection from a folder of image files.
- Camera abstraction through `CameraInterface` and `CameraProvider`.
- `XimeaUvCamera` implementation using XIMEA xiAPI at runtime.
- `MockCamera` fallback for UI, save-flow, and scan testing without hardware.
- XIMEA camera settings for device index, exposure, gain, trigger mode, FPS,
  image format, timeout, width, and height.
- UV-oriented mono/grayscale capture first, with optional `rgb24` conversion for
  compatibility.
- Preview, single capture, continuous capture, scan capture, image saving, and
  JSON/CSV logs.
- Decoder-ready scan folders with the fixed 0..21 pattern contract:
  White, Black, Gray0..Gray7, Sine_000..Sine_270, and Gray0_inv..Gray7_inv.
- Per-pattern multi-exposure HDR capture. Raw bracket frames are preserved under
  `exposures/`, merged decode images are written as `pattern_000.png` ...
  `pattern_021.png`, and saturated/dark masks are written under `hdr_masks/`.
- Reference/object scan metadata for projector tilt, focus confirmation,
  Scheimpflug confirmation, rig/calibration id, projector brightness, and
  keystone pre-distortion state.

## Files

- `StructuredLightControlPanel.cpp`: native Win32 control panel for LED control,
  projection/capture launch, and camera settings.
- `structured_light_pc_controller.py`: pattern display and camera capture loop.
- `camera_provider.py`: `CameraInterface`, `CameraProvider`, `XimeaUvCamera`,
  and `MockCamera`.
- `camera_config.json`: camera configuration. Edit this instead of hardcoding
  camera values.
- `build_native_control_panel.bat`: builds `StructuredLightControlPanel.exe`.
- `prepare_pc_python_env.ps1`: creates `.venv-pc` and installs Python packages.
- `build.bat` / `PRO4500.cpp`: original compact PRO4500 projection/LED utility.
- `GUI/`: TI LightCrafter 4500 API and HIDAPI sources used for building.
- `generated_patterns/`: sample pattern images for mock and scan testing.

## XIMEA SDK Requirements

Install the XIMEA Windows Software Package that includes:

- XIMEA USB/PCIe camera driver.
- XIMEA CamTool or xiCOP for camera visibility checks.
- xiAPI runtime DLL, usually `xiapi64.dll`.

Before using provider `ximea`, confirm the camera is visible in XIMEA CamTool or
xiCOP. If the controller cannot find the runtime, it exits cleanly with a
message telling you to install the SDK or set `camera.ximea.dll_path` in
`camera_config.json`.

Typical `camera_config.json` XIMEA section:

```json
{
  "camera": {
    "provider": "ximea",
    "ximea": {
      "device_index": 0,
      "dll_path": "",
      "exposure_us": 10000,
      "gain_db": 0.0,
      "fps": 15.0,
      "trigger_mode": "software",
      "image_format": "mono8",
      "timeout_ms": 5000,
      "width": 0,
      "height": 0
    }
  },
  "capture": {
    "hdr": {
      "enabled": true,
      "output_bit_depth": 8,
      "saturated_threshold": 250,
      "dark_threshold": 5,
      "brackets": [
        { "name": "short", "exposure_us": 2500, "gain_db": 0.0 },
        { "name": "mid", "exposure_us": 10000, "gain_db": 0.0 },
        { "name": "long", "exposure_us": 40000, "gain_db": 0.0 }
      ]
    },
    "metadata": {
      "scan_type": "object",
      "projector_tilt_deg": 30.0,
      "focus_confirmed": false,
      "scheimpflug_confirmed": false,
      "rig_id": "",
      "calibration_id": "",
      "projector_brightness": "",
      "keystone_predistortion": false
    }
  }
}
```

If `dll_path` is empty, the controller tries common XIMEA install locations and
then the system `PATH`. You can also set the `XIMEA_XIAPI_DLL` environment
variable to the DLL path.

## Synchronization

The recommended default is software-level synchronization from the master PC:

1. The PC displays one pattern on the projector screen.
2. The PC waits `settle_ms`.
3. The PC sends XIMEA `trigger_software` through xiAPI.
4. The PC waits for `xiGetImage`, saves the frame, logs metadata, then advances
   to the next pattern.

Use this with:

```json
"trigger_mode": "software"
```

If you wire a hardware trigger cable from the master setup/projector trigger
source to the XIMEA camera input, use:

```json
"trigger_mode": "edge_rising"
```

or:

```json
"trigger_mode": "edge_falling"
```

In hardware-trigger mode the controller does not emit `trigger_software`; it
starts acquisition and waits for `xiGetImage` to return the frame after the
external edge arrives. Keep `timeout_ms` long enough for the trigger interval.

Use `trigger_mode: "off"` or `"freerun"` only for preview or non-synchronized
testing.

## Mock Camera

The default config uses:

```json
"provider": "mock"
```

This generates mono gradient frames and lets you test preview, capture, scan
logging, and image saving without a physical camera.

## Setup

1. Install MSYS2 MinGW-w64 if you want to build the native control panel.
2. Prepare Python:

```powershell
powershell -ExecutionPolicy Bypass -File .\prepare_pc_python_env.ps1
```

3. Build the native control panel:

```bat
build_native_control_panel.bat
```

4. Run:

```bat
run_control_panel.bat
```

## Command-Line Examples

Mock single capture:

```powershell
.\.venv-pc\Scripts\python.exe .\structured_light_pc_controller.py --single-capture --camera-provider mock
```

Live preview with XIMEA:

```powershell
.\.venv-pc\Scripts\python.exe .\structured_light_pc_controller.py --preview --camera-provider ximea
```

Check XIMEA SDK/device connection without opening a preview window:

```powershell
.\.venv-pc\Scripts\python.exe .\structured_light_pc_controller.py --check-camera --camera-provider ximea
```

Structured-light scan with XIMEA:

```powershell
.\.venv-pc\Scripts\python.exe .\structured_light_pc_controller.py --camera-provider ximea --patterns .\generated_patterns --output .\captures --angles 0
```

Synthetic no-hardware decoder-folder test:

```powershell
.\.venv-pc\Scripts\python.exe .\structured_light_pc_controller.py --dry-run --patterns .\generated_patterns --output .\captures --scan-type reference --angles 0
```

Reference/object metadata examples:

```powershell
.\.venv-pc\Scripts\python.exe .\structured_light_pc_controller.py --camera-provider ximea --scan-type reference --rig-id uv_rig_01 --calibration-id calib_20260706 --focus-confirmed --scheimpflug-confirmed
.\.venv-pc\Scripts\python.exe .\structured_light_pc_controller.py --camera-provider ximea --scan-type object --rig-id uv_rig_01 --calibration-id calib_20260706 --focus-confirmed --scheimpflug-confirmed
```

Legacy 14-pattern scan for older tools:

```powershell
.\.venv-pc\Scripts\python.exe .\structured_light_pc_controller.py --camera-provider ximea --legacy-14-patterns --patterns .\generated_patterns --output .\captures --angles 0
```

Mock-camera projection/log test:

```powershell
.\.venv-pc\Scripts\python.exe .\structured_light_pc_controller.py --camera-provider mock --patterns .\generated_patterns --output .\captures --angles 0 --windowed
```

## Output

Captures are stored under:

```text
captures/<scan_id>/
```

Scan mode writes:

- Final decoder images named `pattern_000.png` ... `pattern_021.png`.
- Raw bracket frames such as `exposures/pattern_002/short.png`,
  `exposures/pattern_002/mid.png`, and `exposures/pattern_002/long.png`.
- HDR masks such as `hdr_masks/pattern_002_saturated.png` and
  `hdr_masks/pattern_002_dark.png`.
- `scan_log.json`
- `hdr_merge_report.json`
- `scan_log.csv`

`scan_log.json` records the fixed pattern id/label contract, final filenames,
bracket filenames, exposure/gain values, camera frame metadata, HDR thresholds,
merge statistics, and reference/object rig metadata. The controller validates
that every expected final pattern id exists before completing the scan.

Single/continuous capture modes write a capture folder with images and
`capture_log.json`.

## Notes

- `mono8` is the recommended UV default. Use `mono16` when you need higher bit
  depth and downstream tools can handle 16-bit images.
- `trigger_mode` supports `off`, `software`, `edge_rising`, and `edge_falling`.
- FPS control is requested through xiAPI. Some XIMEA models may reject a value
  depending on exposure, ROI, bandwidth, or camera family; this is logged as a
  warning while capture continues when possible.
- The control panel launches the Python controller in a child process and shows
  its log output in the main window.
