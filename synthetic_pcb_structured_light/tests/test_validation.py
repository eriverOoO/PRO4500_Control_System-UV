from pathlib import Path

import numpy as np

from synthetic_pcb_sl.dataset import make_contact_sheet


def test_contact_sheet_is_preview_only_rgb() -> None:
    images = [np.full((20, 30), value, np.uint16) for value in (0, 65535)]
    sheet = make_contact_sheet(images, ["black", "white"], columns=2)
    assert sheet.dtype == np.uint8
    assert sheet.ndim == 3
    assert sheet.shape[2] == 3
