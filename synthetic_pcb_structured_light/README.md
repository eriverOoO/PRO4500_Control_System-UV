# Synthetic PCB Structured-Light Dataset

Deterministic Python/OpenCV renderer for PCB height-map decoder verification. It reads the 14 real `1280 x 800` projector BMPs, derives exact Gray-code inverses, and writes 22 independent grayscale PNG files for each of two physical PCB orientations (`0` and `180` degrees). No HDR brackets or contact-sheet images are placed in the decoder input folders.

The camera model defaults to the XIMEA MC023MG-SY-UB / Sony IMX174 geometry requested by the project: `1936 x 1216`, 5.86 um pixels, 35 mm lens, monochrome global shutter. Images are rendered from a shared albedo/height scene. Height changes projector coordinates, Lambertian shading, and projector visibility; the implementation does not alpha-blend a 2D stripe image over a photograph.

## Setup

Python 3.12 is required.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Generate

Run commands from this directory.

```powershell
.\.venv\Scripts\python.exe -m synthetic_pcb_sl.cli generate --patterns-dir .\patterns --output-dir .\output --config .\configs\default.yaml
```

This produces `angle_000/pattern_000.png` through `pattern_021.png`, the same set under `angle_180`, ground-truth height/normal files, `manifest.json`, and a copy of the effective configuration. Images are 16-bit grayscale PNG by default.

Generate one angle or one frame:

```powershell
.\.venv\Scripts\python.exe -m synthetic_pcb_sl.cli generate --angle 0
.\.venv\Scripts\python.exe -m synthetic_pcb_sl.cli generate --angle 0 --pattern-index 0
```

Use `--bit-depth 8` for 8-bit output. The renderer never applies auto-exposure, auto-gamma, frame-dependent focus, camera motion, or frame-dependent PCB material changes.

Generate a reference scan of the uniform, flat stage with the PCB removed. The
camera/projector coordinate system remains identical to the object scan:

```powershell
.\.venv\Scripts\python.exe -m synthetic_pcb_sl.cli generate --patterns-dir .\patterns --output-dir .\output\reference --config .\configs\default.yaml --empty-stage-reference
```

This writes decoder-ready `reference/angle_000` and `reference/angle_180` folders. Use the matching angle reference before 0/180 fusion; do not reuse an object absolute phase as its own reference.
The reference frames contain only the projected patterns on a uniform matte stage;
no PCB mask, PCB texture, pads, traces, or components are retained.

## Validate and preview

```powershell
.\.venv\Scripts\python.exe -m synthetic_pcb_sl.cli validate --output-dir .\output
.\.venv\Scripts\python.exe -m synthetic_pcb_sl.cli preview --output-dir .\output
.\.venv\Scripts\python.exe -m pytest
```

Validation checks the exact 44-file mapping, shape/dtype consistency, White/Black ordering, Gray inverse complementarity, opposite sine phases, finite values, and the true 180-degree ground-truth rotation. Contrast, modulation, and wrapped-phase previews are written only under `output/diagnostics`. Contact sheets are written only under `output/preview`.

## Scene assets

If `assets/pcb_albedo.png`, `assets/pcb_height.png`, and `assets/pcb_mask.png` exist, they are loaded as the shared scene. Otherwise the first generation creates a deterministic procedural PCB using seed `20260720` and saves those files for repeatability. Height PNG values map linearly from zero to `pcb.max_component_height_mm`; floating-point millimetre ground truth is stored as `.npy`.

For angle 180 the PCB albedo, height, mask, material, and component maps are rotated first. Camera, lens, stage, projector pose, and sensor model remain fixed, and projection and occlusion are rendered again from the rotated geometry.
