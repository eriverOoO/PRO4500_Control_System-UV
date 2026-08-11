from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np
import pytest

from structured_light_pc_controller import load_pattern_profile


PATTERN_NAMES = (
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


def _powershell() -> str:
    executable = shutil.which("powershell") or shutil.which("pwsh")
    if executable is None:
        pytest.skip("PowerShell is required for the pattern generator test")
    return executable


def test_generator_keeps_active_height_and_horizontal_stripe_period(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    placeholder = np.zeros((48, 120), dtype=np.uint8)
    for name in PATTERN_NAMES:
        assert cv2.imwrite(str(source / name), placeholder)

    script = Path(__file__).with_name("generate_centered_patterns.ps1")
    subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-SourceDirectory",
            str(source),
            "-OutputDirectory",
            str(output),
            "-Scale",
            "0.5",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    white = cv2.imread(str(output / "00_White.bmp"), cv2.IMREAD_GRAYSCALE)
    assert white is not None
    assert np.all(white[:12, :] == 0)
    assert np.all(white[12:36, :] == 255)
    assert np.all(white[36:, :] == 0)

    sine = cv2.imread(str(output / "10_Sine_000.bmp"), cv2.IMREAD_GRAYSCALE)
    assert sine is not None
    np.testing.assert_array_equal(sine[12:24, :], sine[24:36, :])

    gray_lsb = cv2.imread(str(output / "09_Gray7.bmp"), cv2.IMREAD_GRAYSCALE)
    gray_lsb_inverse = cv2.imread(str(output / "21_Gray7_inv.bmp"), cv2.IMREAD_GRAYSCALE)
    assert gray_lsb is not None and gray_lsb_inverse is not None
    assert np.all(gray_lsb[12:24, :] == 0)
    assert np.all(gray_lsb[24:36, :] == 255)
    np.testing.assert_array_equal(
        gray_lsb_inverse[12:36, :],
        255 - gray_lsb[12:36, :],
    )

    profile = load_pattern_profile(output)
    assert profile is not None
    assert profile["phase_axis"] == "y"
    assert profile["active_height_px"] == 24
    assert profile["stripe_period_px"] == 12
    assert profile["stripe_cycle_count"] == 2


def test_generator_rejects_a_period_below_twelve_pixels(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    placeholder = np.zeros((8, 24), dtype=np.uint8)
    for name in PATTERN_NAMES:
        assert cv2.imwrite(str(source / name), placeholder)

    script = Path(__file__).with_name("generate_centered_patterns.ps1")
    completed = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-SourceDirectory",
            str(source),
            "-OutputDirectory",
            str(output),
            "-Scale",
            "1",
            "-StripePeriodPixels",
            "11",
        ],
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert not output.exists()
