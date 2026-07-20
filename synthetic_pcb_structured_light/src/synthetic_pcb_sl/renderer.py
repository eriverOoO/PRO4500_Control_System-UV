"""Render one projector pattern against one fixed PCB scene."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .geometry import projector_visibility, surface_normals, world_grid
from .pattern_loader import PatternFrame
from .pcb_scene import PcbScene
from .projector import illumination_shading, projector_coordinates, sample_pattern
from .sensor import apply_sensor_model, quantize


@dataclass(frozen=True)
class RenderContext:
    """Geometry terms reused by all 22 patterns at one angle."""

    x_world: np.ndarray
    y_world: np.ndarray
    normals: np.ndarray
    visibility: np.ndarray
    projector_u: np.ndarray
    projector_v: np.ndarray


def prepare_context(scene: PcbScene, config: dict[str, Any]) -> RenderContext:
    x_world, y_world, dx, dy = world_grid(config, scene.mask)
    normals = surface_normals(scene.height_mm, dx, dy)
    projector = config["projector"]
    visibility = (
        projector_visibility(scene.height_mm, scene.mask, dx, float(projector["angle_deg"]))
        if bool(config["render"].get("enable_shadows", True))
        else np.ones_like(scene.height_mm, dtype=np.float32)
    )
    u, v = projector_coordinates(config, x_world, y_world, scene.height_mm)
    return RenderContext(x_world, y_world, normals, visibility, u, v)


def render_frame(
    scene: PcbScene,
    context: RenderContext,
    pattern: PatternFrame,
    config: dict[str, Any],
    angle_deg: int,
) -> np.ndarray:
    """Render a final decoder input frame; only pattern and fixed sensor noise vary."""

    render = config["render"]
    projector = config["projector"]
    projected = sample_pattern(pattern, config, context.projector_u, context.projector_v)
    shading = illumination_shading(context.normals, float(projector["angle_deg"]))
    direct = (
        float(projector["intensity"])
        * scene.albedo
        * projected
        * shading
        * context.visibility
    )
    ambient = float(render["ambient"]) * scene.albedo
    intensity = float(render["black_level"]) + ambient + direct
    intensity[~scene.mask] = float(render["black_level"]) + float(render["ambient"]) * scene.albedo[~scene.mask]
    seed = int(config["seed"]) + angle_deg * 1009 + pattern.index * 9176
    sensor_image = apply_sensor_model(intensity, config, seed)
    return quantize(sensor_image, int(render["bit_depth"]))
