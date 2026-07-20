"""Dataset orchestration, file layout, manifests, and previews."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np
import yaml

from .geometry import surface_normals, world_grid
from .pattern_loader import PatternFrame, load_patterns
from .pcb_scene import PcbScene, create_scene
from .renderer import prepare_context, render_frame


def load_config(path: Path) -> dict[str, Any]:
    """Load YAML and validate the few values that affect file representation."""

    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if int(config["render"]["bit_depth"]) not in (8, 16):
        raise ValueError("render.bit_depth must be 8 or 16")
    return config


def write_png(path: Path, image: np.ndarray) -> None:
    """Write PNG through imencode so non-ASCII Windows paths are supported."""

    path.parent.mkdir(parents=True, exist_ok=True)
    ok, payload = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError(f"Could not encode {path}")
    payload.tofile(path)


def read_png(path: Path) -> np.ndarray:
    payload = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(payload, cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError(f"Could not read {path}")
    return image


def _hash_array(array: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()


def _save_scene_assets(scene: PcbScene, assets_dir: Path, max_height_mm: float) -> None:
    assets_dir.mkdir(parents=True, exist_ok=True)
    write_png(assets_dir / "pcb_albedo.png", np.rint(scene.albedo * 255).astype(np.uint8))
    height_u16 = np.rint(np.clip(scene.height_mm / max_height_mm, 0, 1) * 65535).astype(np.uint16)
    write_png(assets_dir / "pcb_height.png", height_u16)
    write_png(assets_dir / "pcb_mask.png", scene.mask.astype(np.uint8) * 255)


def _save_ground_truth(scene: PcbScene, output_dir: Path, angle: int, config: dict[str, Any]) -> None:
    gt = output_dir / "ground_truth"
    gt.mkdir(parents=True, exist_ok=True)
    np.save(gt / f"angle_{angle:03d}_height_mm.npy", scene.height_mm.astype(np.float32))
    max_height = float(config["pcb"]["max_component_height_mm"])
    write_png(
        gt / f"angle_{angle:03d}_height.png",
        np.rint(np.clip(scene.height_mm / max_height, 0, 1) * 65535).astype(np.uint16),
    )
    if angle == 0:
        write_png(gt / "albedo.png", np.rint(scene.albedo * 65535).astype(np.uint16))
        write_png(gt / "valid_mask.png", scene.mask.astype(np.uint8) * 255)
        _, _, dx, dy = world_grid(config, scene.mask)
        np.save(gt / "normals.npy", surface_normals(scene.height_mm, dx, dy))


def generate_dataset(
    patterns_dir: Path,
    output_dir: Path,
    config_path: Path,
    *,
    angle: int | None = None,
    pattern_index: int | None = None,
    assets_dir: Path | None = None,
) -> dict[str, Any]:
    """Generate all 44 images or a deterministic requested subset."""

    if angle not in (None, 0, 180):
        raise ValueError("angle must be 0 or 180")
    if pattern_index is not None and pattern_index not in range(22):
        raise ValueError("pattern-index must be in 0..21")

    config = load_config(config_path)
    patterns = load_patterns(patterns_dir)
    selected_patterns = [p for p in patterns if pattern_index is None or p.index == pattern_index]
    selected_angles = [angle] if angle is not None else [0, 180]
    project_assets = assets_dir or config_path.parent.parent / "assets"
    base_scene = create_scene(config, project_assets)
    if not all((project_assets / name).exists() for name in ("pcb_albedo.png", "pcb_height.png", "pcb_mask.png")):
        _save_scene_assets(base_scene, project_assets, float(config["pcb"]["max_component_height_mm"]))
        # Render from the persisted representation so the first and later runs are identical.
        base_scene = create_scene(config, project_assets)

    output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(config_path, output_dir / "generation_config.yaml")
    entries: list[dict[str, Any]] = []
    angle_records: dict[str, Any] = {}
    for angle_value in selected_angles:
        scene = base_scene if angle_value == 0 else base_scene.rotated_180()
        context = prepare_context(scene, config)
        angle_dir = output_dir / f"angle_{angle_value:03d}"
        angle_dir.mkdir(parents=True, exist_ok=True)
        _save_ground_truth(scene, output_dir, angle_value, config)
        angle_records[str(angle_value)] = {
            "height_sha256": _hash_array(scene.height_mm),
            "albedo_sha256": _hash_array(scene.albedo),
            "mask_sha256": _hash_array(scene.mask),
        }
        for pattern in selected_patterns:
            image = render_frame(scene, context, pattern, config, angle_value)
            filename = f"pattern_{pattern.index:03d}.png"
            path = angle_dir / filename
            write_png(path, image)
            entries.append(
                {
                    "angle_deg": angle_value,
                    "pattern_index": pattern.index,
                    "label": pattern.label,
                    "source_pattern": pattern.source_name,
                    "inverse": pattern.inverse,
                    "filename": path.relative_to(output_dir).as_posix(),
                    "dtype": str(image.dtype),
                    "shape": list(image.shape),
                }
            )

    manifest = {
        "schema_version": 1,
        "generator": "synthetic_pcb_sl",
        "seed": int(config["seed"]),
        "patterns_dir": str(patterns_dir.resolve()),
        "bit_depth": int(config["render"]["bit_depth"]),
        "camera": config["camera"],
        "projector": config["projector"],
        "angles": selected_angles,
        "angle_geometry": angle_records,
        "frames": entries,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def make_contact_sheet(images: Iterable[np.ndarray], labels: Iterable[str], columns: int = 6) -> np.ndarray:
    """Create an 8-bit labelled diagnostic montage, never used as decoder input."""

    tiles: list[np.ndarray] = []
    for image, label in zip(images, labels, strict=True):
        if image.dtype == np.uint16:
            tile = (image / 257).astype(np.uint8)
        else:
            tile = image.astype(np.uint8)
        scale = 240 / tile.shape[1]
        tile = cv2.resize(tile, (240, max(1, int(tile.shape[0] * scale))), interpolation=cv2.INTER_AREA)
        tile = cv2.cvtColor(tile, cv2.COLOR_GRAY2BGR)
        cv2.putText(tile, label, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (0, 255, 0), 1, cv2.LINE_AA)
        tiles.append(tile)
    if not tiles:
        raise ValueError("No images for contact sheet")
    tile_h, tile_w = tiles[0].shape[:2]
    rows = (len(tiles) + columns - 1) // columns
    sheet = np.zeros((rows * tile_h, columns * tile_w, 3), dtype=np.uint8)
    for index, tile in enumerate(tiles):
        y, x = divmod(index, columns)
        sheet[y * tile_h : (y + 1) * tile_h, x * tile_w : (x + 1) * tile_w] = tile
    return sheet


def generate_previews(output_dir: Path) -> list[Path]:
    """Generate contact sheets for every complete angle directory."""

    saved: list[Path] = []
    preview_dir = output_dir / "preview"
    for angle in (0, 180):
        paths = [output_dir / f"angle_{angle:03d}" / f"pattern_{index:03d}.png" for index in range(22)]
        if not all(path.is_file() for path in paths):
            continue
        images = [read_png(path) for path in paths]
        sheet = make_contact_sheet(images, [f"{index:03d}" for index in range(22)])
        destination = preview_dir / f"contact_sheet_angle_{angle:03d}.png"
        write_png(destination, sheet)
        saved.append(destination)
    return saved
