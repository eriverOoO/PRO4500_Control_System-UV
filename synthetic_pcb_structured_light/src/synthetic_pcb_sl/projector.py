"""Pinhole projector mapping and original-BMP sampling."""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from .pattern_loader import PatternFrame, normalized_pattern


def projector_coordinates(
    config: dict[str, Any], x_world: np.ndarray, y_world: np.ndarray, height_mm: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Project camera-visible 3D points into the fixed projector image plane."""

    projector = config["projector"]
    pcb = config["pcb"]
    angle = np.deg2rad(float(projector["angle_deg"]))
    distance = float(projector["distance_mm"])
    width, height = int(projector["width"]), int(projector["height"])

    x_camera = np.cos(angle) * x_world - np.sin(angle) * height_mm
    depth = distance - np.sin(angle) * x_world - np.cos(angle) * height_mm
    focal_x = 0.86 * width * distance / float(pcb["width_mm"])
    focal_y = 0.86 * height * distance / float(pcb["height_mm"])
    u = width * 0.5 + focal_x * x_camera / np.maximum(depth, 1e-3)
    v = height * 0.5 + focal_y * y_world / np.maximum(depth, 1e-3)
    return u.astype(np.float32), v.astype(np.float32)


def sample_pattern(
    frame: PatternFrame, config: dict[str, Any], u_projector: np.ndarray, v_projector: np.ndarray
) -> np.ndarray:
    """Blur in projector space, then bilinearly sample the real pattern values."""

    image = normalized_pattern(frame)
    sigma = float(config["projector"]["blur_sigma_px"])
    if sigma > 0:
        image = cv2.GaussianBlur(image, (0, 0), sigmaX=sigma, sigmaY=sigma)
    source_h, source_w = image.shape
    projector = config["projector"]
    map_x = u_projector * (source_w - 1) / max(1, int(projector["width"]) - 1)
    map_y = v_projector * (source_h - 1) / max(1, int(projector["height"]) - 1)
    return cv2.remap(
        image,
        map_x,
        map_y,
        interpolation=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )


def illumination_shading(normals: np.ndarray, angle_deg: float) -> np.ndarray:
    """Lambertian term for the fixed off-axis projector."""

    angle = np.deg2rad(angle_deg)
    direction = np.array([np.sin(angle), 0.0, np.cos(angle)], dtype=np.float32)
    diffuse = np.maximum(np.sum(normals * direction, axis=2), 0.0)
    return (0.18 + 0.82 * diffuse).astype(np.float32)
