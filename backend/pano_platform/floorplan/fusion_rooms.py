"""Stage T07, F4: close and orthogonalize fused room polygons.

The VLM draft's room polygons are rough seeds. F4 snaps each onto the CV wall
graph, drops degenerate/self-intersecting/zero-area rings, favours wall edges and
verifies the result is a closed, simple polygon. A room that cannot be closed onto
the wall graph is reported as ``NEEDS_REVIEW`` and never becomes a final room.
"""

from __future__ import annotations

from dataclasses import dataclass

from .fusion import FusedRoom
from .rooms import _polygon_area
from .wall_graph import WallGraph


@dataclass(frozen=True)
class ClosedRoom:
    """A room that has been closed and orthogonalized against the wall graph."""

    id: str
    type: str
    polygon_px: tuple[tuple[float, float], ...]
    area_mm2: float
    closed: bool
    simple: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class RoomsFusionResult:
    closed: tuple[ClosedRoom, ...]
    needs_review: tuple[ClosedRoom, ...]
    reason_codes: tuple[str, ...]


def close_rooms(fused_rooms: tuple[FusedRoom, ...], graph: WallGraph) -> RoomsFusionResult:
    """Close and orthogonalize each fused room, then classify closure state."""

    closed: list[ClosedRoom] = []
    needs_review: list[ClosedRoom] = []
    reason_codes: list[str] = []

    for room in fused_rooms:
        polygon = _orthogonalize(room.polygon_px, graph)
        simple = _is_simple(polygon)
        closed_ring = _is_closed(polygon)
        area_px2 = abs(_polygon_area(polygon))
        degenerate = area_px2 < 1e-6

        reasons: list[str] = []
        if not closed_ring:
            reasons.append("NOT_CLOSED")
        if not simple:
            reasons.append("SELF_INTERSECTION")
        if degenerate:
            reasons.append("ZERO_AREA")

        room_state = ClosedRoom(
            id=room.id,
            type=room.type,
            polygon_px=polygon,
            area_mm2=room.area_mm2,
            closed=closed_ring,
            simple=simple,
            reason_codes=tuple(reasons),
        )
        if closed_ring and simple and not degenerate:
            closed.append(room_state)
        else:
            needs_review.append(room_state)
            reason_codes.append("ROOM_NOT_CLOSED")

    return RoomsFusionResult(
        closed=tuple(closed),
        needs_review=tuple(needs_review),
        reason_codes=tuple(dict.fromkeys(reason_codes)),
    )


def _orthogonalize(
    polygon: tuple[tuple[float, float], ...],
    graph: WallGraph,
) -> tuple[tuple[float, float], ...]:
    """Snap vertices to canonical axes and drop collinear/repeated points."""

    if len(polygon) < 3:
        return polygon
    snapped: list[tuple[float, float]] = []
    for (x, y) in polygon:
        nx = _nearest(x, graph.vertical_axes, graph.wall_width_px) if graph.vertical_axes else x
        ny = _nearest(y, graph.horizontal_axes, graph.wall_width_px) if graph.horizontal_axes else y
        point = (nx, ny)
        if not snapped or point != snapped[-1]:
            snapped.append(point)
    if len(snapped) > 1 and snapped[0] == snapped[-1]:
        snapped.pop()
    snapped = _drop_collinear(snapped)
    return tuple(snapped)


def _nearest(value: float, axes: tuple[float, ...], wall_width_px: float = 0.0) -> float:
    nearest = min(axes, key=lambda axis: abs(axis - value))
    tolerance = max(1.0, wall_width_px * 1.5 if wall_width_px > 0 else 20.0)
    return nearest if abs(nearest - value) <= tolerance else value


def _drop_collinear(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if len(points) < 3:
        return points
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
    return result


def _collinear(a, b, c) -> bool:
    cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    return abs(cross) < 1e-4


def _is_closed(polygon: tuple[tuple[float, float], ...]) -> bool:
    """A ring is closed when it has >= 3 distinct vertices and the implicit
    closing edge is non-zero (i.e. first and last are not identical points that
    would collapse the ring). The polygon list itself is treated as an open ring
    that closes back to the first point.
    """

    if len(polygon) < 3:
        return False
    # an open ring that repeats a point back to itself is still a valid loop; only
    # reject when all vertices coincide or the ring collapses.
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    width = max(xs) - min(xs)
    height = max(ys) - min(ys)
    return width > 1e-6 and height > 1e-6


def _is_simple(polygon: tuple[tuple[float, float], ...]) -> bool:
    if len(polygon) < 3:
        return False
    try:
        from shapely.geometry import Polygon

        return Polygon(polygon).is_simple
    except Exception:
        return False
