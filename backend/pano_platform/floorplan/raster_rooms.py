"""Raster room segmentation: seal door gaps, flood the exterior, keep cells.

Classic morphological floor-plan room extraction, self-implemented with
OpenCV/Shapely (no third-party code or weights):

1. Barrier = all ink (walls, window lines).
2. Door gaps are sealed by axis-wise closing of the horizontal / vertical wall
   layers. A closing may only fill rows (columns) that already carry a
   horizontal (vertical) wall nearby, so two parallel walls are never bridged.
3. Door swing symbols (thin, compact, curved ink blobs) are filled with their
   convex hull; the hull chord seals the opening at the jamb.
4. Free space connected to the image border is exterior; every other
   component large enough to be a room becomes a room polygon, grown back by
   about one wall width so it reaches the wall band like the CV polygons.

All constants are in wall-width units so they do not depend on the
(unreliable) bootstrap mm/px estimate:
- door gap up to 6 wall widths (~1.2 m door with ~0.2 m walls);
- room area at least 37.5 wall-width^2 (~1.5 m^2 with ~0.2 m walls).
"""

from __future__ import annotations

import cv2
import numpy as np

DOOR_GAP_WALL_WIDTHS = 6.0
MIN_ROOM_WALL_WIDTH2 = 37.5


def raster_room_polygons(
    ink: np.ndarray, wall: np.ndarray, wall_width_px: float,
) -> list[tuple[tuple[float, float], ...]]:
    from shapely.geometry import Polygon

    ww = max(2, int(round(float(wall_width_px or 0))))
    if ink is None or wall is None or ink.size == 0:
        return []
    ink = (ink > 0).astype(np.uint8)
    wall = (wall > 0).astype(np.uint8)
    gap = int(DOOR_GAP_WALL_WIDTHS * ww) + 1
    horizontal = cv2.morphologyEx(wall, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (3 * ww, 1)))
    vertical = cv2.morphologyEx(wall, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 3 * ww)))
    both = horizontal | vertical
    kh = cv2.getStructuringElement(cv2.MORPH_RECT, (gap, 1))
    kv = cv2.getStructuringElement(cv2.MORPH_RECT, (1, gap))
    reach_h = cv2.getStructuringElement(cv2.MORPH_RECT, (2 * gap + 1, 1))
    reach_v = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 2 * gap + 1))
    sealed_h = cv2.morphologyEx(both, cv2.MORPH_CLOSE, kh) & cv2.dilate(horizontal, reach_h)
    sealed_v = cv2.morphologyEx(both, cv2.MORPH_CLOSE, kv) & cv2.dilate(vertical, reach_v)
    barrier = ink | wall | sealed_h | sealed_v

    thin = ink & (1 - cv2.dilate(wall, np.ones((3, 3), np.uint8)))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(thin, connectivity=8)
    for label in range(1, count):
        w, h, area = stats[label, 2], stats[label, 3], stats[label, 4]
        if ww <= min(w, h) and max(w, h) <= 8 * ww and max(w, h) <= 3 * min(w, h) and area < 0.5 * w * h:
            points = cv2.findNonZero((labels == label).astype(np.uint8))
            cv2.fillConvexPoly(barrier, cv2.convexHull(points), 1)

    free = (1 - barrier).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(free, connectivity=4)
    border = set(np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])).tolist())
    min_area = MIN_ROOM_WALL_WIDTH2 * ww * ww
    grow = cv2.getStructuringElement(cv2.MORPH_RECT, (ww + 1, ww + 1))
    polygons = []
    for label in range(1, count):
        if label in border or stats[label, cv2.CC_STAT_AREA] < min_area:
            continue
        region = cv2.dilate((labels == label).astype(np.uint8), grow)
        contours, _ = cv2.findContours(region, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        polygon = Polygon(max(contours, key=cv2.contourArea).reshape(-1, 2)).buffer(0)
        if polygon.is_empty:
            continue
        if polygon.geom_type != "Polygon":
            polygon = max(polygon.geoms, key=lambda part: part.area)
        polygon = polygon.simplify(ww / 2, preserve_topology=True)
        points = tuple((float(x), float(y)) for x, y in list(polygon.exterior.coords)[:-1])
        if len(points) >= 3:
            polygons.append(points)
    return polygons


def reconcile_rooms(cv_rooms, raster_polygons, shape, wall_width_px: float, mm_per_px: float):
    """Keep the CV room identities/count, take room extents from raster cells.

    Each raster cell is partitioned among the CV rooms overlapping it (pixel
    goes to the nearest overlapping CV room). Raster cells that no CV room
    touches become new rooms; CV rooms that touch no raster cell are kept.
    """

    from dataclasses import replace

    from shapely.geometry import Polygon

    if not raster_polygons:
        return tuple(cv_rooms), 0
    height, width = shape
    ww = max(2, int(round(float(wall_width_px or 0))))
    min_area = MIN_ROOM_WALL_WIDTH2 * ww * ww
    cv_masks = []
    for room in cv_rooms:
        mask = np.zeros((height, width), np.uint8)
        cv2.fillPoly(mask, [np.round(np.asarray(room.polygon_px)).astype(np.int32)], 1)
        cv_masks.append(mask)
    assigned = [np.zeros((height, width), np.uint8) for _ in cv_rooms]
    new_cells = []
    for polygon in raster_polygons:
        cell = np.zeros((height, width), np.uint8)
        cv2.fillPoly(cell, [np.round(np.asarray(polygon)).astype(np.int32)], 1)
        owners = [i for i, mask in enumerate(cv_masks) if int((mask & cell).sum()) >= min_area]
        if not owners:
            new_cells.append(cell)
            continue
        if len(owners) == 1:
            assigned[owners[0]] |= cell
            continue
        # Nearest-owner partition of the cell via a labelled distance transform.
        seeds = np.zeros((height, width), np.int32)
        for rank, i in enumerate(owners, start=1):
            seeds[(cv_masks[i] & cell) > 0] = rank
        background = (seeds == 0).astype(np.uint8)
        _, labels = cv2.distanceTransformWithLabels(
            background, cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL,
        )
        seed_rank = np.zeros(int(labels.max()) + 1, np.int32)
        ys, xs = np.nonzero(seeds)
        seed_rank[labels[ys, xs]] = seeds[ys, xs]
        nearest = seed_rank[labels]
        for rank, i in enumerate(owners, start=1):
            assigned[i] |= ((nearest == rank) & (cell > 0)).astype(np.uint8)

    result = []
    for room, mask in zip(cv_rooms, assigned):
        points = _mask_polygon(mask, ww) if mask.any() else None
        if not points:
            result.append(room)
            continue
        area_px2 = float(abs(Polygon(points).area))
        result.append(replace(
            room, polygon_px=points, area_px2=area_px2,
            area_mm2=area_px2 * mm_per_px * mm_per_px,
            reason_codes=tuple(dict.fromkeys((*room.reason_codes, "CV_RASTER_EXTENT"))),
        ))
    template = cv_rooms[0] if cv_rooms else None
    added = 0
    for cell in new_cells:
        points = _mask_polygon(cell, ww)
        if not points or template is None:
            continue
        area_px2 = float(abs(Polygon(points).area))
        added += 1
        result.append(replace(
            template, id=f"room_raster_{added:03d}", type="other", polygon_px=points,
            area_px2=area_px2, area_mm2=area_px2 * mm_per_px * mm_per_px,
            status="review", reason_codes=("CV_ONLY_ROOM", "CV_RASTER_ROOM"),
        ))
    return tuple(result), added


def _mask_polygon(mask: np.ndarray, ww: int):
    from shapely.geometry import Polygon

    contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    polygon = Polygon(max(contours, key=cv2.contourArea).reshape(-1, 2)).buffer(0)
    if polygon.is_empty:
        return None
    if polygon.geom_type != "Polygon":
        polygon = max(polygon.geoms, key=lambda part: part.area)
    polygon = polygon.simplify(ww / 2, preserve_topology=True)
    points = tuple((float(x), float(y)) for x, y in list(polygon.exterior.coords)[:-1])
    return points if len(points) >= 3 else None
