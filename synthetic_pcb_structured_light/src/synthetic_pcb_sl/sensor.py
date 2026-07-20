"""Deterministic camera optics, sensor noise, and PNG conversion."""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np


def apply_sensor_model(image: np.ndarray, config: dict[str, Any], seed: int) -> np.ndarray:
    """Apply fixed optics plus deterministic per-frame read and shot noise."""

    render = config["render"]
    result = image.astype(np.float32, copy=True)
    sigma = float(render["camera_blur_sigma_px"])
    if sigma > 0:
        result = cv2.GaussianBlur(result, (0, 0), sigmaX=sigma, sigmaY=sigma)

    distortion = config["camera"].get("distortion", {})
    if bool(distortion.get("enabled", False)):
        height, width = result.shape
        yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
        x = (xx - (width - 1) * 0.5) / max(width * 0.5, 1.0)
        y = (yy - (height - 1) * 0.5) / max(height * 0.5, 1.0)
        radius2 = x * x + y * y
        scale = 1.0 + float(distortion.get("k1", 0.0)) * radius2 + float(distortion.get("k2", 0.0)) * radius2 * radius2
        map_x = (x * scale * width * 0.5 + (width - 1) * 0.5).astype(np.float32)
        map_y = (y * scale * height * 0.5 + (height - 1) * 0.5).astype(np.float32)
        result = cv2.remap(result, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)

    if bool(render.get("enable_vignetting", True)):
        height, width = result.shape
        yy, xx = np.mgrid[-1:1:complex(height), -1:1:complex(width)]
        result *= np.clip(1.0 - 0.16 * (xx * xx + yy * yy), 0.7, 1.0).astype(np.float32)

    if bool(render.get("enable_noise", True)):
        rng = np.random.default_rng(seed)
        shot_std = np.sqrt(np.clip(result, 0, 1)) * float(render["shot_noise_scale"])
        result += rng.normal(0.0, shot_std, result.shape).astype(np.float32)
        result += rng.normal(0.0, float(render["read_noise_std"]), result.shape).astype(np.float32)

    gamma = float(render.get("gamma", 1.0))
    if gamma != 1.0:
        result = np.power(np.clip(result, 0, 1), 1.0 / gamma)
    return np.clip(result, 0.0, 1.0)


def quantize(image: np.ndarray, bit_depth: int) -> np.ndarray:
    """Convert normalized linear intensity to 8- or 16-bit grayscale."""

    if bit_depth == 8:
        return np.rint(np.clip(image, 0, 1) * 255.0).astype(np.uint8)
    if bit_depth == 16:
        return np.rint(np.clip(image, 0, 1) * 65535.0).astype(np.uint16)
    raise ValueError("bit depth must be 8 or 16")
