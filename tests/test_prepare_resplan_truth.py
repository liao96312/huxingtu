import io
import pickle

import numpy as np
import pytest
from shapely.geometry import box

from scripts.prepare_resplan_truth import (
    CONVERTER_VERSION,
    RestrictedUnpickler,
    _draw_door_symbol,
    _draw_window_symbol,
    _long_axis,
    _transform,
)


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


def test_window_symbol_draws_two_parallel_panes():
    image = np.full((140, 140), 255, dtype=np.uint8)
    _draw_window_symbol(image, {"x": 30, "y": 70}, {"x": 110, "y": 70})
    occupied_rows = np.flatnonzero(np.any(image < 255, axis=1))
    assert np.count_nonzero(np.diff(occupied_rows) > 1) == 1


def test_transform_applies_requested_canvas_margin():
    transform = _transform({"wall": box(10, 20, 110, 70)}, canvas=300, margin=25)
    assert transform(box(10, 20, 110, 70)).bounds == pytest.approx((25, 25, 275, 150))
    assert CONVERTER_VERSION == "v3-window-parallel-lines"
