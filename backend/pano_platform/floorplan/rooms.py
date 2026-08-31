"""Stage S4: polygonize the wall graph into room polygons.

A door or window is an *opening in a wall*, so it still separates two rooms at
the structural level. Stage S4 therefore treats every wall segment -- solid or
``virtual`` (a gap recorded by stage S3) -- as a closed barrier, floods the
remaining free space, and turns each enclosed region into an orthogonal polygon.
Opening-type merging (doorway -> one room, per the frozen R-01 rule) happens
later, after stage S5 has classified the gaps.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .wall_graph import WallGraph


@dataclass(frozen=True)
class Room:
    """A single enclosed room as an orthogonal polygon."""

    id: str
    polygon: tuple[tuple[float, float], ...]
    area_mm2: float

    @property
    def area_px2(self) -> float:
        return abs(_polygon_area(self.polygon))


def polygonize_rooms(
    graph: WallGraph,
    wall_thickness_px: float,
    canvas_shape: tuple[int, int],
    min_room_area_mm2: float = 0.5,
) -> tuple[Room, ...]:
    """Produce room polygons from a closed wall graph.

    ``canvas_shape`` is ``(height, width)`` of the original drawing. The barrier
    is drawn at image resolution; free space that is enclosed is a room. Regions
    below ``min_room_area_mm2`` are dropped as slivers.
    """
    if graph.mm_per_px <= 0:
        return ()
    height, width = canvas_shape
    thickness = max(2, int(round(wall_thickness_px)))

    barrier = np.zeros((height, width), np.uint8)
    for segment in graph.segments:
        _draw_wall(barrier, segment.start, segment.end, thickness)
    for gap in graph.gaps:
        _draw_wall(barrier, gap.gap_start, gap.gap_end, thickness)

    # Free space that is not connected to the image border is a room. Label
    # background components; those touching the border are exterior.
    num, labels = cv2.connectedComponents((barrier == 0).astype(np.uint8), connectivity=4)
    border_labels = set(labels[0, :]) | set(labels[-1, :]) | set(labels[:, 0]) | set(labels[:, -1])
    border_labels.discard(0)

    rooms: list[Room] = []
    counter = 1
    for label in range(1, num):
        if label in border_labels:
            continue
        region = (labels == label).astype(np.uint8)
        if region.sum() == 0:
            continue
        polygon = _region_polygon(region, graph)
        area_mm2 = abs(_polygon_area(polygon)) * graph.mm_per_px * graph.mm_per_px
        if area_mm2 < min_room_area_mm2:
            continue
        rooms.append(Room(f"room_{counter:03d}", polygon, area_mm2))
        counter += 1
    return tuple(sorted(rooms, key=lambda room: -room.area_mm2))


def _draw_wall(barrier: np.ndarray, start: tuple[float, float], end: tuple[float, float], thickness: int) -> None:
    """Draw a wall as a filled thick segment on ``barrier``."""
    x1, y1 = int(round(start[0])), int(round(start[1]))
    x2, y2 = int(round(end[0])), int(round(end[1]))
    cv2.line(barrier, (x1, y1), (x2, y2), 1, thickness, cv2.LINE_8)


def _region_polygon(region: np.ndarray, graph: WallGraph) -> tuple[tuple[float, float], ...]:
    """Extract and orthogonalize a room's outline.

    The raw contour is polygon-approximated, then every vertex is snapped to the
    nearest canonical axis so the result is a clean orthogonal room polygon.
    """
    contours, _ = cv2.findContours(region, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return ()
    contour = max(contours, key=cv2.contourArea)
    if contour.shape[0] < 3:
        return ()
    approx = cv2.approxPolyDP(contour, 1.5, True).reshape(-1, 2)
    if approx.shape[0] < 3:
        return ()
    points = [(float(px), float(py)) for px, py in approx]
    # Snap to canonical axes, then drop collinear duplicates.
    snapped = [_snap(point, graph) for point in points]
    cleaned = _drop_collinear(snapped)
    return cleaned


def _snap(point: tuple[float, float], graph: WallGraph) -> tuple[float, float]:
    x, y = point
    nx = _nearest(x, graph.vertical_axes) if graph.vertical_axes else x
    ny = _nearest(y, graph.horizontal_axes) if graph.horizontal_axes else y
    return (nx, ny)


def _nearest(value: float, axes: tuple[float, ...]) -> float:
    return min(axes, key=lambda axis: abs(axis - value))


def _drop_collinear(points: list[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    """Remove middle vertices that lie on the straight line between neighbours."""
    if len(points) < 3:
        return tuple(points)
    result = points[:]
    changed = True
    while changed and len(result) >= 3:
        changed = False
        for index in range(len(result)):
            a = result[(index - 1) % len(result)]
            b = result[index]
            c = result[(index + 1) % len(result)]
            if _collinear(a, b, c):
                del result[index]
                changed = True
                break
    return tuple(result)


def _collinear(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> bool:
    cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    return abs(cross) < 1e-4


def _polygon_area(points) -> float:
    if len(points) < 3:
        return 0.0
    area = 0.0
    for index in range(len(points)):
        p = points[index]
        q = points[(index + 1) % len(points)]
        area += p[0] * q[1] - q[0] * p[1]
    return area / 2.0
