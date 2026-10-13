import numpy as np

import pano_platform.floorplan.cubicasa as cubicasa


def test_missing_weights_falls_back_to_none(tmp_path, monkeypatch):
    monkeypatch.setenv("PANO_CUBICASA_DIR", str(tmp_path))
    monkeypatch.setattr(cubicasa, "_MODEL", None)
    monkeypatch.setattr(cubicasa, "_FAILED", False)
    assert cubicasa.cubicasa_wall_mask(np.zeros((64, 64, 3), np.uint8)) is None
