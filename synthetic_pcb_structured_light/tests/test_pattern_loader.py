from pathlib import Path

import numpy as np

from synthetic_pcb_sl.pattern_loader import load_patterns


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_real_patterns_and_inverse_mapping() -> None:
    patterns = load_patterns(PROJECT_ROOT / "patterns")
    assert [pattern.index for pattern in patterns] == list(range(22))
    assert patterns[0].image.shape == (800, 1280)
    assert patterns[0].image.dtype == np.uint8
    for gray_index in range(8):
        assert np.array_equal(patterns[14 + gray_index].image, 255 - patterns[2 + gray_index].image)
