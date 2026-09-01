"""T07, F4: room closure, orthogonalization and topology checks."""

from __future__ import annotations

from pano_platform.floorplan.fusion import FusedRoom
from pano_platform.floorplan.fusion_rooms import close_rooms
from pano_platform.floorplan.wall_graph import WallGraph


def graph(mm_per_px=10.0) -> WallGraph:
    return WallGraph(
        horizontal_axes=(100.0, 200.0, 300.0),
        vertical_axes=(100.0, 200.0, 300.0),
        segments=(), gaps=(), mm_per_px=mm_per_px,
    )


def room(polygon, rid="r1") -> FusedRoom:
    return FusedRoom(id=rid, type="bedroom", polygon_px=polygon, area_mm2=1e6, area_px2=10000.0, status="ok", reason_codes=())


def typed_room(polygon, rid, room_type) -> FusedRoom:
    value = room(polygon, rid)
    return FusedRoom(**{**value.__dict__, "type": room_type})


def test_axis_aligned_room_closes() -> None:
    poly = ((100.0, 100.0), (250.0, 100.0), (250.0, 250.0), (100.0, 250.0))
    result = close_rooms((room(poly),), graph())
    assert len(result.closed) == 1
    assert result.closed[0].closed is True
    assert result.closed[0].simple is True


def test_degenerate_room_needs_review() -> None:
    poly = ((100.0, 100.0), (100.0, 100.0), (100.0, 100.0))
    result = close_rooms((room(poly),), graph())
    assert len(result.needs_review) == 1
    assert "NOT_CLOSED" in result.needs_review[0].reason_codes or "ZERO_AREA" in result.needs_review[0].reason_codes


def test_self_intersecting_room_needs_review() -> None:
    poly = ((100.0, 100.0), (250.0, 250.0), (100.0, 250.0), (250.0, 100.0))
    result = close_rooms((room(poly),), graph())
    # bow-tie is either degenerate via area or detected self-intersecting
    assert result.needs_review


def test_vertices_snap_to_axes() -> None:
    # vertices off-axis (e.g. 105, 198) snap to 100/200
    poly = ((105.0, 198.0), (240.0, 110.0), (240.0, 240.0), (105.0, 240.0))
    result = close_rooms((room(poly),), graph())
    closed = result.closed[0]
    xs = [p[0] for p in closed.polygon_px]
    ys = [p[1] for p in closed.polygon_px]
    # near-axis vertices snap; points 40px away stay put under the wall-width
    # tolerance, preventing a room from jumping to a distant parallel wall.
    assert (xs[0], ys[0]) == (100.0, 200.0)
    assert (xs[1], ys[1]) == (240.0, 100.0)


def test_collinear_duplicate_vertices_removed() -> None:
    poly = ((100.0, 100.0), (150.0, 100.0), (200.0, 100.0), (200.0, 200.0), (100.0, 200.0))
    result = close_rooms((room(poly),), graph())
    closed = result.closed[0]
    assert len(closed.polygon_px) == 4  # midpoint (150,100) dropped as collinear


def test_same_type_duplicate_room_is_suppressed() -> None:
    first = room(((100, 100), (300, 100), (300, 300), (100, 300)), "r1")
    duplicate = room(((105, 105), (295, 105), (295, 295), (105, 295)), "r2")
    result = close_rooms((first, duplicate), graph())
    assert len(result.closed) == 1
    assert not result.needs_review
    assert "DUPLICATE_ROOM_SUPPRESSED" in result.reason_codes


def test_different_room_types_with_large_overlap_need_review() -> None:
    bedroom = typed_room(((100, 100), (300, 100), (300, 300), (100, 300)), "r1", "bedroom")
    kitchen = typed_room(((120, 120), (300, 120), (300, 300), (120, 300)), "r2", "kitchen")
    result = close_rooms((bedroom, kitchen), graph())
    assert not result.closed
    assert len(result.needs_review) == 2
    assert "ROOM_OVERLAP_CONFLICT" in result.reason_codes
