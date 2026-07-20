"""Camera-grid geometry, normals, and projector visibility helpers."""

from __future__ import annotations

from typing import Any

import numpy as np


def world_grid(config: dict[str, Any], mask: np.ndarray) -> tuple[np.ndarray, np.ndarray, float, float]:
    """Map the board camera footprint to physical X/Y coordinates in millimetres."""

    pcb = config["pcb"]
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        raise ValueError("PCB mask is empty")
    x_min, x_max = int(xs.min()), int(xs.max())
    y_min, y_max = int(ys.min()), int(ys.max())
    dx = float(pcb["width_mm"]) / max(1, x_max - x_min)
    dy = float(pcb["height_mm"]) / max(1, y_max - y_min)
    x_axis = (np.arange(mask.shape[1], dtype=np.float32) - (x_min + x_max) / 2.0) * dx
    y_axis = (np.arange(mask.shape[0], dtype=np.float32) - (y_min + y_max) / 2.0) * dy
    x_world, y_world = np.meshgrid(x_axis, y_axis)
    return x_world, y_world, dx, dy


def surface_normals(height_mm: np.ndarray, dx_mm: float, dy_mm: float) -> np.ndarray:
    """Compute upward-facing height-field normals in camera coordinates."""

    dz_dy, dz_dx = np.gradient(height_mm, dy_mm, dx_mm)
    normals = np.dstack((-dz_dx, -dz_dy, np.ones_like(height_mm)))
    length = np.linalg.norm(normals, axis=2, keepdims=True)
    return (normals / np.maximum(length, 1e-8)).astype(np.float32)


def projector_visibility(height_mm: np.ndarray, mask: np.ndarray, dx_mm: float, angle_deg: float) -> np.ndarray:
    """Approximate a projector shadow map for a projector on the +X side."""

    angle = np.deg2rad(angle_deg)
    fall_per_pixel = dx_mm / max(np.tan(angle), 1e-6)
    horizon = np.full(height_mm.shape[0], -np.inf, dtype=np.float32)
    visibility = np.ones_like(height_mm, dtype=np.float32)
    for x in range(height_mm.shape[1] - 1, -1, -1):
        candidate = horizon - fall_per_pixel
        current = height_mm[:, x]
        visibility[:, x] = (current + 0.02 >= candidate).astype(np.float32)
        horizon = np.maximum(current, candidate)
    visibility[~mask] = 1.0
    return visibility
