from __future__ import annotations

import cv2
import numpy as np

from pano_platform.floorplan.rooms import (
    polygonize_rooms, unsupported_boundary_room_ids, unsupported_wall_room_ids,
)
from pano_platform.floorplan.wall_graph import WallGraph, WallSegment


def test_wall_mask_recovers_partition_missing_from_axis_graph() -> None:
    graph = WallGraph(
        horizontal_axes=(10.0, 90.0), vertical_axes=(10.0, 150.0),
        segments=(
            WallSegment("top", "horizontal", (10.0, 10.0), (150.0, 10.0), 5.0, False),
            WallSegment("right", "vertical", (150.0, 10.0), (150.0, 90.0), 5.0, False),
            WallSegment("bottom", "horizontal", (150.0, 90.0), (10.0, 90.0), 5.0, False),
            WallSegment("left", "vertical", (10.0, 90.0), (10.0, 10.0), 5.0, False),
        ), gaps=(), mm_per_px=10.0, wall_width_px=5.0,
    )
    wall_mask = np.zeros((100, 160), np.uint8)
    cv2.rectangle(wall_mask, (10, 10), (150, 90), 1, 5)
    cv2.line(wall_mask, (80, 10), (80, 90), 1, 5)

    rooms = polygonize_rooms(graph, 5.0, wall_mask.shape, wall_mask=wall_mask)

    assert len(rooms) == 2


def test_measured_wall_close_seals_corner_micro_gap() -> None:
    graph = WallGraph(
        horizontal_axes=(10.0, 110.0), vertical_axes=(10.0, 110.0),
        segments=(
            WallSegment("top", "horizontal", (10, 10), (82, 10), 10, False),
            WallSegment("right", "vertical", (110, 10), (110, 110), 10, False),
            WallSegment("bottom", "horizontal", (110, 110), (10, 110), 10, False),
            WallSegment("left", "vertical", (10, 110), (10, 10), 10, False),
        ),
        gaps=(), mm_per_px=10.0, wall_width_px=10.0,
    )

    assert polygonize_rooms(graph, 10, (130, 130), close_radius_factor=0.75) == ()
    assert len(polygonize_rooms(graph, 10, (130, 130))) == 1


def test_sub_005_square_metre_wall_pocket_is_not_a_room() -> None:
    segments = [
        WallSegment("outer-top", "horizontal", (10, 10), (110, 10), 2, False),
        WallSegment("outer-right", "vertical", (110, 10), (110, 110), 2, False),
        WallSegment("outer-bottom", "horizontal", (110, 110), (10, 110), 2, False),
        WallSegment("outer-left", "vertical", (10, 110), (10, 10), 2, False),
        WallSegment("tiny-top", "horizontal", (30, 30), (36, 30), 2, False),
        WallSegment("tiny-right", "vertical", (36, 30), (36, 36), 2, False),
        WallSegment("tiny-bottom", "horizontal", (36, 36), (30, 36), 2, False),
        WallSegment("tiny-left", "vertical", (30, 36), (30, 30), 2, False),
    ]
    graph = WallGraph(
        horizontal_axes=(10.0, 30.0, 36.0, 110.0),
        vertical_axes=(10.0, 30.0, 36.0, 110.0),
        segments=tuple(segments), gaps=(), mm_per_px=10.0, wall_width_px=2.0,
    )

    rooms = polygonize_rooms(graph, 2.0, (120, 120))

    assert len(rooms) == 1
    assert rooms[0].area_mm2 > 500_000


def test_small_room_mostly_closed_by_unsupported_bbox_edges_is_filtered() -> None:
    from types import SimpleNamespace

    graph = WallGraph(
        horizontal_axes=(10.0, 90.0), vertical_axes=(10.0, 90.0),
        segments=(
            WallSegment("wall_boundary_h1", "horizontal", (10, 10), (90, 10), 5, False),
            WallSegment("wall_boundary_v2", "vertical", (10, 10), (10, 90), 5, False),
        ),
        gaps=(), mm_per_px=10.0, wall_width_px=5.0,
    )
    room = SimpleNamespace(
        id="exterior", area_mm2=640_000,
        polygon_px=((10, 10), (90, 10), (90, 90), (10, 90)),
    )
    empty_wall_mask = np.zeros((100, 100), np.uint8)

    assert unsupported_boundary_room_ids(graph, [room], empty_wall_mask) == ("exterior",)
    large_fallback = SimpleNamespace(
        id="fallback", area_mm2=6_000_000, polygon_px=room.polygon_px,
    )
    assert unsupported_boundary_room_ids(graph, [large_fallback], empty_wall_mask) == ()


def test_small_room_with_mostly_unsupported_perimeter_is_filtered() -> None:
    from types import SimpleNamespace

    room = SimpleNamespace(
        id="unsupported", area_mm2=1_000_000,
        polygon_px=((10, 10), (90, 10), (90, 90), (10, 90)),
    )
    empty_wall_mask = np.zeros((100, 100), np.uint8)

    assert unsupported_wall_room_ids([room], empty_wall_mask, 5.0) == ("unsupported",)
    large = SimpleNamespace(id="fallback", area_mm2=4_000_000, polygon_px=room.polygon_px)
    assert unsupported_wall_room_ids([large], empty_wall_mask, 5.0) == ()
