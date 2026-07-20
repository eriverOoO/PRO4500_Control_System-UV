from pathlib import Path

import cv2
import numpy as np
import yaml

from synthetic_pcb_sl.dataset import generate_dataset, read_png
from synthetic_pcb_sl.pattern_loader import load_patterns
from synthetic_pcb_sl.pcb_scene import create_scene
from synthetic_pcb_sl.renderer import prepare_context, render_frame


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def small_config() -> dict:
    config = yaml.safe_load((PROJECT_ROOT / "configs" / "default.yaml").read_text(encoding="utf-8"))
    config["camera"]["width"] = 320
    config["camera"]["height"] = 200
    config["render"]["enable_noise"] = False
    return config


def test_scene_rotation_and_deterministic_render() -> None:
    config = small_config()
    scene = create_scene(config)
    rotated = scene.rotated_180()
    assert np.array_equal(rotated.height_mm, np.rot90(scene.height_mm, 2))
    patterns = load_patterns(PROJECT_ROOT / "patterns")
    context = prepare_context(scene, config)
    first = render_frame(scene, context, patterns[10], config, 0)
    second = render_frame(scene, context, patterns[10], config, 0)
    assert first.dtype == np.uint16
    assert np.array_equal(first, second)
    assert float(first.std()) > 0


def test_partial_cli_generation_layout(tmp_path: Path) -> None:
    config = small_config()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    manifest = generate_dataset(
        PROJECT_ROOT / "patterns", tmp_path / "output", config_path,
        angle=180, pattern_index=14, assets_dir=tmp_path / "assets",
    )
    image_path = tmp_path / "output" / "angle_180" / "pattern_014.png"
    assert image_path.is_file()
    image = read_png(image_path)
    assert image.shape == (200, 320)
    assert image.dtype == np.uint16
    assert manifest["frames"][0]["inverse"] is True
