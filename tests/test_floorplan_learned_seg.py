import numpy as np

import pano_platform.floorplan.learned_seg as learned_seg


def test_missing_weights_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("PANO_LEARNED_SEG", str(tmp_path / "nope.pth"))
    monkeypatch.setattr(learned_seg, "_STATE", {})
    assert learned_seg.learned_barrier(np.zeros((64, 64, 3), np.uint8)) is None
