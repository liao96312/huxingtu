import io
import pickle

import numpy as np
import pytest
from shapely.geometry import box

from scripts.prepare_resplan_truth import RestrictedUnpickler, _draw_door_symbol, _long_axis


def test_restricted_unpickler_blocks_unexpected_globals():
    payload = pickle.dumps(eval)
    with pytest.raises(pickle.UnpicklingError, match="blocked pickle global"):
        RestrictedUnpickler(io.BytesIO(payload)).load()


def test_long_axis_uses_opening_length():
    start, end = _long_axis(box(10, 20, 110, 30))
    assert start["y"] == end["y"] == 25
    assert abs(end["x"] - start["x"]) == 100


def test_door_symbol_draws_leaf_and_arc():
    image = np.full((140, 140), 255, dtype=np.uint8)
    _draw_door_symbol(image, {"x": 30, "y": 30}, {"x": 90, "y": 30})
    assert np.count_nonzero(image < 255) > 100
