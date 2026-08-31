from types import SimpleNamespace

import numpy as np

from pano_platform.floorplan.parse import _rooms_publishable
from pano_platform.floorplan.wall_graph import _extend_toward, _modal_run


def test_wall_thickness_is_span_not_page_coordinate() -> None:
    along = np.repeat(np.arange(100), 10)
    perp = np.tile(np.arange(200, 210), 100)
    assert _modal_run(perp, along, "x") == 10


def test_right_extension_uses_nearest_axis() -> None:
    extension = _extend_toward(10, 50, (20, 100), "right", 50, "horizontal")
    assert extension.target == 20
    assert extension.reached == 20
    assert extension.virtual is False


def test_room_publish_gate_rejects_any_unconfirmed_room() -> None:
    closed = SimpleNamespace(closed=(object(),), needs_review=())
    assert _rooms_publishable(
        SimpleNamespace(rooms=(SimpleNamespace(status="ok"),), scale_status="confirmed"), closed
    )
    assert not _rooms_publishable(
        SimpleNamespace(rooms=(SimpleNamespace(status="review"),), scale_status="confirmed"), closed
    )
    assert not _rooms_publishable(
        SimpleNamespace(rooms=(SimpleNamespace(status="ok"),), scale_status="review"), closed
    )
