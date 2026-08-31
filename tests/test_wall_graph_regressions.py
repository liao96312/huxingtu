from types import SimpleNamespace

import numpy as np

from pano_platform.floorplan.parse import _rooms_publishable
from pano_platform.floorplan.wall_graph import (
    _AxisRun,
    _collinear_gap_segments,
    _extend_toward,
    _ensure_outer_boundary,
    _modal_run,
)


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


def test_collinear_wall_runs_create_physical_gap_segment() -> None:
    runs = [
        _AxisRun(100, 10, 100, 10, "horizontal"),
        _AxisRun(100, 180, 300, 12, "horizontal"),
    ]
    segments, gaps = _collinear_gap_segments(
        runs, [], (100.0,), (), 10.0, 3, 300.0, 3000.0
    )
    assert len(segments) == len(gaps) == 1
    assert segments[0].id == gaps[0].wall_edge_id == "wall_gap3"
    assert segments[0].virtual is True
    assert segments[0].start == (100, 100.0)
    assert segments[0].end == (180, 100.0)
    assert gaps[0].width_px == 80


def test_collinear_noise_gap_below_physical_minimum_is_ignored() -> None:
    runs = [
        _AxisRun(100, 10, 100, 10, "horizontal"),
        _AxisRun(100, 110, 300, 10, "horizontal"),
    ]
    segments, gaps = _collinear_gap_segments(
        runs, [], (100.0,), (), 10.0, 3, 300.0, 3000.0
    )
    assert segments == [] and gaps == []


def test_missing_outer_boundary_is_closed_from_wall_mask_bbox() -> None:
    mask = np.zeros((200, 240), np.uint8)
    mask[30:34, 40:200] = 1
    mask[160:164, 40:200] = 1
    mask[30:164, 40:44] = 1
    mask[30:164, 196:200] = 1
    boundaries = _ensure_outer_boundary(mask, 4.0, (30.0, 160.0), (40.0, 196.0), 1)
    assert boundaries == []


def test_outer_boundary_adds_only_missing_edges() -> None:
    mask = np.zeros((200, 240), np.uint8)
    mask[30:34, 40:200] = 1
    mask[30:164, 40:44] = 1
    boundaries = _ensure_outer_boundary(mask, 4.0, (30.0,), (40.0,), 1)
    assert len(boundaries) == 2
    assert {item.axis for item in boundaries} == {"horizontal", "vertical"}
