"""Load the real projector BMPs and expose the decoder file mapping."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


SOURCE_FILES = (
    "00_White.bmp",
    "01_Black.bmp",
    "02_Gray0.bmp",
    "03_Gray1.bmp",
    "04_Gray2.bmp",
    "05_Gray3.bmp",
    "06_Gray4.bmp",
    "07_Gray5.bmp",
    "08_Gray6.bmp",
    "09_Gray7.bmp",
    "10_Sine_000.bmp",
    "11_Sine_090.bmp",
    "12_Sine_180.bmp",
    "13_Sine_270.bmp",
)


@dataclass(frozen=True)
class PatternFrame:
    """One decoder output pattern and its source provenance."""

    index: int
    label: str
    source_name: str
    inverse: bool
    image: np.ndarray


def _read_gray(path: Path) -> np.ndarray:
    payload = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(payload, cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError(f"Could not decode pattern: {path}")
    if image.ndim == 3:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if image.dtype not in (np.uint8, np.uint16):
        raise ValueError(f"Unsupported pattern dtype {image.dtype}: {path}")
    return image


def load_patterns(patterns_dir: Path) -> list[PatternFrame]:
    """Read 14 real BMPs and construct the exact 0..21 decoder mapping."""

    missing = [name for name in SOURCE_FILES if not (patterns_dir / name).is_file()]
    if missing:
        raise FileNotFoundError("Missing projector patterns: " + ", ".join(missing))

    source = [_read_gray(patterns_dir / name) for name in SOURCE_FILES]
    shapes = {image.shape for image in source}
    dtypes = {image.dtype for image in source}
    if len(shapes) != 1 or len(dtypes) != 1:
        raise ValueError("All projector BMPs must share size and dtype")

    frames: list[PatternFrame] = []
    labels = ["White", "Black"] + [f"Gray{i}" for i in range(8)] + [
        "Sine_000", "Sine_090", "Sine_180", "Sine_270"
    ]
    for index, (label, name, image) in enumerate(zip(labels, SOURCE_FILES, source, strict=True)):
        frames.append(PatternFrame(index, label, name, False, image))

    maximum = np.iinfo(source[2].dtype).max
    for gray_index in range(8):
        source_index = gray_index + 2
        frames.append(
            PatternFrame(
                14 + gray_index,
                f"Gray{gray_index}_inv",
                SOURCE_FILES[source_index],
                True,
                maximum - source[source_index],
            )
        )
    return frames


def normalized_pattern(frame: PatternFrame) -> np.ndarray:
    """Convert a source pattern to float32 in [0, 1] without thresholding."""

    maximum = float(np.iinfo(frame.image.dtype).max)
    return frame.image.astype(np.float32) / maximum
