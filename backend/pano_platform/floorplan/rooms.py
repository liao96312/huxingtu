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

MIN_ROOM_AREA_MM2 = 50_000.0
MAX_UNSUPPORTED_BOUNDARY_ROOM_MM2 = 5_000_000.0


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
    min_room_area_mm2: float = MIN_ROOM_AREA_MM2,
    wall_mask: np.ndarray | None = None,
    close_radius_factor: float = 0.9,
) -> tuple[Room, ...]:
    """Produce room polygons from a closed wall graph.

    ``canvas_shape`` is ``(height, width)`` of the original drawing. The barrier
    is drawn at image resolution; free space that is enclosed is a room. Regions
    below ``min_room_area_mm2`` are dropped as slivers. When supplied,
    ``wall_mask`` preserves wall pixels split by a door leaf or scan artifact
    before the canonical graph is drawn.
    """
    if graph.mm_per_px <= 0:
        return ()
    height, width = canvas_shape
    thickness = max(2, int(round(wall_thickness_px)))

    barrier = np.zeros((height, width), np.uint8)
    if wall_mask is not None and wall_mask.shape[:2] == (height, width):
        # Preserve wall pixels that the axis simplifier may have split at a
        # door leaf or a short scan break; graph segments still supply the
        # canonical thickness and virtual-gap barriers below.
        barrier[wall_mask > 0] = 1
        mask_radius = max(1, int(round(thickness * 0.35)))
        barrier = cv2.dilate(
            barrier, np.ones((mask_radius * 2 + 1, mask_radius * 2 + 1), np.uint8)
        )
    for segment in graph.segments:
        _draw_wall(barrier, segment.start, segment.end, thickness)
    for gap in graph.gaps:
        _draw_wall(barrier, gap.gap_start, gap.gap_end, thickness)

    # Seal only scan/morphology corner micro-gaps. The kernel is tied to the
    # measured wall stroke, so it cannot close a physical 300mm+ opening.
    close_radius = max(1, int(round(thickness * close_radius_factor)))
    close_kernel = np.ones((close_radius * 2 + 1, close_radius * 2 + 1), np.uint8)
    barrier = cv2.morphologyEx(barrier, cv2.MORPH_CLOSE, close_kernel)

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


def unsupported_boundary_room_ids(
    graph: WallGraph,
    rooms,
    wall_mask: np.ndarray | None,
    *,
    minimum_unsupported_fraction: float = 0.48,
    maximum_area_mm2: float = MAX_UNSUPPORTED_BOUNDARY_ROOM_MM2,
) -> tuple[str, ...]:
    """Find small exterior cells enclosed mostly by synthetic bbox edges."""

    if wall_mask is None:
        return ()
    boundaries = tuple(
        segment for segment in graph.segments if segment.id.startswith("wall_boundary_")
    )
    if not boundaries:
        return ()
    tolerance = max(2.0, graph.wall_width_px * 1.5)
    rejected = []
    for room in rooms:
        if room.area_mm2 > maximum_area_mm2:
            continue
        perimeter = unsupported = 0.0
        points = room.polygon_px
        for index, start in enumerate(points):
            end = points[(index + 1) % len(points)]
            perimeter += ((end[0] - start[0]) ** 2 + (end[1] - start[1]) ** 2) ** 0.5
            for boundary in boundaries:
                interval = _boundary_overlap(start, end, boundary, tolerance)
                if interval is None:
                    continue
                low, high = interval
                support = _wall_interval_support(
                    wall_mask, boundary.axis, boundary.start, low, high, tolerance
                )
                unsupported += (high - low) * (1.0 - support)
        if perimeter > 0 and unsupported / perimeter >= minimum_unsupported_fraction:
            rejected.append(room.id)
    return tuple(rejected)


def unsupported_wall_room_ids(
    rooms,
    wall_mask: np.ndarray | None,
    wall_width_px: float,
    *,
    minimum_unsupported_fraction: float = 0.58,
    maximum_area_mm2: float = 3_000_000.0,
) -> tuple[str, ...]:
    """Find small cells whose full perimeter is mostly absent from the wall mask."""

    if wall_mask is None:
        return ()
    tolerance = max(2.0, wall_width_px * 1.5)
    rejected = []
    for room in rooms:
        if room.area_mm2 > maximum_area_mm2:
            continue
        perimeter = unsupported = 0.0
        points = room.polygon_px
        for index, start in enumerate(points):
            end = points[(index + 1) % len(points)]
            length = ((end[0] - start[0]) ** 2 + (end[1] - start[1]) ** 2) ** 0.5
            perimeter += length
            if abs(start[1] - end[1]) < 1:
                support = _wall_interval_support(
                    wall_mask, "horizontal", (0, start[1]), start[0], end[0], tolerance
                )
            elif abs(start[0] - end[0]) < 1:
                support = _wall_interval_support(
                    wall_mask, "vertical", (start[0], 0), start[1], end[1], tolerance
                )
            else:
                support = 1.0
            unsupported += length * (1.0 - support)
        if perimeter > 0 and unsupported / perimeter >= minimum_unsupported_fraction:
            rejected.append(room.id)
    return tuple(rejected)


def _boundary_overlap(start, end, boundary, tolerance: float):
    if (
        boundary.axis == "horizontal"
        and abs(start[1] - end[1]) < 1
        and abs(start[1] - boundary.start[1]) <= tolerance
    ):
        low = max(min(start[0], end[0]), min(boundary.start[0], boundary.end[0]))
        high = min(max(start[0], end[0]), max(boundary.start[0], boundary.end[0]))
    elif (
        boundary.axis == "vertical"
        and abs(start[0] - end[0]) < 1
        and abs(start[0] - boundary.start[0]) <= tolerance
    ):
        low = max(min(start[1], end[1]), min(boundary.start[1], boundary.end[1]))
        high = min(max(start[1], end[1]), max(boundary.start[1], boundary.end[1]))
    else:
        return None
    return (low, high) if high > low else None


def _wall_interval_support(mask, axis: str, start, low, high, tolerance: float) -> float:
    first, last = sorted((round(low), round(high)))
    if axis == "horizontal":
        first, last = max(0, first), min(mask.shape[1] - 1, last)
        fixed = round(start[1])
        crop = mask[
            max(0, round(fixed - tolerance)):min(mask.shape[0], round(fixed + tolerance) + 1),
            first:last + 1,
        ]
        supported = np.any(crop > 0, axis=0) if crop.size else np.array([])
    else:
        first, last = max(0, first), min(mask.shape[0] - 1, last)
        fixed = round(start[0])
        crop = mask[
            first:last + 1,
            max(0, round(fixed - tolerance)):min(mask.shape[1], round(fixed + tolerance) + 1),
        ]
        supported = np.any(crop > 0, axis=1) if crop.size else np.array([])
    return float(supported.mean()) if supported.size else 0.0


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
    tolerance = max(2.0, graph.wall_width_px * 1.5 if graph.wall_width_px > 0 else 20.0)
    nx = _nearest(x, graph.vertical_axes) if graph.vertical_axes else x
    ny = _nearest(y, graph.horizontal_axes) if graph.horizontal_axes else y
    if graph.vertical_axes and abs(nx - x) > tolerance:
        nx = x
    if graph.horizontal_axes and abs(ny - y) > tolerance:
        ny = y
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
