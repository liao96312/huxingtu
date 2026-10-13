"""Split merged CV rooms along confirmed door openings and interior wall ink.

When a door gap is not sealed before polygonization, two rooms flood into one
region. After opening arbitration we know where the doors are, so we rasterize
each room, subtract the wall mask, graph wall segments and confirmed door
segments, and keep each sufficiently large connected component as its own
room. Self-implemented with OpenCV/Shapely morphology; no third-party code.
"""

from __future__ import annotations

from dataclasses import replace

import cv2
import numpy as np

DEFAULT_MIN_PART_MM2 = 1_500_000.0  # 1.5 m^2: smaller remainders stay merged


def split_rooms_by_doors(
    rooms,
    graph,
    wall_mask: np.ndarray | None,
    openings,
    *,
    min_part_mm2: float = DEFAULT_MIN_PART_MM2,
):
    """Return ``(rooms, split_ids)``; rooms that do not split are unchanged."""

    from shapely.geometry import Polygon

    if wall_mask is None or not rooms:
        return tuple(rooms), ()
    mm_per_px = float(getattr(graph, "mm_per_px", 0) or 0)
    wall_px = float(getattr(graph, "wall_width_px", 0) or 0)
    if mm_per_px <= 0 or wall_px <= 0:
        return tuple(rooms), ()
    height, width = wall_mask.shape[:2]
    thickness = max(2, int(round(wall_px)))
    barrier = (wall_mask > 0).astype(np.uint8)
    for segment in getattr(graph, "segments", ()):
        if str(getattr(segment, "id", "")).startswith("wall_boundary"):
            continue
        cv2.line(barrier, _pt(segment.start), _pt(segment.end), 1, thickness)
    for opening in openings or ():
        if getattr(opening, "type", "") not in ("door", "doorway"):
            continue
        cv2.line(barrier, _pt(opening.start_px), _pt(opening.end_px), 1, thickness)
    min_part_px = min_part_mm2 / (mm_per_px * mm_per_px)
    grow = max(1, int(round(wall_px * 0.75)))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2 * grow + 1, 2 * grow + 1))

    result, split_ids = [], []
    for room in rooms:
        region = np.zeros((height, width), np.uint8)
        cv2.fillPoly(region, [np.round(np.asarray(room.polygon_px)).astype(np.int32)], 1)
        free = region & (1 - barrier)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(free, connectivity=4)
        big = [i for i in range(1, count) if stats[i, cv2.CC_STAT_AREA] >= min_part_px]
        if len(big) < 2:
            result.append(room)
            continue
        parts = []
        for label in big:
            part = cv2.dilate((labels == label).astype(np.uint8), kernel) & region
            contours, _ = cv2.findContours(part, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if not contours:
                continue
            contour = max(contours, key=cv2.contourArea)
            polygon = Polygon(contour.reshape(-1, 2)).buffer(0)
            if polygon.is_empty:
                continue
            if polygon.geom_type != "Polygon":
                polygon = max(polygon.geoms, key=lambda g: g.area)
            polygon = polygon.simplify(max(1.0, wall_px * 0.5), preserve_topology=True)
            points = tuple((float(x), float(y)) for x, y in list(polygon.exterior.coords)[:-1])
            if len(points) >= 3:
                parts.append((points, float(polygon.area)))
        if len(parts) < 2:
            result.append(room)
            continue
        parts.sort(key=lambda item: -item[1])
        for index, (points, area_px2) in enumerate(parts):
            result.append(replace(
                room,
                id=room.id if index == 0 else f"{room.id}_s{index}",
                polygon_px=points,
                area_px2=area_px2,
                area_mm2=area_px2 * mm_per_px * mm_per_px,
                reason_codes=tuple(dict.fromkeys((*room.reason_codes, "CV_DOOR_SPLIT"))),
            ))
        split_ids.append(room.id)
    return tuple(result), tuple(split_ids)


def _pt(point) -> tuple[int, int]:
    return int(round(point[0])), int(round(point[1]))
