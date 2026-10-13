"""Building footprint (exterior mask) estimation for room polygonization.

Weak point addressed
--------------------
Stage S3 adds *synthetic* bounding-box perimeter edges
(``wall_boundary_*``) so that flood fill can close rooms on drawings whose
outer wall has gaps. On concave (L/T/U-shaped) plans those bbox edges also
enclose the empty exterior notches, which then become fake rooms or get glued
onto real rooms. On the ResPlan regression this is the main reason the summed
predicted room area exceeds the truth area and room mIoU stays below 0.5.

Approach
--------
Learning-based vectorizers such as Raster-to-Graph (Hu et al., EG 2024) and
the floorplan-to-3d / neural_floorplan pipelines predict an explicit
"outside" / background class and only polygonize interior cells. We do not use
their networks or weights (their public weights are trained on CubiCasa5K,
CC-BY-NC-4.0). Instead we reimplement the same idea with classic morphology:

1. draw the *real* structure: the wall mask plus every solid and virtual
   (door/window gap) segment of the wall graph, but **not** the synthetic
   bbox boundary edges;
2. morphologically close it with a kernel tied to a physical size (default
   1200 mm) to seal residual exterior doors/windows;
3. fill holes: everything not reachable from the image border is inside.

The kernel is expressed in millimetres, following the repo rule that
thresholds must not depend on stored pixel size.
"""

from __future__ import annotations

import cv2
import numpy as np

DEFAULT_SEAL_MM = 1200.0


def building_footprint(
    wall_mask: np.ndarray,
    segments=(),
    *,
    mm_per_px: float,
    wall_width_px: float,
    seal_mm: float = DEFAULT_SEAL_MM,
) -> np.ndarray | None:
    """Return a uint8 mask (1 = inside building) or ``None`` if unreliable."""

    if wall_mask is None or wall_mask.ndim != 2 or mm_per_px <= 0:
        return None
    height, width = wall_mask.shape
    structure = (wall_mask > 0).astype(np.uint8)
    thickness = max(2, int(round(wall_width_px or 2)))
    for segment in segments:
        if str(getattr(segment, "id", "")).startswith("wall_boundary_"):
            continue
        x1, y1 = (int(round(v)) for v in segment.start)
        x2, y2 = (int(round(v)) for v in segment.end)
        cv2.line(structure, (x1, y1), (x2, y2), 1, thickness, cv2.LINE_8)
    if not structure.any():
        return None

    radius = int(round(seal_mm / mm_per_px / 2.0))
    radius = max(1, min(radius, max(height, width) // 8))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2 * radius + 1, 2 * radius + 1))
    padded = cv2.copyMakeBorder(structure, radius + 1, radius + 1, radius + 1, radius + 1,
                                cv2.BORDER_CONSTANT, value=0)
    closed = cv2.morphologyEx(padded, cv2.MORPH_CLOSE, kernel)

    flood = closed.copy()
    mask = np.zeros((flood.shape[0] + 2, flood.shape[1] + 2), np.uint8)
    cv2.floodFill(flood, mask, (0, 0), 2)
    inside = (flood != 2).astype(np.uint8)
    inside = inside[radius + 1:radius + 1 + height, radius + 1:radius + 1 + width]
    if inside.sum() < 0.02 * height * width:
        return None
    return inside


def inside_fraction(region: np.ndarray, footprint: np.ndarray) -> float:
    area = int(region.sum())
    if area == 0:
        return 0.0
    return float(np.logical_and(region > 0, footprint > 0).sum()) / area


def footprint_polygon(footprint: np.ndarray):
    """Vectorize a footprint mask into a shapely (Multi)Polygon in pixel space."""

    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    contours, hierarchy = cv2.findContours(
        footprint.astype(np.uint8), cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE
    )
    if not contours or hierarchy is None:
        return None
    shells = []
    for index, contour in enumerate(contours):
        if hierarchy[0][index][3] != -1 or contour.shape[0] < 3:
            continue
        holes = [
            contours[child].reshape(-1, 2) for child in range(len(contours))
            if hierarchy[0][child][3] == index and contours[child].shape[0] >= 3
        ]
        polygon = Polygon(contour.reshape(-1, 2), holes).buffer(0)
        if not polygon.is_empty:
            shells.append(polygon)
    if not shells:
        return None
    # Contours run through pixel centres; grow by half a pixel so a room drawn
    # flush against the inner wall face is not shaved.
    return unary_union(shells).buffer(0.5, join_style=2)


def clip_rooms_to_footprint(
    rooms,
    graph,
    wall_mask: np.ndarray | None,
    *,
    min_inside_fraction: float = 0.5,
    seal_mm: float = DEFAULT_SEAL_MM,
):
    """Clip room polygons to the building footprint.

    Returns ``(rooms, dropped_ids, clipped_ids)``. Rooms mostly outside the
    footprint are dropped; the others are intersected with it and keep their
    largest connected part. Never returns an empty room list: when the
    footprint would remove every room the input is returned unchanged.
    """

    from dataclasses import replace

    from shapely.geometry import Polygon

    if wall_mask is None or not rooms:
        return tuple(rooms), (), ()
    mm_per_px = float(getattr(graph, "mm_per_px", 0) or 0)
    footprint = building_footprint(
        wall_mask,
        tuple(getattr(graph, "segments", ())) + tuple(
            _GapSegment(gap) for gap in getattr(graph, "gaps", ())
        ),
        mm_per_px=mm_per_px,
        wall_width_px=float(getattr(graph, "wall_width_px", 0) or 0),
        seal_mm=seal_mm,
    )
    if footprint is None:
        return tuple(rooms), (), ()
    shape = footprint_polygon(footprint)
    if shape is None or shape.is_empty:
        return tuple(rooms), (), ()

    kept, dropped, clipped = [], [], []
    for room in rooms:
        try:
            polygon = Polygon(room.polygon_px).buffer(0)
        except Exception:
            kept.append(room)
            continue
        if polygon.is_empty or polygon.area <= 0:
            kept.append(room)
            continue
        inside = polygon.intersection(shape)
        fraction = inside.area / polygon.area
        if fraction < min_inside_fraction:
            dropped.append(room.id)
            continue
        if fraction > 0.995:
            kept.append(room)
            continue
        # Opening (erode+dilate) removes slivers left along wall bands.
        sliver = max(1.0, float(getattr(graph, "wall_width_px", 0) or 0) * 0.5)
        inside = inside.buffer(-sliver, join_style=2).buffer(sliver, join_style=2)
        parts = getattr(inside, "geoms", [inside])
        largest = max((part for part in parts if part.geom_type == "Polygon"),
                      key=lambda part: part.area, default=None)
        if largest is None:
            kept.append(room)
            continue
        largest = largest.simplify(1.0, preserve_topology=True)
        points = tuple((float(x), float(y)) for x, y in list(largest.exterior.coords)[:-1])
        if len(points) < 3:
            kept.append(room)
            continue
        area_px2 = float(largest.area)
        kept.append(replace(
            room,
            polygon_px=points,
            area_px2=area_px2,
            area_mm2=area_px2 * mm_per_px * mm_per_px,
            reason_codes=tuple(dict.fromkeys((*room.reason_codes, "CV_FOOTPRINT_CLIPPED"))),
        ))
        clipped.append(room.id)
    if not kept:
        return tuple(rooms), (), ()
    return tuple(kept), tuple(dropped), tuple(clipped)


class _GapSegment:
    __slots__ = ("id", "start", "end")

    def __init__(self, gap) -> None:
        self.id = gap.wall_edge_id
        self.start = gap.gap_start
        self.end = gap.gap_end
