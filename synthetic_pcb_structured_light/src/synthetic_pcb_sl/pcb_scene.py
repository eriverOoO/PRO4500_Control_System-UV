"""Create or load one deterministic PCB reflectance and height scene."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np


@dataclass(frozen=True)
class PcbScene:
    """Camera-grid PCB properties shared by every rendered pattern."""

    albedo: np.ndarray
    height_mm: np.ndarray
    mask: np.ndarray
    material: np.ndarray
    components: np.ndarray

    def rotated_180(self) -> "PcbScene":
        return PcbScene(*(np.rot90(item, 2).copy() for item in (
            self.albedo, self.height_mm, self.mask, self.material, self.components
        )))


def _rectangle(array: np.ndarray, center: tuple[int, int], size: tuple[int, int], value: Any) -> None:
    cx, cy = center
    width, height = size
    x0, x1 = max(0, cx - width // 2), min(array.shape[1], cx + (width + 1) // 2)
    y0, y1 = max(0, cy - height // 2), min(array.shape[0], cy + (height + 1) // 2)
    array[y0:y1, x0:x1] = value


def _load_optional_assets(assets_dir: Path, shape: tuple[int, int], max_height: float) -> PcbScene | None:
    paths = [assets_dir / name for name in ("pcb_albedo.png", "pcb_height.png", "pcb_mask.png")]
    if not all(path.is_file() for path in paths):
        return None
    albedo = cv2.imread(str(paths[0]), cv2.IMREAD_GRAYSCALE)
    height_raw = cv2.imread(str(paths[1]), cv2.IMREAD_UNCHANGED)
    mask = cv2.imread(str(paths[2]), cv2.IMREAD_GRAYSCALE)
    if albedo is None or height_raw is None or mask is None:
        raise ValueError("Could not read PCB assets")
    albedo = cv2.resize(albedo, shape[::-1], interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    height_raw = cv2.resize(height_raw, shape[::-1], interpolation=cv2.INTER_NEAREST)
    height_mm = height_raw.astype(np.float32) / float(np.iinfo(height_raw.dtype).max) * max_height
    mask = cv2.resize(mask, shape[::-1], interpolation=cv2.INTER_NEAREST) > 0
    material = np.where(mask, 1, 0).astype(np.uint8)
    components = (height_mm > 0.05).astype(np.uint8)
    return PcbScene(albedo, height_mm, mask, material, components)


def create_scene(config: dict[str, Any], assets_dir: Path | None = None) -> PcbScene:
    """Build a repeatable PCB scene on the final camera pixel grid."""

    camera = config["camera"]
    pcb = config["pcb"]
    image_h, image_w = int(camera["height"]), int(camera["width"])
    fill = float(pcb["frame_fill_ratio"])
    board_px = max(32, int(min(image_w, image_h) * fill))
    board_w = board_px
    board_h = max(32, int(board_px * float(pcb["height_mm"]) / float(pcb["width_mm"])))
    board_h = min(board_h, image_h - 8)
    shape = (image_h, image_w)

    if assets_dir is not None:
        loaded = _load_optional_assets(assets_dir, shape, float(pcb["max_component_height_mm"]))
        if loaded is not None:
            return loaded

    rng = np.random.default_rng(int(config["seed"]))
    mask = np.zeros(shape, dtype=bool)
    x0, y0 = (image_w - board_w) // 2, (image_h - board_h) // 2
    x1, y1 = x0 + board_w, y0 + board_h
    mask[y0:y1, x0:x1] = True
    albedo = np.full(shape, 0.025, dtype=np.float32)
    albedo[mask] = 0.23 + rng.normal(0, 0.008, int(mask.sum())).astype(np.float32)
    height = np.zeros(shape, dtype=np.float32)
    material = np.zeros(shape, dtype=np.uint8)
    material[mask] = 1
    components = np.zeros(shape, dtype=np.uint8)

    occupied: list[tuple[int, int, int, int]] = []
    specs = [(8, (0.08, 0.16), (0.07, 0.13), (1.0, 1.8), 2, 0.12),
             (34, (0.025, 0.07), (0.012, 0.035), (0.3, 0.8), 3, 0.48),
             (3, (0.08, 0.15), (0.12, 0.22), (2.0, 3.0), 4, 0.32)]
    for count, wr, hr, zr, mat_id, reflectance in specs:
        placed = 0
        for _ in range(count * 80):
            if placed >= count:
                break
            width = max(5, int(board_w * rng.uniform(*wr)))
            comp_h = max(4, int(board_h * rng.uniform(*hr)))
            cx = int(rng.integers(x0 + width, x1 - width))
            cy = int(rng.integers(y0 + comp_h, y1 - comp_h))
            box = (cx - width // 2, cy - comp_h // 2, cx + width // 2, cy + comp_h // 2)
            if any(not (box[2] + 8 < old[0] or box[0] - 8 > old[2] or box[3] + 8 < old[1] or box[1] - 8 > old[3]) for old in occupied):
                continue
            occupied.append(box)
            _rectangle(height, (cx, cy), (width, comp_h), rng.uniform(*zr))
            _rectangle(albedo, (cx, cy), (width, comp_h), reflectance + rng.uniform(-0.03, 0.03))
            _rectangle(material, (cx, cy), (width, comp_h), mat_id)
            _rectangle(components, (cx, cy), (width, comp_h), mat_id)
            placed += 1

    # Deterministic metallic pads and vias provide useful reflectance contrast.
    for _ in range(90):
        cx = int(rng.integers(x0 + 8, x1 - 8))
        cy = int(rng.integers(y0 + 8, y1 - 8))
        radius = int(rng.integers(2, 6))
        cv2.circle(albedo, (cx, cy), radius, float(rng.uniform(0.62, 0.9)), -1)
        cv2.circle(material, (cx, cy), radius, 5, -1)
    for _ in range(16):
        start = (int(rng.integers(x0, x1)), int(rng.integers(y0, y1)))
        end = (int(rng.integers(x0, x1)), int(rng.integers(y0, y1)))
        cv2.line(albedo, start, end, float(rng.uniform(0.38, 0.58)), int(rng.integers(1, 3)))
    albedo[~mask] = 0.025
    return PcbScene(np.clip(albedo, 0, 1), height, mask, material, components)
