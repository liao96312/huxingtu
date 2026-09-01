from __future__ import annotations

import cv2
import numpy as np

from pano_platform.floorplan.rooms import polygonize_rooms
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
